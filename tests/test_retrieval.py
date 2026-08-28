"""M5 检索层单测：arXiv/S2 解析、缓存、查询改写循环、降级路径。

用标准库 unittest 编写，`python -m unittest discover -s tests -v` 可运行（兼容 pytest 收集）。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

from papermine import retrieval
from papermine.llm import LLMError, NullProvider
from papermine.retrieval import (
    GAP_SCHEMA,
    QUERY_REWRITE_SCHEMA,
    RELEVANCE_SCHEMA,
    _arxiv_search,
    _dedup_papers,
    _default_gap_note,
    _extract_keywords,
    _filter_relevant,
    _keyword_relevance,
    _openalex_search,
    _crossref_search,
    _dblp_search,
    _s2_search,
    _translate_query,
    search_literature,
)


class _FakeResp:
    """最小 httpx.Response 替身，供 patch httpx.get 用。"""

    def __init__(self, content=b"", status_code=200, json_data=None):
        self.content = content
        self.status_code = status_code
        self._json = json_data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPError("HTTP {}".format(self.status_code))

    def json(self):
        return self._json


ARXIV_ATOM = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n'
    b'<feed xmlns="http://www.w3.org/2005/Atom">\n'
    b'  <entry>\n'
    b'    <id>http://arxiv.org/abs/2301.12345v1</id>\n'
    b'    <title>Anomaly Detection in Time Series</title>\n'
    b'    <summary>We study anomaly detection in industrial time series.</summary>\n'
    b'    <published>2023-01-10T00:00:00Z</published>\n'
    b'    <author><name>Alice</name></author>\n'
    b'    <author><name>Bob</name></author>\n'
    b'  </entry>\n'
    b'</feed>\n'
)


def _paper(title, source="arxiv"):
    return {
        "title": title, "authors": ["A"], "year": 2023, "abstract": "",
        "url": "", "venue": "arXiv", "source": source, "external_id": "",
    }


class _StubLLM:
    """可编程 LLM stub：handler 按 schema 返回，或统一抛错。"""

    def __init__(self, handler=None, error=None):
        self.handler = handler
        self.error = error
        self.calls = []

    def complete(self, system, user, schema, temperature=0.2):
        self.calls.append((system, user, schema, temperature))
        if self.error is not None:
            raise self.error
        if self.handler is None:
            return {}
        return self.handler(system, user, schema, temperature)


class ParsingTest(unittest.TestCase):
    def test_arxiv_search_parses_atom(self):
        with mock.patch.object(retrieval, "_http") as m_http:
            m_http.return_value.get.return_value = _FakeResp(content=ARXIV_ATOM)
            papers = _arxiv_search("anomaly detection")
        self.assertEqual(len(papers), 1)
        p = papers[0]
        self.assertEqual(p["title"], "Anomaly Detection in Time Series")
        self.assertEqual(p["authors"], ["Alice", "Bob"])
        self.assertEqual(p["year"], 2023)
        self.assertEqual(p["source"], "arxiv")
        self.assertEqual(p["external_id"], "2301.12345")
        # M10：首个检索用 ti: 标题字段约束（不再 all:）
        first = m_http.return_value.get.call_args_list[0].kwargs["params"]["search_query"]
        self.assertIn("ti:anomaly", first)
        self.assertIn("ti:detection", first)

    def test_arxiv_field_query_builds_ti_constraint(self):
        q = retrieval._arxiv_field_query("remaining useful life prediction for", "ti")
        self.assertEqual(q, "ti:remaining AND ti:useful AND ti:life AND ti:prediction")

    def test_arxiv_title_fallback_to_abs(self):
        with mock.patch.object(retrieval, "_arxiv_fetch") as m_fetch:
            m_fetch.side_effect = [[], [_paper("Abstract Hit", "arxiv")]]
            papers = _arxiv_search("rare term query")
        self.assertEqual(len(papers), 1)
        self.assertEqual(papers[0]["title"], "Abstract Hit")
        calls = m_fetch.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertIn("ti:", calls[0].args[0])
        self.assertIn("abs:", calls[1].args[0])

    def test_s2_search_parses_json(self):
        data = {"data": [{
            "title": "Deep RUL Prediction",
            "authors": [{"name": "Carol"}, {"name": "Dave"}],
            "abstract": "Abstract here.",
            "year": 2022,
            "venue": "IEEE TII",
            "url": "https://doi.org/10.1/x",
            "externalIds": {"ArXiv": "2201.00001", "DOI": "10.1/x"},
        }]}
        with mock.patch.object(retrieval, "_http") as m_http:
            m_http.return_value.get.return_value = _FakeResp(json_data=data)
            papers = _s2_search("RUL prediction")
        self.assertEqual(len(papers), 1)
        p = papers[0]
        self.assertEqual(p["title"], "Deep RUL Prediction")
        self.assertEqual(p["authors"], ["Carol", "Dave"])
        self.assertEqual(p["source"], "semantic_scholar")
        self.assertEqual(p["external_id"], "2201.00001")
        self.assertEqual(p["venue"], "IEEE TII")

    def test_arxiv_search_http_error_raises(self):
        with mock.patch.object(retrieval, "_http") as m_http:
            m_http.return_value.get.return_value = _FakeResp(status_code=500)
            with self.assertRaises(httpx.HTTPError):
                _arxiv_search("x")

    def test_openalex_search_parses_json(self):
        data = {"results": [{
            "id": "https://openalex.org/W1",
            "doi": "https://doi.org/10.1/shared",
            "display_name": "Robust Flight Control",
            "publication_year": 2024,
            "authorships": [{"author": {"display_name": "Alice"}}],
            "primary_location": {"source": {"display_name": "Control Journal"}},
            "abstract_inverted_index": {"flight": [0], "control": [1]},
        }]}
        with mock.patch.object(retrieval, "_http") as m_http:
            m_http.return_value.get.return_value = _FakeResp(json_data=data)
            paper = _openalex_search("flight control")[0]
        self.assertEqual(paper["title"], "Robust Flight Control")
        self.assertEqual(paper["abstract"], "flight control")
        self.assertEqual(paper["doi"], "10.1/shared")
        self.assertEqual(paper["source"], "openalex")

    def test_crossref_search_parses_json(self):
        data = {"message": {"items": [{
            "title": ["Trajectory Tracking"], "DOI": "10.2/x",
            "author": [{"given": "Bo", "family": "Li"}],
            "published": {"date-parts": [[2023, 1, 1]]},
            "container-title": ["IEEE Access"], "URL": "https://doi.org/10.2/x",
        }]}}
        with mock.patch.object(retrieval, "_http") as m_http:
            m_http.return_value.get.return_value = _FakeResp(json_data=data)
            paper = _crossref_search("trajectory tracking")[0]
        self.assertEqual(paper["authors"], ["Bo Li"])
        self.assertEqual(paper["year"], 2023)
        self.assertEqual(paper["source"], "crossref")

    def test_dblp_search_parses_json(self):
        data = {"result": {"hits": {"hit": [{"info": {
            "title": "Motion Planning", "authors": {"author": [{"text": "C. Wu"}]},
            "year": "2022", "venue": "ICRA", "key": "conf/icra/x",
        }}]}}}
        with mock.patch.object(retrieval, "_http") as m_http:
            m_http.return_value.get.return_value = _FakeResp(json_data=data)
            paper = _dblp_search("motion planning")[0]
        self.assertEqual(paper["authors"], ["C. Wu"])
        self.assertEqual(paper["venue"], "ICRA")
        self.assertEqual(paper["source"], "dblp")


class SearchLiteratureTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cache_dir = Path(self._tmp.name)
        self._extra_source_patches = [
            mock.patch.object(retrieval, "_openalex_search", return_value=[]),
            mock.patch.object(retrieval, "_crossref_search", return_value=[]),
            mock.patch.object(retrieval, "_dblp_search", return_value=[]),
        ]
        for patcher in self._extra_source_patches:
            patcher.start()

    def tearDown(self):
        for patcher in self._extra_source_patches:
            patcher.stop()
        self._tmp.cleanup()

    def test_returns_entry_per_query(self):
        with mock.patch.object(retrieval, "_arxiv_search", return_value=[_paper("Paper A")]), \
             mock.patch.object(retrieval, "_s2_search", return_value=[_paper("Paper B", "semantic_scholar")]):
            entries = search_literature(["q1", "q2"], self.cache_dir, NullProvider())
        self.assertEqual(len(entries), 2)
        for e in entries:
            self.assertIn("query", e)
            self.assertIn("papers", e)
            self.assertIn("gap_note", e)
            self.assertIn("sources", e)
        self.assertEqual(entries[0]["query"], "q1")
        self.assertEqual(len(entries[0]["papers"]), 2)
        self.assertEqual(set(entries[0]["sources"]), {"arxiv", "semantic_scholar"})

    def test_offline_degrades_to_empty_papers(self):
        def boom(q):
            raise httpx.ConnectError("no network")

        with mock.patch.object(retrieval, "_arxiv_search", side_effect=boom), \
             mock.patch.object(retrieval, "_s2_search", side_effect=boom):
            entries = search_literature(["q1"], self.cache_dir, NullProvider())
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["papers"], [])
        self.assertEqual(entries[0]["sources"], [])
        self.assertTrue(entries[0]["gap_note"])  # 离线提示非空

    def test_cache_hit_avoids_network(self):
        with mock.patch.object(retrieval, "_arxiv_search", return_value=[_paper("Cached Paper")]), \
             mock.patch.object(retrieval, "_s2_search", return_value=[]):
            search_literature(["cache q"], self.cache_dir, NullProvider())

        def boom(q):
            raise httpx.ConnectError("no network")

        with mock.patch.object(retrieval, "_arxiv_search", side_effect=boom), \
             mock.patch.object(retrieval, "_s2_search", side_effect=boom):
            entries = search_literature(["cache q"], self.cache_dir, NullProvider())
        self.assertEqual(len(entries[0]["papers"]), 1)
        self.assertEqual(entries[0]["papers"][0]["title"], "Cached Paper")

    def test_expired_cache_misses(self):
        path = retrieval._cache_path(self.cache_dir, "stale q")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "_schema": "literature_cache", "_schema_version": 1,
            "query": "stale q", "fetched_at": 0,
            "papers": [_paper("Old")], "sources": ["arxiv"],
        }), encoding="utf-8")

        def boom(q):
            raise httpx.ConnectError("no network")

        with mock.patch.object(retrieval, "_arxiv_search", side_effect=boom), \
             mock.patch.object(retrieval, "_s2_search", side_effect=boom):
            entries = search_literature(["stale q"], self.cache_dir, NullProvider())
        self.assertEqual(entries[0]["papers"], [])

    def test_query_rewrite_loop(self):
        seen = []

        def arxiv(q):
            seen.append(q)
            return [_paper("Paper for " + q)]

        def s2(q):
            return []

        state = {"rewrites": 0}

        def handler(system, user, schema, temperature=0.2):
            if schema is QUERY_REWRITE_SCHEMA:
                state["rewrites"] += 1
                if state["rewrites"] == 1:
                    return {"rewrite": "better query", "stop": False}
                return {"rewrite": "", "stop": True}
            if schema is RELEVANCE_SCHEMA:
                # 相关性过滤器：把两轮检索合并到的论文都判为相关，验证改写循环合并不丢结果
                papers = json.loads(user).get("papers", [])
                return {"relevant_titles": [p["title"] for p in papers]}
            if schema is GAP_SCHEMA:
                return {"gap_note": "存在缺口"}
            return {}

        llm = _StubLLM(handler=handler)
        with mock.patch.object(retrieval, "_arxiv_search", side_effect=arxiv), \
             mock.patch.object(retrieval, "_s2_search", side_effect=s2):
            entries = search_literature(["original query"], self.cache_dir, llm)

        self.assertIn("original query", seen)
        self.assertIn("better query", seen)
        self.assertEqual(len(entries), 1)
        self.assertEqual(len(entries[0]["papers"]), 2)
        self.assertEqual(entries[0]["gap_note"], "存在缺口")

    def test_null_provider_no_rewrite(self):
        seen = []

        def arxiv(q):
            seen.append(q)
            return [_paper("Paper")]

        def s2(q):
            return []

        with mock.patch.object(retrieval, "_arxiv_search", side_effect=arxiv), \
             mock.patch.object(retrieval, "_s2_search", side_effect=s2):
            search_literature(["q"], self.cache_dir, NullProvider())
        self.assertEqual(seen, ["q"])  # 无 LLM -> 只搜原始查询

    def test_llm_error_does_not_crash(self):
        with mock.patch.object(retrieval, "_arxiv_search", return_value=[_paper("P")]), \
             mock.patch.object(retrieval, "_s2_search", return_value=[]):
            entries = search_literature(["q"], self.cache_dir, _StubLLM(error=LLMError("boom")))
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["papers"][0]["title"], "P")
        self.assertTrue(entries[0]["gap_note"])  # 确定性 gap_note 兜底

    def test_duplicate_queries_deduped(self):
        with mock.patch.object(retrieval, "_arxiv_search", return_value=[]), \
             mock.patch.object(retrieval, "_s2_search", return_value=[]):
            entries = search_literature(["q", "q", " q "], self.cache_dir, NullProvider())
        self.assertEqual(len(entries), 1)


class TranslateTest(unittest.TestCase):
    def test_translate_query_produces_focused_keywords(self):
        llm = _StubLLM(handler=lambda s, u, schema, t: {
            "english_query": "remaining useful life prediction sensor time series",
            "keywords": ["remaining useful life", "prognostics"],
        })
        en, kws = _translate_query(llm, "设备剩余寿命预测")
        self.assertEqual(en, "remaining useful life prediction sensor time series")
        self.assertEqual(kws, ["remaining useful life", "prognostics"])

    def test_translate_query_no_chinese_skips_llm(self):
        llm = _StubLLM(handler=lambda s, u, schema, t: {"english_query": "X", "keywords": []})
        en, kws = _translate_query(llm, "anomaly detection")
        self.assertEqual(en, "anomaly detection")
        self.assertEqual(kws, ["anomaly", "detection"])
        self.assertEqual(llm.calls, [])  # 无中文 -> 不调用 LLM，直接确定性拆词


class RelevanceFilterTest(unittest.TestCase):
    def test_filter_relevant_llm_drops_irrelevant(self):
        papers = [
            _paper("Remaining Useful Life Prediction via LSTM"),
            _paper("The LIFE Space Mission for Exoplanets"),
        ]
        llm = _StubLLM(handler=lambda s, u, schema, t: {
            "relevant_titles": ["Remaining Useful Life Prediction via LSTM"],
        })
        out = _filter_relevant(llm, "remaining useful life prediction", [], papers)
        self.assertEqual([p["title"] for p in out],
                         ["Remaining Useful Life Prediction via LSTM"])

    def test_filter_relevant_keyword_drops_irrelevant(self):
        papers = [
            _paper("Remaining Useful Life Prediction via LSTM"),
            _paper("The LIFE Space Mission for Exoplanets"),
        ]
        out = _filter_relevant(NullProvider(), "remaining useful life prediction", [], papers)
        self.assertEqual([p["title"] for p in out],
                         ["Remaining Useful Life Prediction via LSTM"])

    def test_keyword_relevance_multi_term_cooccurrence(self):
        papers = [_paper("LIFE telescope survey"), _paper("Remaining Useful Life Estimation")]
        kept = _keyword_relevance("remaining useful life", [], papers)
        self.assertEqual(kept, ["Remaining Useful Life Estimation"])

    def test_extract_keywords_drops_stopwords(self):
        self.assertEqual(_extract_keywords("how to improve the RUL prediction for"),
                         ["improve", "rul", "prediction"])


class TieredRetrievalTest(unittest.TestCase):
    def test_high_first_then_partial_fills_target(self):
        papers = []
        for index in range(5):
            paper = _paper("Obstacle Avoidance Flight Control {}".format(index))
            paper["abstract"] = "obstacle avoidance for aircraft flight control"
            papers.append(paper)
        for index in range(5):
            paper = _paper("Aircraft Guidance {}".format(index), "openalex")
            paper["abstract"] = "flight guidance for aircraft"
            papers.append(paper)
        selected = retrieval._select_tiered_papers(
            "obstacle avoidance flight control", [], papers
        )
        self.assertGreaterEqual(len(selected), 8)
        self.assertEqual([p["relevance_level"] for p in selected[:5]], ["high"] * 5)
        self.assertTrue(all(p.get("match_reason") for p in selected))

    def test_expansion_runs_only_until_target(self):
        initial = []
        for index in range(3):
            paper = _paper("Trajectory Tracking Control {}".format(index))
            paper["abstract"] = "trajectory tracking control"
            initial.append(paper)
        extra = []
        for index in range(8):
            paper = _paper("Path Following Guidance {}".format(index), "dblp")
            paper["abstract"] = "trajectory guidance and path following"
            extra.append(paper)
        with mock.patch.object(retrieval, "_retrieve_with_rewrite", return_value=(initial, ["arxiv"])), \
             mock.patch.object(retrieval, "_search_once", return_value=(extra, ["dblp"])) as search:
            papers, sources = retrieval._retrieve_tiered(
                "trajectory tracking control aircraft", [], Path("cache"), NullProvider()
            )
        self.assertGreaterEqual(len(papers), 8)
        self.assertLessEqual(len(papers), retrieval.MAX_PAPERS)
        self.assertIn("dblp", sources)
        self.assertEqual(search.call_count, 1)

    def test_still_insufficient_is_reported_without_duplicates(self):
        only = _paper("Rare Topic Study")
        only["abstract"] = "rare topic"
        with mock.patch.object(retrieval, "_retrieve_with_rewrite", return_value=([only], ["arxiv"])), \
             mock.patch.object(retrieval, "_search_once", return_value=([only], ["arxiv"])):
            papers, _ = retrieval._retrieve_tiered(
                "rare topic research", [], Path("cache"), NullProvider()
            )
        self.assertEqual(len(papers), 1)

    def test_one_source_failure_keeps_other_sources(self):
        hit = _paper("Anomaly Detection", "openalex")
        with mock.patch.object(retrieval, "_arxiv_search", side_effect=RuntimeError("down")), \
             mock.patch.object(retrieval, "_s2_search", return_value=[]), \
             mock.patch.object(retrieval, "_openalex_search", return_value=[hit]), \
             mock.patch.object(retrieval, "_crossref_search", return_value=[]), \
             mock.patch.object(retrieval, "_dblp_search", return_value=[]), \
             tempfile.TemporaryDirectory() as raw:
            papers, sources = retrieval._search_once("anomaly detection", Path(raw))
        self.assertEqual(len(papers), 1)
        self.assertEqual(sources, ["openalex"])


class UtilTest(unittest.TestCase):
    def test_dedup_papers(self):
        papers = [_paper("A"), _paper("a"), _paper("B"), {"no_title": 1}]
        out = _dedup_papers(papers)
        self.assertEqual([p["title"] for p in out], ["A", "B"])

    def test_dedup_papers_merges_cross_source_doi(self):
        first = _paper("Original Title", "semantic_scholar")
        first["doi"] = "10.1/shared"
        second = _paper("Slightly Different Title", "openalex")
        second["doi"] = "https://doi.org/10.1/shared"
        second["abstract"] = "Detailed abstract"
        out = _dedup_papers([first, second])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["abstract"], "Detailed abstract")
        self.assertEqual(out[0]["source_records"], ["semantic_scholar", "openalex"])

    def test_default_gap_note(self):
        self.assertTrue(_default_gap_note([], []))
        self.assertIn("2", _default_gap_note([_paper("A"), _paper("B")], ["arxiv"]))


if __name__ == "__main__":
    unittest.main()
