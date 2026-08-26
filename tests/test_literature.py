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
    BATCH_EVIDENCE_CARD_SCHEMA,
    BATCH_UNDERSTANDING_SCHEMA,
    EVIDENCE_CARD_SCHEMA,
    EVIDENCE_LEVELS,
    GAP_HYPOTHESIS_SCHEMA,
    HYPOTHESIS_SCHEMA,
    MINING_SCHEMA,
    _compute_evidence_level,
    _evidence_source_for,
    _soften_universal,
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


class PaperEvidenceCardTest(unittest.TestCase):
    """M19：论文级证据卡——字段要么有值要么 null，evidence_source 正确，无编造的 baseline/gain。"""

    def test_offline_every_paper_gets_8_field_card(self):
        lit = [_entry([_paper("LSTM RUL Prediction"), _paper("Isolation Forest Anomaly Detection")])]
        analyze_literature(lit, NullProvider())
        for p in lit[0]["papers"]:
            card = p["evidence_card"]
            self.assertEqual(
                sorted(card.keys()),
                sorted(["title", "dataset", "baseline", "metric",
                        "main_gain", "limitation", "claim_strength", "evidence_source"]),
            )
            self.assertEqual(card["title"], p["title"])       # title 恒取真实标题
            self.assertEqual(card["evidence_source"], "abstract")  # v1 仅摘要
            # 无 LLM：baseline/main_gain/limitation/claim_strength 一律 null（绝不编造）
            self.assertIsNone(card["baseline"])
            self.assertIsNone(card["main_gain"])
            self.assertIsNone(card["limitation"])
            self.assertIsNone(card["claim_strength"])
            # dataset/metric：要么 None 要么命中保守词典
            self.assertTrue(card["dataset"] is None or isinstance(card["dataset"], str))
            self.assertTrue(card["metric"] is None or isinstance(card["metric"], str))

    def test_offline_no_fabricated_baseline_gain(self):
        # 摘要明确出现基线/方法词（SVM/LSTM），但确定性降级仍不得编造 baseline/main_gain
        abstract = "We compare LSTM against SVM and report a large improvement."
        lit = [_entry([_paper("LSTM vs SVM", abstract=abstract)])]
        analyze_literature(lit, NullProvider())
        card = lit[0]["papers"][0]["evidence_card"]
        self.assertIsNone(card["baseline"])
        self.assertIsNone(card["main_gain"])

    def test_offline_extracts_dataset_and_metric_conservatively(self):
        abstract = "We evaluate on the C-MAPSS dataset and report RMSE and accuracy."
        lit = [_entry([_paper("RUL Model", abstract=abstract)])]
        analyze_literature(lit, NullProvider())
        card = lit[0]["papers"][0]["evidence_card"]
        self.assertIn("C-MAPSS", card["dataset"])
        self.assertIn("RMSE", card["metric"])
        self.assertIn("accuracy", card["metric"])

    def test_evidence_source_for_levels(self):
        self.assertEqual(_evidence_source_for({}), "abstract")
        self.assertEqual(_evidence_source_for({"fulltext": "全文……"}), "fulltext")
        self.assertEqual(_evidence_source_for({"tables": [{"x": 1}]}), "table")
        # table 优先于 fulltext（表格是最强的证据层级）
        self.assertEqual(_evidence_source_for({"fulltext": "x", "tables": [{}]}), "table")

    def test_llm_evidence_card_extracted_and_source_overridden(self):
        def handler(system, user, schema, temperature=0.2):
            if schema is BATCH_EVIDENCE_CARD_SCHEMA:
                return {"papers": [{
                    "title": "LSTM RUL Prediction",
                    "evidence_card": {
                        "title": "LSTM RUL Prediction",
                        "dataset": "C-MAPSS",
                        "baseline": "SVM",
                        "metric": "RMSE",
                        "main_gain": "RMSE 相对基线降低 10%",
                        "limitation": None,
                        "claim_strength": "moderate",
                        # LLM 乱填来源层级，应被系统按论文真实证据覆盖为 abstract
                        "evidence_source": "fulltext",
                    },
                }]}
            return {}
        lit = [_entry([_paper("LSTM RUL Prediction", abstract="x")])]
        analyze_literature(lit, _StubLLM(handler=handler))
        card = lit[0]["papers"][0]["evidence_card"]
        self.assertEqual(card["title"], "LSTM RUL Prediction")
        self.assertEqual(card["dataset"], "C-MAPSS")
        self.assertEqual(card["baseline"], "SVM")
        self.assertEqual(card["metric"], "RMSE")
        self.assertEqual(card["main_gain"], "RMSE 相对基线降低 10%")
        self.assertIsNone(card["limitation"])
        self.assertEqual(card["claim_strength"], "moderate")
        self.assertEqual(card["evidence_source"], "abstract")

    def test_llm_null_fields_preserved_and_invalid_claim_strength_nulled(self):
        def handler(system, user, schema, temperature=0.2):
            if schema is BATCH_EVIDENCE_CARD_SCHEMA:
                return {"papers": [{
                    "title": "Paper A",
                    "evidence_card": {
                        "title": "Paper A",
                        "dataset": None,
                        "baseline": None,
                        "metric": None,
                        "main_gain": None,
                        "limitation": None,
                        "claim_strength": "超强",  # 非法枚举 → null
                        "evidence_source": "abstract",
                    },
                }]}
            return {}
        lit = [_entry([_paper("Paper A")])]
        analyze_literature(lit, _StubLLM(handler=handler))
        card = lit[0]["papers"][0]["evidence_card"]
        self.assertIsNone(card["dataset"])
        self.assertIsNone(card["baseline"])
        self.assertIsNone(card["metric"])
        self.assertIsNone(card["main_gain"])
        self.assertIsNone(card["limitation"])
        self.assertIsNone(card["claim_strength"])
        self.assertEqual(card["evidence_source"], "abstract")

    def test_llm_card_title_must_match_real_paper(self):
        # LLM 返回的标题与真实论文不一致时，该证据卡被丢弃 → 走确定性降级（title 仍取真实标题）
        def handler(system, user, schema, temperature=0.2):
            if schema is BATCH_EVIDENCE_CARD_SCHEMA:
                return {"papers": [{
                    "title": "Fake Title",
                    "evidence_card": {
                        "title": "Fake Title", "dataset": "MNIST", "baseline": "X",
                        "metric": "accuracy", "main_gain": "提升", "limitation": None,
                        "claim_strength": "strong", "evidence_source": "abstract",
                    },
                }]}
            return {}
        lit = [_entry([_paper("Real Paper")])]
        analyze_literature(lit, _StubLLM(handler=handler))
        card = lit[0]["papers"][0]["evidence_card"]
        self.assertEqual(card["title"], "Real Paper")   # 恒取真实标题
        self.assertEqual(card["evidence_source"], "abstract")
        self.assertIsNone(card["baseline"])             # 幻觉标题的卡片被丢弃 → 确定性降级
        self.assertIsNone(card["main_gain"])

    def test_evidence_card_schema_contract(self):
        self.assertEqual(EVIDENCE_CARD_SCHEMA["type"], "object")
        self.assertEqual(sorted(EVIDENCE_CARD_SCHEMA["required"]), sorted([
            "title", "dataset", "baseline", "metric",
            "main_gain", "limitation", "claim_strength", "evidence_source",
        ]))
        self.assertEqual(
            EVIDENCE_CARD_SCHEMA["properties"]["evidence_source"]["enum"],
            ["abstract", "fulltext", "table"],
        )


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


class EvidenceLevelTest(unittest.TestCase):
    """M18：gap 从「事实断言」改为「证据有界的假设」（gap_hypothesis）+ 证据级别 evidence_level。"""

    def test_gap_records_are_hypothesis_form(self):
        lit = [_entry([_paper("Paper A")])]
        analyze_literature(lit, NullProvider())
        gaps = [g for g in lit[0]["contradiction_graph"]["gaps"] if g["type"] == "gap"]
        self.assertGreaterEqual(len(gaps), 1)
        gh = gaps[0]["gap_hypothesis"]
        self.assertEqual(sorted(gh.keys()), sorted(["claim", "evidence_level", "basis", "scope"]))
        # claim 恒为「尚未发现…（假设，非事实）」假设形式
        self.assertTrue(gh["claim"].startswith("尚未发现"))
        self.assertIn("假设，非事实", gh["claim"])
        # basis / scope 界定证据边界
        self.assertIn("基于检索到的", gh["basis"])
        self.assertIn("检索范围", gh["scope"])
        self.assertIn(gh["evidence_level"], EVIDENCE_LEVELS)
        # 无全称断言（如「领域无人做」）
        self.assertNotIn("领域无人", gh["claim"])
        self.assertNotIn("领域无人", gh["basis"])
        self.assertNotIn("整个领域", gh["claim"])

    def test_contradiction_is_positive_evidence_strong(self):
        papers = [_paper("Paper A"), _paper("Paper B")]

        def handler(system, user, schema, temperature=0.2):
            if schema is MINING_SCHEMA:
                return {
                    "gaps": [],
                    "contradictions": [{
                        "claim_point": "异常检测是否需要标注",
                        "description": "A 主张无需标注，B 主张需要标注",
                        "paper_a": "Paper A", "paper_b": "Paper B",
                    }],
                }
            return {}
        lit = [_entry(papers)]
        analyze_literature(lit, _StubLLM(handler=handler))
        cont = [g for g in lit[0]["contradiction_graph"]["gaps"]
                if g["type"] == "contradiction"][0]
        # 矛盾 = 正证据（有反例），证据级别天然 strong
        self.assertEqual(cont["evidence_level"], "strong")

    def test_compute_evidence_level_reflects_evidence_amount(self):
        # 样本量 + 系统性 + 相关性 + 反例共同决定（docs/build-plan.md §4 M18 要点 2）
        self.assertEqual(_compute_evidence_level(0, 1, 0), "weak")
        self.assertEqual(_compute_evidence_level(1, 1, 1), "weak")        # 样本不足
        self.assertEqual(_compute_evidence_level(3, 1, 3), "moderate")    # 中等样本
        self.assertEqual(_compute_evidence_level(8, 2, 3), "strong")      # 大样本 + 双源 + 相关
        self.assertEqual(_compute_evidence_level(8, 2, 1), "moderate")    # 样本多但相关少 → 降档
        self.assertEqual(_compute_evidence_level(5, 2, 5), "strong")      # 双源 + 全相关
        self.assertEqual(_compute_evidence_level(8, 2, 3, counterexample=True), "weak")  # 有反例 → 削弱

    def test_soften_universal_assertions(self):
        softened = _soften_universal("整个领域没人做这件事")
        self.assertIn("假设，非事实", softened)
        self.assertNotIn("没人做", softened)
        self.assertNotIn("整个领域", softened)
        # 无全称断言的文本不改动
        clean = "基于检索到的论文，未发现统一框架"
        self.assertEqual(_soften_universal(clean), clean)

    def test_gap_hypothesis_schema_contract(self):
        self.assertEqual(GAP_HYPOTHESIS_SCHEMA["type"], "object")
        self.assertEqual(
            sorted(GAP_HYPOTHESIS_SCHEMA["required"]),
            sorted(["claim", "evidence_level", "basis", "scope"]),
        )
        self.assertEqual(
            GAP_HYPOTHESIS_SCHEMA["properties"]["evidence_level"]["enum"],
            list(EVIDENCE_LEVELS),
        )


if __name__ == "__main__":
    unittest.main()
