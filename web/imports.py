"""M27 项目导入：文件夹/ZIP 安全复制、预览记录与分析确认。

导入副本只写 ``~/.papermine/imports/<import_id>/source``，不触碰用户原项目。
接口契约与安全限制见 ``docs/build-plan.md`` M27。
"""
from __future__ import annotations

import os
import shutil
import stat
import time
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, List, Optional, Tuple

from papermine import storage

IMPORT_SCHEMA = "import_record"
IMPORT_SCHEMA_VERSION = 1
IMPORT_FILENAME = "import.json"
SOURCE_DIRNAME = "source"

MAX_FILE_SIZE = 20 * 1024 * 1024
MAX_PROJECT_SIZE = 500 * 1024 * 1024
MAX_FILE_COUNT = 10_000

EXCLUDED_DIRS = {
    ".git", "node_modules", ".venv", "venv", "__pycache__", ".next",
    "dist", "build", "coverage", ".idea", ".vscode",
}
SENSITIVE_NAMES = {".env", "credentials.json", "id_rsa"}
SENSITIVE_SUFFIXES = {".pem", ".key"}


class ImportValidationError(ValueError):
    """导入内容不安全或格式无效。"""


class ImportLimitError(ImportValidationError):
    """导入内容超过文件数或大小限制。"""


def imports_dir() -> Path:
    path = storage.layout()["imports"]
    path.mkdir(parents=True, exist_ok=True)
    return path


def new_import_id() -> str:
    return "import_{}_{}".format(time.strftime("%Y%m%d%H%M%S"), uuid.uuid4().hex[:8])


def import_dir(import_id: str) -> Path:
    if not import_id.startswith("import_") or any(c in import_id for c in "/\\."):
        raise ImportValidationError("无效的 import_id")
    return imports_dir() / import_id


def _relative_path(raw: str) -> PurePosixPath:
    text = str(raw or "").replace("\\", "/").strip()
    path = PurePosixPath(text)
    if not text or path.is_absolute() or ".." in path.parts:
        raise ImportValidationError("包含不安全路径：{}".format(raw))
    parts = tuple(p for p in path.parts if p not in ("", "."))
    if not parts or ":" in parts[0]:
        raise ImportValidationError("包含不安全路径：{}".format(raw))
    return PurePosixPath(*parts)


def _exclusion(path: PurePosixPath) -> Optional[str]:
    lowered = [p.lower() for p in path.parts]
    if any(p in EXCLUDED_DIRS for p in lowered[:-1]):
        return "ignored_directory"
    name = lowered[-1]
    if name in SENSITIVE_NAMES or PurePosixPath(name).suffix.lower() in SENSITIVE_SUFFIXES:
        return "sensitive_file"
    return None


def _safe_target(source_dir: Path, relative: PurePosixPath) -> Path:
    target = source_dir.joinpath(*relative.parts)
    root = source_dir.resolve()
    resolved_parent = target.parent.resolve()
    try:
        resolved_parent.relative_to(root)
    except ValueError as exc:
        raise ImportValidationError("导入路径越界：{}".format(relative)) from exc
    return target


def _project_name(value: Optional[str], fallback: str) -> str:
    name = " ".join(str(value or fallback or "未命名项目").split())
    return name[:120]


def _base_record(import_id: str, source_type: str, name: str) -> Dict[str, Any]:
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    root = import_dir(import_id)
    return {
        "import_id": import_id,
        "project_id": "project_{}".format(uuid.uuid4().hex[:12]),
        "project_name": name,
        "source_type": source_type,
        "file_count": 0,
        "total_size": 0,
        "included_files": [],
        "excluded_files": [],
        "warnings": [],
        "status": "ready",
        "source_dir": str((root / SOURCE_DIRNAME).resolve()),
        "run_id": None,
        "created_at": now,
        "updated_at": now,
    }


def save_record(record: Dict[str, Any]) -> None:
    record["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    root = import_dir(str(record["import_id"]))
    storage.save_json(root / IMPORT_FILENAME, record, IMPORT_SCHEMA, IMPORT_SCHEMA_VERSION)


def load_record(import_id: str) -> Dict[str, Any]:
    path = import_dir(import_id) / IMPORT_FILENAME
    if not path.exists():
        raise FileNotFoundError(import_id)
    data = storage.load_json(path, IMPORT_SCHEMA)
    data.pop("_schema", None)
    data.pop("_schema_version", None)
    return data


def _check_totals(count: int, total: int, size: int) -> None:
    if size > MAX_FILE_SIZE:
        raise ImportLimitError("单文件超过 20 MiB 限制")
    if count > MAX_FILE_COUNT:
        raise ImportLimitError("项目文件数超过 10,000 限制")
    if total > MAX_PROJECT_SIZE:
        raise ImportLimitError("项目总大小超过 500 MiB 限制")


def _finalize(record: Dict[str, Any], included: List[str], excluded: List[dict], total: int) -> Dict[str, Any]:
    record["included_files"] = included
    record["excluded_files"] = excluded
    record["file_count"] = len(included)
    record["total_size"] = total
    sensitive = sum(1 for item in excluded if item.get("reason") == "sensitive_file")
    if sensitive:
        record["warnings"].append("已排除 {} 个可能包含密钥的文件".format(sensitive))
    if not included:
        raise ImportValidationError("项目中没有可导入文件")
    save_record(record)
    return record


async def import_folder(files: Iterable[Any], paths: List[str], project_name: Optional[str]) -> Dict[str, Any]:
    uploads = list(files)
    if len(uploads) != len(paths):
        raise ImportValidationError("files 与 paths 数量不一致")
    iid = new_import_id()
    root = import_dir(iid)
    source = root / SOURCE_DIRNAME
    source.mkdir(parents=True, exist_ok=False)
    fallback = PurePosixPath(paths[0]).parts[0] if paths else "未命名项目"
    record = _base_record(iid, "folder", _project_name(project_name, fallback))
    included: List[str] = []
    excluded: List[dict] = []
    total = 0
    try:
        for upload, raw_path in zip(uploads, paths):
            rel = _relative_path(raw_path)
            reason = _exclusion(rel)
            if reason:
                excluded.append({"path": str(rel), "reason": reason})
                continue
            target = _safe_target(source, rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            size = 0
            with open(target, "wb") as fh:
                while True:
                    chunk = await upload.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    _check_totals(len(included) + 1, total + size, size)
                    fh.write(chunk)
            total += size
            included.append(str(rel))
        return _finalize(record, included, excluded, total)
    except Exception:
        shutil.rmtree(root, ignore_errors=True)
        raise


def _zip_is_symlink(info: zipfile.ZipInfo) -> bool:
    mode = (info.external_attr >> 16) & 0xFFFF
    return stat.S_ISLNK(mode)


async def import_archive(upload: Any, project_name: Optional[str]) -> Dict[str, Any]:
    filename = str(getattr(upload, "filename", "") or "")
    if not filename.lower().endswith(".zip"):
        raise ImportValidationError("仅支持 .zip 压缩包")
    iid = new_import_id()
    root = import_dir(iid)
    source = root / SOURCE_DIRNAME
    source.mkdir(parents=True, exist_ok=False)
    archive_path = root / "upload.zip"
    try:
        uploaded = 0
        with open(archive_path, "wb") as fh:
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk:
                    break
                uploaded += len(chunk)
                if uploaded > MAX_PROJECT_SIZE:
                    raise ImportLimitError("ZIP 文件超过 500 MiB 限制")
                fh.write(chunk)
        record = _base_record(
            iid, "zip", _project_name(project_name, PurePosixPath(filename).stem))
        included: List[str] = []
        excluded: List[dict] = []
        total = 0
        with zipfile.ZipFile(archive_path) as zf:
            infos = [i for i in zf.infolist() if not i.is_dir()]
            if len(infos) > MAX_FILE_COUNT:
                raise ImportLimitError("ZIP 解压文件数超过 10,000 限制")
            for info in infos:
                rel = _relative_path(info.filename)
                if _zip_is_symlink(info):
                    raise ImportValidationError("ZIP 包含符号链接：{}".format(rel))
                reason = _exclusion(rel)
                if reason:
                    excluded.append({"path": str(rel), "reason": reason})
                    continue
                _check_totals(len(included) + 1, total + info.file_size, info.file_size)
                target = _safe_target(source, rel)
                target.parent.mkdir(parents=True, exist_ok=True)
                written = 0
                with zf.open(info) as src, open(target, "wb") as dst:
                    while True:
                        chunk = src.read(1024 * 1024)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > info.file_size or written > MAX_FILE_SIZE:
                            raise ImportLimitError("ZIP 解压大小异常：{}".format(rel))
                        dst.write(chunk)
                total += written
                included.append(str(rel))
        archive_path.unlink(missing_ok=True) if hasattr(archive_path, "unlink") else os.remove(str(archive_path))
        return _finalize(record, included, excluded, total)
    except (zipfile.BadZipFile, RuntimeError) as exc:
        shutil.rmtree(root, ignore_errors=True)
        raise ImportValidationError("ZIP 文件损坏或格式无效") from exc
    except Exception:
        shutil.rmtree(root, ignore_errors=True)
        raise
