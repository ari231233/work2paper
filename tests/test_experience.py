"""M7 经验库 v1 单测：record_decision、retrieve、去重、晋升、scope 匹配。

用标准库 unittest 编写（与 tests/test_dossier.py 一致），`python -m unittest discover -s tests -v`
即可运行，无需新增第三方依赖（也兼容 pytest 收集）。
"""
from __future__ import annotations

import os
import tempfile
import unittest

from papermine import experience, storage


def _semantic(**overrides):
    base = {
        "experience_id": "exp_test",
        "type": "pattern",
        "scope": "task:异常检测",
        "trigger": "工业时序 + 传感器",
        "insight": "异常检测可抽象成可发表问题",
        "action": "优先异常检测方向",
        "confidence": 0.8,
        "support_count": 1,
        "source_runs": ["run_1"],
        "status": "candidate",
    }
    base.update(overrides)
    return base


class ExperienceTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = os.environ.get(storage.ENV_HOME)
        os.environ[storage.ENV_HOME] = self._tmp.name
        storage.ensure_layout()

    def tearDown(self) -> None:
        if self._orig is None:
            os.environ.pop(storage.ENV_HOME, None)
        else:
            os.environ[storage.ENV_HOME] = self._orig
        self._tmp.cleanup()

    def _semantic_path(self):
        return storage.experience_dir() / experience.SEMANTIC_FILENAME

    def _episodic_path(self):
        return storage.experience_dir() / experience.EPISODIC_FILENAME


class RecordDecisionTest(ExperienceTest):
    def test_record_decision_writes_episodic(self):
        experience.record_decision("run_1", "cp2", "accept", "问题 1 认可")
        entries = storage.read_jsonl(self._episodic_path())
        self.assertEqual(len(entries), 1)
        e = entries[0]
        self.assertEqual(e["type"], "decision")
        self.assertEqual(e["run_id"], "run_1")
        self.assertEqual(e["checkpoint"], "cp2")
        self.assertEqual(e["decision"], "accept")
        self.assertEqual(e["note"], "问题 1 认可")
        self.assertIn("experience_id", e)
        self.assertIn("created_at", e)

    def test_record_decision_accept_promotes_candidate(self):
        # 先写入一条 candidate（source_runs=[run_1]）
        experience.append_semantic(_semantic(support_count=1, status="candidate",
                                             source_runs=["run_1"]))
        experience.record_decision("run_1", "cp1", "accept", "")
        entries = experience.read_semantic()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["support_count"], 2)   # 1 + 人工确认
        self.assertEqual(entries[0]["status"], "active")   # 达到 PROMOTE_THRESHOLD

    def test_record_decision_note_also_promotes(self):
        experience.append_semantic(_semantic(support_count=1, status="candidate",
                                             source_runs=["run_1"]))
        experience.record_decision("run_1", "cp1", "note", "附注")
        self.assertEqual(experience.read_semantic()[0]["status"], "active")

    def test_record_decision_rework_does_not_promote(self):
        experience.append_semantic(_semantic(support_count=1, status="candidate",
                                             source_runs=["run_1"]))
        experience.record_decision("run_1", "cp1", "rework", "")
        self.assertEqual(experience.read_semantic()[0]["status"], "candidate")
        self.assertEqual(experience.read_semantic()[0]["support_count"], 1)


class AppendSemanticTest(ExperienceTest):
    def test_append_normalizes_and_writes(self):
        eid = experience.append_semantic(_semantic())
        entries = experience.read_semantic()
        self.assertEqual(len(entries), 1)
        e = entries[0]
        self.assertEqual(e["experience_id"], eid)
        for key in ("experience_id", "type", "scope", "trigger", "insight",
                    "action", "confidence", "support_count", "source_runs",
                    "status", "created_at", "updated_at"):
            self.assertIn(key, e)
        self.assertEqual(e["confidence"], 0.8)
        self.assertEqual(e["support_count"], 1)

    def test_append_dedups_and_accumulates(self):
        experience.append_semantic(_semantic(insight="同一洞察", support_count=1))
        experience.append_semantic(_semantic(insight="同一洞察", support_count=1,
                                             confidence=0.6))
        entries = experience.read_semantic()
        self.assertEqual(len(entries), 1)                  # 去重，不堆叠
        self.assertEqual(entries[0]["support_count"], 2)   # 累加
        self.assertEqual(entries[0]["confidence"], 0.8)    # 取较大值
        self.assertEqual(entries[0]["status"], "active")   # 达到阈值晋升

    def test_append_skips_empty_insight(self):
        experience.append_semantic(_semantic(insight=""))
        self.assertEqual(experience.read_semantic(), [])


class RetrieveTest(ExperienceTest):
    def _seed(self):
        experience.append_semantic(_semantic(experience_id="a", scope="task:异常检测",
                                             insight="洞察A", confidence=0.9, status="active"))
        experience.append_semantic(_semantic(experience_id="b", scope="task:异常检测",
                                             insight="洞察B", confidence=0.5, status="active"))
        experience.append_semantic(_semantic(experience_id="c", scope="domain:工业制造",
                                             insight="洞察C", confidence=0.8, status="candidate"))
        experience.append_semantic(_semantic(experience_id="d", scope="task:异常检测",
                                             insight="洞察D", confidence=1.0, status="candidate"))

    def test_retrieve_returns_only_active_sorted(self):
        self._seed()
        got = experience.retrieve("task:异常检测", k=3)
        self.assertEqual([e["experience_id"] for e in got], ["a", "b"])
        self.assertEqual(got[0]["confidence"], 0.9)

    def test_retrieve_k_limits(self):
        self._seed()
        self.assertEqual(len(experience.retrieve("task:异常检测", k=1)), 1)

    def test_retrieve_global_matches_all_active(self):
        self._seed()
        got = experience.retrieve("global", k=10)
        ids = {e["experience_id"] for e in got}
        self.assertEqual(ids, {"a", "b"})

    def test_retrieve_empty_when_none_active(self):
        self._seed()
        self.assertEqual(experience.retrieve("domain:工业制造", k=3), [])


class ScopeMatchTest(unittest.TestCase):
    def test_scope_matching(self):
        self.assertTrue(experience._scope_matches("global", "task:异常检测"))
        self.assertTrue(experience._scope_matches("task:异常检测", "task:异常检测"))
        self.assertTrue(experience._scope_matches("task:异常检测", "task"))       # 前缀
        self.assertTrue(experience._scope_matches("task:异常检测", "global"))
        self.assertTrue(experience._scope_matches("task:异常检测", ""))
        self.assertFalse(experience._scope_matches("domain:工业制造", "task:异常检测"))


if __name__ == "__main__":
    unittest.main()
