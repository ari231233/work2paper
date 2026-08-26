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

from papermine import experience, orchestrator, policy, reporting, storage
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
    """M23：报告「A. Literature Evidence」附录（完整文献检索 + 证据卡）+ Decision §3 概览。"""

    def _dossier(self) -> Dossier:
        d = Dossier()
        d.meta["run_id"] = "run_test"
        d.meta["llm_backend"] = "null"
        return d

    def test_render_literature_offline(self):
        md = orchestrator._render_report_md(self._dossier())
        self.assertIn("## A. Literature Evidence", md)
        self.assertIn("（离线/无结果）", md)
        # Decision Report §3 也标注离线
        self.assertIn("## 3. Literature Landscape", md)
        self.assertIn("关键论文：（离线/无结果）", md)

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
        self.assertIn("## A. Literature Evidence", md)
        self.assertIn("稀疏表示 推荐系统", md)
        self.assertIn("Deep Sparse Representation（NeurIPS，2020）", md)
        self.assertIn("Learning to Hash（arXiv）", md)
        self.assertIn("gap_note：现有工作未覆盖跨模态稀疏表示。", md)
        self.assertIn("来源：arxiv、semantic_scholar", md)
        # Decision Report §3 给出证据覆盖度
        self.assertIn("证据覆盖度：共 2 篇论文", md)

    def test_render_literature_query_no_papers_marks_offline(self):
        d = self._dossier()
        d.literature = [{
            "query": "某查询",
            "papers": [],
            "gap_note": "（离线/无结果）未检索到可用文献。",
            "sources": [],
        }]
        md = orchestrator._render_report_md(d)
        self.assertIn("## A. Literature Evidence", md)
        self.assertIn("某查询", md)
        self.assertIn("离线/无结果", md)


class RenderReportNoveltyDimensionsTest(unittest.TestCase):
    """M23：报告「D. Full Novelty Evaluation」附录展示分维度 novelty 明细。"""

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
        self.assertIn("## D. Full Novelty Evaluation", md)
        self.assertIn("分维度明细", md)
        self.assertIn("novelty（总分）=63.0（Revise）", md)
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
        self.assertIn("## D. Full Novelty Evaluation", md)
        self.assertIn("novelty（总分）=3.5", md)
        self.assertNotIn("分维度明细", md)


class RenderReportCalibrationTest(unittest.TestCase):
    """M20：报告渲染「问题 → 答案 → 规则 → 得分」完整链路（分数可追溯）。"""

    def _dossier(self) -> Dossier:
        d = Dossier()
        d.meta["run_id"] = "run_test"
        d.meta["llm_backend"] = "null"
        return d

    def test_render_calibration_chain(self):
        d = self._dossier()
        d.evaluations = [{
            "idea_ref": "i1",
            "novelty_score": 63.0,
            "novelty_band": "Revise",
            "novelty_dimensions": {
                "problem_novelty": {"score": 4, "reason": "规则计算：起点 2 + Q2:+1 + Q3:+1 = 4"},
                "method_novelty": {"score": 3, "reason": "规则计算：起点 2 + Q3:+1 = 3"},
                "technical_depth": {"score": 2, "reason": "规则计算：起点 1 + Q1:+1 = 2"},
                "gap": {"score": 2, "reason": "规则计算：起点 1 + Q1:+1 = 2"},
                "generalization": {"score": 1, "reason": "规则计算：起点 1 = 1"},
            },
            "calibration": {
                "method_novelty": {
                    "label": "方法新颖性", "weight": 35, "score": 3,
                    "base": 2.0, "derivation": "起点 2 + Q3:+1 = 3",
                    "questions": [
                        {"id": "Q1", "text": "是否只是已有模块组合？", "answer": "no",
                         "rule": "yes → 封顶 ≤ 3", "effect": "—", "evidence": "证据：非组合"},
                        {"id": "Q2", "text": "是否改变核心 optimization objective？", "answer": "no",
                         "rule": "yes → +1", "effect": "—", "evidence": "证据：无"},
                        {"id": "Q3", "text": "是否提出新的学习机制？", "answer": "yes",
                         "rule": "yes → +1", "effect": "+1", "evidence": "证据：自适应机制"},
                    ],
                },
            },
            "data_feasibility": "high",
            "workload_hours": 60,
            "venue_guess": "CCF-B",
            "verdict": "rework",
            "rework_reason": "新颖性偏低",
            "evidence": [],
        }]
        md = orchestrator._render_report_md(d)
        self.assertIn("## D. Full Novelty Evaluation", md)
        self.assertIn("评分校准", md)
        self.assertIn("问题 → 答案 → 规则 → 得分", md)
        self.assertIn("方法新颖性（权重35）", md)
        self.assertIn("是否只是已有模块组合？", md)
        self.assertIn("yes → 封顶 ≤ 3", md)
        self.assertIn("证据：自适应机制", md)
        # 有 calibration 时不再重复渲染「分维度明细」旧块
        self.assertNotIn("分维度明细", md)


class RenderReportM9V2Test(unittest.TestCase):
    """M9 v2：报告渲染 M5 v2 新字段（文献理解 / 矛盾图 / 假设 + idea 关联可追溯）。"""

    def _dossier(self) -> Dossier:
        d = Dossier()
        d.meta["run_id"] = "run_test"
        d.meta["llm_backend"] = "null"
        return d

    def _literature(self) -> list:
        return [{
            "query": "剩余寿命预测",
            "papers": [
                {
                    "title": "LSTM RUL Prediction", "venue": "IEEE TII", "year": 2021,
                    "source": "arxiv",
                    "understanding": {
                        "claim": "LSTM 可预测剩余寿命",
                        "method": "LSTM",
                        "conclusion": "优于传统基线",
                        "applicability": "小样本时序数据",
                        "limitations": "长序列退化",
                    },
                },
                {
                    "title": "Isolation Forest Anomaly Detection", "venue": "arXiv", "year": 2019,
                    "source": "semantic_scholar",
                    "understanding": {
                        "claim": "孤立森林可做无监督异常检测",
                        "method": "孤立森林",
                        "conclusion": "无需标注",
                        "applicability": "静态分布数据",
                        "limitations": "数据漂移下失效",
                    },
                },
            ],
            "gap_note": "现有工作未覆盖数据漂移场景。",
            "sources": ["arxiv", "semantic_scholar"],
            "contradiction_graph": {
                "nodes": [
                    {"id": "p:0", "label": "LSTM RUL Prediction", "kind": "paper"},
                    {"id": "p:1", "label": "Isolation Forest Anomaly Detection", "kind": "paper"},
                ],
                "edges": [],
                "gaps": [
                    {
                        "gap_id": "g1", "type": "gap",
                        "claim_point": "数据漂移下的剩余寿命预测",
                        "description": "现有工作都假设静态分布，缺少数据漂移角度",
                        "angle": "数据漂移", "paper_refs": [],
                    },
                    {
                        "gap_id": "g2", "type": "contradiction",
                        "claim_point": "异常检测是否需要标注",
                        "description": "LSTM 方法需要标注，孤立森林无需标注",
                        "angle": "",
                        "paper_refs": ["LSTM RUL Prediction", "Isolation Forest Anomaly Detection"],
                    },
                ],
            },
            "hypotheses": [
                {
                    "hypothesis_id": "h1", "gap_ref": "g1",
                    "statement": "若引入漂移自适应机制，则剩余寿命预测精度提升",
                    "falsification": "精度无显著提升则证伪",
                },
                {
                    "hypothesis_id": "h2", "gap_ref": "g2",
                    "statement": "若采用半监督标注，则成本下降且精度保持",
                    "falsification": "成本不降或精度下降则证伪",
                },
            ],
        }]

    def test_render_understanding_attached_to_papers(self):
        d = self._dossier()
        d.literature = self._literature()
        md = orchestrator._render_report_md(d)
        self.assertIn("## A. Literature Evidence", md)
        for label in ("核心主张", "方法", "结论", "适用条件", "局限"):
            self.assertIn(label, md)
        self.assertIn("LSTM 可预测剩余寿命", md)
        self.assertIn("孤立森林可做无监督异常检测", md)

    def test_render_contradiction_graph(self):
        d = self._dossier()
        d.literature = self._literature()
        md = orchestrator._render_report_md(d)
        self.assertIn("## B. Gap Mining", md)
        self.assertIn("缺口 g1：数据漂移下的剩余寿命预测", md)
        self.assertIn("矛盾 g2：异常检测是否需要标注", md)
        self.assertIn("冲突双方：LSTM RUL Prediction ⇄ Isolation Forest Anomaly Detection", md)

    def test_render_hypotheses_with_idea_traceability(self):
        d = self._dossier()
        d.literature = self._literature()
        d.ideas = [{
            "idea_id": "i1",
            "claim": "漂移自适应剩余寿命预测方法",
            "novelty_hypothesis": "现有工作未覆盖数据漂移",
            "problem_ref": "p1",
            "literature_refs": ["LSTM RUL Prediction"],
            "gap_refs": ["g1"],
            "hypothesis_refs": ["h1"],
            "evidence": [],
            "status": "pending_eval",
        }]
        md = orchestrator._render_report_md(d)
        self.assertIn("## C. Hypotheses", md)
        self.assertIn("h1：若引入漂移自适应机制，则剩余寿命预测精度提升", md)
        self.assertIn("可证伪条件：精度无显著提升则证伪", md)
        self.assertIn("催生的 idea：i1", md)
        # idea 段标注来源缺口/假设（关联可追溯）
        self.assertIn("来源缺口：g1", md)
        self.assertIn("来源假设：h1", md)

    def test_render_m9v2_absent_fields_graceful(self):
        """旧格式 literature（无 M5 v2 字段）不崩溃：矛盾/假设段显示（无）。"""
        d = self._dossier()
        d.literature = [{
            "query": "某查询",
            "papers": [{"title": "Plain Paper", "venue": "arXiv", "year": 2020}],
            "gap_note": "gap",
            "sources": ["arxiv"],
        }]
        md = orchestrator._render_report_md(d)
        self.assertIn("## B. Gap Mining", md)
        self.assertIn("## C. Hypotheses", md)
        self.assertNotIn("核心主张", md)


class RenderReportM23TwoLayerTest(unittest.TestCase):
    """M23：两层报告——Decision Report（默认精简）+ Evidence Appendix（完整证据，后置）。"""

    def _dossier(self) -> Dossier:
        d = Dossier()
        d.meta["run_id"] = "run_test"
        d.meta["llm_backend"] = "null"
        return d

    @staticmethod
    def _contribution(ctype: str) -> dict:
        label = {"A": "新模块创新（Method Innovation）",
                 "B": "框架集成创新（Framework Integration）"}[ctype]
        return {
            "type": ctype,
            "type_label": label,
            "reason": "确定性测试桩：{}".format(label),
            "matrix": {
                "method": {"strength": "low", "label": "低", "reason": "无新模块"},
                "framework": {"strength": "medium_high", "label": "中高", "reason": "任务交互"},
                "application": {"strength": "medium", "label": "中", "reason": "面向场景"},
                "problem": {"strength": "high", "label": "高", "reason": "重新定义联合任务"},
                "training": {"strength": "none", "label": "无", "reason": "无"},
                "engineering": {"strength": "high", "label": "高", "reason": "易落地"},
            },
            "attacks": {
                "ablation": {"attack": "删除异常检测后剩什么", "answer": "退化为普通 RUL 预测"},
                "concatenation": {"attack": "A+B concat 是否等效", "answer": "不等效，交互有效"},
                "reviewer": {"attack": "merely a combination", "answer": "共享表示 + 联合优化反驳"},
            },
            "degraded": False,
        }

    def _rich_dossier(self) -> Dossier:
        d = self._dossier()
        d.assets["narrative"] = "工业设备剩余寿命预测与异常检测的横向项目。"
        d.problems = [{"problem_id": "p1", "title": "数据漂移下的 RUL",
                       "formulation": "如何在数据漂移下预测剩余寿命？"}]
        d.literature = [{
            "query": "剩余寿命预测",
            "papers": [{
                "title": "LSTM RUL Prediction", "venue": "IEEE TII", "year": 2021, "source": "arxiv",
                "understanding": {"claim": "LSTM 可预测剩余寿命", "method": "LSTM", "conclusion": "优于基线",
                                  "applicability": "小样本时序", "limitations": "长序列退化"},
                "evidence_card": {"title": "LSTM RUL Prediction", "dataset": "C-MAPSS",
                                  "baseline": "SVR", "metric": "RMSE", "main_gain": "提升 3%",
                                  "limitation": None, "claim_strength": "moderate",
                                  "evidence_source": "abstract"},
            }],
            "gap_note": "现有工作未覆盖数据漂移场景。",
            "sources": ["arxiv", "semantic_scholar"],
            "contradiction_graph": {"nodes": [], "edges": [], "gaps": [{
                "gap_id": "g1", "type": "gap", "claim_point": "数据漂移下的剩余寿命预测",
                "description": "检索论文均假设静态分布", "angle": "数据漂移", "paper_refs": [],
                "gap_hypothesis": {"claim": "尚未发现数据漂移下的剩余寿命预测（假设，非事实）",
                                   "evidence_level": "weak", "basis": "基于 1 篇论文",
                                   "scope": "检索范围：arXiv，共 1 篇"}}]},
            "hypotheses": [{"hypothesis_id": "h1", "gap_ref": "g1",
                            "statement": "若引入漂移自适应机制，则预测精度提升",
                            "falsification": "精度无提升则证伪"}],
        }]
        d.ideas = [
            {"idea_id": "i1", "claim": "漂移自适应剩余寿命预测方法",
             "novelty_hypothesis": "现有工作未覆盖数据漂移", "problem_ref": "p1",
             "literature_refs": ["LSTM RUL Prediction"], "gap_refs": ["g1"],
             "hypothesis_refs": ["h1"], "evidence": [], "status": "pending_eval"},
            {"idea_id": "i2", "claim": "异常检测辅助 RUL 的框架集成",
             "novelty_hypothesis": "两任务协同产生交互", "problem_ref": "p1",
             "literature_refs": [], "gap_refs": [], "hypothesis_refs": [],
             "evidence": [], "status": "pending_eval"},
        ]
        d.evaluations = [
            {"idea_ref": "i1", "contribution": self._contribution("A"),
             "novelty_score": 78.0, "novelty_band": "Accept",
             "novelty_dimensions": {"problem_novelty": {"score": 4, "reason": "r"},
                                    "method_novelty": {"score": 4, "reason": "r"},
                                    "technical_depth": {"score": 3, "reason": "r"},
                                    "gap": {"score": 4, "reason": "r"},
                                    "generalization": {"score": 3, "reason": "r"}},
             "calibration": {},
             "evidence_validation": {"evidence": "medium", "reason": "有文献对拍",
                                     "checks": {}, "degraded": False},
             "data_feasibility": "high", "workload_hours": 80, "venue_guess": "CCF-B",
             "verdict": "proceed", "rework_reason": None, "evidence": []},
            {"idea_ref": "i2", "contribution": self._contribution("B"),
             "novelty_score": 65.0, "novelty_band": "Revise",
             "novelty_dimensions": {}, "calibration": {},
             "evidence_validation": {"evidence": "weak", "reason": "claim 过强",
                                     "checks": {}, "degraded": False},
             "data_feasibility": "medium", "workload_hours": 100, "venue_guess": "EI 会议",
             "verdict": "rework", "rework_reason": "需细化", "evidence": []},
        ]
        d.roadmap = {
            "selected_idea": "i1",
            "paper_type": "方法论文",
            "outline": ["1. 引言", "2. 方法"],
            "core_story": {"status_quo": "现状", "problem": "问题", "method": "方法", "contribution": "贡献"},
            "research_questions": [{"id": "RQ1", "question": "核心方案是否优于 baseline",
                                    "target_experiments": ["E1"]}],
            "experiment_matrix": [{"experiment": "E1", "purpose": "主实验",
                                   "independent_variable": "是否启用核心方案",
                                   "baselines": ["LSTM"], "metrics": ["RMSE"], "rq": "RQ1"}],
            "minimum_viable_paper": {"must_have": ["准备数据", "复现 baseline"], "optional": ["理论分析"]},
            "success_criteria": {"success": ["显著优于"], "failure": ["无提升"], "pivot": "转失效分析"},
            "risk_branches": [{"risk": "XGBoost 始终占优", "branch": "转失效条件分析"}],
            "stage_exits": [{"stage": "Week 1", "tasks": ["跑通数据"], "exit_criteria": "baseline 可复现"}],
            "missing_items": [],
        }
        d.human_decisions = [{"checkpoint": "cp1", "decision": "accept", "note": "ok"}]
        return d

    def test_two_layer_structure_and_order(self):
        md = orchestrator._render_report_md(self._rich_dossier())
        self.assertIn("# Papermine Research Report", md)
        self.assertIn("# Evidence Appendix（完整证据）", md)
        self.assertLess(md.index("# Papermine Research Report"), md.index("# Evidence Appendix"))
        for sec in ("## 0. Executive Summary", "## 1. Project Understanding",
                    "## 2. Research Questions", "## 3. Literature Landscape",
                    "## 4. Candidate Ideas", "## 5. Recommended Idea",
                    "## 6. Paper Roadmap", "## 7. Immediate Next Actions"):
            self.assertIn(sec, md)
        for sec in ("## A. Literature Evidence", "## B. Gap Mining", "## C. Hypotheses",
                    "## D. Full Novelty Evaluation", "## E. Attack Tests", "## F. Human Decisions"):
            self.assertIn(sec, md)

    def test_decision_report_concise_and_separated(self):
        d = self._rich_dossier()
        decision = reporting.render_decision_report(d)
        appendix = reporting.render_evidence_appendix(d)
        # 决策版不含附录段
        for sec in ("## A. Literature Evidence", "## D. Full Novelty Evaluation", "## E. Attack Tests"):
            self.assertNotIn(sec, decision)
        # 细节（证据卡 / 完整评分 / 攻击测试）只进附录，正文只给结论
        self.assertNotIn("证据卡", decision)
        self.assertIn("证据卡", appendix)
        self.assertNotIn("分维度明细", decision)
        self.assertIn("分维度明细", appendix)
        self.assertIn("攻击测试", appendix)
        # Executive Summary 给出推荐结论（M23 改动 2）
        self.assertIn("## 0. Executive Summary", decision)
        self.assertIn("推荐方向", decision)
        self.assertIn("推荐程度", decision)
        self.assertIn("为什么推荐", decision)
        self.assertIn("当前最重要的 3 个动作", decision)
        # 候选 idea 排名表 + 贡献矩阵进度条（M23 改动 3/4）
        self.assertIn("| Idea | 类型 | Novelty | Evidence | Feasibility | 推荐 |", decision)
        self.assertIn("█████", decision)

    def test_gap_table_compressed_in_decision(self):
        """M23 改动 6：Decision Report 不含 gap 完整依据，附录 B 才有表格 + 完整依据。"""
        d = self._rich_dossier()
        decision = reporting.render_decision_report(d)
        appendix = reporting.render_evidence_appendix(d)
        self.assertNotIn("| Gap | 研究空白假设 | Evidence | Coverage |", decision)
        self.assertIn("| Gap | 研究空白假设 | Evidence | Coverage |", appendix)
        self.assertIn("完整依据", appendix)


if __name__ == "__main__":
    unittest.main()
