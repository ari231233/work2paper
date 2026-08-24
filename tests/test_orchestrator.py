"""M7 编排器单测：端到端状态机、检查点暂停/续跑、回退、快照、经验沉淀。

用标准库 unittest 编写（与 tests/test_dossier.py 一致），`python -m unittest discover -s tests -v`
即可运行，无需新增第三方依赖（也兼容 pytest 收集）。
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from papermine import experience, orchestrator, policy, storage
from papermine.dossier import Dossier
from papermine.llm import NullProvider

SAMPLE_PROJECT = Path(__file__).resolve().parent.parent / "examples" / "sample-project"


class OrchestratorTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = os.environ.get(storage.ENV_HOME)
        os.environ[storage.ENV_HOME] = self._tmp.name
        storage.ensure_layout()
        # 离线：不真调 LLM + 不真联网检索
        self._llm_patch = mock.patch.object(orchestrator, "get_provider", return_value=NullProvider())
        self._retrieval_patch = mock.patch.object(
            orchestrator.ideate, "search_literature", return_value=[])
        self._llm_patch.start()
        self._retrieval_patch.start()

    def tearDown(self) -> None:
        self._retrieval_patch.stop()
        self._llm_patch.stop()
        if self._orig is None:
            os.environ.pop(storage.ENV_HOME, None)
        else:
            os.environ[storage.ENV_HOME] = self._orig
        self._tmp.cleanup()

    def _run_dir(self, run_id):
        return storage.run_dir(run_id)

    def test_run_pipeline_auto_end_to_end(self):
        run_id = orchestrator.run_pipeline(str(SAMPLE_PROJECT), auto=True)
        self.assertTrue(run_id)

        run_dir = self._run_dir(run_id)
        self.assertTrue((run_dir / "dossier.json").exists())
        self.assertTrue((run_dir / "report.md").exists())
        self.assertTrue((run_dir / "report.json").exists())
        self.assertTrue((run_dir / "dossier.history").is_dir())

        dossier = Dossier.load(run_dir)
        self.assertGreaterEqual(len(dossier.problems), 2)
        self.assertGreaterEqual(len(dossier.ideas), 2)
        self.assertGreaterEqual(len(dossier.evaluations), 2)
        self.assertIsNotNone(dossier.roadmap["selected_idea"])
        # auto=True：5 个检查点全部 accept
        self.assertEqual(len(dossier.human_decisions), 5)
        self.assertTrue(all(d["decision"] == "accept" for d in dossier.human_decisions))

        # 结束写出一条经验
        entries = experience.read_semantic()
        self.assertGreaterEqual(len(entries), 1)
        for key in ("confidence", "support_count", "status"):
            self.assertIn(key, entries[0])

        # 状态到 DONE，且每状态迁移都写快照
        st = orchestrator.status(run_id)
        self.assertEqual(st["state"], "DONE")
        self.assertGreaterEqual(len(list((run_dir / "dossier.history").iterdir())), 6)

    def test_status_and_resume_completed_run(self):
        run_id = orchestrator.run_pipeline(str(SAMPLE_PROJECT), auto=True)
        st = orchestrator.status(run_id)
        self.assertEqual(st["run_id"], run_id)
        self.assertEqual(st["state"], "DONE")

        # 续跑已完成 run → 仍 DONE，run_id 不变
        resumed = orchestrator.resume(run_id, auto=True)
        self.assertEqual(resumed, run_id)
        self.assertEqual(orchestrator.status(run_id)["state"], "DONE")

    def test_checkpoint_pause_accepts_decision(self):
        decisions = []
        calls = []

        def fake_prompt(checkpoint, label):
            calls.append(checkpoint)
            decisions.append(("accept", "人工确认"))
            return decisions[-1]

        with mock.patch.object(orchestrator, "_prompt", side_effect=fake_prompt):
            run_id = orchestrator.run_pipeline(str(SAMPLE_PROJECT), auto=False)

        self.assertEqual(calls, ["cp1", "cp2", "cp3", "cp4", "cp5"])
        dossier = Dossier.load(self._run_dir(run_id))
        self.assertEqual(len(dossier.human_decisions), 5)
        self.assertTrue(all(d["decision"] == "accept" for d in dossier.human_decisions))
        self.assertTrue(all(d["note"] == "人工确认" for d in dossier.human_decisions))

    def test_rework_rolls_back_to_prior_state(self):
        calls = []

        def fake_prompt(checkpoint, label):
            calls.append(checkpoint)
            # 第一个检查点 rework 一次 → 回退到 UNDERSTAND 重跑
            if len(calls) == 1:
                return ("rework", "理解有误")
            return ("accept", "")

        with mock.patch.object(orchestrator, "_prompt", side_effect=fake_prompt):
            run_id = orchestrator.run_pipeline(str(SAMPLE_PROJECT), auto=False)

        st = orchestrator.status(run_id)
        self.assertEqual(st["state"], "DONE")
        self.assertEqual(st["rollback_rounds"].get("cp1"), 1)
        # cp1 rework → 重跑 UNDERSTAND 后再次 cp1，故 checkpoint 总数 = 6
        self.assertEqual(len(calls), 6)
        dossier = Dossier.load(self._run_dir(run_id))
        self.assertTrue(any(d["decision"] == "rework" for d in dossier.human_decisions))


class RollbackTest(unittest.TestCase):
    def test_rollback_respects_max_rounds(self):
        state = {"rollback_rounds": {}}
        for _ in range(orchestrator.MAX_ROLLBACK_ROUNDS):
            self.assertTrue(orchestrator._rollback("cp1", state))
        self.assertFalse(orchestrator._rollback("cp1", state))   # 超限 → 降级
        self.assertEqual(state["rollback_rounds"]["cp1"], orchestrator.MAX_ROLLBACK_ROUNDS)


class ParseDecisionTest(unittest.TestCase):
    def test_parse_decision(self):
        self.assertEqual(orchestrator._parse_decision(""), ("accept", ""))
        self.assertEqual(orchestrator._parse_decision("accept"), ("accept", ""))
        self.assertEqual(orchestrator._parse_decision("y"), ("accept", ""))
        self.assertEqual(orchestrator._parse_decision("接受"), ("accept", ""))
        self.assertEqual(orchestrator._parse_decision("rework"), ("rework", ""))
        self.assertEqual(orchestrator._parse_decision("r"), ("rework", ""))
        self.assertEqual(orchestrator._parse_decision("note 问题不错"), ("note", "问题不错"))
        self.assertEqual(orchestrator._parse_decision("备注 挺好的"), ("note", "挺好的"))
        # 未识别输入 → 接受并附注原文
        self.assertEqual(orchestrator._parse_decision("随便看看"), ("accept", "随便看看"))


class AdvanceTest(unittest.TestCase):
    def test_advance_sequence(self):
        self.assertEqual(orchestrator._advance("UNDERSTAND"), "cp1")
        self.assertEqual(orchestrator._advance("cp1"), "ABSTRACT")
        self.assertEqual(orchestrator._advance("PLAN"), "cp5")
        self.assertEqual(orchestrator._advance("REFLECT"), "DONE")


class _RecordingLLM:
    """记录每次 complete 的 system 文本，返回空 dict 触发确定性降级。"""

    def __init__(self):
        self.systems = []

    def complete(self, system, user, schema, temperature=0.2):
        self.systems.append(system)
        return {}


class PolicyTest(unittest.TestCase):
    def test_targets_for_state(self):
        self.assertEqual(policy.targets_for_state("EVALUATE"), ["evaluation"])
        self.assertEqual(policy.targets_for_state("PLAN"), ["planning"])
        self.assertEqual(policy.targets_for_state("IDEATE"), ["search"])
        self.assertEqual(sorted(policy.targets_for_state("UNDERSTAND")), ["prompt"])
        self.assertEqual(policy.targets_for_state("REFLECT"), [])

    def test_group_by_target_and_render(self):
        grouped = policy.group_by_target([
            {"policy": {"target": "evaluation", "directive": "D1"}},
            {"policy": {"target": "evaluation", "directive": "D2"}},
            {"policy": {"target": "planning", "directive": "D3"}},
            {"policy": {"target": "evaluation", "directive": "D1"}},  # 重复去重
        ])
        self.assertEqual(grouped["evaluation"], ["D1", "D2"])
        self.assertEqual(grouped["planning"], ["D3"])
        self.assertIn("D1", policy.render_block(grouped["evaluation"]))

    def test_inject_appends_to_system(self):
        inner = _RecordingLLM()
        wrapped = policy.inject(inner, ["约束X"])
        wrapped.complete("系统提示", "用户", {}, 0.2)
        self.assertEqual(len(inner.systems), 1)
        self.assertIn("系统提示", inner.systems[0])
        self.assertIn("约束X", inner.systems[0])

    def test_inject_noop_without_directives(self):
        inner = _RecordingLLM()
        self.assertIs(policy.inject(inner, []), inner)


class PolicyInjectionIntegrationTest(unittest.TestCase):
    """验收 #2：applicability 不匹配时不注入、匹配时 policy 注入到对应 target 的 Agent。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = os.environ.get(storage.ENV_HOME)
        os.environ[storage.ENV_HOME] = self._tmp.name
        storage.ensure_layout()
        self._retrieval_patch = mock.patch.object(
            orchestrator.ideate, "search_literature", return_value=[])
        self._retrieval_patch.start()

    def tearDown(self) -> None:
        self._retrieval_patch.stop()
        if self._orig is None:
            os.environ.pop(storage.ENV_HOME, None)
        else:
            os.environ[storage.ENV_HOME] = self._orig
        self._tmp.cleanup()

    def _seed(self, directive, target="evaluation", domains=("*",), task_types=("*",)):
        experience.append_semantic({
            "experience_id": "exp_inject",
            "type": "pattern",
            "source_domain": "*",
            "applicability": {"domains": list(domains), "task_types": list(task_types),
                              "preconditions": []},
            "principle": "评估前先检查机制性创新",
            "policy": {"target": target, "directive": directive},
            "effect": {"outcome": "positive", "measured_by": "human_review",
                       "note": "人工核验", "updated_at": None},
            "confidence": 0.9,
            "support_count": 2,
            "status": "active",
            "source_runs": ["seed"],
        })

    def test_policy_injected_to_matching_target(self):
        probe = "【注入探针】评估 novelty 前先检查机制性创新"
        self._seed(probe, target="evaluation")

        rec = _RecordingLLM()
        with mock.patch.object(orchestrator, "get_provider", return_value=rec):
            orchestrator.run_pipeline(str(SAMPLE_PROJECT), auto=True)

        eval_systems = [s for s in rec.systems if "可行性评估" in s]
        plan_systems = [s for s in rec.systems if "路线规划" in s]
        self.assertTrue(eval_systems, "应有评估 Agent 的 LLM 调用")
        self.assertTrue(any(probe in s for s in eval_systems), "评估 Agent 应收到注入 directive")
        self.assertTrue(plan_systems, "应有路线规划 Agent 的 LLM 调用")
        self.assertFalse(any(probe in s for s in plan_systems), "非 target Agent 不应收到注入")

    def test_policy_not_injected_on_applicability_mismatch(self):
        probe = "【注入探针】不匹配域不应注入"
        # 领域/任务与 sample 项目不匹配（推荐系统域）
        self._seed(probe, target="evaluation", domains=("推荐系统",), task_types=("推荐",))

        rec = _RecordingLLM()
        with mock.patch.object(orchestrator, "get_provider", return_value=rec):
            orchestrator.run_pipeline(str(SAMPLE_PROJECT), auto=True)

        self.assertFalse(any(probe in s for s in rec.systems), "applicability 不匹配不应注入")


class RenderReportLiteratureTest(unittest.TestCase):
    """M9：报告渲染补文献段（dossier.literature → report.md「文献检索结果」段）。"""

    def _dossier(self) -> Dossier:
        d = Dossier()
        d.meta["run_id"] = "run_test"
        d.meta["llm_backend"] = "null"
        return d

    def test_render_literature_offline(self):
        md = orchestrator._render_report_md(self._dossier())
        self.assertIn("## 文献检索结果", md)
        self.assertIn("（离线/无结果）", md)

    def test_render_literature_with_results(self):
        d = self._dossier()
        d.literature = [{
            "query": "稀疏表示 推荐系统",
            "papers": [
                {"title": "Deep Sparse Representation", "venue": "NeurIPS", "year": 2020,
                 "authors": [], "url": "", "source": "arxiv", "external_id": "2001.00001"},
                {"title": "Learning to Hash", "venue": "arXiv", "year": None,
                 "authors": [], "url": "", "source": "semantic_scholar", "external_id": ""},
            ],
            "gap_note": "现有工作未覆盖跨模态稀疏表示。",
            "sources": ["arxiv", "semantic_scholar"],
        }]
        md = orchestrator._render_report_md(d)
        self.assertIn("## 文献检索结果", md)
        self.assertIn("稀疏表示 推荐系统", md)
        self.assertIn("Deep Sparse Representation（NeurIPS，2020）", md)
        self.assertIn("Learning to Hash（arXiv）", md)
        self.assertIn("gap_note：现有工作未覆盖跨模态稀疏表示。", md)
        self.assertIn("来源：arxiv、semantic_scholar", md)

    def test_render_literature_query_no_papers_marks_offline(self):
        d = self._dossier()
        d.literature = [{
            "query": "某查询",
            "papers": [],
            "gap_note": "（离线/无结果）未检索到可用文献。",
            "sources": [],
        }]
        md = orchestrator._render_report_md(d)
        self.assertIn("## 文献检索结果", md)
        self.assertIn("某查询", md)
        self.assertIn("离线/无结果", md)


class RenderReportNoveltyDimensionsTest(unittest.TestCase):
    """M11：报告渲染展示分维度 novelty 明细（配合 M9 文献段）。"""

    def _dossier(self) -> Dossier:
        d = Dossier()
        d.meta["run_id"] = "run_test"
        d.meta["llm_backend"] = "null"
        return d

    def test_render_evaluation_dimension_details(self):
        d = self._dossier()
        d.evaluations = [{
            "idea_ref": "i1",
            "novelty_score": 63.0,
            "novelty_band": "Revise",
            "novelty_dimensions": {
                "problem_novelty": {"score": 4, "reason": "问题未被充分解决（gap 支持）"},
                "method_novelty": {"score": 3, "reason": "方法以组合为主，新机制有限"},
                "technical_depth": {"score": 2, "reason": "未解决关键技术瓶颈"},
                "gap": {"score": 4, "reason": "与 SOTA 差异明确"},
                "generalization": {"score": 3, "reason": "可迁移到其他任务"},
            },
            "data_feasibility": "high",
            "workload_hours": 60,
            "venue_guess": "CCF-B",
            "verdict": "rework",
            "rework_reason": "新颖性偏低",
            "evidence": [],
        }]
        md = orchestrator._render_report_md(d)
        self.assertIn("## 可行性评估", md)
        self.assertIn("分维度明细", md)
        self.assertIn("novelty=63.0（Revise）", md)
        for label in ("问题新颖性", "方法新颖性", "技术突破性",
                      "与已有工作的差异程度", "可推广价值"):
            self.assertIn(label, md)
        self.assertIn("问题未被充分解决（gap 支持）", md)

    def test_render_old_evaluation_without_dimensions(self):
        """旧格式评估（无 novelty_dimensions）不崩溃，仅回退为单总分展示。"""
        d = self._dossier()
        d.evaluations = [{
            "idea_ref": "i1",
            "novelty_score": 3.5,
            "data_feasibility": "high",
            "workload_hours": 60,
            "venue_guess": "某会议",
            "verdict": "proceed",
            "rework_reason": None,
            "evidence": [],
        }]
        md = orchestrator._render_report_md(d)
        self.assertIn("## 可行性评估", md)
        self.assertIn("novelty=3.5", md)
        self.assertNotIn("分维度明细", md)


if __name__ == "__main__":
    unittest.main()
