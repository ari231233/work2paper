"""M5 创新点生成 Agent 单测：接口契约、检索编排、确定性降级、引用过滤。

用标准库 unittest 编写，`python -m unittest discover -s tests -v` 可运行（兼容 pytest 收集）。
"""
from __future__ import annotations

import unittest
from unittest import mock

from papermine.agents import ideate
from papermine.agents.ideate import (
    IDEA_SCHEMA,
    _derive_queries,
    _deterministic_ideas,
    _finalize_ideas,
    run,
)
from papermine.dossier import Dossier
from papermine.llm import LLMError, NullProvider


def _problems():
    return [
        {
            "problem_id": "p1", "title": "工业时序异常检测",
            "formulation": "如何在数据漂移下保持检测精度？",
            "motivation": "……", "why_not_engineering": "……", "evidence_refs": [],
        },
        {
            "problem_id": "p2", "title": "剩余寿命预测",
            "formulation": "如何在小样本下预测 RUL？",
            "motivation": "……", "why_not_engineering": "……", "evidence_refs": [],
        },
    ]


def _facts():
    return {
        "tasks": ["异常检测", "剩余寿命预测"], "methods": ["孤立森林", "LSTM"],
        "data": ["时序数据"], "scenarios": ["工业制造"], "metrics": ["F1"],
        "libraries": ["torch"], "modules": ["DataPipeline"],
    }


def _dossier():
    d = Dossier(project_id="proj-m5", llm_backend="deepseek")
    d.problems = _problems()
    d.assets["facts"] = _facts()
    return d


def _literature_entry(papers):
    return {
        "query": "q", "gap_note": "gap",
        "papers": papers, "sources": ["arxiv", "semantic_scholar"],
    }


def _paper(title, source="arxiv"):
    return {
        "title": title, "authors": [], "year": 2022, "abstract": "",
        "url": "", "venue": "arXiv", "source": source, "external_id": "",
    }


class _StubLLM:
    def __init__(self, result=None, error=None):
        self.result = result if result is not None else {}
        self.error = error
        self.calls = []

    def complete(self, system, user, schema, temperature=0.2):
        self.calls.append((system, user, schema, temperature))
        if self.error is not None:
            raise self.error
        return self.result


class IdeateAgentTest(unittest.TestCase):
    def test_run_offline_generates_ideas_from_problems(self):
        d = _dossier()
        with mock.patch.object(ideate, "search_literature", return_value=[]):
            run(d, NullProvider())
        self.assertEqual(d.literature, [])
        self.assertGreaterEqual(len(d.ideas), 2)
        for idea in d.ideas:
            self.assertIn("idea_id", idea)
            self.assertTrue(idea["claim"].strip())
            self.assertTrue(idea["novelty_hypothesis"].strip())
            self.assertIn("problem_ref", idea)
            self.assertIsInstance(idea["literature_refs"], list)
            self.assertEqual(idea["status"], "pending_eval")
        self.assertEqual(d.meta["prompt_versions"]["ideate"], "v1")

    def test_run_with_literature_cites_real_papers(self):
        d = _dossier()
        literature = [_literature_entry([_paper("Paper A"), _paper("Paper B", "semantic_scholar")])]
        with mock.patch.object(ideate, "search_literature", return_value=literature):
            run(d, NullProvider())
        self.assertGreaterEqual(len(d.ideas), 2)
        cited = [i for i in d.ideas if i["literature_refs"]]
        self.assertTrue(cited)
        for idea in cited:
            for ref in idea["literature_refs"]:
                self.assertIn(ref, {"Paper A", "Paper B"})

    def test_run_with_llm_generates_ideas(self):
        d = _dossier()
        llm = _StubLLM(result={"ideas": [{
            "claim": "提出一种面向数据漂移的在线异常检测方法。",
            "novelty_hypothesis": "现有方法多假设静态分布，本点假设漂移场景下存在方法空白。",
            "problem_ref": "p1",
            "literature_refs": ["Paper A"],
        }]})
        literature = [_literature_entry([_paper("Paper A")])]
        with mock.patch.object(ideate, "search_literature", return_value=literature):
            run(d, llm)
        self.assertEqual(len(d.ideas), 1)
        idea = d.ideas[0]
        self.assertEqual(idea["idea_id"], "i1")
        self.assertEqual(idea["problem_ref"], "p1")
        self.assertEqual(idea["literature_refs"], ["Paper A"])
        self.assertEqual(idea["status"], "pending_eval")

    def test_hallucinated_refs_are_filtered(self):
        d = _dossier()
        llm = _StubLLM(result={"ideas": [{
            "claim": "claim", "novelty_hypothesis": "hypo",
            "problem_ref": "p1", "literature_refs": ["Fake Paper"],
        }]})
        literature = [_literature_entry([_paper("Real Paper")])]
        with mock.patch.object(ideate, "search_literature", return_value=literature):
            run(d, llm)
        idea = d.ideas[0]
        self.assertNotIn("Fake Paper", idea["literature_refs"])
        # 有真实文献却没引用时，补真实标题，保证 idea 带引用
        self.assertEqual(idea["literature_refs"], ["Real Paper"])

    def test_empty_problems_derive_from_facts(self):
        d = Dossier()
        d.assets["facts"] = _facts()
        with mock.patch.object(ideate, "search_literature", return_value=[]):
            run(d, NullProvider())
        self.assertGreaterEqual(len(d.ideas), 2)
        for idea in d.ideas:
            self.assertTrue(idea["claim"].strip())
            self.assertTrue(idea["novelty_hypothesis"].strip())

    def test_llm_error_falls_back_deterministic(self):
        d = _dossier()
        with mock.patch.object(ideate, "search_literature", return_value=[]):
            run(d, _StubLLM(error=LLMError("网络失败")))
        self.assertGreaterEqual(len(d.ideas), 2)


class DeriveQueriesTest(unittest.TestCase):
    def test_queries_from_problems(self):
        q = _derive_queries(_problems(), _facts())
        self.assertEqual(len(q), 2)
        self.assertIn("工业时序异常检测", q[0])

    def test_queries_fallback_to_facts(self):
        q = _derive_queries([], _facts())
        self.assertEqual(len(q), 1)
        self.assertIn("工业制造", q[0])
        self.assertIn("异常检测", q[0])

    def test_queries_empty_when_no_signal(self):
        self.assertEqual(_derive_queries([], {}), [])


class DeterministicIdeasTest(unittest.TestCase):
    def test_two_problems_two_ideas(self):
        ideas = _deterministic_ideas(_problems(), [], _facts())
        self.assertEqual(len(ideas), 2)
        for i in ideas:
            self.assertTrue(i["claim"])
            self.assertTrue(i["novelty_hypothesis"])
            self.assertIsInstance(i["literature_refs"], list)

    def test_no_problems_still_two_ideas(self):
        ideas = _deterministic_ideas([], [], _facts())
        self.assertGreaterEqual(len(ideas), 2)


class FinalizeIdeasTest(unittest.TestCase):
    def test_assigns_ids_and_normalizes(self):
        raw = [
            {"claim": " c1 ", "novelty_hypothesis": " h ", "problem_ref": "p1", "literature_refs": []},
            {"claim": "c2", "novelty_hypothesis": "", "problem_ref": "bad", "literature_refs": ["x"]},
        ]
        out = _finalize_ideas(raw, _problems(), [])
        self.assertEqual([i["idea_id"] for i in out], ["i1", "i2"])
        self.assertEqual(out[0]["claim"], "c1")
        self.assertEqual(out[1]["problem_ref"], "p1")  # 非法 ref 回退到第一个问题
        self.assertTrue(out[1]["novelty_hypothesis"])  # 空 hypothesis 补默认

    def test_schema_contract(self):
        self.assertEqual(IDEA_SCHEMA["type"], "object")
        self.assertEqual(IDEA_SCHEMA["required"], ["ideas"])
        req = IDEA_SCHEMA["properties"]["ideas"]["items"]["required"]
        self.assertEqual(req, ["claim", "novelty_hypothesis", "problem_ref", "literature_refs"])


if __name__ == "__main__":
    unittest.main()
