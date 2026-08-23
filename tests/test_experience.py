"""经验库单测（M8 策略版）：record_decision、applicability 门控 retrieve、去重、生命周期、退役。

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
        "source_domain": "工业制造",
        "applicability": {
            "domains": ["工业制造"],
            "task_types": ["异常检测"],
            "preconditions": [],
        },
        "principle": "建模类任务可抽象成可发表问题",
        "policy": {"target": "evaluation", "directive": "评估 novelty 前先检查机制性创新"},
        "effect": {"outcome": "neutral", "measured_by": "human_review", "note": "", "updated_at": None},
        "confidence": 0.8,
        "support_count": 1,
        "source_runs": ["run_1"],
        "status": "candidate",
    }
    base.update(overrides)
    return base


# 与 _semantic 默认 applicability 匹配 / 不匹配的上下文
CTX_MATCH = {"domains": ["工业制造"], "task_types": ["异常检测"], "preconditions": []}
CTX_MISMATCH = {"domains": ["推荐系统"], "task_types": ["推荐"], "preconditions": []}


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
        self.assertEqual(entries[0]["status"], "active")   # 达到 PROMOTE_THRESHOLD 且 effect positive

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
        for key in ("experience_id", "type", "source_domain", "applicability", "principle",
                    "policy", "effect", "confidence", "support_count", "status",
                    "source_runs", "created_at", "updated_at"):
            self.assertIn(key, e)
        self.assertEqual(e["confidence"], 0.8)
        self.assertEqual(e["support_count"], 1)
        self.assertEqual(e["policy"]["target"], "evaluation")
        self.assertEqual(e["applicability"]["domains"], ["工业制造"])

    def test_append_dedups_and_accumulates_without_autopromote(self):
        experience.append_semantic(_semantic(principle="同一原则", support_count=1))
        experience.append_semantic(_semantic(principle="同一原则", support_count=1,
                                             confidence=0.6))
        entries = experience.read_semantic()
        self.assertEqual(len(entries), 1)                  # 去重，不堆叠
        self.assertEqual(entries[0]["support_count"], 2)   # 累加
        self.assertEqual(entries[0]["confidence"], 0.8)    # 取较大值
        # 重复观察本身不晋升（§3.7：需人工确认或 effect positive）
        self.assertEqual(entries[0]["status"], "candidate")

    def test_append_dedup_key_includes_applicability(self):
        experience.append_semantic(_semantic(principle="同一原则", support_count=1))
        experience.append_semantic(_semantic(
            principle="同一原则", support_count=1,
            applicability={"domains": ["推荐系统"], "task_types": ["推荐"], "preconditions": []},
        ))
        # principle 相同但 applicability 不同 → 不去重
        self.assertEqual(len(experience.read_semantic()), 2)

    def test_append_skips_empty_principle(self):
        experience.append_semantic(_semantic(principle=""))
        self.assertEqual(experience.read_semantic(), [])

    def test_append_migrates_legacy_fields(self):
        """旧字段（scope/trigger/insight/action）写入时被迁移为新 schema。"""
        experience.append_semantic({
            "experience_id": "legacy_1",
            "scope": "task:异常检测",
            "trigger": "工业时序 + 传感器",
            "insight": "异常检测可抽象成可发表问题",
            "action": "优先异常检测方向",
            "confidence": 0.7,
            "support_count": 1,
            "status": "candidate",
        })
        e = experience.read_semantic()[0]
        self.assertEqual(e["principle"], "异常检测可抽象成可发表问题")
        self.assertEqual(e["policy"]["directive"], "优先异常检测方向")
        self.assertEqual(e["applicability"]["task_types"], ["异常检测"])
        self.assertEqual(e["source_domain"], "*")


class RetrieveTest(ExperienceTest):
    def _seed(self):
        experience.append_semantic(_semantic(
            experience_id="a", principle="洞察A",
            applicability={"domains": ["工业制造"], "task_types": ["异常检测"], "preconditions": []},
            confidence=0.9, status="active"))
        experience.append_semantic(_semantic(
            experience_id="b", principle="洞察B",
            applicability={"domains": ["工业制造"], "task_types": ["异常检测"], "preconditions": []},
            confidence=0.5, status="active"))
        experience.append_semantic(_semantic(
            experience_id="c", principle="洞察C",
            applicability={"domains": ["工业制造"], "task_types": ["异常检测"], "preconditions": []},
            confidence=0.8, status="candidate"))
        experience.append_semantic(_semantic(
            experience_id="d", principle="洞察D",
            applicability={"domains": ["工业制造"], "task_types": ["异常检测"], "preconditions": []},
            confidence=1.0, status="degraded"))

    def test_retrieve_returns_only_active_sorted(self):
        self._seed()
        got = experience.retrieve(CTX_MATCH, k=3)
        self.assertEqual([e["experience_id"] for e in got], ["a", "b"])
        self.assertEqual(got[0]["confidence"], 0.9)

    def test_retrieve_k_limits(self):
        self._seed()
        self.assertEqual(len(experience.retrieve(CTX_MATCH, k=1)), 1)

    def test_retrieve_excludes_candidate_and_degraded(self):
        self._seed()
        ids = {e["experience_id"] for e in experience.retrieve(CTX_MATCH, k=10)}
        self.assertEqual(ids, {"a", "b"})

    def test_retrieve_applicability_gate_blocks_mismatch(self):
        """applicability 不匹配时不注入（验收 #2 前半）。"""
        experience.append_semantic(_semantic(experience_id="x", status="active"))
        self.assertEqual(experience.retrieve(CTX_MISMATCH, k=10), [])

    def test_retrieve_applicability_gate_allows_match(self):
        """applicability 匹配时命中。"""
        experience.append_semantic(_semantic(experience_id="x", status="active"))
        got = experience.retrieve(CTX_MATCH, k=10)
        self.assertEqual([e["experience_id"] for e in got], ["x"])

    def test_retrieve_domain_star_matches_any(self):
        experience.append_semantic(_semantic(
            experience_id="x", status="active",
            applicability={"domains": ["*"], "task_types": ["*"], "preconditions": []}))
        self.assertEqual(len(experience.retrieve(CTX_MISMATCH, k=10)), 1)


class ApplicabilityMatchTest(unittest.TestCase):
    def test_domain_and_task_gate(self):
        app = {"domains": ["工业制造"], "task_types": ["异常检测"], "preconditions": []}
        self.assertTrue(experience._applicability_matches(app, CTX_MATCH))
        self.assertFalse(experience._applicability_matches(app, CTX_MISMATCH))

    def test_star_matches_any(self):
        app = {"domains": ["*"], "task_types": ["*"], "preconditions": []}
        self.assertTrue(experience._applicability_matches(app, CTX_MISMATCH))
        self.assertTrue(experience._applicability_matches(app, CTX_MATCH))

    def test_entry_constrained_but_query_empty_is_conservative(self):
        app = {"domains": ["工业制造"], "task_types": ["异常检测"], "preconditions": []}
        self.assertFalse(experience._applicability_matches(
            app, {"domains": [], "task_types": [], "preconditions": []}))

    def test_precondition_substring(self):
        app = {"domains": ["*"], "task_types": ["*"],
               "preconditions": ["项目包含任务：异常检测"]}
        query = {"domains": ["工业制造"], "task_types": ["异常检测"],
                 "preconditions": ["项目包含任务：异常检测、剩余寿命预测"]}
        self.assertTrue(experience._applicability_matches(app, query))
        self.assertFalse(experience._applicability_matches(
            app, {"domains": [], "task_types": [], "preconditions": ["项目包含任务：推荐"]}))

    def test_none_query_matches_all(self):
        self.assertTrue(experience._applicability_matches(
            {"domains": ["工业制造"], "task_types": ["异常检测"], "preconditions": []}, None))


class LifecycleTest(ExperienceTest):
    def test_record_effect_positive_promotes(self):
        experience.append_semantic(_semantic(experience_id="x", support_count=2, status="candidate"))
        experience.record_effect("x", "positive")
        e = experience.read_semantic()[0]
        self.assertEqual(e["status"], "active")
        self.assertEqual(e["effect"]["outcome"], "positive")
        self.assertGreater(e["confidence"], 0.8)   # +CONFIDENCE_STEP

    def test_record_effect_positive_below_threshold_stays_candidate(self):
        experience.append_semantic(_semantic(experience_id="x", support_count=1, status="candidate"))
        experience.record_effect("x", "positive")
        self.assertEqual(experience.read_semantic()[0]["status"], "candidate")

    def test_record_effect_negative_degrades(self):
        experience.append_semantic(_semantic(experience_id="x", confidence=0.4, status="active"))
        experience.record_effect("x", "negative")
        e = experience.read_semantic()[0]
        self.assertEqual(e["confidence"], 0.15)     # 0.4 - CONFIDENCE_STEP
        self.assertEqual(e["status"], "degraded")   # <= DEGRADE_CONFIDENCE_FLOOR

    def test_record_effect_unknown_id_returns_none(self):
        self.assertIsNone(experience.record_effect("missing", "positive"))

    def test_retire_excludes_from_retrieve(self):
        experience.append_semantic(_semantic(experience_id="x", status="active"))
        experience.retire("x", "已证伪")
        self.assertEqual(experience.retrieve(CTX_MATCH, k=10), [])
        e = experience.read_semantic()[0]
        self.assertEqual(e["status"], "retired")


if __name__ == "__main__":
    unittest.main()
