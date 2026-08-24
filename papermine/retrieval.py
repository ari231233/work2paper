"""③ 领域知识检索：arXiv + Semantic Scholar（httpx）+ 查询改写循环 + 本地缓存 + 优雅降级。

对应 docs/build-plan.md §4 M5 与 docs/architecture.md §5 ③：

- 检索源：arXiv API（Atom XML）+ Semantic Scholar Graph API（JSON），优先级 arXiv / Semantic Scholar。
- agentic：查询改写循环（LLM 改写 query，最多 ``MAX_ROUNDS`` 轮检索）。
- 缓存：结果按 query 缓存到 ``literature_cache/``，带 ``fetched_at`` + TTL（engineering.md §2.3）。
- 降级：网络失败 → 该源跳过、papers 留空、gap_note 标离线，绝不抛异常影响确定性环节。

冻结接口（docs/build-plan.md §3.3 / §4 M5）：

    def search_literature(queries, cache_dir, llm) -> list[dict]

每个返回条目与 docs/architecture.md §4 的 ``literature[]`` 对齐：

    { "query": "...", "papers": [...], "gap_note": "...", "sources": ["arxiv", "semantic_scholar"] }
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from xml.etree import ElementTree

import httpx

from .config import apply_proxy
from .llm import LLMError, LLMProvider, SchemaError

__all__ = [
    "search_literature",
    "ARXIV_API",
    "S2_API",
    "MAX_ROUNDS",
    "DEFAULT_TTL_SECONDS",
    "QUERY_REWRITE_SCHEMA",
    "GAP_SCHEMA",
    "_arxiv_search",
    "_s2_search",
    "_search_once",
    "_llm_rewrite",
    "_llm_gap_note",
    "_default_gap_note",
    "_dedup_papers",
    "_cache_read",
    "_cache_write",
]

# ---- 常量 ----
ARXIV_API = "https://export.arxiv.org/api/query"
S2_API = "https://api.semanticscholar.org/graph/v1/paper/search"
S2_FIELDS = "title,authors,abstract,year,externalIds,url,venue"

MAX_RESULTS = 5          # 每个源每轮最多取回论文数
TIMEOUT = 20.0           # 单次 HTTP 超时（秒）
MAX_ROUNDS = 3           # 查询改写循环最大轮数（含首轮原始查询）
DEFAULT_TTL_SECONDS = 7 * 24 * 3600   # 文献缓存 TTL：7 天

# Semantic Scholar 429 限流重试（公开 API 无 key 时易触发）
S2_MAX_RETRIES = 2
S2_RETRY_BACKOFF = 1.5

# 缓存文件内嵌 schema 名 / 版本（区别于 dossier）
CACHE_SCHEMA = "literature_cache"
CACHE_SCHEMA_VERSION = 1

_ATOM = "{http://www.w3.org/2005/Atom}"


# ---- 结构化输出契约（校验走 papermine/llm.py 的极简子集）----

QUERY_REWRITE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["rewrite", "stop"],
    "properties": {
        "rewrite": {"type": "string"},
        "stop": {"type": "boolean"},
    },
}

GAP_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["gap_note"],
    "properties": {
        "gap_note": {"type": "string"},
    },
}

TRANSLATE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["english_query"],
    "properties": {
        "english_query": {"type": "string"},
    },
}


# ---- 通用小工具 ----

def _clean_ws(s: Any) -> str:
    """把任意文本折叠成单行（去首尾/合并空白），供摘要与标题清洗。"""
    if s is None:
        return ""
    return " ".join(str(s).split())


def _dedup_strings(items: Any) -> List[str]:
    """去重保序地清洗字符串列表（丢弃空串 / 非字符串）。"""
    out: List[str] = []
    seen: set = set()
    for it in items or []:
        s = _clean_ws(it)
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _paper_key(paper: Dict[str, Any]) -> str:
    """论文去重键：归一化标题（小写）。"""
    return _clean_ws(paper.get("title")).lower()


def _dedup_papers(papers: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按标题去重（无标题的论文无法被引用，丢弃）。"""
    out: List[Dict[str, Any]] = []
    seen: set = set()
    for p in papers or []:
        if not isinstance(p, dict):
            continue
        key = _paper_key(p)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def _year_from(text: Any) -> Optional[int]:
    m = re.search(r"(\d{4})", str(text or ""))
    return int(m.group(1)) if m else None


def _arxiv_id(url: Any) -> str:
    """从 arXiv 链接提取纯 ID（去版本号）。"""
    if not url:
        return ""
    m = re.search(r"arxiv\.org/abs/([^/\s]+)", str(url))
    if not m:
        return ""
    return re.sub(r"v\d+$", "", m.group(1))


# ---- arXiv 检索（Atom XML）----

def _text(elem: Any, tag: str) -> str:
    node = elem.find(tag)
    if node is None:
        return ""
    return _clean_ws("".join(node.itertext()))


def _arxiv_search(query: str, max_results: int = MAX_RESULTS,
                  timeout: float = TIMEOUT) -> List[Dict[str, Any]]:
    """arXiv API 检索，返回标准化论文列表；网络/HTTP 错误向上抛（由上层降级）。"""
    params = {
        "search_query": "all:" + query,
        "start": 0,
        "max_results": max_results,
        "sortBy": "relevance",
    }
    resp = httpx.get(ARXIV_API, params=params, timeout=timeout)
    resp.raise_for_status()

    try:
        root = ElementTree.fromstring(resp.content)
    except ElementTree.ParseError:
        return []

    papers: List[Dict[str, Any]] = []
    for entry in root.findall(_ATOM + "entry"):
        title = _text(entry, _ATOM + "title")
        if not title:
            continue
        authors = [
            _clean_ws(a.findtext(_ATOM + "name"))
            for a in entry.findall(_ATOM + "author")
        ]
        authors = [a for a in authors if a]
        url = _text(entry, _ATOM + "id")
        papers.append({
            "title": title,
            "authors": authors,
            "year": _year_from(_text(entry, _ATOM + "published")),
            "abstract": _text(entry, _ATOM + "summary"),
            "url": url,
            "venue": "arXiv",
            "source": "arxiv",
            "external_id": _arxiv_id(url),
        })
    return papers


# ---- Semantic Scholar 检索（JSON）----

def _s2_search(query: str, max_results: int = MAX_RESULTS,
               timeout: float = TIMEOUT) -> List[Dict[str, Any]]:
    """Semantic Scholar Graph API 检索，返回标准化论文列表；网络/HTTP 错误向上抛。

    429（限流）时做有限次退避重试，仍失败则抛错由上层降级。
    """
    params = {"query": query, "fields": S2_FIELDS, "limit": max_results}
    for attempt in range(S2_MAX_RETRIES + 1):
        resp = httpx.get(S2_API, params=params, timeout=timeout)
        if resp.status_code == 429 and attempt < S2_MAX_RETRIES:
            time.sleep(S2_RETRY_BACKOFF * (attempt + 1))
            continue
        resp.raise_for_status()
        break

    data = resp.json()
    if not isinstance(data, dict):
        return []

    papers: List[Dict[str, Any]] = []
    for item in data.get("data") or []:
        if not isinstance(item, dict):
            continue
        title = _clean_ws(item.get("title"))
        if not title:
            continue
        authors = [
            _clean_ws(a.get("name"))
            for a in (item.get("authors") or [])
            if isinstance(a, dict) and a.get("name")
        ]
        ext = item.get("externalIds") or {}
        papers.append({
            "title": title,
            "authors": authors,
            "year": item.get("year"),
            "abstract": _clean_ws(item.get("abstract")),
            "url": item.get("url") or "",
            "venue": _clean_ws(item.get("venue")),
            "source": "semantic_scholar",
            "external_id": ext.get("ArXiv") or ext.get("DOI") or "",
        })
    return papers


# ---- 本地缓存（literature_cache/）----

def _cache_key(query: str) -> str:
    return hashlib.sha1(_clean_ws(query).lower().encode("utf-8")).hexdigest()


def _cache_path(cache_dir: Any, query: str) -> Path:
    return Path(cache_dir) / ("q_" + _cache_key(query) + ".json")


def _cache_read(query: str, cache_dir: Any,
                ttl: int = DEFAULT_TTL_SECONDS) -> Optional[Dict[str, Any]]:
    """命中且未过期返回 {papers, sources}，否则 None。"""
    path = _cache_path(cache_dir, query)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    if time.time() - float(data.get("fetched_at", 0)) > ttl:
        return None
    papers, sources = data.get("papers"), data.get("sources")
    if not isinstance(papers, list) or not isinstance(sources, list):
        return None
    return {"papers": papers, "sources": sources}


def _cache_write(query: str, cache_dir: Any,
                 papers: List[Dict[str, Any]], sources: List[str]) -> None:
    """写入缓存（原子替换）；失败静默，不影响主流程。"""
    path = _cache_path(cache_dir, query)
    try:
        Path(cache_dir).mkdir(parents=True, exist_ok=True)
        payload = {
            "_schema": CACHE_SCHEMA,
            "_schema_version": CACHE_SCHEMA_VERSION,
            "query": query,
            "fetched_at": time.time(),
            "papers": papers,
            "sources": sources,
        }
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


# ---- 单轮检索（缓存优先，双源 best-effort）----

def _search_once(query: str, cache_dir: Any,
                 ttl: int = DEFAULT_TTL_SECONDS) -> tuple:
    """对单个 query 做一轮检索（缓存命中优先）。

    返回 ``(papers, sources)``。任一源失败只跳过该源，绝不抛异常。
    仅当至少一个源成功响应时才写缓存，避免把「断网空结果」缓存 7 天。
    """
    cached = _cache_read(query, cache_dir, ttl)
    if cached is not None:
        return cached["papers"], cached["sources"]

    papers: List[Dict[str, Any]] = []
    sources: List[str] = []
    for name, fn in (("arxiv", _arxiv_search), ("semantic_scholar", _s2_search)):
        try:
            batch = fn(query)
        except Exception:
            # 网络 / 限流 / 解析失败：该源降级跳过，不崩整个检索
            continue
        if isinstance(batch, list):
            papers.extend(batch)
            sources.append(name)

    papers = _dedup_papers(papers)
    if sources:
        _cache_write(query, cache_dir, papers, sources)
    return papers, sources


# ---- 查询改写循环（LLM，最多 MAX_ROUNDS 轮）----

_REWRITE_SYSTEM = (
    "你是 papermine 的「检索查询改写器」。给定一个检索查询和它命中的部分论文，"
    "判断是否需要改写查询以提升相关性，并给出改写后的查询。\n"
    "规则：\n"
    "1. 查询应使用学术英文关键词（必要时把中文/口语翻译为规范的英文术语）；\n"
    "2. 若当前结果已高度相关或无需再改，设置 stop=true；\n"
    "3. 若需改写，给出更聚焦/更规范的查询并设置 stop=false。\n"
    "只输出 JSON，严格满足给定 schema。"
)


def _llm_rewrite(llm: Optional[LLMProvider], query: str,
                 papers: List[Dict[str, Any]]) -> Optional[str]:
    """让 LLM 判断相关性并改写查询；返回新查询，或 None（停止 / 无 LLM / 失败）。"""
    if llm is None or not papers:
        return None
    user = json.dumps({
        "query": query,
        "top_results": [
            {"title": p.get("title", ""), "abstract": (p.get("abstract") or "")[:300]}
            for p in papers[:5]
        ],
    }, ensure_ascii=False)
    try:
        result = llm.complete(_REWRITE_SYSTEM, user, QUERY_REWRITE_SCHEMA, temperature=0.3)
    except (LLMError, SchemaError):
        return None
    if not isinstance(result, dict):
        return None
    if result.get("stop") is True:
        return None
    rewrite = _clean_ws(result.get("rewrite"))
    if not rewrite or rewrite.lower() == _clean_ws(query).lower():
        return None
    return rewrite


_TRANSLATE_SYSTEM = (
    "你是 papermine 的「检索查询翻译器」。把给定的中文查询翻译成规范的英文学术检索关键词"
    "（短语即可，不要完整句子，不要解释）。只输出 JSON，严格满足给定 schema。"
)


def _translate_query(llm: Optional[LLMProvider], query: str) -> str:
    """把含中文的查询翻译成英文学术关键词；无中文 / 无 LLM / 失败时原样返回。"""
    if llm is None:
        return query
    if not any("\u4e00" <= ch <= "\u9fff" for ch in query):
        return query
    user = json.dumps({"query": query}, ensure_ascii=False)
    try:
        result = llm.complete(_TRANSLATE_SYSTEM, user, TRANSLATE_SCHEMA, temperature=0.2)
    except (LLMError, SchemaError):
        return query
    if not isinstance(result, dict):
        return query
    en = _clean_ws(result.get("english_query"))
    return en if en else query


def _retrieve_with_rewrite(query: str, cache_dir: Any, llm: Optional[LLMProvider]) -> tuple:
    """对一个查询跑查询改写循环（最多 MAX_ROUNDS 轮），合并去重各轮论文。"""
    merged: Dict[str, Dict[str, Any]] = {}
    sources: List[str] = []
    current = _translate_query(llm, query)
    for round_no in range(1, MAX_ROUNDS + 1):
        papers, srcs = _search_once(current, cache_dir)
        for p in papers:
            merged.setdefault(_paper_key(p), p)
        for s in srcs:
            if s not in sources:
                sources.append(s)
        if round_no == MAX_ROUNDS:
            break
        nxt = _llm_rewrite(llm, current, papers)
        if not nxt:
            break
        current = nxt
    return list(merged.values()), sources


# ---- gap_note ----

_GAP_SYSTEM = (
    "你是 papermine 的「文献 gap 分析器」。给定检索查询与命中的论文，"
    "用 1~3 句中文概括：这些工作做了什么，与查询目标相比仍有哪些缺口 / 未被覆盖的角度。"
    "只基于给定论文，不得编造不存在的文献。只输出 JSON，严格满足给定 schema。"
)


def _llm_gap_note(llm: Optional[LLMProvider], query: str,
                  papers: List[Dict[str, Any]]) -> Optional[str]:
    """让 LLM 产出 gap_note；无 LLM / 无论文 / 失败返回 None。"""
    if llm is None or not papers:
        return None
    user = json.dumps({
        "query": query,
        "papers": [
            {"title": p.get("title", ""), "abstract": (p.get("abstract") or "")[:400],
             "venue": p.get("venue", "")}
            for p in papers[:8]
        ],
    }, ensure_ascii=False)
    try:
        result = llm.complete(_GAP_SYSTEM, user, GAP_SCHEMA, temperature=0.2)
    except (LLMError, SchemaError):
        return None
    if not isinstance(result, dict):
        return None
    note = _clean_ws(result.get("gap_note"))
    return note or None


def _default_gap_note(papers: List[Dict[str, Any]], sources: List[str]) -> str:
    """无 LLM 时的确定性 gap_note（离线保底）。"""
    if papers:
        return "检索到 {} 篇相关文献（来源：{}）。未做语义级 gap 对比，需人工核验与项目问题的差异。".format(
            len(papers), "、".join(sources) if sources else "未知"
        )
    return "（离线/无结果）未检索到可用文献，novelty 无法对照，需人工补检索。"


# ---- 冻结接口 ----

def search_literature(queries, cache_dir, llm) -> List[dict]:
    """对一组查询做检索（含查询改写循环 + 缓存 + 降级），返回 literature[]。

    冻结契约（docs/build-plan.md §3.3 / §4 M5）：

        def search_literature(queries, cache_dir, llm) -> list[dict]

    - 每个查询返回一条 ``{query, papers, gap_note, sources}``；
    - 网络不可用时 papers 留空、gap_note 标离线，不抛异常；
    - ``llm`` 为 None 或 NullProvider 时跳过改写与 LLM gap，走确定性 gap_note。
    """
    apply_proxy()
    entries: List[dict] = []
    for query in _dedup_strings(queries):
        papers, sources = _retrieve_with_rewrite(query, cache_dir, llm)
        gap_note = _llm_gap_note(llm, query, papers) or _default_gap_note(papers, sources)
        entries.append({
            "query": query,
            "papers": papers,
            "gap_note": gap_note,
            "sources": sorted(set(sources)),
        })
    return entries
