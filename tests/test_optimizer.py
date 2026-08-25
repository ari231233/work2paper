"""M8 v2 Policy Optimizer 单测：记录使用、effect/evidence 驱动 confidence 升降、
生命周期自动推进/降级/退役、检索注入优先级排序、防漂移阈值。

用标准库 unittest 编写（与 tests/test_experience.py 一致），`python -m unittest discover -s tests -v`
即可运行，无需新增第三方依赖（也兼容 pytest 收集）。
"""
from __future__ import annotations

import os
import tempfile
import unittest

from papermine import experience, optimizer, storage


def _semantic(**overrides):
    base = {
        "experience_id": "exp_opt",
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


CTX_MATCH = {"domains": ["工业制造"], "task_types": ["异常检测"], "preconditions": []}


class OptimizerTest(unittest.TestCase):
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

    def _one(self):
        return experience.read_semantic()[0]


class RecordUsageTest(OptimizerTest):
    def test_record_usage_increments_and_tracks_run_idea(self):
        experience.append_semantic(_semantic(experience_id="x"))
        self.assertEqual(self._one()["usage"]["injections"], 0)

        experience.record_usage("x", run_id="run_9", idea_refs=["i1", "i2"])
        u = self._one()["usage"]
        self.assertEqual(u["injections"], 1)
        self.assertEqual(u["runs"], ["run_9"])
        self.assertEqual(u["ideas"], ["i1", "i2"])

        # 再次注入：次数累加、run/idea 去重
        experience.record_usage("x", run_id="run_9", idea_refs=["i1", "i3"])
        u = self._one()["usage"]
        self.assertEqual(u["injections"], 2)
        self.assertEqual(u["runs"], ["run_9"])
        self.assertEqual(u["ideas"], ["i1", "i2", "i3"])

    def test_record_usage_unknown_returns_none(self):
        self.assertIsNone(experience.record_usage("missing"))

    def test_record_usage_does_not_change_confidence(self):
        """usage 是弱信号：只改优先级，不直接改 confidence（防漂移）。"""
        experience.append_semantic(_semantic(experience_id="x", confidence=0.6, status="active"))
        experience.record_usage("x", run_id="run_1")
        self.assertEqual(self._one()["confidence"], 0.6)

    def test_usage_promotes_candidate_to_active(self):
        """usage 达标 + confidence 达标 -> candidate 自动晋升 active（M8 v2 自动优化）。"""
        experience.append_semantic(_semantic(
            experience_id="x", confidence=0.7, support_count=1, status="candidate"))
        experience.record_usage("x", run_id="r1")   # usage=1
        self.assertEqual(self._one()["status"], "candidate")
        experience.record_usage("x", run_id="r2")   # usage=2 >= PROMOTE_USAGE_THRESHOLD
        self.assertEqual(self._one()["status"], "active")


class RecordEffectOptimizationTest(OptimizerTest):
    def test_acceptance_policy_optimization(self):
        """验收 #2：一条 policy，模拟多次 positive/negative effect，验证 confidence 升降、
        生命周期 推进/降级/退役、优先级随之变化。"""
        experience.append_semantic(_semantic(
            experience_id="p", support_count=2, confidence=0.3, status="candidate"))
        p_initial = self._one()["priority"]

        # 两次 positive：confidence 自动升、生命周期晋升 active、优先级上升
        experience.record_effect("p", "positive")
        e = self._one()
        self.assertEqual(e["confidence"], 0.55)
        self.assertEqual(e["status"], "active")
        p1 = e["priority"]
        self.assertGreater(p1, p_initial)

        experience.record_effect("p", "positive")
        e = self._one()
        self.assertEqual(e["confidence"], 0.8)
        p2 = e["priority"]
        self.assertGreater(p2, p1)

        # 多次 negative：confidence 自动降、优先级随之下降
        experience.record_effect("p", "negative")
        e = self._one()
        self.assertEqual(e["confidence"], 0.55)
        self.assertEqual(e["status"], "active")
        p3 = e["priority"]
        self.assertLess(p3, p2)

        experience.record_effect("p", "negative")
        e = self._one()
        self.assertEqual(e["confidence"], 0.3)
        self.assertEqual(e["status"], "degraded")   # 跌破阈值 -> 降级
        p4 = e["priority"]
        self.assertLess(p4, p3)

        experience.record_effect("p", "negative")
        e = self._one()
        self.assertEqual(e["confidence"], 0.05)
        self.assertEqual(e["status"], "retired")    # 连续负信号达标 -> 退役
        p5 = e["priority"]
        self.assertLess(p5, p4)

    def test_positive_effect_resets_negative_streak(self):
        """正信号清零连负计数：负->正->负 不应因历史负信号被误退役。"""
        experience.append_semantic(_semantic(
            experience_id="p", support_count=2, confidence=0.6, status="active"))
        experience.record_effect("p", "negative")   # 0.35, neg=1
        experience.record_effect("p", "negative")   # 0.1, neg=2 -> degraded
        self.assertEqual(self._one()["status"], "degraded")
        experience.record_effect("p", "positive")   # 0.35, neg 清零 -> active（恢复）
        e = self._one()
        self.assertEqual(e["negative_count"], 0)
        self.assertEqual(e["status"], "active")
        self.assertEqual(e["confidence"], 0.35)


class OptimizeEvidenceTest(OptimizerTest):
    def test_evidence_strong_raises_confidence_idempotent(self):
        """M12 evidence strong -> confidence 升；同一 run 不重复折入（幂等）。"""
        experience.append_semantic(_semantic(
            experience_id="x", confidence=0.5, status="active", source_runs=["run_1"]))
        summary = experience.optimize(evidence_by_run={"run_1": ["strong"]})
        self.assertGreaterEqual(summary["optimized"], 1)
        self.assertEqual(self._one()["confidence"], 0.75)
        self.assertEqual(self._one()["evidence_runs"], ["run_1"])

        # 幂等：再次折入同一 run 不再重复提 confidence
        experience.optimize(evidence_by_run={"run_1": ["strong"]})
        self.assertEqual(self._one()["confidence"], 0.75)

    def test_evidence_weak_degrades(self):
        experience.append_semantic(_semantic(
            experience_id="x", confidence=0.5, status="active", source_runs=["run_1"]))
        experience.optimize(evidence_by_run={"run_1": ["weak"]})
        e = self._one()
        self.assertEqual(e["confidence"], 0.25)
        self.assertEqual(e["status"], "degraded")
        self.assertEqual(e["negative_count"], 1)

    def test_evidence_medium_is_neutral(self):
        experience.append_semantic(_semantic(
            experience_id="x", confidence=0.5, status="active", source_runs=["run_1"]))
        experience.optimize(evidence_by_run={"run_1": ["medium"]})
        e = self._one()
        self.assertEqual(e["confidence"], 0.5)          # 不动 confidence
        self.assertEqual(e["evidence_runs"], ["run_1"])  # 但仍记录已折入，防重复

    def test_evidence_only_folds_source_runs(self):
        """evidence_by_run 只折入 source_runs 含该 run 的条目（域隔离）。"""
        experience.append_semantic(_semantic(
            experience_id="x", confidence=0.5, status="active", source_runs=["run_other"]))
        experience.optimize(evidence_by_run={"run_1": ["strong"]})
        self.assertEqual(self._one()["confidence"], 0.5)


class PrioritySortingTest(OptimizerTest):
    def test_retrieve_orders_by_priority_usage_breaks_tie(self):
        """M8 v2：检索注入按 priority 排序；高 usage 的条目可超越仅凭 confidence 更靠前者。"""
        # A：confidence 较低但被大量注入 -> priority 更高（principle 不同，避免去重合并）
        experience.append_semantic(_semantic(
            experience_id="a", principle="原则A 高 usage", confidence=0.6, status="active",
            usage={"injections": 10, "runs": ["r"], "ideas": []}))
        # B：confidence 较高但从未被注入
        experience.append_semantic(_semantic(
            experience_id="b", principle="原则B 低 usage", confidence=0.7, status="active"))
        got = experience.retrieve(CTX_MATCH, k=10)
        self.assertEqual([e["experience_id"] for e in got], ["a", "b"])

    def test_retire_priority_penalized(self):
        experience.append_semantic(_semantic(experience_id="x", status="retired"))
        e = self._one()
        self.assertLess(e["priority"], 0.0)


class PureFunctionsTest(unittest.TestCase):
    def test_priority_score_reflects_confidence_and_usage(self):
        low = _semantic(confidence=0.5, support_count=1, usage={"injections": 0, "runs": [], "ideas": []})
        high = _semantic(confidence=0.5, support_count=1, usage={"injections": 10, "runs": [], "ideas": []})
        self.assertGreater(optimizer.priority_score(high), optimizer.priority_score(low))

    def test_aggregate_evidence(self):
        self.assertAlmostEqual(optimizer.aggregate_evidence(["strong", "strong", "weak"]),
                               0.3333, places=3)
        self.assertEqual(optimizer.aggregate_evidence(["weak", "weak"]), -1.0)
        self.assertEqual(optimizer.aggregate_evidence(["strong", "weak"]), 0.0)
        self.assertEqual(optimizer.aggregate_evidence(["medium"]), 0.0)
        self.assertEqual(optimizer.aggregate_evidence([]), 0.0)

    def test_recompute_lifecycle_usage_promote(self):
        e = _semantic(confidence=0.7, status="candidate",
                      usage={"injections": 2, "runs": [], "ideas": []})
        self.assertEqual(optimizer.recompute_lifecycle(e), "active")

    def test_recompute_lifecycle_retired_is_terminal(self):
        e = _semantic(confidence=1.0, status="retired")
        self.assertEqual(optimizer.recompute_lifecycle(e), "retired")

    def test_evidence_levels_from_evaluations(self):
        evals = [
            {"idea_ref": "i1", "evidence_validation": {"evidence": "strong"}},
            {"idea_ref": "i2", "evidence_validation": {"evidence": "weak"}},
            {"idea_ref": "i3"},  # 无 evidence_validation
            {"idea_ref": "i4", "evidence_validation": {"evidence": "medium"}},
        ]
        self.assertEqual(optimizer.evidence_levels_from_evaluations(evals),
                         ["strong", "weak", "medium"])


if __name__ == "__main__":
    unittest.main()
