"""M22 ⑥ 论文路线规划 Agent 单测：7 部分路线图、idea 选择、降级路径、M21 集成、sample 验收。

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
    render_roadmap_lines,
    run,
)
from papermine.agents.understand import run as understand_run
from papermine.dossier import Dossier
from papermine.llm import LLMError, NullProvider

SAMPLE_PROJECT = Path(__file__).resolve().parent.parent / "examples" / "sample-project"

# M22 七部分（替代旧 timeline / missing_items 的主内容地位）
_SEVEN_PARTS = {
    "core_story", "research_questions", "experiment_matrix",
    "minimum_viable_paper", "success_criteria", "risk_branches", "stage_exits",
}

_ROADMAP_KEYS = _SEVEN_PARTS | {"selected_idea", "paper_type", "outline", "missing_items"}


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


def _contribution():
    """M21 贡献分析子对象（类型 B 框架集成 + 攻击测试），供 M21 集成测试。"""
    return {
        "type": "B",
        "type_label": "框架集成创新（Framework Integration）",
        "reason": "已有异常检测 + 寿命预测的联合建模",
        "matrix": {
            "method": {"strength": "low", "label": "低", "reason": "无新模块"},
            "framework": {"strength": "high", "label": "高", "reason": "两任务交互"},
            "application": {"strength": "medium", "label": "中", "reason": "落地场景"},
            "problem": {"strength": "low", "label": "低", "reason": "未重新建模"},
            "training": {"strength": "none", "label": "无", "reason": "无训练策略"},
            "engineering": {"strength": "medium_high", "label": "中高", "reason": "可落地"},
        },
        "attacks": {
            "ablation": {"attack": "删除核心模块后剩下什么？", "answer": "退化为普通 RUL 预测"},
            "concatenation": {"attack": "A+B concat 是否等效？", "answer": "dynamic weighting 有效"},
            "reviewer": {"attack": "merely a combination", "answer": "共享表示 + 消融证明"},
        },
        "degraded": False,
    }


def _evals():
    return [
        {"idea_ref": "i1", "novelty_score": 71.5, "data_feasibility": "high",
         "workload_hours": 110, "venue_guess": "CCF-B", "verdict": "proceed",
         "rework_reason": None, "evidence": [], "contribution": _contribution()},
        {"idea_ref": "i2", "novelty_score": 55.0, "data_feasibility": "high",
         "workload_hours": 90, "venue_guess": "EI 会议", "verdict": "rework",
         "rework_reason": "新颖性偏低", "evidence": []},
    ]


def _valid_llm_roadmap():
    """schema 合规的完整 LLM 输出（7 部分 + paper_type + outline）。"""
    return {
        "paper_type": "方法论文",
        "outline": ["1. intro", "2. related work", "3. method"],
        "core_story": {
            "status_quo": "现状", "problem": "问题", "method": "方法", "contribution": "贡献",
        },
        "research_questions": [
            {"id": "RQ1", "question": "Q1?", "target_experiments": ["E1"]},
            {"id": "RQ2", "question": "Q2?", "target_experiments": ["E2"]},
        ],
        "experiment_matrix": [
            {"experiment": "E1", "purpose": "p", "independent_variable": "v",
             "baselines": ["b"], "metrics": ["m"], "rq": "RQ1"},
        ],
        "minimum_viable_paper": {"must_have": ["主实验"], "optional": ["理论分析"]},
        "success_criteria": {"success": ["显著"], "failure": ["无提升"], "pivot": "转分析"},
        "risk_branches": [{"risk": "XGBoost 最好", "branch": "转分析失效"}],
        "stage_exits": [{"stage": "Week 1", "tasks": ["跑通"], "exit_criteria": "可复现"}],
    }


def _dossier(ideas=None, evaluations=None):
    d = Dossier(project_id="proj-m22", llm_backend="deepseek")
    understand_run(str(SAMPLE_PROJECT), d, NullProvider())
    d.ideas = ideas if ideas is not None else _ideas()
    d.evaluations = evaluations if evaluations is not None else _evals()
    return d


class RunTest(unittest.TestCase):
    def test_run_null_llm_writes_seven_part_roadmap(self) -> None:
        d = _dossier()
        run(d, NullProvider())

        self.assertEqual(set(d.roadmap), _ROADMAP_KEYS)
        self.assertTrue(_SEVEN_PARTS.issubset(set(d.roadmap)))
        self.assertEqual(d.roadmap["selected_idea"], "i1")   # proceed 优先
        self.assertTrue(d.roadmap["paper_type"])
        self.assertGreaterEqual(len(d.roadmap["outline"]), 5)

        # 1 论文主线四段
        cs = d.roadmap["core_story"]
        for k in ("status_quo", "problem", "method", "contribution"):
            self.assertTrue(cs[k], "core_story.{} 应非空".format(k))

        # 2 Research Questions：2~4 个，各带 target_experiments
        rqs = d.roadmap["research_questions"]
        self.assertGreaterEqual(len(rqs), 2)
        self.assertLessEqual(len(rqs), 4)
        for q in rqs:
            self.assertTrue(q["id"] and q["question"])
            self.assertIsInstance(q["target_experiments"], list)

        # 3 实验矩阵：6 列，rq 挂到某个 RQ
        matrix = d.roadmap["experiment_matrix"]
        self.assertTrue(matrix)
        for e in matrix:
            for k in ("experiment", "purpose", "independent_variable", "baselines", "metrics", "rq"):
                self.assertIn(k, e)

        # 4 MVP：must_have 非空
        mvp = d.roadmap["minimum_viable_paper"]
        self.assertTrue(mvp["must_have"])
        self.assertIsInstance(mvp["optional"], list)

        # 5 成功/失败标准
        sc = d.roadmap["success_criteria"]
        self.assertTrue(sc["success"])
        self.assertTrue(sc["pivot"])

        # 6 风险分支
        self.assertTrue(d.roadmap["risk_branches"])

        # 7 阶段出口
        self.assertTrue(d.roadmap["stage_exits"])
        self.assertEqual(d.meta["prompt_versions"]["plan"], "v2")

    def test_run_with_llm_uses_seven_part_output(self) -> None:
        d = _dossier()
        run(d, _FakeLLM(result=_valid_llm_roadmap()))

        self.assertEqual(d.roadmap["paper_type"], "方法论文")
        self.assertEqual(d.roadmap["core_story"]["problem"], "问题")
        self.assertEqual(d.roadmap["research_questions"][0]["id"], "RQ1")
        self.assertEqual(d.roadmap["experiment_matrix"][0]["rq"], "RQ1")
        self.assertEqual(d.roadmap["minimum_viable_paper"]["must_have"], ["主实验"])
        self.assertEqual(d.roadmap["risk_branches"][0]["branch"], "转分析失效")
        self.assertEqual(d.roadmap["stage_exits"][0]["exit_criteria"], "可复现")

    def test_run_llm_error_falls_back_to_seven_part(self) -> None:
        d = _dossier()
        run(d, _FakeLLM(exc=LLMError("网络失败")))
        self.assertTrue(_SEVEN_PARTS.issubset(set(d.roadmap)))
        self.assertTrue(d.roadmap["core_story"]["contribution"])
        self.assertTrue(d.roadmap["experiment_matrix"])

    def test_run_invalid_rq_count_falls_back(self) -> None:
        # LLM 返回 1 个 RQ（<2）→ 整体回退确定性 3 个 RQ
        out = _valid_llm_roadmap()
        out["research_questions"] = [
            {"id": "RQ1", "question": "Q1?", "target_experiments": ["E1"]},
        ]
        d = _dossier()
        run(d, _FakeLLM(result=out))
        self.assertEqual(len(d.roadmap["research_questions"]), 3)

    def test_run_no_ideas_empty_roadmap(self) -> None:
        d = _dossier(ideas=[], evaluations=[])
        run(d, NullProvider())
        self.assertIsNone(d.roadmap["selected_idea"])
        self.assertEqual(d.roadmap["research_questions"], [])
        self.assertEqual(d.roadmap["experiment_matrix"], [])
        self.assertEqual(d.roadmap["missing_items"], [])

    def test_run_missing_items_flags_gaps(self) -> None:
        d = _dossier()
        # 清空 facts 的数据与指标 → 应触发数据/指标缺口（派生 missing_items 仍生效）
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
        self.assertEqual(idea["idea_id"], "i1")
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


class M21IntegrationTest(unittest.TestCase):
    """M22 复用 M21 贡献分析：Core Story 贡献段 + 风险分支来自攻击测试。"""

    def test_core_story_contribution_from_contribution_matrix(self) -> None:
        d = _dossier()
        run(d, NullProvider())
        cs = d.roadmap["core_story"]
        self.assertIn("框架集成创新", cs["contribution"])
        self.assertIn("框架创新", cs["contribution"])

    def test_risk_branches_seed_from_attacks(self) -> None:
        d = _dossier()
        run(d, NullProvider())
        risks = [rb["risk"] for rb in d.roadmap["risk_branches"]]
        # 攻击测试「merely a combination」应转化为风险分支
        self.assertTrue(any("merely a combination" in r for r in risks))


class RenderRoadmapLinesTest(unittest.TestCase):
    def test_render_new_roadmap(self) -> None:
        d = _dossier()
        run(d, NullProvider())
        lines = render_roadmap_lines(d.roadmap)
        text = "\n".join(lines)
        for marker in ("论文主线", "Research Questions", "实验矩阵", "最小可发表版本",
                       "成功/失败标准", "风险分支", "阶段出口时间线"):
            self.assertIn(marker, text)

    def test_render_old_roadmap_returns_empty(self) -> None:
        # 旧格式 roadmap（无 core_story）→ 空列表，由 orchestrator 走旧渲染兜底
        self.assertEqual(render_roadmap_lines({"outline": ["1"], "missing_items": ["x"]}), [])


class SchemaTest(unittest.TestCase):
    def test_schema_requires_seven_part_fields(self) -> None:
        self.assertEqual(PLAN_SCHEMA["type"], "object")
        required = PLAN_SCHEMA["required"]
        for key in ("paper_type", "outline", "core_story", "research_questions",
                    "experiment_matrix", "minimum_viable_paper", "success_criteria",
                    "risk_branches", "stage_exits"):
            self.assertIn(key, required)


class SampleAcceptanceTest(unittest.TestCase):
    """验收：对 sample 项目产出含 7 部分的路线图，学生读完能直接开写、知道哪些可不做。"""

    def test_sample_project_produces_seven_part_roadmap(self) -> None:
        d = _dossier()
        run(d, NullProvider())
        self.assertIsNotNone(d.roadmap["selected_idea"])
        self.assertTrue(_SEVEN_PARTS.issubset(set(d.roadmap)))
        self.assertTrue(d.roadmap["minimum_viable_paper"]["must_have"])
        self.assertTrue(d.roadmap["minimum_viable_paper"]["optional"])
        self.assertTrue(d.roadmap["success_criteria"]["pivot"])
        self.assertTrue(d.roadmap["risk_branches"])
        self.assertTrue(d.roadmap["stage_exits"])


if __name__ == "__main__":
    unittest.main()
