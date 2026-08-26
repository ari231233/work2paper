"""M24 Web 后端单测：FastAPI REST API（围绕 Dossier）。

覆盖：
- ``GET /projects/{id}`` 返回 Dossier 的 Decision Report 数据；
- ``POST /projects/{id}/ideas/{iid}/refine`` 触发单 idea 细化并返回更新；
- ``POST /projects/{id}/ideas/{iid}/evaluate`` 单 idea 评估；
- ``POST /projects/{id}/gaps/{gid}/retrieve-more`` 只跑 检索→gap 证据级别更新→评估更新；
- 查询路由（ideas/literature/gaps/roadmap/history）与「当前项目」别名路由。

沿用 tests/test_orchestrator.py 的离线测试约定：``PAPERMINE_HOME`` 指向临时目录、
mock ``get_provider``→NullProvider、mock ``search_literature``，不真调 LLM / 联网。
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient
from papermine import orchestrator, storage
from papermine.dossier import Dossier
from papermine.llm import NullProvider
from web import api
from web.app import create_app

SAMPLE_PROJECT = Path(__file__).resolve().parent.parent / "examples" / "sample-project"


def _rich_dossier() -> Dossier:
    """一个含 1 问题 / 1 文献条目(g1,h1) / 1 idea(i1) / 1 评估 的手工 dossier。"""
    d = Dossier(llm_backend="null")
    d.meta["run_id"] = "run_test"
    d.assets["narrative"] = "工业设备剩余寿命预测与异常检测的横向项目。"
    d.assets["facts"] = {
        "tasks": ["剩余寿命预测", "异常检测"],
        "methods": ["LSTM", "孤立森林"],
        "data": ["传感器时序"],
        "scenarios": ["工业时序"],
        "metrics": ["RMSE", "F1"],
        "libraries": ["torch"],
        "modules": ["model.py"],
    }
    d.problems = [{
        "problem_id": "p1",
        "title": "数据漂移下的剩余寿命预测",
        "formulation": "如何在数据漂移下预测剩余寿命？",
        "motivation": "横向项目反复出现。",
        "why_not_engineering": "需可泛化、可比较的结论。",
        "evidence_refs": ["README.md"],
    }]
    d.literature = [{
        "query": "剩余寿命预测",
        "papers": [{
            "title": "LSTM RUL Prediction", "venue": "IEEE TII", "year": 2021,
            "abstract": "使用 LSTM 在静态分布数据上预测剩余寿命。",
            "source": "arxiv",
            "understanding": {
                "claim": "LSTM 可预测剩余寿命", "method": "LSTM", "conclusion": "优于基线",
                "applicability": "小样本时序", "limitations": "长序列退化",
            },
            "evidence_card": {
                "title": "LSTM RUL Prediction", "dataset": "C-MAPSS", "baseline": "SVR",
                "metric": "RMSE", "main_gain": "提升 3%", "limitation": None,
                "claim_strength": "moderate", "evidence_source": "abstract",
            },
        }],
        "gap_note": "现有工作未覆盖数据漂移场景。",
        "sources": ["arxiv"],
        "contradiction_graph": {
            "nodes": [{"id": "p:0", "label": "LSTM RUL Prediction", "kind": "paper"}],
            "edges": [],
            "gaps": [{
                "gap_id": "g1", "type": "gap",
                "claim_point": "数据漂移下的剩余寿命预测",
                "description": "检索论文均假设静态分布", "angle": "数据漂移", "paper_refs": [],
                "gap_hypothesis": {
                    "claim": "尚未发现数据漂移下的剩余寿命预测（假设，非事实）",
                    "evidence_level": "weak", "basis": "基于检索到的 1 篇论文",
                    "scope": "检索范围：arXiv，共 1 篇",
                },
            }],
        },
        "hypotheses": [{
            "hypothesis_id": "h1", "gap_ref": "g1",
            "statement": "若引入漂移自适应机制，则预测精度提升",
            "falsification": "精度无提升则证伪",
        }],
    }]
    d.ideas = [{
        "idea_id": "i1", "claim": "漂移自适应剩余寿命预测方法",
        "novelty_hypothesis": "现有工作未覆盖数据漂移", "problem_ref": "p1",
        "literature_refs": ["LSTM RUL Prediction"], "gap_refs": ["g1"],
        "hypothesis_refs": ["h1"], "evidence": [], "status": "pending_eval",
    }]
    d.evaluations = [{
        "idea_ref": "i1", "novelty_score": 48.0, "novelty_band": "Weak Reject",
        "data_feasibility": "high", "workload_hours": 80, "venue_guess": "EI 会议",
        "verdict": "rework", "rework_reason": "证据不足，需补文献对拍", "evidence": [],
    }]
    return d


def _save(dossier: Dossier, run_id: str) -> Path:
    run_dir = storage.run_dir(run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    dossier.meta["run_id"] = run_id
    dossier.save(run_dir)
    return run_dir


class _StubLLM:
    """返回固定结果的 LLM 桩。"""

    def __init__(self, result):
        self.result = result

    def complete(self, system, user, schema, temperature=0.2):
        return self.result

    def complete_fast(self, system, user, schema, temperature=0.2):
        return self.result


class WebApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = os.environ.get(storage.ENV_HOME)
        os.environ[storage.ENV_HOME] = self._tmp.name
        storage.ensure_layout()
        self.client = TestClient(create_app())

    def tearDown(self) -> None:
        if self._orig is None:
            os.environ.pop(storage.ENV_HOME, None)
        else:
            os.environ[storage.ENV_HOME] = self._orig
        self._tmp.cleanup()


class QueryRoutesTest(WebApiTest):
    def _pipeline_run_id(self) -> str:
        with mock.patch.object(orchestrator, "get_provider", return_value=NullProvider()), \
                mock.patch.object(orchestrator.ideate, "search_literature", return_value=[]):
            return orchestrator.run_pipeline(str(SAMPLE_PROJECT), auto=True)

    def test_get_project_returns_decision_report(self):
        run_id = self._pipeline_run_id()
        resp = self.client.get("/projects/{}".format(run_id))
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["project_id"], run_id)
        # Decision Report 数据（默认精简版）：含 Executive Summary 与候选 idea 排名
        self.assertIn("## 0. Executive Summary", body["decision_report"])
        self.assertIn("## 4. Candidate Ideas", body["decision_report"])
        dossier = body["dossier"]
        self.assertGreaterEqual(len(dossier["ideas"]), 2)
        self.assertGreaterEqual(len(dossier["evaluations"]), 2)
        # 状态推进到 DONE
        self.assertEqual(body["status"]["state"], "DONE")

    def test_get_ideas_literature_gaps_roadmap_history(self):
        run_id = "run_q"
        _save(_rich_dossier(), run_id)
        self.assertEqual(self.client.get("/projects/{}/ideas".format(run_id)).status_code, 200)
        ideas = self.client.get("/projects/{}/ideas".format(run_id)).json()["ideas"]
        self.assertEqual(ideas[0]["idea"]["idea_id"], "i1")
        self.assertEqual(ideas[0]["evaluation"]["idea_ref"], "i1")

        lit = self.client.get("/projects/{}/literature".format(run_id)).json()["literature"]
        self.assertEqual(lit[0]["query"], "剩余寿命预测")

        gaps = self.client.get("/projects/{}/gaps".format(run_id)).json()["gaps"]
        self.assertEqual(gaps[0]["gap_id"], "g1")
        self.assertEqual(gaps[0]["evidence_level"], "weak")

        roadmap = self.client.get("/projects/{}/roadmap".format(run_id)).json()["roadmap"]
        self.assertIn("selected_idea", roadmap)

        history = self.client.get("/projects/{}/history".format(run_id)).json()
        self.assertIn("human_decisions", history)
        self.assertIn("snapshots", history)

    def test_create_and_analyze_project(self):
        with mock.patch.object(orchestrator, "get_provider", return_value=NullProvider()), \
                mock.patch.object(orchestrator.ideate, "search_literature", return_value=[]):
            created = self.client.post("/projects", json={"project_dir": str(SAMPLE_PROJECT)})
        self.assertEqual(created.status_code, 200)
        run_id = created.json()["project_id"]
        self.assertIn("## 0. Executive Summary", created.json()["decision_report"])

        # 续跑已完成项目：仍 DONE，project_id 不变
        with mock.patch.object(orchestrator, "get_provider", return_value=NullProvider()):
            analyzed = self.client.post("/projects/{}/analyze".format(run_id))
        self.assertEqual(analyzed.status_code, 200)
        self.assertEqual(analyzed.json()["project_id"], run_id)
        self.assertEqual(analyzed.json()["status"]["state"], "DONE")

    def test_get_project_404(self):
        resp = self.client.get("/projects/does-not-exist")
        self.assertEqual(resp.status_code, 404)

    def test_top_level_alias_resolves_latest_project(self):
        run_id = "run_alias"
        _save(_rich_dossier(), run_id)
        resp = self.client.get("/ideas")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["ideas"][0]["idea"]["idea_id"], "i1")


class RefineIdeaTest(WebApiTest):
    def _save_rich(self) -> str:
        run_id = "run_refine"
        _save(_rich_dossier(), run_id)
        return run_id

    def test_refine_idea_updates_and_returns(self):
        run_id = self._save_rich()
        stub = _StubLLM({"claim": "细化后的创新点", "novelty_hypothesis": "细化后的可证伪假设"})
        with mock.patch.object(api, "get_provider", return_value=stub):
            resp = self.client.post("/projects/{}/ideas/i1/refine".format(run_id))
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["idea"]["claim"], "细化后的创新点")
        self.assertEqual(body["idea"]["novelty_hypothesis"], "细化后的可证伪假设")
        self.assertEqual(body["idea"]["status"], "refined")
        self.assertFalse(body["degraded"])
        # 记录细化历史
        self.assertEqual(body["idea"]["history"][-1]["action"], "refine")
        # 落盘已更新（版本递增）
        self.assertEqual(Dossier.load(storage.run_dir(run_id)).ideas[0]["claim"], "细化后的创新点")

    def test_refine_idea_deterministic_fallback(self):
        run_id = self._save_rich()
        with mock.patch.object(api, "get_provider", return_value=NullProvider()):
            resp = self.client.post("/projects/{}/ideas/i1/refine".format(run_id))
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["degraded"])
        # 确定性细化把评估结论里的「如何强化」补进 novelty_hypothesis
        self.assertIn("细化方向", body["idea"]["novelty_hypothesis"])

    def test_refine_idea_404(self):
        run_id = self._save_rich()
        self.assertEqual(
            self.client.post("/projects/{}/ideas/nope/refine".format(run_id)).status_code, 404)


class EvaluateIdeaTest(WebApiTest):
    def test_evaluate_single_idea(self):
        run_id = "run_eval"
        _save(_rich_dossier(), run_id)
        with mock.patch.object(api, "get_provider", return_value=NullProvider()):
            resp = self.client.post("/projects/{}/ideas/i1/evaluate".format(run_id))
        self.assertEqual(resp.status_code, 200)
        ev = resp.json()["evaluation"]
        self.assertEqual(ev["idea_ref"], "i1")
        self.assertIn(ev["verdict"], ("proceed", "rework", "drop"))
        self.assertIsInstance(ev["novelty_score"], (int, float))
        # M21 贡献分析 + M12 证据强度 + M20 校准均装配进单条评估
        self.assertIn("contribution", ev)
        self.assertIn("evidence_validation", ev)
        # 落盘：evaluations 里该 idea 只有一条（替换而非追加）
        loaded = Dossier.load(storage.run_dir(run_id))
        self.assertEqual(
            len([e for e in loaded.evaluations if e.get("idea_ref") == "i1"]), 1)


class RetrieveMoreTest(WebApiTest):
    def test_retrieve_more_updates_gap_and_evaluation(self):
        run_id = "run_rm"
        _save(_rich_dossier(), run_id)
        new_entries = [{
            "query": "数据漂移",
            "papers": [{
                "title": "New Drift Paper", "venue": "arXiv", "year": 2023,
                "abstract": "研究数据漂移下的寿命预测。", "source": "arxiv",
            }],
            "gap_note": "新检索到数据漂移相关工作。",
            "sources": ["arxiv"],
        }]
        with mock.patch.object(api, "get_provider", return_value=NullProvider()), \
                mock.patch.object(api, "search_literature", return_value=new_entries):
            resp = self.client.post("/projects/{}/gaps/g1/retrieve-more".format(run_id))
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["added_papers"], ["New Drift Paper"])
        # gap 证据级别用扩大后的论文集重算（basis 反映 2 篇论文）
        self.assertIn("2 篇论文", body["gap"]["gap_hypothesis"]["basis"])
        # 引用该 gap 的 idea i1 被重评估
        self.assertEqual(len(body["updated_evaluations"]), 1)
        self.assertEqual(body["updated_evaluations"][0]["idea_ref"], "i1")
        # 新论文并入父条目
        loaded = Dossier.load(storage.run_dir(run_id))
        titles = [p["title"] for p in loaded.literature[0]["papers"]]
        self.assertIn("New Drift Paper", titles)

    def test_retrieve_more_404(self):
        run_id = "run_rm2"
        _save(_rich_dossier(), run_id)
        self.assertEqual(
            self.client.post("/projects/{}/gaps/nope/retrieve-more".format(run_id)).status_code, 404)


if __name__ == "__main__":
    unittest.main()
