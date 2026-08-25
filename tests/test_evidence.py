"""M12 证据验证 Agent 单测：4 维检查、证据强度聚合、LLM 路径、确定性降级、sample 验收。

用标准库 unittest 编写，`python -m unittest discover -s tests -v` 可运行（兼容 pytest 收集）。
"""
from __future__ import annotations

import unittest

from papermine.agents.evidence import (
    CHECK_DIMENSIONS,
    EVIDENCE_LEVELS,
    EVIDENCE_SCHEMA,
    _aggregate_evidence,
    _deterministic_checks,
    validate_evidence,
)
from papermine.llm import LLMError, NullProvider

_CHECK_KEYS = tuple(k for k, _l in CHECK_DIMENSIONS)


class _FakeLLM:
    def __init__(self, result=None, exc=None):
        self.result = result if result is not None else {}
        self.exc = exc
        self.calls = []

    def complete(self, system, user, schema, temperature=0.2):
        self.calls.append((system, user, schema, temperature))
        if self.exc is not None:
            raise self.exc
        return self.result


def _checks(similar="ok", theory="ok", experiment="ok", claim="ok"):
    return {
        "similar_work": {"status": similar, "note": "文献对拍结论"},
        "theory_basis": {"status": theory, "note": "理论支撑结论"},
        "experiment_support": {"status": experiment, "note": "实验设计支持结论"},
        "claim_strength": {"status": claim, "note": "claim 强度结论"},
    }


def _llm_result(evidence="strong", reason="证据充分", checks=None):
    return {"evidence": evidence, "reason": reason,
            "checks": checks if checks is not None else _checks()}


def _idea(claim="提出一种改进方法", hypothesis="假设改进有效", refs=None,
          hypothesis_refs=None):
    return {
        "idea_id": "i1",
        "claim": claim,
        "novelty_hypothesis": hypothesis,
        "problem_ref": "p1",
        "literature_refs": refs or [],
        "hypothesis_refs": hypothesis_refs or [],
    }


def _literature(n_papers=1, with_understanding=False):
    if n_papers == 0:
        return []
    papers = [{"title": "Paper {}".format(i), "abstract": "abstract {}".format(i),
               "venue": "arXiv", "source": "arxiv"} for i in range(n_papers)]
    if with_understanding:
        for p in papers:
            p["understanding"] = {"claim": "c", "method": "m", "conclusion": "cc",
                                  "applicability": "a", "limitations": "l"}
    return [{"query": "q", "papers": papers, "gap_note": "存在 gap", "sources": ["arxiv"]}]


class ValidateEvidenceTest(unittest.TestCase):
    def test_null_llm_degraded_with_4_checks(self):
        out = validate_evidence(_idea(), _literature(), NullProvider())
        self.assertIn(out["evidence"], EVIDENCE_LEVELS)
        self.assertTrue(out["reason"].strip())
        self.assertEqual(set(out["checks"]), set(_CHECK_KEYS))
        self.assertTrue(out["degraded"])

    def test_llm_valid_result_used(self):
        llm = _FakeLLM(result=_llm_result(evidence="strong", reason="证据充分"))
        out = validate_evidence(_idea(), _literature(), llm)
        self.assertEqual(out["evidence"], "strong")
        self.assertEqual(out["reason"], "证据充分")
        self.assertFalse(out["degraded"])

    def test_llm_invalid_checks_falls_back(self):
        # checks 缺字段 → 确定性兜底
        llm = _FakeLLM(result={"evidence": "strong", "reason": "x",
                               "checks": {"similar_work": {"status": "ok"}}})
        out = validate_evidence(_idea(), _literature(), llm)
        self.assertTrue(out["degraded"])
        self.assertEqual(set(out["checks"]), set(_CHECK_KEYS))

    def test_llm_error_falls_back(self):
        out = validate_evidence(_idea(), _literature(), _FakeLLM(exc=LLMError("网络失败")))
        self.assertTrue(out["degraded"])
        self.assertIn(out["evidence"], EVIDENCE_LEVELS)

    def test_llm_empty_evidence_recomputed_from_checks(self):
        # LLM 给合法 checks 但 evidence 非法 → 用 checks 重新聚合
        llm = _FakeLLM(result=_llm_result(evidence="bogus", reason="",
                                          checks=_checks()))
        out = validate_evidence(_idea(), _literature(), llm)
        self.assertFalse(out["degraded"])
        self.assertEqual(out["evidence"], "strong")  # 4 个 ok → strong
        self.assertTrue(out["reason"].strip())       # 缺 reason 时兜底补


class DeterministicChecksTest(unittest.TestCase):
    def test_no_literature_similar_work_missing(self):
        idea = _idea()
        checks = _deterministic_checks(idea, [], {})
        self.assertEqual(checks["similar_work"]["status"], "missing")

    def test_overstrong_claim_is_missing(self):
        idea = _idea(claim="首创一种完全解决所有问题的完美方法")
        checks = _deterministic_checks(idea, _literature(), {})
        self.assertEqual(checks["claim_strength"]["status"], "missing")

    def test_hedged_claim_is_ok(self):
        idea = _idea(claim="面向工业制造场景的轻量改进方法")
        checks = _deterministic_checks(idea, _literature(), {})
        self.assertEqual(checks["claim_strength"]["status"], "ok")

    def test_theory_signal_is_ok(self):
        idea = _idea(claim="提出一种基于因果机制的改进方法")
        checks = _deterministic_checks(idea, _literature(), {})
        self.assertEqual(checks["theory_basis"]["status"], "ok")

    def test_experiment_support_from_facts(self):
        facts = {"data": ["时序"], "metrics": ["F1"]}
        checks = _deterministic_checks(_idea(), _literature(), facts)
        self.assertEqual(checks["experiment_support"]["status"], "ok")

    def test_experiment_support_missing_without_facts(self):
        checks = _deterministic_checks(_idea(), _literature(), {})
        self.assertEqual(checks["experiment_support"]["status"], "missing")


class AggregateEvidenceTest(unittest.TestCase):
    def test_all_ok_strong(self):
        self.assertEqual(_aggregate_evidence(_checks())[0], "strong")

    def test_overstrong_claim_weak(self):
        evidence, reason = _aggregate_evidence(_checks(claim="missing"))
        self.assertEqual(evidence, "weak")
        self.assertIn("过强", reason)

    def test_no_crosscheck_and_no_theory_weak(self):
        evidence, _ = _aggregate_evidence(_checks(similar="missing", theory="missing"))
        self.assertEqual(evidence, "weak")

    def test_undifferentiated_similar_work_caps_at_medium(self):
        # 3 ok + similar=concern：不能给 strong（差异不明确）
        evidence, _ = _aggregate_evidence(_checks(similar="concern"))
        self.assertEqual(evidence, "medium")

    def test_mixed_is_medium(self):
        evidence, _ = _aggregate_evidence(_checks(theory="concern", experiment="concern"))
        self.assertEqual(evidence, "medium")


class SchemaTest(unittest.TestCase):
    def test_schema_requires_evidence_reason_checks(self):
        self.assertEqual(EVIDENCE_SCHEMA["type"], "object")
        self.assertEqual(EVIDENCE_SCHEMA["required"], ["evidence", "reason", "checks"])
        self.assertEqual(
            set(EVIDENCE_SCHEMA["properties"]["checks"]["required"]),
            set(_CHECK_KEYS))
        enum = EVIDENCE_SCHEMA["properties"]["evidence"]["enum"]
        self.assertEqual(enum, list(EVIDENCE_LEVELS))


class SampleAcceptanceTest(unittest.TestCase):
    """验收：对 sample 场景的 idea 输出证据强度 + 理由（离线确定性路径）。"""

    def test_each_idea_outputs_evidence_and_reason(self):
        literature = _literature(n_papers=1)
        for idea in [
            _idea(claim="面向工业制造的传感器时序异常检测方法：基于孤立森林的轻量方案",
                  hypothesis="现有方法对缺失值鲁棒性差，提出缺失值自适应的异常检测",
                  refs=["Paper 0"]),
            _idea(claim="LSTM 剩余寿命预测的滑动窗口特征工程改进",
                  hypothesis="引入工况自适应窗口提升 RUL 预测精度"),
        ]:
            out = validate_evidence(idea, literature, NullProvider(),
                                    facts={"data": ["时序"], "metrics": ["F1"]})
            self.assertIn(out["evidence"], EVIDENCE_LEVELS)
            self.assertTrue(out["reason"].strip())
            self.assertEqual(set(out["checks"]), set(_CHECK_KEYS))
            self.assertTrue(out["degraded"])


if __name__ == "__main__":
    unittest.main()
