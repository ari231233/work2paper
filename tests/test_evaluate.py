"""M6/M11 ⑤ 可行性评估 Agent 单测：证据驱动评估、多维加权 novelty、verdict、降级路径、sample 验收。

用标准库 unittest 编写（与 tests/test_dossier.py 一致），`python -m unittest discover -s tests -v`
即可运行，无需新增第三方依赖（也兼容 pytest 收集）。

M11 增量：novelty 从单一 0~5 升级为 5 维加权（0~100 总分 + 分维度明细 + 分数段映射 verdict）。
"""
from __future__ import annotations

import unittest
from pathlib import Path

from papermine.agents.evaluate import (
    EVALUATE_SCHEMA,
    NOVELTY_DIMENSIONS,
    _data_feasibility,
    _decide_verdict,
    _deterministic_dimensions,
    _score_band,
    _tier_of,
    _weighted_total,
    run,
)
from papermine.agents.understand import run as understand_run
from papermine.dossier import Dossier
from papermine.llm import LLMError, NullProvider

SAMPLE_PROJECT = Path(__file__).resolve().parent.parent / "examples" / "sample-project"

_DIM_KEYS = tuple(k for k, _l, _w in NOVELTY_DIMENSIONS)
_BANDS = ("Reject", "Weak Reject", "Revise", "Accept", "Priority")


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


def _dims(problem=4, method=4, tech=3, gap=4, gen=3):
    return {
        "problem_novelty": {"score": problem, "reason": "问题未被充分解决（gap 支持）"},
        "method_novelty": {"score": method, "reason": "方法有自适应新机制"},
        "technical_depth": {"score": tech, "reason": "关键技术瓶颈未完全解决"},
        "gap": {"score": gap, "reason": "与 SOTA 差异明确"},
        "generalization": {"score": gen, "reason": "可迁移到其他任务"},
    }


def _llm_eval(dims=None, workload=80, suggestion="proceed", reason=None):
    return {
        "novelty_dimensions": dims if dims is not None else _dims(),
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


def _assert_dimensions(self, dims):
    self.assertEqual(set(dims), set(_DIM_KEYS))
    for key in _DIM_KEYS:
        item = dims[key]
        self.assertIn("score", item)
        self.assertIn("reason", item)
        self.assertGreaterEqual(item["score"], 0)
        self.assertLessEqual(item["score"], 5)
        self.assertTrue(str(item["reason"]).strip())


class RunTest(unittest.TestCase):
    def test_run_null_llm_writes_evaluations_with_verdict(self) -> None:
        d = _dossier()
        run(d, NullProvider())

        self.assertEqual(len(d.evaluations), 2)
        for ev in d.evaluations:
            self.assertIn(ev["verdict"], ("proceed", "rework", "drop"))
            self.assertIn(ev["idea_ref"], ("i1", "i2"))
            self.assertIsInstance(ev["novelty_score"], float)
            self.assertGreaterEqual(ev["novelty_score"], 0)
            self.assertLessEqual(ev["novelty_score"], 100)
            self.assertIn(ev["novelty_band"], _BANDS)
            _assert_dimensions(self, ev["novelty_dimensions"])
            self.assertIn(ev["data_feasibility"], ("high", "medium", "low"))
            self.assertIsInstance(ev["workload_hours"], int)
            self.assertTrue(ev["venue_guess"])
            self.assertTrue(ev["evidence"])
            for e in ev["evidence"]:
                self.assertIn("source", e)
                self.assertIn("note", e)
        self.assertEqual(d.meta["prompt_versions"]["evaluate"], "v2")

    def test_run_is_evidence_driven(self) -> None:
        """评估必须挂确定性证据源（facts / literature + 分维度明细），不是凭空断言。"""
        d = _dossier()
        run(d, NullProvider())
        sources = {e["source"] for ev in d.evaluations for e in ev["evidence"]}
        self.assertIn("literature.gap_note", sources)
        self.assertIn("assets.facts.data", sources)      # sample 有数据标签
        self.assertIn("assets.facts.metrics", sources)   # sample 有指标标签
        self.assertIn("literature.venues", sources)      # 检索论文带 venue
        # M11：分维度明细挂进证据链
        self.assertTrue(any(s.startswith("novelty_dimensions.") for s in sources))

    def test_run_with_llm_uses_dimensions_and_weighted_total(self) -> None:
        d = _dossier()
        dims_high = _dims(problem=5, method=4, tech=4, gap=4, gen=4)   # → 84.0 → Priority
        dims_low = _dims(problem=1, method=2, tech=1, gap=1, gen=2)    # → 29.0 → Reject
        llm = _FakeLLM(results=[
            _llm_eval(dims=dims_high, workload=120, suggestion="proceed"),
            _llm_eval(dims=dims_low, workload=50, suggestion="drop"),
        ])
        run(d, llm)

        self.assertEqual(d.evaluations[0]["novelty_score"], _weighted_total(dims_high))
        self.assertEqual(d.evaluations[1]["novelty_score"], _weighted_total(dims_low))
        self.assertEqual(d.evaluations[0]["verdict"], "proceed")
        self.assertIn(d.evaluations[0]["novelty_band"], ("Accept", "Priority"))
        self.assertEqual(d.evaluations[1]["novelty_score"], 29.0)
        self.assertEqual(d.evaluations[1]["novelty_band"], "Reject")
        self.assertEqual(d.evaluations[1]["verdict"], "drop")
        self.assertEqual(
            d.evaluations[0]["novelty_dimensions"]["method_novelty"]["score"], 4)

    def test_run_llm_error_falls_back_to_deterministic(self) -> None:
        d = _dossier()
        run(d, _FakeLLM(exc=LLMError("网络失败")))
        self.assertEqual(len(d.evaluations), 2)
        self.assertTrue(all("novelty_score" in ev for ev in d.evaluations))
        self.assertTrue(all("novelty_dimensions" in ev for ev in d.evaluations))

    def test_run_empty_ideas_writes_empty(self) -> None:
        d = _dossier(ideas=[], literature=[])
        run(d, NullProvider())
        self.assertEqual(d.evaluations, [])
        self.assertEqual(d.meta["prompt_versions"]["evaluate"], "v2")

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


class DeterministicDimensionsTest(unittest.TestCase):
    def test_five_dimensions_not_all_equal(self) -> None:
        """M11 验收：确定性兜底也必须让 5 个维度分不全相等。"""
        idea = {"claim": "缺失值自适应的异常检测", "novelty_hypothesis": "缺失值自适应的异常检测"}
        notes = ["现有异常检测方法在缺失值场景下存在 gap，尚未有系统研究"]
        facts = {"methods": ["深度学习", "孤立森林"], "tasks": ["异常检测"],
                 "data": ["时序"], "metrics": ["F1"], "scenarios": ["工业制造"],
                 "libraries": [], "modules": []}
        dims = _deterministic_dimensions(idea, notes, facts)
        _assert_dimensions(self, dims)
        scores = [dims[k]["score"] for k in _DIM_KEYS]
        self.assertGreater(len(set(scores)), 1)

    def test_no_gap_conservative(self) -> None:
        dims = _deterministic_dimensions({"claim": "x", "novelty_hypothesis": ""}, [], {})
        self.assertEqual(dims["problem_novelty"]["score"], 2.0)   # 无法对拍，保守
        self.assertLessEqual(dims["gap"]["score"], 2.0)
        self.assertGreaterEqual(_weighted_total(dims), 0)
        self.assertLessEqual(_weighted_total(dims), 100)


class WeightedTotalTest(unittest.TestCase):
    def test_all_zero_and_all_five(self) -> None:
        zero = {k: {"score": 0, "reason": "x"} for k in _DIM_KEYS}
        five = {k: {"score": 5, "reason": "x"} for k in _DIM_KEYS}
        self.assertEqual(_weighted_total(zero), 0.0)
        self.assertEqual(_weighted_total(five), 100.0)

    def test_known_mix(self) -> None:
        self.assertEqual(_weighted_total(_dims(problem=4, method=4, tech=3, gap=4, gen=3)), 74.0)


class ScoreBandTest(unittest.TestCase):
    def test_bands(self) -> None:
        self.assertEqual(_score_band(39.9), ("Reject", "drop"))
        self.assertEqual(_score_band(40), ("Weak Reject", "drop"))
        self.assertEqual(_score_band(59.9), ("Weak Reject", "drop"))
        self.assertEqual(_score_band(60), ("Revise", "rework"))
        self.assertEqual(_score_band(69.9), ("Revise", "rework"))
        self.assertEqual(_score_band(70), ("Accept", "proceed"))
        self.assertEqual(_score_band(80), ("Accept", "proceed"))
        self.assertEqual(_score_band(80.1), ("Priority", "proceed"))


class VerdictTest(unittest.TestCase):
    def test_band_maps_to_verdict(self) -> None:
        self.assertEqual(_decide_verdict(30, "high", 60, "proceed"), "drop")     # Reject
        self.assertEqual(_decide_verdict(50, "high", 60, "proceed"), "drop")     # Weak Reject
        self.assertEqual(_decide_verdict(65, "high", 60, None), "rework")        # Revise
        self.assertEqual(_decide_verdict(75, "high", 60, None), "proceed")       # Accept
        self.assertEqual(_decide_verdict(90, "high", 60, None), "proceed")       # Priority

    def test_low_data_rework(self) -> None:
        self.assertEqual(_decide_verdict(75, "low", 60, "proceed"), "rework")

    def test_high_workload_rework(self) -> None:
        self.assertEqual(_decide_verdict(75, "high", 500, "proceed"), "rework")

    def test_medium_data_drop_band_rework(self) -> None:
        self.assertEqual(_decide_verdict(50, "medium", 60, "proceed"), "rework")

    def test_suggestion_downgrade_only(self) -> None:
        # LLM 建议 rework 且 novelty 达标 → rework
        self.assertEqual(_decide_verdict(75, "high", 60, "rework"), "rework")
        # LLM 建议 proceed 但 novelty 过低 → 分数段硬护栏 drop（不可上调）
        self.assertEqual(_decide_verdict(30, "high", 60, "proceed"), "drop")


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
        for key in ("novelty_dimensions", "workload_hours",
                    "verdict_suggestion", "rework_reason"):
            self.assertIn(key, required)
        dims = EVALUATE_SCHEMA["properties"]["novelty_dimensions"]
        self.assertEqual(set(dims["required"]), set(_DIM_KEYS))
        self.assertEqual(set(dims["properties"]), set(_DIM_KEYS))
        enum = EVALUATE_SCHEMA["properties"]["verdict_suggestion"]["enum"]
        self.assertEqual(enum, ["proceed", "rework", "drop"])


class SampleAcceptanceTest(unittest.TestCase):
    """验收：对 sample 项目产出评估，5 个维度分不全相等（M11 验收 #2）。"""

    def test_sample_project_evaluations_differentiated(self) -> None:
        d = _dossier()
        run(d, NullProvider())
        self.assertGreaterEqual(len(d.evaluations), 2)
        for ev in d.evaluations:
            self.assertIn(ev["verdict"], ("proceed", "rework", "drop"))
            self.assertTrue(ev["evidence"])
            scores = [ev["novelty_dimensions"][k]["score"] for k in _DIM_KEYS]
            self.assertGreater(len(set(scores)), 1,
                               "5 个维度分应不全相等（避免评分趋同）")


if __name__ == "__main__":
    unittest.main()
