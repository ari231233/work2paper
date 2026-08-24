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
    _arxiv_search,
    _dedup_papers,
    _default_gap_note,
    _s2_search,
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
        _, kwargs = m_http.return_value.get.call_args
        self.assertIn("anomaly detection", kwargs["params"]["search_query"])

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


class SearchLiteratureTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cache_dir = Path(self._tmp.name)

    def tearDown(self):
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


class UtilTest(unittest.TestCase):
    def test_dedup_papers(self):
        papers = [_paper("A"), _paper("a"), _paper("B"), {"no_title": 1}]
        out = _dedup_papers(papers)
        self.assertEqual([p["title"] for p in out], ["A", "B"])

    def test_default_gap_note(self):
        self.assertTrue(_default_gap_note([], []))
        self.assertIn("2", _default_gap_note([_paper("A"), _paper("B")], ["arxiv"]))


if __name__ == "__main__":
    unittest.main()
