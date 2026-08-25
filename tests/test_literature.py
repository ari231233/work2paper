"""M5 v2 文献理解/矛盾挖掘/假设生成层单测。

覆盖：确定性降级、LLM 批量理解、矛盾/缺口挖掘（含 contradiction 边）、if-then 假设生成、
结构非法/异常降级、空文献 no-op、gap_id/hypothesis_id 全局唯一。
"""
from __future__ import annotations

import threading
import time
import unittest

from papermine import literature
from papermine.llm import LLMError, NullProvider
from papermine.literature import (
    BATCH_UNDERSTANDING_SCHEMA,
    HYPOTHESIS_SCHEMA,
    MINING_SCHEMA,
    analyze_literature,
)


def _paper(title, abstract="", source="arxiv"):
    return {
        "title": title, "authors": ["A"], "year": 2022, "abstract": abstract,
        "url": "", "venue": "arXiv", "source": source, "external_id": "",
    }


def _entry(papers):
    return {
        "query": "剩余寿命预测", "gap_note": "gap",
        "papers": papers, "sources": ["arxiv"],
    }


class _StubLLM:
    """可编程 LLM stub：handler 按 schema 分派，或统一抛错/返回固定 dict。"""

    def __init__(self, handler=None, error=None, result=None):
        self.handler = handler
        self.error = error
        self.result = result
        self.calls = []

    def complete(self, system, user, schema, temperature=0.2):
        self.calls.append((system, user, schema, temperature))
        if self.error is not None:
            raise self.error
        if self.handler is not None:
            return self.handler(system, user, schema, temperature)
        return self.result if self.result is not None else {}


class AnalyzeLiteratureOfflineTest(unittest.TestCase):
    def test_offline_enriches_papers_and_entries(self):
        lit = [_entry([_paper("LSTM RUL Prediction"), _paper("Isolation Forest Anomaly Detection")])]
        out = analyze_literature(lit, NullProvider())
        self.assertIs(out, lit)
        entry = lit[0]
        for p in entry["papers"]:
            self.assertIn("understanding", p)
            self.assertTrue(p["understanding"]["claim"])
        graph = entry["contradiction_graph"]
        self.assertIn("nodes", graph)
        self.assertIn("edges", graph)
        self.assertIn("gaps", graph)
        self.assertGreaterEqual(len(graph["gaps"]), 1)
        self.assertGreaterEqual(len(entry["hypotheses"]), 1)

    def test_offline_deterministic_hypothesis_is_if_then(self):
        lit = [_entry([_paper("A Method for RUL")])]
        analyze_literature(lit, NullProvider())
        h = lit[0]["hypotheses"][0]
        self.assertTrue(h["statement"].strip())
        self.assertTrue(h["falsification"].strip())
        self.assertIn("若", h["statement"])  # if-then 形式（中文）

    def test_empty_literature_noop(self):
        self.assertEqual(analyze_literature([], NullProvider()), [])

    def test_gap_and_hypothesis_ids_globally_unique(self):
        lit = [
            _entry([_paper("Paper A1")]),
            _entry([_paper("Paper B1")]),
        ]
        analyze_literature(lit, NullProvider())
        gap_ids = [g["gap_id"] for e in lit for g in e["contradiction_graph"]["gaps"]]
        hyp_ids = [h["hypothesis_id"] for e in lit for h in e["hypotheses"]]
        self.assertEqual(gap_ids, ["g1", "g2"])
        self.assertEqual(hyp_ids, ["h1", "h2"])
        self.assertEqual(len(set(gap_ids)), len(gap_ids))


class AnalyzeLiteratureLLMTest(unittest.TestCase):
    def test_llm_batch_understanding_applied(self):
        def handler(system, user, schema, temperature=0.2):
            if schema is BATCH_UNDERSTANDING_SCHEMA:
                return {"papers": [{
                    "title": "LSTM RUL Prediction",
                    "understanding": {
                        "claim": "LSTM 可预测 RUL", "method": "LSTM",
                        "conclusion": "优于基线", "applicability": "小样本时序",
                        "limitations": "长序列退化",
                    },
                }]}
            return {}
        lit = [_entry([_paper("LSTM RUL Prediction", abstract="x")])]
        analyze_literature(lit, _StubLLM(handler=handler))
        u = lit[0]["papers"][0]["understanding"]
        self.assertEqual(u["method"], "LSTM")
        self.assertEqual(u["applicability"], "小样本时序")

    def test_llm_mines_contradiction_and_gap(self):
        papers = [_paper("Paper A"), _paper("Paper B")]

        def handler(system, user, schema, temperature=0.2):
            if schema is MINING_SCHEMA:
                return {
                    "gaps": [{
                        "claim_point": "数据漂移下异常检测",
                        "description": "现有工作都假设静态分布，缺数据漂移角度",
                        "angle": "数据漂移",
                    }],
                    "contradictions": [{
                        "claim_point": "异常检测是否需要标注",
                        "description": "A 主张无需标注，B 主张需要标注",
                        "paper_a": "Paper A", "paper_b": "Paper B",
                    }],
                }
            if schema is HYPOTHESIS_SCHEMA:
                return {"hypotheses": [
                    {"if_then": "若引入漂移自适应机制，则检测精度提升", "falsification": "精度无显著提升则证伪"},
                    {"if_then": "若半监督标注，则成本下降", "falsification": "成本不降则证伪"},
                ]}
            return {}
        lit = [_entry(papers)]
        analyze_literature(lit, _StubLLM(handler=handler))
        graph = lit[0]["contradiction_graph"]
        gaps = graph["gaps"]
        self.assertEqual([g["type"] for g in gaps], ["gap", "contradiction"])
        # contradiction 型 gap 挂两篇论文，且生成一条 contradiction 边
        cont = [g for g in gaps if g["type"] == "contradiction"][0]
        self.assertEqual(cont["paper_refs"], ["Paper A", "Paper B"])
        self.assertEqual(len(graph["edges"]), 1)
        self.assertEqual(graph["edges"][0]["kind"], "contradiction")
        # 两条 gap → 两条假设，各自回指 gap
        hyps = lit[0]["hypotheses"]
        self.assertEqual(len(hyps), 2)
        self.assertEqual([h["gap_ref"] for h in hyps], [g["gap_id"] for g in gaps])
        self.assertIn("若", hyps[0]["statement"])

    def test_llm_error_falls_back_deterministic(self):
        lit = [_entry([_paper("Paper A")])]
        analyze_literature(lit, _StubLLM(error=LLMError("boom")))
        entry = lit[0]
        self.assertIn("understanding", entry["papers"][0])
        self.assertGreaterEqual(len(entry["contradiction_graph"]["gaps"]), 1)
        self.assertGreaterEqual(len(entry["hypotheses"]), 1)

    def test_invalid_llm_shape_falls_back_deterministic(self):
        # LLM 返回不匹配任何 schema 的固定 dict（模拟 schema 逃逸），应全部降级且不崩
        lit = [_entry([_paper("Paper A")])]
        analyze_literature(lit, _StubLLM(result={"unexpected": True}))
        entry = lit[0]
        self.assertTrue(entry["papers"][0]["understanding"]["claim"])
        self.assertGreaterEqual(len(entry["contradiction_graph"]["gaps"]), 1)

    def test_llm_hallucinated_contradiction_dropped(self):
        papers = [_paper("Real Paper A")]

        def handler(system, user, schema, temperature=0.2):
            if schema is MINING_SCHEMA:
                return {
                    "gaps": [],
                    "contradictions": [{
                        "claim_point": "x", "description": "y",
                        "paper_a": "Real Paper A", "paper_b": "Fake Paper",
                    }],
                }
            return {}
        lit = [_entry(papers)]
        analyze_literature(lit, _StubLLM(handler=handler))
        # 幻觉论文标题的 contradiction 被丢弃 → 无 contradiction gap、无 edge
        graph = lit[0]["contradiction_graph"]
        self.assertEqual(graph["edges"], [])
        self.assertEqual([g for g in graph["gaps"] if g["type"] == "contradiction"], [])


class _ConcurrentStubLLM:
    """线程安全的 LLM stub：返回空 dict（触发确定性降级），并统计最大并发调用数。"""

    def __init__(self, sleep=0.05):
        self._lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self._sleep = sleep

    def complete(self, system, user, schema, temperature=0.2):
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(self._sleep)
            return {}
        finally:
            with self._lock:
                self.active -= 1


class AnalyzeLiteratureParallelTest(unittest.TestCase):
    """M16 方向⑥：多篇论文（多条文献条目）的理解/挖掘/假设生成跨条目并行，ID 仍全局唯一可复现。"""

    def test_parallel_across_entries_preserves_global_ids(self):
        lit = [
            _entry([_paper("Paper A")]),
            _entry([_paper("Paper B")]),
            _entry([_paper("Paper C")]),
        ]
        llm = _ConcurrentStubLLM()
        analyze_literature(lit, llm)

        self.assertGreater(llm.max_active, 1)   # 证明跨条目并行
        gap_ids = [g["gap_id"] for e in lit for g in e["contradiction_graph"]["gaps"]]
        hyp_ids = [h["hypothesis_id"] for e in lit for h in e["hypotheses"]]
        self.assertEqual(gap_ids, ["g1", "g2", "g3"])
        self.assertEqual(hyp_ids, ["h1", "h2", "h3"])


if __name__ == "__main__":
    unittest.main()
