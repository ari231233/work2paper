"""M6/M11/M12/M18/M20 ⑤ 可行性评估 Agent 单测：证据驱动评估、多维加权 novelty、M12 证据强度、
M18 gap 证据级别打折、M20 评分校准（规则 + LLM 解释）、verdict、降级路径、sample 验收。

用标准库 unittest 编写（与 tests/test_dossier.py 一致），`python -m unittest discover -s tests -v`
即可运行，无需新增第三方依赖（也兼容 pytest 收集）。

M11 增量：novelty 从单一 0~5 升级为 5 维加权（0~100 总分 + 分维度明细 + 分数段映射 verdict）。
M12 增量：每条 evaluation 附带 ``evidence_validation``（证据强度 weak/medium/strong + 理由 + 4 维检查）。
M18 增量：gap 假设证据级别整体 weak → Gap 维度 novelty 打折。
M20 增量：novelty 各维度从「LLM 自由打分」改为「规则 + LLM 解释」——LLM 只答题（rubric），分数由规则算出。
"""
from __future__ import annotations

import os
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from papermine.agents.evaluate import (
    EVALUATE_SCHEMA,
    NOVELTY_DIMENSIONS,
    RUBRIC,
    _apply_gap_evidence_discount,
    _data_feasibility,
    _decide_verdict,
    _deterministic_dimensions,
    _score_band,
    _tier_of,
    _weighted_total,
    run,
    score_rubric,
)
from papermine.agents.evidence import CHECK_DIMENSIONS, EVIDENCE_LEVELS
from papermine.agents.understand import run as understand_run
from papermine.dossier import Dossier
from papermine.llm import LLMError, NullProvider

SAMPLE_PROJECT = Path(__file__).resolve().parent.parent / "examples" / "sample-project"

_DIM_KEYS = tuple(k for k, _l, _w in NOVELTY_DIMENSIONS)
_Q_IDS = {key: tuple(q[0] for q in qs) for key, _l, _w, _base, qs in RUBRIC}
_CHECK_KEYS = tuple(k for k, _l in CHECK_DIMENSIONS)
_BANDS = ("Reject", "Weak Reject", "Revise", "Accept", "Priority")


class _FakeLLM:
    """按 schema 路由返回结果：evaluate / evidence 各分「批量」与「单条」两种路径。

    - 批量评估 schema（props 含 ``evaluations``）走 ``batch_results``，未提供时返回空批量
      （触发逐条回退，不消耗单条队列）；
    - 批量证据 schema（props 含 ``results``）走 ``batch_evidence``，未提供时返回空批量；
    - 单条证据 schema（props 含 ``checks`` + ``evidence``）走 ``evidence_results``；
    - 单条评估 schema 走 ``results``。
    各自耗尽后返回空 dict（触发确定性兜底）。
    """

    def __init__(self, results=None, evidence_results=None, batch_results=None,
                 batch_evidence=None, exc=None):
        self.results = list(results or [])
        self.evidence_results = list(evidence_results or [])
        self.batch_results = list(batch_results or [])
        self.batch_evidence = list(batch_evidence or [])
        self.exc = exc
        self.calls = []

    def complete(self, system, user, schema, temperature=0.2):
        self.calls.append((system, user, schema, temperature))
        if self.exc is not None:
            raise self.exc
        props = schema.get("properties") or {}
        if "evaluations" in props:
            if self.batch_results:
                return self.batch_results.pop(0)
            return {"evaluations": []}
        if "results" in props:
            if self.batch_evidence:
                return self.batch_evidence.pop(0)
            return {"results": []}
        if "checks" in props and "evidence" in props:
            if self.evidence_results:
                return self.evidence_results.pop(0)
            return {}
        if self.results:
            return self.results.pop(0)
        return {}


def _dims(problem=4, method=4, tech=3, gap=4, gen=3):
    """手工构造的维度分 dict（仅用于 _weighted_total 等纯函数的单测）。"""
    return {
        "problem_novelty": {"score": problem, "reason": "问题未被充分解决（gap 支持）"},
        "method_novelty": {"score": method, "reason": "方法有自适应新机制"},
        "technical_depth": {"score": tech, "reason": "关键技术瓶颈未完全解决"},
        "gap": {"score": gap, "reason": "与 SOTA 差异明确"},
        "generalization": {"score": gen, "reason": "可迁移到其他任务"},
    }


def _all_no_answers():
    """构造一组「全 no」的 rubric 答案（每问都带证据）。"""
    ans = {}
    for key, _l, _w, _base, qs in RUBRIC:
        ans[key] = {qid: {"answer": "no", "evidence": "证据：无"} for (qid, _t, _k, _v) in qs}
    return ans


def _answers(**overrides):
    """以「全 no」为底，按维度/问题覆盖为 yes 的答案。"""
    a = _all_no_answers()
    for dim, qmap in overrides.items():
        for qid, val in qmap.items():
            a[dim][qid]["answer"] = val
    return a


def _answers_high():
    """高分答案：所有加分项 yes、封顶项 no。"""
    return _answers(
        problem_novelty={"Q2": "yes", "Q3": "yes", "Q4": "yes"},
        method_novelty={"Q2": "yes", "Q3": "yes"},
        technical_depth={"Q1": "yes", "Q2": "yes", "Q3": "yes", "Q4": "yes"},
        gap={"Q1": "yes", "Q2": "yes", "Q4": "yes"},
        generalization={"Q1": "yes", "Q2": "yes", "Q4": "yes"},
    )


def _answers_low():
    """低分答案：全 no。"""
    return _all_no_answers()


def _llm_eval(answers=None, workload=80, suggestion="proceed", reason=None):
    """构造一条 LLM 评估输出：rubric 答案 + 工作量 + verdict 建议（M20：无 novelty 分数）。"""
    return {
        "rubric": answers if answers is not None else _answers(),
        "workload_hours": workload,
        "verdict_suggestion": suggestion,
        "rework_reason": reason,
    }


def _evidence_checks(similar="ok", theory="ok", experiment="ok", claim="ok"):
    return {
        "similar_work": {"status": similar, "note": "文献对拍：有可比文献且明确区分"},
        "theory_basis": {"status": theory, "note": "理论支撑：有机制性依据"},
        "experiment_support": {"status": experiment, "note": "实验设计支持：可验证"},
        "claim_strength": {"status": claim, "note": "claim 强度校准：范围克制"},
    }


def _llm_evidence(evidence="strong", reason="证据充分", checks=None):
    return {
        "evidence": evidence,
        "reason": reason,
        "checks": checks if checks is not None else _evidence_checks(),
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


def _assert_calibration(self, cal):
    self.assertEqual(set(cal), set(_DIM_KEYS))
    for key in _DIM_KEYS:
        item = cal[key]
        self.assertIn("score", item)
        self.assertIn("derivation", item)
        self.assertIn("questions", item)
        qids = set()
        for q in item["questions"]:
            qids.add(q["id"])
            self.assertIn(q["answer"], ("yes", "no"))
            self.assertTrue(q["text"])
            self.assertTrue(q["rule"])
            self.assertIn("evidence", q)
        self.assertEqual(qids, set(_Q_IDS[key]))


def _assert_evidence_validation(self, evv):
    self.assertIsInstance(evv, dict)
    self.assertIn(evv["evidence"], EVIDENCE_LEVELS)
    self.assertTrue(str(evv["reason"]).strip())
    self.assertEqual(set(evv["checks"]), set(_CHECK_KEYS))
    for key in _CHECK_KEYS:
        item = evv["checks"][key]
        self.assertIn(item["status"], ("ok", "concern", "missing"))
        self.assertTrue(str(item["note"]).strip())
    self.assertIn(evv["degraded"], (True, False))


class RunTest(unittest.TestCase):
    def setUp(self) -> None:
        # M16 并行默认开启；_FakeLLM 是按序队列的 stub（非线程安全），此处强制顺序执行以保持确定性。
        self._parallel_patch = mock.patch.dict(os.environ, {"PAPERMINE_PARALLEL": "0"})
        self._parallel_patch.start()

    def tearDown(self) -> None:
        self._parallel_patch.stop()

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
            _assert_calibration(self, ev["calibration"])
            self.assertIn(ev["data_feasibility"], ("high", "medium", "low"))
            self.assertIsInstance(ev["workload_hours"], int)
            self.assertTrue(ev["venue_guess"])
            self.assertTrue(ev["evidence"])
            _assert_evidence_validation(self, ev["evidence_validation"])
            for e in ev["evidence"]:
                self.assertIn("source", e)
                self.assertIn("note", e)
        self.assertEqual(d.meta["prompt_versions"]["evaluate"], "v3")

    def test_run_is_evidence_driven(self) -> None:
        """评估必须挂确定性证据源（facts / literature + 分维度明细），不是凭空断言。"""
        d = _dossier()
        run(d, NullProvider())
        sources = {e["source"] for ev in d.evaluations for e in ev["evidence"]}
        self.assertIn("literature.gap_note", sources)
        self.assertIn("assets.facts.data", sources)      # sample 有数据标签
        self.assertIn("assets.facts.metrics", sources)   # sample 有指标标签
        self.assertIn("literature.venues", sources)      # 检索论文带 venue
        # M11/M20：分维度明细挂进证据链
        self.assertTrue(any(s.startswith("novelty_dimensions.") for s in sources))
        # M12：证据强度 + 4 维检查挂进证据链
        self.assertIn("evidence_validation", sources)
        self.assertTrue(any(s.startswith("evidence_validation.") for s in sources))

    def test_run_with_llm_uses_rubric_answers_and_rule_scores(self) -> None:
        """M20：LLM 只答题（rubric），分数由规则引擎算出（novelty 不来自 LLM 直接打分）。"""
        d = _dossier()
        answers_high = _answers_high()
        answers_low = _answers_low()
        dims_high, _ = score_rubric(answers_high)
        dims_low, _ = score_rubric(answers_low)
        llm = _FakeLLM(results=[
            _llm_eval(answers=answers_high, workload=120, suggestion="proceed"),
            _llm_eval(answers=answers_low, workload=50, suggestion="drop"),
        ])
        run(d, llm)

        self.assertEqual(d.evaluations[0]["novelty_score"], _weighted_total(dims_high))
        self.assertEqual(d.evaluations[1]["novelty_score"], _weighted_total(dims_low))
        self.assertEqual(d.evaluations[0]["verdict"], "proceed")
        self.assertIn(d.evaluations[0]["novelty_band"], ("Accept", "Priority"))
        self.assertEqual(d.evaluations[1]["novelty_band"], "Reject")
        self.assertEqual(d.evaluations[1]["verdict"], "drop")
        # method_novelty 由模板三问规则算出，与规则引擎重算一致
        self.assertEqual(
            d.evaluations[0]["novelty_dimensions"]["method_novelty"]["score"],
            dims_high["method_novelty"]["score"])

    def test_run_with_llm_evidence_routed_and_recorded(self) -> None:
        """M12：证据审查独立调用，LLM 结果写入 evidence_validation 且 degraded=False。"""
        d = _dossier()
        llm = _FakeLLM(
            results=[_llm_eval(answers=_answers_high(), workload=80, suggestion="proceed"),
                     _llm_eval(answers=_answers(), workload=80, suggestion="proceed")],
            evidence_results=[
                _llm_evidence(evidence="strong", reason="证据充分：有文献+理论+可验证"),
                _llm_evidence(evidence="medium", reason="证据中等"),
            ],
        )
        run(d, llm)
        self.assertEqual(d.evaluations[0]["evidence_validation"]["evidence"], "strong")
        self.assertEqual(d.evaluations[0]["evidence_validation"]["reason"],
                         "证据充分：有文献+理论+可验证")
        self.assertFalse(d.evaluations[0]["evidence_validation"]["degraded"])
        self.assertEqual(d.evaluations[1]["evidence_validation"]["evidence"], "medium")
        _assert_evidence_validation(self, d.evaluations[0]["evidence_validation"])

    def test_run_batches_evaluations_and_evidence(self) -> None:
        """M15 方向④：多个 idea 的评估 + 证据审查各合并成一次 LLM 调用（2 次而非 4 次）。"""
        d = _dossier()
        answers_high = _answers_high()
        answers_low = _answers_low()
        dims_high, _ = score_rubric(answers_high)
        dims_low, _ = score_rubric(answers_low)
        batch_eval = {"evaluations": [
            {"idea_id": "i1", **_llm_eval(answers=answers_high, workload=120, suggestion="proceed")},
            {"idea_id": "i2", **_llm_eval(answers=answers_low, workload=50, suggestion="drop")},
        ]}
        batch_evidence = {"results": [
            {"idea_id": "i1", **_llm_evidence(evidence="strong", reason="证据充分")},
            {"idea_id": "i2", **_llm_evidence(evidence="medium", reason="证据中等")},
        ]}
        llm = _FakeLLM(batch_results=[batch_eval], batch_evidence=[batch_evidence])
        run(d, llm)

        self.assertEqual(d.evaluations[0]["novelty_score"], _weighted_total(dims_high))
        self.assertEqual(d.evaluations[1]["novelty_score"], _weighted_total(dims_low))
        self.assertEqual(d.evaluations[0]["verdict"], "proceed")
        self.assertEqual(d.evaluations[1]["verdict"], "drop")
        self.assertEqual(d.evaluations[0]["evidence_validation"]["evidence"], "strong")
        self.assertEqual(d.evaluations[1]["evidence_validation"]["evidence"], "medium")
        # 只调 2 次 LLM（1 批量评估 + 1 批量证据），而非 2 idea × 2 调用 = 4 次
        self.assertEqual(len(llm.calls), 2)

    def test_run_batch_partial_falls_back_per_idea(self) -> None:
        """批量结果缺失某 idea 时，该 idea 逐条回退（不丢评估、不崩溃）。"""
        d = _dossier()
        answers_high = _answers_high()
        batch_eval = {"evaluations": [
            {"idea_id": "i1", **_llm_eval(answers=answers_high, workload=120, suggestion="proceed")},
        ]}
        batch_evidence = {"results": [
            {"idea_id": "i1", **_llm_evidence(evidence="strong", reason="证据充分")},
        ]}
        # i2 批量缺失 → 单条队列兜底
        llm = _FakeLLM(
            batch_results=[batch_eval], batch_evidence=[batch_evidence],
            results=[_llm_eval(answers=_answers(), workload=60)],
            evidence_results=[_llm_evidence(evidence="medium", reason="证据中等")],
        )
        run(d, llm)
        self.assertEqual(len(d.evaluations), 2)
        self.assertEqual(d.evaluations[0]["evidence_validation"]["evidence"], "strong")
        self.assertEqual(d.evaluations[1]["evidence_validation"]["evidence"], "medium")

    def test_weak_evidence_forces_rework(self) -> None:
        """M12：evidence=weak 时即便 novelty 高也要回炉（rework），随 verdict 回炉到 ④。"""
        d = _dossier()
        llm = _FakeLLM(
            results=[_llm_eval(answers=_answers_high(), workload=60, suggestion="proceed")],
            evidence_results=[_llm_evidence(
                evidence="weak", reason="已有 memory work 很多，需明确区别")],
        )
        run(d, llm)
        ev = d.evaluations[0]
        self.assertEqual(ev["evidence_validation"]["evidence"], "weak")
        self.assertEqual(ev["verdict"], "rework")
        self.assertIn("证据不足", ev["rework_reason"])
        self.assertIn("evidence=weak", ev["rework_reason"])

    def test_weak_evidence_does_not_override_drop(self) -> None:
        """M12：evidence=weak 不覆盖 drop（新颖性不足判死优先）。"""
        d = _dossier()
        llm = _FakeLLM(
            results=[_llm_eval(answers=_answers_low(), workload=60, suggestion="proceed")],
            evidence_results=[_llm_evidence(evidence="weak", reason="无文献对拍")],
        )
        run(d, llm)
        self.assertEqual(d.evaluations[0]["evidence_validation"]["evidence"], "weak")
        self.assertEqual(d.evaluations[0]["verdict"], "drop")

    def test_run_llm_error_falls_back_to_deterministic(self) -> None:
        d = _dossier()
        run(d, _FakeLLM(exc=LLMError("网络失败")))
        self.assertEqual(len(d.evaluations), 2)
        self.assertTrue(all("novelty_score" in ev for ev in d.evaluations))
        self.assertTrue(all("novelty_dimensions" in ev for ev in d.evaluations))
        self.assertTrue(all("calibration" in ev for ev in d.evaluations))

    def test_run_empty_ideas_writes_empty(self) -> None:
        d = _dossier(ideas=[], literature=[])
        run(d, NullProvider())
        self.assertEqual(d.evaluations, [])
        self.assertEqual(d.meta["prompt_versions"]["evaluate"], "v3")

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


class GapEvidenceDiscountTest(unittest.TestCase):
    """M18：gap 假设证据级别 weak → Gap 维度 novelty 打折（moderate/strong/无 不打折）。"""

    def _dims(self, gap_score=4.0):
        return {
            "problem_novelty": {"score": 4, "reason": "r"},
            "method_novelty": {"score": 4, "reason": "r"},
            "technical_depth": {"score": 3, "reason": "r"},
            "gap": {"score": gap_score, "reason": "与 SOTA 差异明确"},
            "generalization": {"score": 3, "reason": "r"},
        }

    def test_weak_gap_evidence_discounts_gap_dimension(self) -> None:
        dims = _apply_gap_evidence_discount(self._dims(4.0), ["weak"])
        self.assertEqual(dims["gap"]["score"], 2.4)   # 4.0 × 0.6
        self.assertIn("证据级别=weak", dims["gap"]["reason"])
        # 其他维度不受影响
        self.assertEqual(dims["method_novelty"]["score"], 4)

    def test_moderate_strong_and_absent_no_discount(self) -> None:
        self.assertEqual(_apply_gap_evidence_discount(self._dims(4.0), ["moderate"])["gap"]["score"], 4.0)
        self.assertEqual(_apply_gap_evidence_discount(self._dims(4.0), ["strong"])["gap"]["score"], 4.0)
        self.assertEqual(_apply_gap_evidence_discount(self._dims(4.0), [])["gap"]["score"], 4.0)
        self.assertEqual(_apply_gap_evidence_discount(self._dims(4.0), None)["gap"]["score"], 4.0)

    def test_mixed_weak_and_strong_no_discount(self) -> None:
        # 非「全 weak」不打折（存在 strong 证据）
        self.assertEqual(_apply_gap_evidence_discount(self._dims(4.0), ["weak", "strong"])["gap"]["score"], 4.0)


class RuleEngineTest(unittest.TestCase):
    """M20：规则引擎是纯确定性函数——相同答案 → 相同分数（可复现）。"""

    def test_score_rubric_deterministic(self) -> None:
        a = _answers(method_novelty={"Q2": "yes", "Q3": "yes"})
        dims1, cal1 = score_rubric(a)
        dims2, cal2 = score_rubric(a)
        self.assertEqual(dims1, dims2)
        self.assertEqual(cal1, cal2)

    def test_same_answers_same_score(self) -> None:
        a1 = _answers(method_novelty={"Q3": "yes"})
        a2 = _answers(method_novelty={"Q3": "yes"})
        d1, _ = score_rubric(a1)
        d2, _ = score_rubric(a2)
        self.assertEqual(d1["method_novelty"]["score"], d2["method_novelty"]["score"])

    def test_method_novelty_template_cap(self) -> None:
        """M20 模板：Q1=yes（只是已有模块组合）→ 封顶 ≤ 3，即便 Q2/Q3 都 yes。"""
        a = _answers(method_novelty={"Q1": "yes", "Q2": "yes", "Q3": "yes"})
        dims, _ = score_rubric(a)
        self.assertEqual(dims["method_novelty"]["score"], 3.0)
        # 非组合 + 双 yes → 起点2 + 1 + 1 = 4
        a2 = _answers(method_novelty={"Q1": "no", "Q2": "yes", "Q3": "yes"})
        dims2, _ = score_rubric(a2)
        self.assertEqual(dims2["method_novelty"]["score"], 4.0)

    def test_add_rule_and_base(self) -> None:
        dims, _ = score_rubric(_all_no_answers())
        self.assertEqual(dims["method_novelty"]["score"], 2.0)   # 起点 2
        self.assertEqual(dims["technical_depth"]["score"], 1.0)  # 起点 1
        self.assertEqual(dims["generalization"]["score"], 1.0)   # 起点 1


class CalibrationTraceabilityTest(unittest.TestCase):
    """M20 验收：每个维度分数都能追溯到「回答了哪些问题、规则怎么算」。"""

    def test_calibration_score_matches_rule_engine(self) -> None:
        d = _dossier()
        run(d, NullProvider())
        for ev in d.evaluations:
            cal = ev["calibration"]
            # 从 calibration 逐题答案重建 answers，再走规则引擎，应得到相同分数（可复现）
            answers = {}
            for key in _DIM_KEYS:
                answers[key] = {
                    q["id"]: {"answer": q["answer"], "evidence": q["evidence"]}
                    for q in cal[key]["questions"]
                }
            dims2, _ = score_rubric(answers)
            for key in _DIM_KEYS:
                self.assertEqual(ev["novelty_dimensions"][key]["score"], dims2[key]["score"])
                self.assertEqual(ev["calibration"][key]["score"], dims2[key]["score"])

    def test_calibration_derivation_traceable(self) -> None:
        """calibration 的 derivation 文本应包含起点 + 加分项，规则可读。"""
        a = _answers(method_novelty={"Q2": "yes", "Q3": "yes"})
        _dims_out, cal = score_rubric(a)
        self.assertIn("起点", cal["method_novelty"]["derivation"])
        self.assertIn("Q2:+1", cal["method_novelty"]["derivation"])
        self.assertIn("Q3:+1", cal["method_novelty"]["derivation"])


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
        # 左闭右开：40→Weak Reject、60→Revise、70→Accept、80→Accept、>80→Priority
        self.assertEqual(_score_band(39.9), ("Reject", "drop"))
        self.assertEqual(_score_band(40), ("Weak Reject", "drop"))
        self.assertEqual(_score_band(40.1), ("Weak Reject", "drop"))
        self.assertEqual(_score_band(59.9), ("Weak Reject", "drop"))
        self.assertEqual(_score_band(60), ("Revise", "rework"))
        self.assertEqual(_score_band(60.1), ("Revise", "rework"))
        self.assertEqual(_score_band(69.9), ("Revise", "rework"))
        self.assertEqual(_score_band(70), ("Accept", "proceed"))
        self.assertEqual(_score_band(70.1), ("Accept", "proceed"))
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

    def test_weak_evidence_downgrades_proceed_to_rework(self) -> None:
        """M12：evidence=weak 把 proceed 下调为 rework。"""
        self.assertEqual(_decide_verdict(75, "high", 60, None, evidence="weak"), "rework")

    def test_weak_evidence_does_not_override_drop(self) -> None:
        """M12：evidence=weak 不覆盖 drop。"""
        self.assertEqual(_decide_verdict(30, "high", 60, None, evidence="weak"), "drop")

    def test_medium_evidence_no_downgrade(self) -> None:
        self.assertEqual(_decide_verdict(75, "high", 60, None, evidence="medium"), "proceed")


class VenueTierTest(unittest.TestCase):
    def test_tier_of_known_and_unknown(self) -> None:
        self.assertEqual(_tier_of("KDD"), "CCF-A")
        self.assertEqual(_tier_of("arXiv"), "预印本（arXiv）")
        self.assertIn("未分级", _tier_of("Some Unknown Venue"))
        self.assertEqual(_tier_of(""), "未知档位")


class SchemaTest(unittest.TestCase):
    def test_schema_requires_rubric_not_scores(self) -> None:
        self.assertEqual(EVALUATE_SCHEMA["type"], "object")
        required = EVALUATE_SCHEMA["required"]
        for key in ("rubric", "workload_hours", "verdict_suggestion", "rework_reason"):
            self.assertIn(key, required)
        # M20：novelty 分数由规则算出，LLM 输出契约里不应有 novelty_dimensions（score）
        self.assertNotIn("novelty_dimensions", required)
        rubric = EVALUATE_SCHEMA["properties"]["rubric"]
        self.assertEqual(set(rubric["required"]), set(_DIM_KEYS))
        self.assertEqual(set(rubric["properties"]), set(_DIM_KEYS))
        # 每个问题：answer ∈ {yes,no} + 必须有 evidence
        q_schema = rubric["properties"]["method_novelty"]["properties"]["Q1"]
        self.assertEqual(q_schema["properties"]["answer"]["enum"], ["yes", "no"])
        self.assertEqual(q_schema["required"], ["answer", "evidence"])
        enum = EVALUATE_SCHEMA["properties"]["verdict_suggestion"]["enum"]
        self.assertEqual(enum, ["proceed", "rework", "drop"])


class SampleAcceptanceTest(unittest.TestCase):
    """验收：对 sample 项目产出评估，5 个维度分不全相等（M11 验收 #2）+ M20 分数可追溯。"""

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
            # M12 验收：每个 idea 输出证据强度 + 理由
            _assert_evidence_validation(self, ev["evidence_validation"])
            self.assertIn(ev["evidence_validation"]["evidence"], EVIDENCE_LEVELS)
            # M20 验收：每个维度分数都能追溯到「问题 → 答案 → 规则」
            _assert_calibration(self, ev["calibration"])


class _ConcurrentEvalLLM:
    """线程安全的评估/证据 LLM stub：批量 schema 返回空（触发逐条回退），单条按 schema 分派并统计并发。"""

    def __init__(self, sleep=0.05):
        self._lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self._sleep = sleep

    def _touch(self):
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)

    def _release(self):
        with self._lock:
            self.active -= 1

    def _dispatch(self, schema):
        self._touch()
        try:
            time.sleep(self._sleep)
            props = (schema or {}).get("properties") or {}
            if "evaluations" in props:
                return {"evaluations": []}          # 批量评估失败 → 逐条回退
            if "results" in props:
                return {"results": []}              # 批量证据失败 → 逐条回退
            if "checks" in props and "evidence" in props:
                return _llm_evidence(evidence="strong", reason="证据充分")
            return _llm_eval(answers=_answers(), workload=60, suggestion="proceed")
        finally:
            self._release()

    def complete(self, system, user, schema, temperature=0.2):
        return self._dispatch(schema)

    def complete_fast(self, system, user, schema, temperature=0.2):
        return self._dispatch(schema)


class ParallelEvaluateTest(unittest.TestCase):
    """M16 方向⑥：多个 idea 的评估并行执行（批量失败回退逐条时并行提速，结果保序）。"""

    def test_per_idea_fallback_evaluates_in_parallel(self):
        ideas = [
            {"idea_id": "i{}".format(n), "claim": "改进方法 {}".format(n),
             "novelty_hypothesis": "假设有效", "problem_ref": "p1",
             "literature_refs": [], "status": "pending_eval"}
            for n in range(1, 4)
        ]
        d = _dossier(ideas=ideas)
        llm = _ConcurrentEvalLLM()
        run(d, llm)

        self.assertEqual(len(d.evaluations), 3)
        self.assertEqual([ev["idea_ref"] for ev in d.evaluations], ["i1", "i2", "i3"])
        for ev in d.evaluations:
            self.assertIn(ev["verdict"], ("proceed", "rework", "drop"))
        self.assertGreater(llm.max_active, 1)   # 证明并行（多线程同时调用）


if __name__ == "__main__":
    unittest.main()
