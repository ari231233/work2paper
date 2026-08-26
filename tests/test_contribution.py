"""M21 创新贡献分析（类型分类 + 贡献矩阵 + 攻击测试）单测。

覆盖：类型分类 A-E、贡献矩阵强度、matrix_viable（verdict 差异化依据）、
LLM 输出规范化 / 确定性降级、批量路由、报告渲染、sample 验收（每个 idea 先输出
「类型 + 矩阵 + 攻击测试」而非直接 novelty 分）。

用标准库 unittest 编写（与 tests/test_evaluate.py 一致），`python -m unittest discover -s tests -v`
即可运行，无需新增第三方依赖。
"""
from __future__ import annotations

import unittest
from pathlib import Path

from papermine.agents.contribution import (
    ATTACK_KEYS,
    CONTRIBUTION_BATCH_SCHEMA,
    CONTRIBUTION_SCHEMA,
    CONTRIBUTION_TYPES,
    CONTRIBUTION_TYPE_LABELS,
    MATRIX_DIMENSIONS,
    MATRIX_LABELS,
    STRENGTH_LEVELS,
    STRENGTH_ORDER,
    classify_contribution,
    classify_contribution_batch,
    matrix_viable,
    render_contribution_lines,
    _deterministic_contribution,
    _finalize_contribution,
)
from papermine.llm import NullProvider

SAMPLE_PROJECT = Path(__file__).resolve().parent.parent / "examples" / "sample-project"

_STRENGTHS = ("none", "low", "medium", "medium_high", "high")


def _idea(claim, hypothesis="假设有效", **extra):
    idea = {"idea_id": "i1", "claim": claim, "novelty_hypothesis": hypothesis,
            "problem_ref": "p1", "literature_refs": [], "status": "pending_eval"}
    idea.update(extra)
    return idea


def _facts(**overrides):
    facts = {"tasks": ["异常检测"], "methods": ["孤立森林"], "scenarios": ["工业制造"],
             "data": ["时序"], "metrics": ["F1"], "modules": ["pipeline"], "libraries": []}
    facts.update(overrides)
    return facts


def _matrix(**overrides):
    matrix = {d: {"strength": "medium", "reason": "r-{}".format(d)} for d in MATRIX_DIMENSIONS}
    matrix.update(overrides)
    return matrix


def _attacks(**overrides):
    attacks = {k: {"attack": "攻击-{}".format(k), "answer": "回答-{}".format(k)}
               for k in ATTACK_KEYS}
    attacks.update(overrides)
    return attacks


def _llm_contribution(ctype="B", matrix=None, attacks=None):
    return {
        "contribution_type": {"type": ctype, "reason": "把两个任务联合建模"},
        "matrix": matrix if matrix is not None else _matrix(),
        "attacks": attacks if attacks is not None else _attacks(),
    }


class _FakeLLM:
    """按 schema 路由：批量贡献 / 单条贡献 / 其余返回空（触发确定性兜底）。"""

    def __init__(self, contribution_results=None, batch_contribution=None):
        self.contribution_results = list(contribution_results or [])
        self.batch_contribution = list(batch_contribution or [])
        self.calls = []

    def complete(self, system, user, schema, temperature=0.2):
        self.calls.append((system, user, schema, temperature))
        props = schema.get("properties") or {}
        if "results" in props:
            items_req = ((props["results"].get("items") or {}).get("required")) or []
            if "contribution_type" in items_req:
                if self.batch_contribution:
                    return self.batch_contribution.pop(0)
                return {"results": []}
            return {"results": []}
        if "contribution_type" in props:
            if self.contribution_results:
                return self.contribution_results.pop(0)
            return {}
        return {}


class ContributionTypesTest(unittest.TestCase):
    def test_types_and_labels(self):
        self.assertEqual(CONTRIBUTION_TYPES, ("A", "B", "C", "D", "E"))
        for t in CONTRIBUTION_TYPES:
            self.assertIn(t, CONTRIBUTION_TYPE_LABELS)
            self.assertTrue(CONTRIBUTION_TYPE_LABELS[t].strip())

    def test_matrix_dimensions_and_labels(self):
        self.assertEqual(MATRIX_DIMENSIONS,
                         ("method", "framework", "application", "problem", "training", "engineering"))
        self.assertEqual(set(MATRIX_LABELS), set(MATRIX_DIMENSIONS))

    def test_strength_levels(self):
        self.assertEqual(STRENGTH_LEVELS, ("none", "low", "medium", "medium_high", "high"))
        # 强度序：none < low < medium < medium_high < high
        self.assertLess(STRENGTH_ORDER["none"], STRENGTH_ORDER["low"])
        self.assertLess(STRENGTH_ORDER["low"], STRENGTH_ORDER["medium"])
        self.assertLess(STRENGTH_ORDER["medium"], STRENGTH_ORDER["medium_high"])
        self.assertLess(STRENGTH_ORDER["medium_high"], STRENGTH_ORDER["high"])

    def test_attack_keys(self):
        self.assertEqual(ATTACK_KEYS, ("ablation", "concatenation", "reviewer"))


class DeterministicClassifyTest(unittest.TestCase):
    """M21.1：确定性分类（词面信号）。"""

    def _type(self, claim, hypothesis="假设有效"):
        c = _deterministic_contribution(_idea(claim, hypothesis), _facts(), [])
        self.assertTrue(c["degraded"])
        return c["type"]

    def test_type_a_method(self):
        self.assertEqual(self._type("提出一种全新的注意力模块以提升时序建模能力"), "A")

    def test_type_b_framework(self):
        self.assertEqual(self._type("将异常检测与剩余寿命预测结合，进行联合建模"), "B")

    def test_type_b_aided(self):
        # M21 任务卡示例：异常检测辅助 RUL → 框架集成
        self.assertEqual(self._type("异常检测辅助剩余寿命预测"), "B")

    def test_type_c_application(self):
        self.assertEqual(self._type("将 Transformer 迁移到工业设备剩余寿命预测的新场景"), "C")

    def test_type_d_problem(self):
        self.assertEqual(self._type("重新定义剩余寿命预测为多任务联合优化问题"), "D")

    def test_type_e_training(self):
        self.assertEqual(self._type("引入课程学习训练策略以缓解数据不均衡"), "E")


class DeterministicMatrixTest(unittest.TestCase):
    """M21.2：确定性贡献矩阵强度。"""

    def test_matrix_has_all_dimensions(self):
        c = _deterministic_contribution(_idea("结合两个任务", "h"), _facts(), [])
        self.assertEqual(set(c["matrix"]), set(MATRIX_DIMENSIONS))
        for dim in MATRIX_DIMENSIONS:
            item = c["matrix"][dim]
            self.assertIn(item["strength"], _STRENGTHS)
            self.assertTrue(item["label"])
            self.assertTrue(item["reason"])

    def test_framework_high_when_combined(self):
        c = _deterministic_contribution(_idea("多任务联合建模", "h"), _facts(), [])
        self.assertGreaterEqual(
            STRENGTH_ORDER[c["matrix"]["framework"]["strength"]], STRENGTH_ORDER["medium"])

    def test_engineering_high_with_assets(self):
        c = _deterministic_contribution(_idea("改进方法", "h"), _facts(), [])
        self.assertGreaterEqual(
            STRENGTH_ORDER[c["matrix"]["engineering"]["strength"]], STRENGTH_ORDER["medium"])

    def test_engineering_low_without_assets(self):
        c = _deterministic_contribution(_idea("改进方法", "h"),
                                        {"tasks": [], "methods": [], "scenarios": [],
                                         "data": [], "metrics": [], "modules": []}, [])
        self.assertLess(STRENGTH_ORDER[c["matrix"]["engineering"]["strength"]], STRENGTH_ORDER["medium"])


class DeterministicAttacksTest(unittest.TestCase):
    """M21.3：确定性攻击测试模板。"""

    def test_three_attacks_present(self):
        c = _deterministic_contribution(_idea("结合两个任务", "h"), _facts(), [])
        self.assertEqual(set(c["attacks"]), set(ATTACK_KEYS))
        for key in ATTACK_KEYS:
            self.assertTrue(c["attacks"][key]["attack"])
            self.assertTrue(c["attacks"][key]["answer"])


class MatrixViableTest(unittest.TestCase):
    def test_any_medium_is_viable(self):
        self.assertTrue(matrix_viable(_matrix(method={"strength": "medium", "reason": "r"})))

    def test_all_low_not_viable(self):
        m = {d: {"strength": "low", "reason": "r"} for d in MATRIX_DIMENSIONS}
        self.assertFalse(matrix_viable(m))

    def test_none_matrix_is_viable(self):
        # 无矩阵 → 保守，避免误 reject
        self.assertTrue(matrix_viable(None))
        self.assertTrue(matrix_viable({}))


class SchemaTest(unittest.TestCase):
    def test_schema_requires_type_matrix_attacks(self):
        self.assertEqual(CONTRIBUTION_SCHEMA["type"], "object")
        self.assertEqual(set(CONTRIBUTION_SCHEMA["required"]),
                         {"contribution_type", "matrix", "attacks"})
        # 无 novelty 分数字段（M21：只分类不评分）
        props = CONTRIBUTION_SCHEMA["properties"]
        self.assertNotIn("novelty", props)
        self.assertNotIn("score", props)
        self.assertEqual(set(props["matrix"]["required"]), set(MATRIX_DIMENSIONS))
        self.assertEqual(set(props["attacks"]["required"]), set(ATTACK_KEYS))

    def test_batch_schema(self):
        items = CONTRIBUTION_BATCH_SCHEMA["properties"]["results"]["items"]
        self.assertIn("idea_id", items["required"])
        self.assertIn("contribution_type", items["required"])


class FinalizeTest(unittest.TestCase):
    def test_valid_llm_output_not_degraded(self):
        raw = _llm_contribution(ctype="B")
        out = _finalize_contribution(raw, _idea("结合两个任务"), _facts(), [])
        self.assertFalse(out["degraded"])
        self.assertEqual(out["type"], "B")
        self.assertEqual(out["type_label"], CONTRIBUTION_TYPE_LABELS["B"])
        self.assertEqual(set(out["matrix"]), set(MATRIX_DIMENSIONS))
        self.assertEqual(set(out["attacks"]), set(ATTACK_KEYS))

    def test_invalid_type_falls_back(self):
        raw = _llm_contribution(ctype="Z")
        out = _finalize_contribution(raw, _idea("结合两个任务"), _facts(), [])
        self.assertTrue(out["degraded"])

    def test_invalid_matrix_falls_back(self):
        bad = _matrix(method={"strength": "super", "reason": "x"})  # 非法强度档
        raw = _llm_contribution(ctype="B", matrix=bad)
        out = _finalize_contribution(raw, _idea("结合两个任务"), _facts(), [])
        self.assertTrue(out["degraded"])

    def test_missing_attack_falls_back(self):
        attacks = _attacks()
        del attacks["reviewer"]
        raw = _llm_contribution(ctype="B", attacks=attacks)
        out = _finalize_contribution(raw, _idea("结合两个任务"), _facts(), [])
        self.assertTrue(out["degraded"])

    def test_empty_raw_falls_back(self):
        out = _finalize_contribution({}, _idea("结合两个任务"), _facts(), [])
        self.assertTrue(out["degraded"])


class ClassifyContributionTest(unittest.TestCase):
    def test_null_provider_degraded(self):
        out = classify_contribution(_idea("结合两个任务"), _facts(), [], NullProvider())
        self.assertTrue(out["degraded"])
        self.assertIn(out["type"], CONTRIBUTION_TYPES)

    def test_llm_path_not_degraded(self):
        llm = _FakeLLM(contribution_results=[_llm_contribution(ctype="C")])
        out = classify_contribution(_idea("迁移到新场景"), _facts(), [], llm)
        self.assertFalse(out["degraded"])
        self.assertEqual(out["type"], "C")

    def test_batch_routes_and_finalizes(self):
        llm = _FakeLLM(batch_contribution=[{"results": [
            {"idea_id": "i1", **_llm_contribution(ctype="B")},
            {"idea_id": "i2", **_llm_contribution(ctype="D")},
        ]}])
        ideas = [_idea("结合两个任务"), {"idea_id": "i2", "claim": "重新定义问题",
                                          "novelty_hypothesis": "h", "problem_ref": "p1",
                                          "literature_refs": []}]
        out = classify_contribution_batch(ideas, _facts(), [], llm)
        self.assertEqual(set(out), {"i1", "i2"})
        self.assertEqual(out["i1"]["type"], "B")
        self.assertEqual(out["i2"]["type"], "D")
        self.assertFalse(out["i1"]["degraded"])
        # 一次批量 LLM 调用（而非逐条）
        self.assertEqual(len(llm.calls), 1)


class RenderTest(unittest.TestCase):
    def _finalized(self, ctype="B"):
        # 用 _finalize_contribution 把原始 LLM 输出转成最终 contribution 子对象（含顶层 type）
        return _finalize_contribution(_llm_contribution(ctype=ctype),
                                      _idea("结合两个任务"), _facts(), [])

    def test_render_contains_type_matrix_attacks(self):
        ev = {"idea_ref": "i1", "contribution": self._finalized("B")}
        lines = render_contribution_lines(ev)
        text = "\n".join(lines)
        self.assertIn("创新类型：B", text)
        self.assertIn("贡献矩阵", text)
        self.assertIn("攻击测试", text)
        self.assertIn("消融", text)
        self.assertIn("简单拼接", text)
        self.assertIn("reviewer", text)
        self.assertIn("方法创新", text)
        self.assertIn("框架创新", text)

    def test_render_empty_for_legacy_eval(self):
        self.assertEqual(render_contribution_lines({"idea_ref": "i1"}), [])

    def test_render_marks_degraded(self):
        c = _deterministic_contribution(_idea("结合两个任务"), _facts(), [])
        lines = render_contribution_lines({"idea_ref": "i1", "contribution": c})
        self.assertTrue(any("确定性降级" in l for l in lines))


class SampleAcceptanceTest(unittest.TestCase):
    """M21 验收：sample 项目每个 idea 都产出「类型 + 贡献矩阵 + 攻击测试」子对象。"""

    def test_evaluation_has_contribution(self):
        # 直接对 sample 项目走 evaluate.run，断言每条 evaluation 带 contribution（前置于 novelty）
        from papermine.agents.evaluate import run as evaluate_run
        from papermine.agents.understand import run as understand_run
        from papermine.dossier import Dossier

        d = Dossier(project_id="proj-m21", llm_backend="null")
        understand_run(str(SAMPLE_PROJECT), d, NullProvider())
        d.ideas = [
            {"idea_id": "i1",
             "claim": "面向工业制造的传感器时序异常检测方法：基于孤立森林的轻量方案",
             "novelty_hypothesis": "现有方法对缺失值鲁棒性差，提出缺失值自适应的异常检测",
             "problem_ref": "p1", "literature_refs": [], "status": "pending_eval"},
        ]
        d.literature = []
        evaluate_run(d, NullProvider())
        self.assertEqual(len(d.evaluations), 1)
        ev = d.evaluations[0]
        c = ev.get("contribution")
        self.assertIsInstance(c, dict)
        self.assertIn(c["type"], CONTRIBUTION_TYPES)
        self.assertEqual(set(c["matrix"]), set(MATRIX_DIMENSIONS))
        self.assertEqual(set(c["attacks"]), set(ATTACK_KEYS))
        # novelty 仍在（作为参考维度），但不是唯一定论
        self.assertIn("novelty_score", ev)
        self.assertTrue(c["degraded"])   # NullProvider 离线 → 确定性降级


if __name__ == "__main__":
    unittest.main()
