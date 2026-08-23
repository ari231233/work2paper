"""M6 ⑤ 可行性评估 Agent 单测：证据驱动评估、verdict、降级路径、sample 验收。

用标准库 unittest 编写（与 tests/test_dossier.py 一致），`python -m unittest discover -s tests -v`
即可运行，无需新增第三方依赖（也兼容 pytest 收集）。
"""
from __future__ import annotations

import unittest
from pathlib import Path

from papermine.agents.evaluate import (
    EVALUATE_SCHEMA,
    _data_feasibility,
    _decide_verdict,
    _deterministic_novelty,
    _tier_of,
    run,
)
from papermine.agents.understand import run as understand_run
from papermine.dossier import Dossier
from papermine.llm import LLMError, NullProvider

SAMPLE_PROJECT = Path(__file__).resolve().parent.parent / "examples" / "sample-project"

_FACTS_KEYS = {"tasks", "methods", "data", "scenarios", "metrics", "libraries", "modules"}


class _FakeLLM:
    """按调用顺序依次返回 results（每个 idea 一次调用）；耗尽后返回空 dict。"""

    def __init__(self, results=None, exc=None):
        self.results = list(results or [])
        self.exc = exc
        self.calls = []

    def complete(self, system, user, schema, temperature=0.2):
        self.calls.append((system, user, schema, temperature))
        if self.exc is not None:
            raise self.exc
        if self.results:
            return self.results.pop(0)
        return {}


def _llm_eval(novelty=4.0, workload=80, suggestion="proceed", reason=None):
    return {
        "novelty_score": novelty,
        "novelty_reason": "gap 明确支持该假设",
        "workload_hours": workload,
        "verdict_suggestion": suggestion,
        "rework_reason": reason,
    }


def _dossier(ideas=None, literature=None):
    """基于 sample 项目 facts 构造一个带 ideas / literature 的 dossier。"""
    d = Dossier(project_id="proj-m6", llm_backend="deepseek")
    understand_run(str(SAMPLE_PROJECT), d, NullProvider())
    d.ideas = ideas if ideas is not None else [
        {
            "idea_id": "i1",
            "claim": "面向工业制造的传感器时序异常检测方法：基于孤立森林的轻量方案",
            "novelty_hypothesis": "现有方法对缺失值鲁棒性差，提出缺失值自适应的异常检测",
            "problem_ref": "p1",
            "literature_refs": ["Paper A"],
            "status": "pending_eval",
        },
        {
            "idea_id": "i2",
            "claim": "LSTM 剩余寿命预测的滑动窗口特征工程改进",
            "novelty_hypothesis": "引入工况自适应窗口提升 RUL 预测精度",
            "problem_ref": "p2",
            "literature_refs": [],
            "status": "pending_eval",
        },
    ]
    d.literature = literature if literature is not None else [
        {
            "query": "time series anomaly detection",
            "papers": [{"title": "Paper A", "venue": "KDD", "source": "semantic_scholar"}],
            "gap_note": "现有异常检测方法在缺失值场景下存在 gap，尚未有系统研究",
            "sources": ["semantic_scholar"],
        }
    ]
    return d


class RunTest(unittest.TestCase):
    def test_run_null_llm_writes_evaluations_with_verdict(self) -> None:
        d = _dossier()
        run(d, NullProvider())

        self.assertEqual(len(d.evaluations), 2)
        for ev in d.evaluations:
            self.assertIn(ev["verdict"], ("proceed", "rework", "drop"))
            self.assertIn(ev["idea_ref"], ("i1", "i2"))
            self.assertIsInstance(ev["novelty_score"], float)
            self.assertIn(ev["data_feasibility"], ("high", "medium", "low"))
            self.assertIsInstance(ev["workload_hours"], int)
            self.assertTrue(ev["venue_guess"])
            self.assertTrue(ev["evidence"])
            for e in ev["evidence"]:
                self.assertIn("source", e)
                self.assertIn("note", e)
        self.assertEqual(d.meta["prompt_versions"]["evaluate"], "v1")

    def test_run_is_evidence_driven(self) -> None:
        """评估必须挂确定性证据源（facts / literature），不是凭空断言。"""
        d = _dossier()
        run(d, NullProvider())
        sources = {e["source"] for ev in d.evaluations for e in ev["evidence"]}
        self.assertIn("literature.gap_note", sources)
        self.assertIn("assets.facts.data", sources)      # sample 有数据标签
        self.assertIn("assets.facts.metrics", sources)   # sample 有指标标签
        self.assertIn("literature.venues", sources)      # 检索论文带 venue

    def test_run_with_llm_uses_novelty_and_workload(self) -> None:
        d = _dossier()
        llm = _FakeLLM(results=[_llm_eval(novelty=4.5, workload=120, suggestion="proceed"),
                                _llm_eval(novelty=1.0, workload=50, suggestion="drop")])
        run(d, llm)

        self.assertEqual(d.evaluations[0]["novelty_score"], 4.5)
        self.assertEqual(d.evaluations[0]["workload_hours"], 120)
        self.assertEqual(d.evaluations[0]["verdict"], "proceed")
        # novelty 1.0 → 硬护栏 drop，即使 LLM 建议 drop 也一致
        self.assertEqual(d.evaluations[1]["novelty_score"], 1.0)
        self.assertEqual(d.evaluations[1]["verdict"], "drop")

    def test_run_llm_error_falls_back_to_deterministic(self) -> None:
        d = _dossier()
        run(d, _FakeLLM(exc=LLMError("网络失败")))
        self.assertEqual(len(d.evaluations), 2)
        self.assertTrue(all("novelty_score" in ev for ev in d.evaluations))

    def test_run_empty_ideas_writes_empty(self) -> None:
        d = _dossier(ideas=[], literature=[])
        run(d, NullProvider())
        self.assertEqual(d.evaluations, [])
        self.assertEqual(d.meta["prompt_versions"]["evaluate"], "v1")

    def test_run_skips_idea_without_id(self) -> None:
        d = _dossier(ideas=[{"claim": "无 id 的 idea", "novelty_hypothesis": "x"}], literature=[])
        run(d, NullProvider())
        self.assertEqual(d.evaluations, [])


class DataFeasibilityTest(unittest.TestCase):
    def test_high_medium_low(self) -> None:
        self.assertEqual(_data_feasibility({"data": ["时序"], "metrics": ["F1"]}), "high")
        self.assertEqual(_data_feasibility({"data": ["时序"], "metrics": []}), "medium")
        self.assertEqual(_data_feasibility({"data": [], "metrics": ["F1"]}), "low")
        self.assertEqual(_data_feasibility({}), "low")


class NoveltyTest(unittest.TestCase):
    def test_no_gap_notes_conservative_mid(self) -> None:
        self.assertEqual(_deterministic_novelty({"claim": "x"}, []), 2.5)

    def test_gap_supports_novelty(self) -> None:
        idea = {"claim": "缺失值鲁棒的异常检测", "novelty_hypothesis": "缺失值自适应的异常检测"}
        notes = ["现有异常检测方法在缺失值场景下存在 gap，尚未有系统研究"]
        score = _deterministic_novelty(idea, notes)
        self.assertGreaterEqual(score, 3.0)


class VerdictTest(unittest.TestCase):
    def test_low_novelty_drops(self) -> None:
        self.assertEqual(_decide_verdict(1.5, "high", 60, "proceed"), "drop")

    def test_low_data_rework(self) -> None:
        self.assertEqual(_decide_verdict(3.5, "low", 60, "proceed"), "rework")

    def test_high_workload_rework(self) -> None:
        self.assertEqual(_decide_verdict(4.0, "high", 500, "proceed"), "rework")

    def test_medium_data_low_novelty_rework(self) -> None:
        self.assertEqual(_decide_verdict(2.5, "medium", 60, "proceed"), "rework")

    def test_good_idea_proceeds(self) -> None:
        self.assertEqual(_decide_verdict(3.5, "high", 60, None), "proceed")

    def test_suggestion_respected_but_not_above_guard(self) -> None:
        # LLM 建议 rework 且 novelty 达标 → rework
        self.assertEqual(_decide_verdict(3.5, "high", 60, "rework"), "rework")
        # LLM 建议 proceed 但 novelty 过低 → 硬护栏 drop
        self.assertEqual(_decide_verdict(1.0, "high", 60, "proceed"), "drop")


class VenueTierTest(unittest.TestCase):
    def test_tier_of_known_and_unknown(self) -> None:
        self.assertEqual(_tier_of("KDD"), "CCF-A")
        self.assertEqual(_tier_of("arXiv"), "预印本（arXiv）")
        self.assertIn("未分级", _tier_of("Some Unknown Venue"))
        self.assertEqual(_tier_of(""), "未知档位")


class SchemaTest(unittest.TestCase):
    def test_schema_requires_evaluation_fields(self) -> None:
        self.assertEqual(EVALUATE_SCHEMA["type"], "object")
        required = EVALUATE_SCHEMA["required"]
        for key in ("novelty_score", "novelty_reason", "workload_hours",
                    "verdict_suggestion", "rework_reason"):
            self.assertIn(key, required)
        enum = EVALUATE_SCHEMA["properties"]["verdict_suggestion"]["enum"]
        self.assertEqual(enum, ["proceed", "rework", "drop"])


class SampleAcceptanceTest(unittest.TestCase):
    """验收：对 sample 项目产出评估（含 verdict）。"""

    def test_sample_project_produces_evaluations_with_verdict(self) -> None:
        d = _dossier()
        run(d, NullProvider())
        self.assertGreaterEqual(len(d.evaluations), 2)
        for ev in d.evaluations:
            self.assertIn(ev["verdict"], ("proceed", "rework", "drop"))
            self.assertTrue(ev["evidence"])


if __name__ == "__main__":
    unittest.main()
