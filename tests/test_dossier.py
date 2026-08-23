"""Dossier 数据层单测：结构、round-trip、版本化、快照、迁移钩子。

用标准库 unittest 编写，`python -m unittest discover -s tests -v` 即可运行，
无需新增第三方依赖（也兼容 pytest 收集）。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from papermine import storage
from papermine.dossier import (
    Dossier,
    SCHEMA_NAME,
    SCHEMA_VERSION,
    register_migration,
)


def _populated_dossier() -> Dossier:
    """填满全部 8 个字段的 dossier，用于 round-trip 断言。"""
    d = Dossier(project_id="proj-1", llm_backend="deepseek")
    d.meta["prompt_versions"] = {"abstract": "v1", "generate": "v1"}
    d.assets["facts"] = {
        "tasks": ["异常检测"],
        "methods": ["LSTM"],
        "data": ["传感器时序"],
        "scenarios": ["工业时序"],
        "metrics": ["F1"],
        "libraries": ["torch"],
        "modules": ["model.py"],
    }
    d.assets["narrative"] = "一个工业时序异常检测项目。"
    d.assets["evidence"] = [{"source": "README.md", "snippet": "检测设备异常"}]
    d.problems = [
        {
            "problem_id": "p1",
            "title": "剩余寿命预测",
            "formulation": "如何……",
            "motivation": "……",
            "why_not_engineering": "……",
            "evidence_refs": ["README.md"],
        }
    ]
    d.literature = [
        {
            "query": "time series anomaly detection",
            "papers": [{"title": "Paper A"}],
            "gap_note": "存在 gap",
            "sources": ["arxiv"],
        }
    ]
    d.ideas = [
        {
            "idea_id": "i1",
            "claim": "……",
            "novelty_hypothesis": "……",
            "problem_ref": "p1",
            "literature_refs": ["Paper A"],
            "status": "pending_eval",
        }
    ]
    d.evaluations = [
        {
            "idea_ref": "i1",
            "novelty_score": 3.5,
            "data_feasibility": "high",
            "workload_hours": 60,
            "venue_guess": "某会议",
            "verdict": "proceed",
            "rework_reason": None,
            "evidence": [{"source": "literature", "note": "……"}],
        }
    ]
    d.roadmap = {
        "selected_idea": "i1",
        "paper_type": "方法论文",
        "outline": ["1. intro"],
        "experiment_plan": ["step1"],
        "timeline": {"week1": "调研"},
        "missing_items": ["数据集"],
    }
    d.human_decisions = [
        {
            "checkpoint": "cp2",
            "decision": "accept",
            "note": "问题 1 认可",
            "ts": "2024-01-01T00:00:00Z",
        }
    ]
    return d


class DossierTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.run_dir = Path(self._tmp.name)
        # 隔离全局迁移注册表，避免测试间互相污染
        self._orig_migrations = dict(storage.SCHEMA_MIGRATIONS.get(SCHEMA_NAME, {}))

    def tearDown(self) -> None:
        storage.SCHEMA_MIGRATIONS[SCHEMA_NAME] = self._orig_migrations
        self._tmp.cleanup()

    def test_default_structure(self) -> None:
        d = Dossier()
        self.assertEqual(
            set(d.meta), {"project_id", "version", "llm_backend", "prompt_versions"}
        )
        self.assertEqual(d.meta["version"], 1)
        self.assertTrue(d.meta["project_id"])
        self.assertEqual(d.meta["prompt_versions"], {})
        self.assertEqual(d.assets["narrative"], "")
        self.assertEqual(d.assets["evidence"], [])
        self.assertEqual(
            list(d.assets["facts"]),
            ["tasks", "methods", "data", "scenarios", "metrics", "libraries", "modules"],
        )
        self.assertTrue(all(d.assets["facts"][k] == [] for k in d.assets["facts"]))
        self.assertEqual(d.problems, [])
        self.assertEqual(d.literature, [])
        self.assertEqual(d.ideas, [])
        self.assertEqual(d.evaluations, [])
        self.assertEqual(d.human_decisions, [])
        self.assertEqual(
            set(d.roadmap),
            {"selected_idea", "paper_type", "outline", "experiment_plan", "timeline", "missing_items"},
        )

    def test_round_trip_preserves_all_fields(self) -> None:
        d = _populated_dossier()
        d.bump_version()  # version 1 -> 2
        d.save(self.run_dir)

        loaded = Dossier.load(self.run_dir)
        self.assertIsInstance(loaded, Dossier)
        self.assertEqual(loaded.to_dict(), d.to_dict())

    def test_save_embeds_schema_version(self) -> None:
        _populated_dossier().save(self.run_dir)
        payload = json.loads((self.run_dir / "dossier.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["_schema"], SCHEMA_NAME)
        self.assertEqual(payload["_schema_version"], SCHEMA_VERSION)

    def test_bump_version(self) -> None:
        d = Dossier()
        self.assertEqual(d.bump_version(), 2)
        self.assertEqual(d.meta["version"], 2)
        self.assertEqual(d.bump_version(), 3)

    def test_snapshot_writes_history(self) -> None:
        d = _populated_dossier()
        d.bump_version()
        d.save(self.run_dir)
        d.snapshot()

        hist_dir = self.run_dir / "dossier.history"
        self.assertTrue(hist_dir.is_dir())
        files = list(hist_dir.iterdir())
        self.assertEqual(len(files), 1)
        payload = json.loads(files[0].read_text(encoding="utf-8"))
        self.assertEqual(payload["meta"]["version"], d.meta["version"])
        self.assertEqual(payload["_schema"], SCHEMA_NAME)

    def test_snapshot_requires_save(self) -> None:
        with self.assertRaises(ValueError):
            Dossier().snapshot()

    def test_load_missing_file_raises(self) -> None:
        with self.assertRaises(FileNotFoundError):
            Dossier.load(self.run_dir)

    def test_migration_hook_applies_on_load(self) -> None:
        # 模拟未来 schema 升级：v1 -> v2 的迁移函数
        def _migrate_v1_to_v2(data):
            data.setdefault("meta", {})["prompt_versions"] = {"abstract": "v1"}
            return data

        register_migration(1, _migrate_v1_to_v2)

        Dossier(project_id="proj-1").save(self.run_dir)
        # 伪造"旧版本"数据：清空 prompt_versions，并显式标 _schema_version=1
        path = self.run_dir / "dossier.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["meta"]["prompt_versions"] = {}
        payload["_schema_version"] = 1
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

        loaded = Dossier.load(self.run_dir)
        self.assertEqual(loaded.meta["prompt_versions"], {"abstract": "v1"})


if __name__ == "__main__":
    unittest.main()
