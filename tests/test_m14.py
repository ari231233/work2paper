"""M14 单测：减少无效 Agent 调用（治回炉循环）。

覆盖 docs/build-plan.md §4 M14 两个方向：

- **方向① Workflow 固化**：检索→文献理解→矛盾挖掘 只跑一次，回炉复用结果、只重跑④生成；
- **方向② 动态 Agent 路由**：按评估证据强度决定是否回炉、回炉到哪一步；
  数据缺口不再自动回炉到①（消除 4x 无效循环）。

用标准库 unittest 编写（与 tests/test_orchestrator.py 一致），
`python -m unittest discover -s tests -v` 即可运行（也兼容 pytest 收集）。
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from papermine import literature, orchestrator, storage, trace
from papermine.dossier import Dossier
from papermine.llm import NullProvider

SAMPLE_PROJECT = Path(__file__).resolve().parent.parent / "examples" / "sample-project"


# ---------------------------------------------------------------------------
# 方向②：路由纯函数
# ---------------------------------------------------------------------------

class RoutingTest(unittest.TestCase):
    """按证据强度 / 数据可得性动态决定回炉目标（M14 方向②）。"""

    def _dossier(self, evaluations=None, literature=None):
        d = Dossier()
        d.evaluations = list(evaluations or [])
        d.literature = list(literature or [])
        return d

    def _lit_with_papers(self):
        return [{"papers": [{"title": "Deep Fault Classification"}]}]

    def test_weak_evidence_with_literature_auto_reworks_to_ideate(self):
        d = self._dossier(
            [{"verdict": "rework", "evidence_validation": {"evidence": "weak"}}],
            self._lit_with_papers(),
        )
        self.assertEqual(orchestrator._evaluation_rework_target(d), "IDEATE")

    def test_weak_evidence_without_literature_does_not_force_rework(self):
        # 离线 / 无文献：重跑生成也无法强化证据，不强制回炉
        d = self._dossier(
            [{"verdict": "rework", "evidence_validation": {"evidence": "weak"}}],
            [],
        )
        self.assertIsNone(orchestrator._evaluation_rework_target(d))

    def test_strong_evidence_not_forced_to_rework(self):
        d = self._dossier(
            [{"verdict": "proceed", "evidence_validation": {"evidence": "strong"}}],
            self._lit_with_papers(),
        )
        self.assertIsNone(orchestrator._evaluation_rework_target(d))

    def test_drop_does_not_rework(self):
        d = self._dossier(
            [{"verdict": "drop", "evidence_validation": {"evidence": "medium"}}],
            self._lit_with_papers(),
        )
        self.assertIsNone(orchestrator._evaluation_rework_target(d))

    def test_cp4_routes_data_gap_to_understand(self):
        d = self._dossier([{"verdict": "rework", "data_feasibility": "low"}])
        self.assertEqual(orchestrator._route_rework("cp4", d), "UNDERSTAND")

    def test_cp4_routes_weak_evidence_to_ideate(self):
        d = self._dossier([{
            "verdict": "rework", "data_feasibility": "high",
            "evidence_validation": {"evidence": "weak"},
        }])
        self.assertEqual(orchestrator._route_rework("cp4", d), "IDEATE")

    def test_cp3_routes_to_ideate(self):
        self.assertEqual(orchestrator._route_rework("cp3", Dossier()), "IDEATE")

    def test_problems_key_stable_and_changes(self):
        a = [{"title": "a", "formulation": "b"}]
        b = [{"title": "a", "formulation": "b"}]
        c = [{"title": "c", "formulation": "d"}]
        self.assertEqual(orchestrator._problems_key(a), orchestrator._problems_key(b))
        self.assertNotEqual(orchestrator._problems_key(a), orchestrator._problems_key(c))
        self.assertEqual(orchestrator._problems_key([]), orchestrator._problems_key([]))

    def test_literature_foundation_fixed_flag(self):
        state = {"literature_fixed": True,
                 "literature_key": orchestrator._problems_key([{"title": "a", "formulation": "b"}])}
        d = Dossier()
        d.problems = [{"title": "a", "formulation": "b"}]
        self.assertTrue(orchestrator._literature_foundation_fixed(state, d))
        # problems 变化 → 缓存失效
        d.problems = [{"title": "x", "formulation": "y"}]
        self.assertFalse(orchestrator._literature_foundation_fixed(state, d))
        # 未固化 → False
        self.assertFalse(orchestrator._literature_foundation_fixed(
            {"literature_fixed": False, "literature_key": ""}, d))


# ---------------------------------------------------------------------------
# 方向①：工作流固化集成测试
# ---------------------------------------------------------------------------

class WorkflowFixationTest(unittest.TestCase):
    """回炉到④只重跑创新点生成，复用已固化的检索/理解/挖掘结果。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = os.environ.get(storage.ENV_HOME)
        os.environ[storage.ENV_HOME] = self._tmp.name
        storage.ensure_layout()

        self._llm_patch = mock.patch.object(
            orchestrator, "get_provider", return_value=NullProvider())
        self._search_patch = mock.patch.object(
            orchestrator.ideate, "search_literature",
            return_value=self._literature())
        self._analyze_patch = mock.patch.object(
            orchestrator.ideate, "analyze_literature",
            wraps=literature.analyze_literature)

        self._llm_patch.start()
        self._search_mock = self._search_patch.start()
        self._analyze_mock = self._analyze_patch.start()

    def tearDown(self):
        self._analyze_patch.stop()
        self._search_patch.stop()
        self._llm_patch.stop()
        if self._orig is None:
            os.environ.pop(storage.ENV_HOME, None)
        else:
            os.environ[storage.ENV_HOME] = self._orig
        self._tmp.cleanup()

    def _literature(self):
        return [{
            "query": "工业设备故障分类",
            "papers": [
                {
                    "title": "Deep Fault Classification", "venue": "arXiv", "year": 2022,
                    "authors": [], "url": "", "source": "arxiv",
                    "external_id": "2201.00001",
                    "abstract": "工业设备故障分类的深度学习方法。",
                }
            ],
            "gap_note": "现有工作未覆盖工业设备小样本故障分类。",
            "sources": ["arxiv"],
        }]

    def _stage_counts(self, run_id):
        return {st["name"]: st["count"]
                for st in trace.summarize(storage.run_dir(run_id))["stages"]}

    def test_cp4_rework_reuses_literature_foundation(self):
        calls = []

        def fake_prompt(checkpoint, label):
            calls.append(checkpoint)
            # 第一次 cp4 决定 rework → 回④，其余 accept
            if checkpoint == "cp4" and calls.count("cp4") == 1:
                return ("rework", "证据不足")
            return ("accept", "")

        with mock.patch.object(orchestrator, "_prompt", side_effect=fake_prompt):
            run_id = orchestrator.run_pipeline(str(SAMPLE_PROJECT), auto=False)

        # 方向①：检索 + 文献理解/矛盾挖掘 只跑一次
        self.assertEqual(self._search_mock.call_count, 1)
        self.assertEqual(self._analyze_mock.call_count, 1)

        # 方向②：回炉只重跑④生成——IDEATE（检索）1 次、IDEATE_GENERATE（生成）1 次、EVALUATE 2 次
        counts = self._stage_counts(run_id)
        self.assertEqual(counts.get("IDEATE"), 1)
        self.assertEqual(counts.get("IDEATE_GENERATE"), 1)
        self.assertEqual(counts.get("EVALUATE"), 2)

        # 状态收敛到 DONE
        self.assertEqual(orchestrator.status(run_id)["state"], "DONE")


# ---------------------------------------------------------------------------
# 方向②：数据缺口不再自动回炉（消除 4x 无效循环）
# ---------------------------------------------------------------------------

class DataGapNoAutoRollbackTest(unittest.TestCase):
    """PLAN 暴露数据缺口时，不再自动回炉到①（确定性扫描无法自动采集数据），只记降级信号。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = os.environ.get(storage.ENV_HOME)
        os.environ[storage.ENV_HOME] = self._tmp.name
        storage.ensure_layout()

        def fake_understand(project_dir, dossier, llm):
            # 造一个「无数据/无指标」的项目，触发 plan 的数据缺口
            dossier.assets["facts"] = {
                "tasks": ["分类"], "methods": ["XGBoost"], "data": [],
                "scenarios": ["工业设备"], "metrics": [],
                "libraries": [], "modules": [],
            }
            dossier.assets["narrative"] = "工业设备故障分类测试项目"
            dossier.assets["evidence"] = [{"source": "README.md", "snippet": "..."}]

        self._llm_patch = mock.patch.object(
            orchestrator, "get_provider", return_value=NullProvider())
        self._understand_patch = mock.patch.object(
            orchestrator.understand, "run", side_effect=fake_understand)
        self._retrieval_patch = mock.patch.object(
            orchestrator.ideate, "search_literature", return_value=[])
        self._llm_patch.start()
        self._understand_patch.start()
        self._retrieval_patch.start()

    def tearDown(self):
        self._retrieval_patch.stop()
        self._understand_patch.stop()
        self._llm_patch.stop()
        if self._orig is None:
            os.environ.pop(storage.ENV_HOME, None)
        else:
            os.environ[storage.ENV_HOME] = self._orig
        self._tmp.cleanup()

    def test_data_gap_does_not_loop(self):
        run_id = orchestrator.run_pipeline(str(SAMPLE_PROJECT), auto=True)

        # 每个状态只跑一次（修复前：PLAN→UNDERSTAND 循环导致各跑 4 次）
        counts = {st["name"]: st["count"]
                  for st in trace.summarize(storage.run_dir(run_id))["stages"]}
        for name in ("UNDERSTAND", "ABSTRACT", "IDEATE", "EVALUATE", "PLAN"):
            self.assertEqual(counts.get(name), 1, "{} 应只跑 1 次，实际 {}".format(name, counts.get(name)))
        self.assertNotIn("IDEATE_GENERATE", counts)

        # 状态收敛到 DONE，缺口留在 missing_items 交给人工
        st = orchestrator.status(run_id)
        self.assertEqual(st["state"], "DONE")
        self.assertGreaterEqual(st["degradations"], 1)
        dossier = Dossier.load(storage.run_dir(run_id))
        missing = " ".join(str(m) for m in dossier.roadmap.get("missing_items") or [])
        self.assertTrue(any(k in missing for k in ("数据", "指标", "采集")))


# ---------------------------------------------------------------------------
# 方向②：证据弱 → 自动窄回炉一次（有界）
# ---------------------------------------------------------------------------

class _WeakEvidenceLLM:
    """按 schema 区分调用：评估给高 novelty + 证据弱 → verdict=rework；其余返回空 dict 走确定性降级。"""

    def complete(self, system, user, schema, temperature=0.2):
        props = (schema or {}).get("properties") or {}
        if "checks" in props and "evidence" in props:
            return {
                "evidence": "weak",
                "reason": "claim 过强，需弱化为可检验主张",
                "checks": {
                    "similar_work": {"status": "concern", "note": "有类似工作但差异不明确"},
                    "theory_basis": {"status": "concern", "note": "理论依据不足"},
                    "experiment_support": {"status": "ok", "note": "有数据有指标可验证"},
                    "claim_strength": {"status": "missing", "note": "claim 过强"},
                },
            }
        if "novelty_dimensions" in props:
            return {
                "novelty_dimensions": {
                    "problem_novelty": {"score": 5, "reason": "新问题"},
                    "method_novelty": {"score": 5, "reason": "新机制"},
                    "technical_depth": {"score": 5, "reason": "突破"},
                    "gap": {"score": 5, "reason": "与 SOTA 差异明确"},
                    "generalization": {"score": 5, "reason": "可推广"},
                },
                "workload_hours": 40,
                "verdict_suggestion": "proceed",
                "rework_reason": None,
            }
        return {}


class AutoEvidenceReworkTest(unittest.TestCase):
    """证据弱 + 有文献 → auto 模式自动窄回炉到④一次（有界，不陷入循环）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = os.environ.get(storage.ENV_HOME)
        os.environ[storage.ENV_HOME] = self._tmp.name
        storage.ensure_layout()

        self._llm_patch = mock.patch.object(
            orchestrator, "get_provider", return_value=_WeakEvidenceLLM())
        self._retrieval_patch = mock.patch.object(
            orchestrator.ideate, "search_literature",
            return_value=[{
                "query": "工业设备故障分类",
                "papers": [{"title": "Deep Fault Classification", "venue": "arXiv",
                            "year": 2022, "authors": [], "url": "", "source": "arxiv",
                            "external_id": "2201.00001", "abstract": "故障分类"}],
                "gap_note": "现有工作未覆盖小样本故障分类。",
                "sources": ["arxiv"],
            }])
        self._llm_patch.start()
        self._retrieval_patch.start()

    def tearDown(self):
        self._retrieval_patch.stop()
        self._llm_patch.stop()
        if self._orig is None:
            os.environ.pop(storage.ENV_HOME, None)
        else:
            os.environ[storage.ENV_HOME] = self._orig
        self._tmp.cleanup()

    def test_weak_evidence_auto_reworks_once(self):
        run_id = orchestrator.run_pipeline(str(SAMPLE_PROJECT), auto=True)

        counts = {st["name"]: st["count"]
                  for st in trace.summarize(storage.run_dir(run_id))["stages"]}
        self.assertEqual(counts.get("IDEATE"), 1)
        self.assertEqual(counts.get("IDEATE_GENERATE"), 1)
        # 初始评估 + 一次窄回炉后的再评估 = 2 次
        self.assertEqual(counts.get("EVALUATE"), 2)

        # 自动回炉预算已用尽，最终收敛
        st = orchestrator.status(run_id)
        self.assertEqual(st["state"], "DONE")
        self.assertEqual(st["rollback_rounds"].get("auto_evidence"), 1)


if __name__ == "__main__":
    unittest.main()
