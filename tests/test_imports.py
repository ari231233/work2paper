"""M27 项目导入：文件夹/ZIP、安全限制与确认分析。"""
from __future__ import annotations

import io
import os
import tempfile
import unittest
import zipfile
from unittest import mock

from fastapi.testclient import TestClient
from papermine import storage
from web import api, imports as project_imports
from web.app import create_app


def _zip_bytes(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries:
            zf.writestr(name, data)
    return buf.getvalue()


class ImportApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_home = os.environ.get(storage.ENV_HOME)
        os.environ[storage.ENV_HOME] = self.tmp.name
        storage.ensure_layout()
        self.client = TestClient(create_app())

    def tearDown(self):
        if self.old_home is None:
            os.environ.pop(storage.ENV_HOME, None)
        else:
            os.environ[storage.ENV_HOME] = self.old_home
        self.tmp.cleanup()

    def test_folder_import_preserves_paths_and_excludes_sensitive(self):
        response = self.client.post(
            "/imports/folder",
            data={"paths": ["demo/README.md", "demo/src/main.py", "demo/.env"]},
            files=[
                ("files", ("README.md", b"# Demo", "text/markdown")),
                ("files", ("main.py", b"print('ok')", "text/x-python")),
                ("files", (".env", b"SECRET=x", "text/plain")),
            ],
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["source_type"], "folder")
        self.assertEqual(body["file_count"], 2)
        self.assertIn("demo/src/main.py", body["included_files"])
        self.assertEqual(body["excluded_files"][0]["reason"], "sensitive_file")
        self.assertTrue(os.path.isfile(os.path.join(body["source_dir"], "demo", "src", "main.py")))
        self.assertFalse(os.path.exists(os.path.join(body["source_dir"], "demo", ".env")))

        preview = self.client.get("/imports/{}".format(body["import_id"]))
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.json()["project_id"], body["project_id"])

    def test_zip_import_and_ignored_directories(self):
        payload = _zip_bytes([
            ("repo/README.md", "hello"),
            ("repo/src/a.py", "x = 1"),
            ("repo/node_modules/pkg/index.js", "ignored"),
        ])
        response = self.client.post(
            "/imports/archive",
            files={"file": ("repo.zip", payload, "application/zip")},
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["project_name"], "repo")
        self.assertEqual(body["file_count"], 2)
        self.assertEqual(body["excluded_files"][0]["reason"], "ignored_directory")

    def test_zip_slip_rejected_without_import_record(self):
        payload = _zip_bytes([("../../outside.txt", "bad")])
        response = self.client.post(
            "/imports/archive",
            files={"file": ("bad.zip", payload, "application/zip")},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("不安全路径", response.json()["detail"])
        self.assertEqual(list(storage.layout()["imports"].glob("*/import.json")), [])

    def test_non_zip_rejected(self):
        response = self.client.post(
            "/imports/archive",
            files={"file": ("project.tar", b"nope", "application/octet-stream")},
        )
        self.assertEqual(response.status_code, 400)

    def test_size_limit_returns_413(self):
        with mock.patch.object(project_imports, "MAX_FILE_SIZE", 3):
            response = self.client.post(
                "/imports/folder",
                data={"paths": ["demo/large.txt"]},
                files={"files": ("large.txt", b"1234", "text/plain")},
            )
        self.assertEqual(response.status_code, 413)

    def test_zip_symlink_rejected(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            info = zipfile.ZipInfo("repo/link")
            info.create_system = 3
            info.external_attr = (0o120777 << 16)
            zf.writestr(info, "target")
        response = self.client.post(
            "/imports/archive",
            files={"file": ("links.zip", buf.getvalue(), "application/zip")},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("符号链接", response.json()["detail"])

    def test_analyze_requires_explicit_endpoint_and_updates_record(self):
        payload = _zip_bytes([("repo/README.md", "hello")])
        imported = self.client.post(
            "/imports/archive",
            files={"file": ("repo.zip", payload, "application/zip")},
        ).json()
        self.assertEqual(imported["status"], "ready")
        with mock.patch.object(api.orchestrator, "run_pipeline", return_value="run_new"), \
                mock.patch.object(api, "_project_payload", return_value={"project_id": "run_new"}):
            response = self.client.post(
                "/imports/{}/analyze".format(imported["import_id"]), json={"auto": True})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["import"]["status"], "done")
        self.assertEqual(response.json()["import"]["run_id"], "run_new")


if __name__ == "__main__":
    unittest.main()
