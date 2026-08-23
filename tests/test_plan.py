"""M6 ⑥ 论文路线规划 Agent 单测：idea 选择、路线图字段、降级路径、sample 验收。

用标准库 unittest 编写（与 tests/test_dossier.py 一致），`python -m unittest discover -s tests -v`
即可运行，无需新增第三方依赖（也兼容 pytest 收集）。
"""
from __future__ import annotations

import unittest
from pathlib import Path

from papermine.agents.plan import (
    PLAN_SCHEMA,
    _deterministic_paper_type,
    _select_idea,
    run,
)
from papermine.agents.understand import run as understand_run
from papermine.dossier import Dossier
from papermine.llm import LLMError, NullProvider

SAMPLE_PROJECT = Path(__file__).resolve().parent.parent / "examples" / "sample-project"

_ROADMAP_KEYS = {"selected_idea", "paper_type", "outline",
                 "experiment_plan", "timeline", "missing_items"}


class _FakeLLM:
    def __init__(self, result=None, exc=None):
        self.result = result if result is not None else {}
        self.exc = exc
        self.calls = []

    def complete(self, system, user, schema, temperature=0.3):
        self.calls.append((system, user, schema, temperature))
        if self.exc is not None:
            raise self.exc
        return self.result


def _ideas():
    return [
        {"idea_id": "i1", "claim": "面向工业制造的传感器时序异常检测方法",
         "novelty_hypothesis": "缺失值自适应的异常检测", "problem_ref": "p1",
         "literature_refs": [], "status": "pending_eval"},
        {"idea_id": "i2", "claim": "通用数据处理流水线工具框架",
         "novelty_hypothesis": "可复用组件", "problem_ref": "p2",
         "literature_refs": [], "status": "pending_eval"},
    ]


def _evals():
    return [
        {"idea_ref": "i1", "novelty_score": 3.5, "data_feasibility": "high",
         "workload_hours": 110, "venue_guess": "CCF-B", "verdict": "proceed",
         "rework_reason": None, "evidence": []},
        {"idea_ref": "i2", "novelty_score": 2.8, "data_feasibility": "high",
         "workload_hours": 90, "venue_guess": "EI 会议", "verdict": "rework",
         "rework_reason": "新颖性偏低", "evidence": []},
    ]


def _dossier(ideas=None, evaluations=None):
    d = Dossier(project_id="proj-m6", llm_backend="deepseek")
    understand_run(str(SAMPLE_PROJECT), d, NullProvider())
    d.ideas = ideas if ideas is not None else _ideas()
    d.evaluations = evaluations if evaluations is not None else _evals()
    return d


class RunTest(unittest.TestCase):
    def test_run_null_llm_writes_roadmap(self) -> None:
        d = _dossier()
        run(d, NullProvider())

        self.assertEqual(set(d.roadmap), _ROADMAP_KEYS)
        self.assertEqual(d.roadmap["selected_idea"], "i1")   # proceed 优先
        self.assertTrue(d.roadmap["paper_type"])
        self.assertGreaterEqual(len(d.roadmap["outline"]), 5)
        self.assertTrue(d.roadmap["experiment_plan"])
        self.assertIsInstance(d.roadmap["timeline"], dict)
        self.assertTrue(d.roadmap["timeline"])
        self.assertIsInstance(d.roadmap["missing_items"], list)
        self.assertEqual(d.meta["prompt_versions"]["plan"], "v1")

    def test_run_with_llm_uses_plan(self) -> None:
        d = _dossier()
        llm = _FakeLLM(result={
            "paper_type": "方法论文",
            "outline": ["1. intro", "2. related work", "3. method"],
            "experiment_plan": ["1. baseline", "2. 主实验"],
            "timeline": {"第1周": "调研"},
            "missing_items": ["补 baseline"],
        })
        run(d, llm)

        self.assertEqual(d.roadmap["paper_type"], "方法论文")
        self.assertEqual(d.roadmap["outline"][0], "1. intro")
        self.assertEqual(d.roadmap["timeline"], {"第1周": "调研"})

    def test_run_llm_error_falls_back(self) -> None:
        d = _dossier()
        run(d, _FakeLLM(exc=LLMError("网络失败")))
        self.assertTrue(d.roadmap["selected_idea"])
        self.assertTrue(d.roadmap["outline"])
        self.assertTrue(d.roadmap["experiment_plan"])

    def test_run_no_ideas_empty_roadmap(self) -> None:
        d = _dossier(ideas=[], evaluations=[])
        run(d, NullProvider())
        self.assertIsNone(d.roadmap["selected_idea"])
        self.assertEqual(d.roadmap["outline"], [])
        self.assertEqual(d.roadmap["missing_items"], [])

    def test_run_missing_items_flags_gaps(self) -> None:
        d = _dossier()
        # 清空 facts 的数据与指标 → 应触发数据/指标缺口
        d.assets["facts"]["data"] = []
        d.assets["facts"]["metrics"] = []
        run(d, NullProvider())
        text = "、".join(d.roadmap["missing_items"])
        self.assertIn("数据集", text)
        self.assertIn("指标", text)


class SelectIdeaTest(unittest.TestCase):
    def test_proceed_preferred_over_rework(self) -> None:
        idea, ev = _select_idea(_ideas(), _evals())
        self.assertEqual(idea["idea_id"], "i1")
        self.assertEqual(ev["verdict"], "proceed")

    def test_rework_selected_when_no_proceed(self) -> None:
        evals = [{**e, "verdict": "rework"} for e in _evals()]
        idea, ev = _select_idea(_ideas(), evals)
        self.assertEqual(idea["idea_id"], "i1")  # 同级 rework 里 novelty 最高(3.5)者优先
        self.assertEqual(ev["verdict"], "rework")

    def test_no_ideas_returns_none(self) -> None:
        self.assertEqual(_select_idea([], []), (None, None))


class PaperTypeTest(unittest.TestCase):
    def test_method_paper_for_modeling_facts(self) -> None:
        facts = {"tasks": ["异常检测"], "methods": ["孤立森林"],
                 "data": ["时序数据"], "scenarios": ["工业制造"],
                 "metrics": ["F1"], "libraries": [], "modules": []}
        self.assertEqual(_deterministic_paper_type(facts, _ideas()[0]), "方法论文")

    def test_system_paper_for_tool_claim(self) -> None:
        facts = {"tasks": ["异常检测"], "methods": [],
                 "data": [], "scenarios": [], "metrics": [], "libraries": [], "modules": []}
        self.assertEqual(_deterministic_paper_type(facts, _ideas()[1]), "系统/工具论文")


class SchemaTest(unittest.TestCase):
    def test_schema_requires_roadmap_fields(self) -> None:
        self.assertEqual(PLAN_SCHEMA["type"], "object")
        required = PLAN_SCHEMA["required"]
        for key in ("paper_type", "outline", "experiment_plan", "timeline", "missing_items"):
            self.assertIn(key, required)


class SampleAcceptanceTest(unittest.TestCase):
    """验收：对 sample 项目产出一份 roadmap。"""

    def test_sample_project_produces_roadmap(self) -> None:
        d = _dossier()
        run(d, NullProvider())
        self.assertIsNotNone(d.roadmap["selected_idea"])
        self.assertTrue(d.roadmap["paper_type"])
        self.assertTrue(d.roadmap["outline"])
        self.assertTrue(d.roadmap["experiment_plan"])
        self.assertTrue(d.roadmap["timeline"])
        self.assertIsInstance(d.roadmap["missing_items"], list)


if __name__ == "__main__":
    unittest.main()
