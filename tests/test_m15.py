"""M15 单测：减少 LLM 调用（模型分级 + 批量推理）。

覆盖 docs/build-plan.md §4 M15 两个方向：

- **方向③ 模型分级**：翻译 / gap_note / 相关性过滤 等「便宜快模型」环节走 ``complete_fast``，
  核心推理（ideate / evaluate）仍走 ``complete``；
- **方向④ 批量推理**：多个 idea 的评估 + 证据审查各合并成一次 LLM 调用（而非逐个）。

用标准库 unittest 编写（与 tests/test_m14.py 一致），`python -m unittest discover -s tests -v`
可运行（也兼容 pytest 收集）。
"""
from __future__ import annotations

import unittest

from papermine import retrieval
from papermine.agents import evaluate
from papermine.dossier import Dossier

_DIM_KEYS = tuple(k for k, _l, _w in evaluate.NOVELTY_DIMENSIONS)
_CHECK_KEYS = ("similar_work", "theory_basis", "experiment_support", "claim_strength")


def _dims(score=4):
    return {k: {"score": score, "reason": "规则/LLM 给出的差异化理由"} for k in _DIM_KEYS}


def _checks():
    return {
        "similar_work": {"status": "ok", "note": "有可比文献且明确区分"},
        "theory_basis": {"status": "ok", "note": "有机制性依据"},
        "experiment_support": {"status": "ok", "note": "有数据有指标可验证"},
        "claim_strength": {"status": "ok", "note": "范围克制"},
    }


class _TieredFakeLLM:
    """带分级的 stub：``complete``（核心模型）与 ``complete_fast``（快模型）分别计数。"""

    def __init__(self, fast_result=None):
        self.fast_result = fast_result if fast_result is not None else {}
        self.fast_calls = []
        self.core_calls = []
        self.model = "deepseek-chat"
        self.fast_model = "fast-model"

    def complete(self, system, user, schema, temperature=0.2):
        self.core_calls.append((system, user, schema, temperature))
        return {}

    def complete_fast(self, system, user, schema, temperature=0.2):
        self.fast_calls.append((system, user, schema, temperature))
        return self.fast_result


class _CountingBatchLLM:
    """按 schema 路由的批量 stub，计数核心/快模型调用次数。"""

    def __init__(self, n_ideas):
        self.n = n_ideas
        self.core_calls = 0
        self.fast_calls = 0
        self.model = "deepseek-chat"
        self.fast_model = "fast-model"

    def complete(self, system, user, schema, temperature=0.2):
        self.core_calls += 1
        props = (schema or {}).get("properties") or {}
        if "evaluations" in props:
            return {"evaluations": [
                {"idea_id": "i{}".format(i + 1),
                 "novelty_dimensions": _dims(4), "workload_hours": 40,
                 "verdict_suggestion": "proceed", "rework_reason": None}
                for i in range(self.n)
            ]}
        return {}

    def complete_fast(self, system, user, schema, temperature=0.2):
        self.fast_calls += 1
        props = (schema or {}).get("properties") or {}
        if "results" in props:
            return {"results": [
                {"idea_id": "i{}".format(i + 1), "evidence": "medium",
                 "reason": "证据中等", "checks": _checks()}
                for i in range(self.n)
            ]}
        return {}


def _dossier(n_ideas=3):
    d = Dossier()
    d.assets["facts"] = {
        "tasks": ["分类"], "methods": ["XGBoost"], "data": ["时序"],
        "scenarios": ["工业"], "metrics": ["F1"], "libraries": [], "modules": [],
    }
    d.literature = [{
        "query": "q",
        "papers": [{"title": "Paper A", "venue": "KDD", "source": "semantic_scholar"}],
        "gap_note": "现有方法存在 gap，尚未有系统研究",
        "sources": ["semantic_scholar"],
    }]
    d.ideas = [
        {
            "idea_id": "i{}".format(i + 1),
            "claim": "提出一种改进方法 {}".format(i + 1),
            "novelty_hypothesis": "假设改进有效",
            "problem_ref": "p{}".format(i + 1),
            "literature_refs": ["Paper A"],
            "status": "pending_eval",
        }
        for i in range(n_ideas)
    ]
    return d


# ---------------------------------------------------------------------------
# 方向③：模型分级
# ---------------------------------------------------------------------------

class ModelTieringTest(unittest.TestCase):
    """翻译 / gap_note / 相关性过滤等「便宜快模型」环节只走 complete_fast，不碰核心 complete。"""

    def test_translate_uses_fast_model(self):
        llm = _TieredFakeLLM(fast_result={
            "english_query": "remaining useful life prediction",
            "keywords": ["remaining useful life"],
        })
        en, kws = retrieval._translate_query(llm, "设备剩余寿命预测")
        self.assertEqual(en, "remaining useful life prediction")
        self.assertEqual(len(llm.fast_calls), 1)
        self.assertEqual(len(llm.core_calls), 0)

    def test_gap_note_uses_fast_model(self):
        llm = _TieredFakeLLM(fast_result={"gap_note": "存在缺口"})
        note = retrieval._llm_gap_note(
            llm, "q", [{"title": "P", "abstract": "a", "venue": "v"}])
        self.assertEqual(note, "存在缺口")
        self.assertEqual(len(llm.fast_calls), 1)
        self.assertEqual(len(llm.core_calls), 0)

    def test_relevance_uses_fast_model(self):
        llm = _TieredFakeLLM(fast_result={"relevant_titles": ["P"]})
        kept = retrieval._llm_relevance(
            llm, "q", [{"title": "P", "abstract": "a"}])
        self.assertEqual(kept, ["P"])
        self.assertEqual(len(llm.fast_calls), 1)
        self.assertEqual(len(llm.core_calls), 0)


# ---------------------------------------------------------------------------
# 方向④：批量推理
# ---------------------------------------------------------------------------

class BatchInferenceTest(unittest.TestCase):
    """多个 idea 的评估 + 证据审查合并为一次调用：3 个 idea 只需 2 次（而非 6 次）。"""

    def test_evaluate_batches_into_two_calls(self):
        d = _dossier(n_ideas=3)
        llm = _CountingBatchLLM(n_ideas=3)
        evaluate.run(d, llm)

        self.assertEqual(len(d.evaluations), 3)
        for ev in d.evaluations:
            self.assertIn(ev["verdict"], ("proceed", "rework", "drop"))
            self.assertEqual(ev["evidence_validation"]["evidence"], "medium")

        # 1 次批量评估（核心模型） + 1 次批量证据（快模型） = 2 次；
        # 修复前逐个调用 = 3 idea × 2 = 6 次。
        self.assertEqual(llm.core_calls, 1)
        self.assertEqual(llm.fast_calls, 1)


if __name__ == "__main__":
    unittest.main()
