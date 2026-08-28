"""③ 领域知识检索：五个公开来源 + 分层召回 + 查询改写 + 缓存 + 优雅降级。

对应 docs/build-plan.md §4 M5 与 docs/architecture.md §5 ③：

- 检索源：arXiv API（Atom XML）+ Semantic Scholar Graph API（JSON），优先级 arXiv / Semantic Scholar。
- agentic：查询改写循环（LLM 改写 query，最多 ``MAX_ROUNDS`` 轮检索）。
- 缓存：结果按 query 缓存到 ``literature_cache/``，带 ``fetched_at`` + TTL（engineering.md §2.3）。
- 降级：网络失败 → 该源跳过、papers 留空、gap_note 标离线，绝不抛异常影响确定性环节。
- 相关性优化（M10）：翻译产出聚焦学术关键词（核心术语组合）；arXiv 标题字段约束（``ti:`` 优先、
  ``abs:`` 回退）；检索后相关性过滤（LLM 判相关，降级为关键词匹配），减少 *Byzantine SGD* /
  *dark energy* 一类词面误命中（lessons-learned §2.4）。

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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional
from xml.etree import ElementTree

import httpx

from .config import apply_proxy
from .llm import LLMError, LLMProvider, SchemaError, complete_fast

__all__ = [
    "search_literature",
    "ARXIV_API",
    "S2_API",
    "OPENALEX_API",
    "CROSSREF_API",
    "DBLP_API",
    "MAX_ROUNDS",
    "DEFAULT_TTL_SECONDS",
    "QUERY_REWRITE_SCHEMA",
    "GAP_SCHEMA",
    "TRANSLATE_SCHEMA",
    "RELEVANCE_SCHEMA",
    "_arxiv_search",
    "_arxiv_fetch",
    "_arxiv_field_query",
    "_s2_search",
    "_openalex_search",
    "_crossref_search",
    "_dblp_search",
    "_search_once",
    "_llm_rewrite",
    "_translate_query",
    "_llm_relevance",
    "_keyword_relevance",
    "_filter_relevant",
    "_llm_gap_note",
    "_default_gap_note",
    "_dedup_papers",
    "_extract_keywords",
    "_cache_read",
    "_cache_write",
]

# ---- 常量 ----
ARXIV_API = "https://export.arxiv.org/api/query"
S2_API = "https://api.semanticscholar.org/graph/v1/paper/search"
OPENALEX_API = "https://api.openalex.org/works"
CROSSREF_API = "https://api.crossref.org/works"
DBLP_API = "https://dblp.org/search/publ/api"
S2_FIELDS = "title,authors,abstract,year,externalIds,url,venue"

MAX_RESULTS = 8          # 每个源每轮最多取回论文数
TIMEOUT = 20.0           # 单次 HTTP 超时（秒）
MAX_ROUNDS = 3           # 查询改写循环最大轮数（含首轮原始查询）
TARGET_PAPERS = 8        # 每个研究方向目标数量
MAX_PAPERS = 12          # 每个研究方向展示上限
DEFAULT_TTL_SECONDS = 7 * 24 * 3600   # 文献缓存 TTL：7 天

# Semantic Scholar 429 限流重试（公开 API 无 key 时易触发）
S2_MAX_RETRIES = 2
S2_RETRY_BACKOFF = 1.5

# 相关性优化（M10）：arXiv 标题字段优先，命中不足时回退摘要字段（避免 all: 在期刊引用/评论等
# 非正文字段的词面误命中，同时保留召回）；字段查询最多 AND 的显著术语数。
ARXIV_MIN_TITLE_HITS = 2
ARXIV_MAX_FIELD_TERMS = 4

# 拆词时剔除的功能词（连词/介词/代词等），避免进入字段约束与相关性匹配
_ARXIV_STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "not", "of", "in", "on", "for",
    "with", "using", "based", "via", "to", "from", "by", "is", "are",
    "was", "were", "be", "been", "being", "our", "we", "you", "your",
    "their", "its", "this", "that", "these", "those", "into", "over",
    "under", "between", "among", "as", "at", "how", "what", "which",
})

# 复用长连接 Client（连接池 + keep-alive + TLS 复用），避免每次请求新建连接。
_http_client: Optional[httpx.Client] = None


def _http() -> httpx.Client:
    """返回进程内复用的 httpx.Client；首次调用时创建（trust_env 读代理）。"""
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.Client(
            timeout=TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": "papermine/0.3 (+https://github.com/ari231233/work2paper)"},
        )
    return _http_client

# 缓存文件内嵌 schema 名 / 版本（区别于 dossier）
CACHE_SCHEMA = "literature_cache"
CACHE_SCHEMA_VERSION = 2

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
    "required": ["english_query", "keywords"],
    "properties": {
        "english_query": {"type": "string"},
        "keywords": {"type": "array", "items": {"type": "string"}},
    },
}

RELEVANCE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["relevant_titles"],
    "properties": {
        "relevant_titles": {"type": "array", "items": {"type": "string"}},
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
    """论文去重键：DOI > arXiv ID > 去标点标题。"""
    doi = _clean_ws(paper.get("doi")).lower()
    if not doi:
        external = _clean_ws(paper.get("external_id")).lower()
        if external.startswith("10."):
            doi = external
    if doi:
        return "doi:" + re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi)
    external = _clean_ws(paper.get("external_id")).lower()
    if external and not external.startswith("10."):
        return "external:" + re.sub(r"v\d+$", "", external)
    title = _clean_ws(paper.get("title")).lower()
    return "title:" + re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", title)


def _dedup_papers(papers: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """跨源去重并合并来源；无标题论文无法被引用，丢弃。"""
    out: List[Dict[str, Any]] = []
    seen: Dict[str, Dict[str, Any]] = {}
    for p in papers or []:
        if not isinstance(p, dict):
            continue
        title = _clean_ws(p.get("title"))
        key = _paper_key(p)
        if not title or key in {"title:", "external:"}:
            continue
        source = _clean_ws(p.get("source"))
        if key in seen:
            current = seen[key]
            records = current.setdefault("source_records", [])
            for item in list(p.get("source_records") or []) + ([source] if source else []):
                if item and item not in records:
                    records.append(item)
            for field in ("abstract", "venue", "url", "doi", "external_id", "year", "authors"):
                if not current.get(field) and p.get(field):
                    current[field] = p[field]
            continue
        copy = dict(p)
        copy["source_records"] = _dedup_strings(
            list(copy.get("source_records") or []) + ([source] if source else [])
        )
        seen[key] = copy
        out.append(copy)
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


def _extract_keywords(query: str) -> List[str]:
    """把查询拆成显著英文学术术语（去停用词 / 超短词），保序去重。

    供两个环节复用（M10）：
    - arXiv 字段约束查询（``ti:`` / ``abs:`` + AND 组合）；
    - 无 LLM 时的确定性关键词相关性过滤。
    """
    words = re.findall(r"[a-z0-9][a-z0-9\-]*", _clean_ws(query).lower())
    out: List[str] = []
    seen: set = set()
    for w in words:
        if w in _ARXIV_STOPWORDS or len(w) <= 2:
            continue
        if w not in seen:
            seen.add(w)
            out.append(w)
    return out


# ---- arXiv 检索（Atom XML）----

def _text(elem: Any, tag: str) -> str:
    node = elem.find(tag)
    if node is None:
        return ""
    return _clean_ws("".join(node.itertext()))


def _arxiv_field_query(query: str, field: str) -> str:
    """构造 arXiv 字段约束查询：对前 ``ARXIV_MAX_FIELD_TERMS`` 个显著术语逐个加字段前缀并 AND 组合。

    直接写 ``ti:term1 term2`` 时字段前缀只作用于紧随其后的单个词（arXiv 语法），
    故此处显式对每个术语加前缀，避免 ``all:`` 在期刊引用 / 评论等非正文字段的词面误命中
    （lessons-learned §2.4）。术语为空（如纯符号查询）时退化为字段短语。
    """
    terms = _extract_keywords(query)[:ARXIV_MAX_FIELD_TERMS]
    if not terms:
        return '{}:"{}"'.format(field, _clean_ws(query))
    return " AND ".join("{}:{}".format(field, t) for t in terms)


def _arxiv_fetch(search_query: str, max_results: int = MAX_RESULTS,
                 timeout: float = TIMEOUT) -> List[Dict[str, Any]]:
    """按给定 ``search_query`` 请求 arXiv API 并解析为标准化论文列表；网络/HTTP 错误向上抛。"""
    params = {
        "search_query": search_query,
        "start": 0,
        "max_results": max_results,
        "sortBy": "relevance",
    }
    resp = _http().get(ARXIV_API, params=params, timeout=timeout)
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
            "doi": "",
        })
    return papers


def _arxiv_search(query: str, max_results: int = MAX_RESULTS,
                  timeout: float = TIMEOUT) -> List[Dict[str, Any]]:
    """arXiv 检索（M10 字段约束）：标题字段优先，命中不足时回退摘要字段合并去重。

    - ``ti:`` 标题限定：核心术语须出现在标题，最大程度减少词面误命中；
    - 标题命中 < ``ARXIV_MIN_TITLE_HITS`` 时回退 ``abs:`` 摘要字段，保留召回；
    - 网络/HTTP 错误向上抛（由 ``_search_once`` 统一降级）。
    """
    title_papers = _arxiv_fetch(_arxiv_field_query(query, "ti"), max_results, timeout)
    if len(title_papers) >= ARXIV_MIN_TITLE_HITS:
        return title_papers
    abs_papers = _arxiv_fetch(_arxiv_field_query(query, "abs"), max_results, timeout)
    return _dedup_papers(title_papers + abs_papers)


# ---- Semantic Scholar 检索（JSON）----

def _s2_search(query: str, max_results: int = MAX_RESULTS,
               timeout: float = TIMEOUT) -> List[Dict[str, Any]]:
    """Semantic Scholar Graph API 检索，返回标准化论文列表；网络/HTTP 错误向上抛。

    429（限流）时做有限次退避重试，仍失败则抛错由上层降级。
    """
    params = {"query": query, "fields": S2_FIELDS, "limit": max_results}
    for attempt in range(S2_MAX_RETRIES + 1):
        resp = _http().get(S2_API, params=params, timeout=timeout)
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
        doi = _clean_ws(ext.get("DOI"))
        papers.append({
            "title": title,
            "authors": authors,
            "year": item.get("year"),
            "abstract": _clean_ws(item.get("abstract")),
            "url": item.get("url") or "",
            "venue": _clean_ws(item.get("venue")),
            "source": "semantic_scholar",
            "external_id": ext.get("ArXiv") or ext.get("DOI") or "",
            "doi": doi,
        })
    return papers


# ---- OpenAlex / Crossref / DBLP 检索（JSON）----

def _openalex_abstract(inverted: Any) -> str:
    """把 OpenAlex abstract_inverted_index 还原为普通摘要。"""
    if not isinstance(inverted, dict):
        return ""
    positioned: List[tuple] = []
    for word, positions in inverted.items():
        for position in positions or []:
            if isinstance(position, int):
                positioned.append((position, str(word)))
    return _clean_ws(" ".join(word for _, word in sorted(positioned)))


def _openalex_search(query: str, max_results: int = MAX_RESULTS,
                     timeout: float = TIMEOUT) -> List[Dict[str, Any]]:
    """OpenAlex Works API 检索并标准化。"""
    params = {
        "search": query,
        "per-page": max_results,
        "select": (
            "id,doi,title,display_name,publication_year,authorships,"
            "primary_location,abstract_inverted_index"
        ),
    }
    resp = _http().get(OPENALEX_API, params=params, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    papers: List[Dict[str, Any]] = []
    for item in (data.get("results") or []) if isinstance(data, dict) else []:
        if not isinstance(item, dict):
            continue
        title = _clean_ws(item.get("display_name") or item.get("title"))
        if not title:
            continue
        authors = [
            _clean_ws((authorship.get("author") or {}).get("display_name"))
            for authorship in (item.get("authorships") or [])
            if isinstance(authorship, dict)
        ]
        location = item.get("primary_location") or {}
        source = (location.get("source") or {}) if isinstance(location, dict) else {}
        doi = _clean_ws(item.get("doi"))
        doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi, flags=re.IGNORECASE)
        papers.append({
            "title": title,
            "authors": [a for a in authors if a],
            "year": item.get("publication_year"),
            "abstract": _openalex_abstract(item.get("abstract_inverted_index")),
            "url": _clean_ws(item.get("doi") or item.get("id")),
            "venue": _clean_ws(source.get("display_name") if isinstance(source, dict) else ""),
            "source": "openalex",
            "external_id": _clean_ws(item.get("id")),
            "doi": doi,
        })
    return papers


def _crossref_year(item: Dict[str, Any]) -> Optional[int]:
    for key in ("published-print", "published-online", "published", "issued"):
        value = item.get(key) or {}
        parts = value.get("date-parts") if isinstance(value, dict) else None
        if parts and parts[0] and isinstance(parts[0][0], int):
            return parts[0][0]
    return None


def _crossref_search(query: str, max_results: int = MAX_RESULTS,
                     timeout: float = TIMEOUT) -> List[Dict[str, Any]]:
    """Crossref Works API 检索并标准化。"""
    params = {"query.bibliographic": query, "rows": max_results}
    resp = _http().get(CROSSREF_API, params=params, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    message = data.get("message") if isinstance(data, dict) else {}
    papers: List[Dict[str, Any]] = []
    for item in (message.get("items") or []) if isinstance(message, dict) else []:
        if not isinstance(item, dict):
            continue
        titles = item.get("title") or []
        title = _clean_ws(titles[0] if titles else "")
        if not title:
            continue
        authors = []
        for author in item.get("author") or []:
            if isinstance(author, dict):
                name = _clean_ws("{} {}".format(author.get("given", ""), author.get("family", "")))
                if name:
                    authors.append(name)
        containers = item.get("container-title") or []
        doi = _clean_ws(item.get("DOI")).lower()
        papers.append({
            "title": title,
            "authors": authors,
            "year": _crossref_year(item),
            "abstract": _clean_ws(re.sub(r"<[^>]+>", " ", str(item.get("abstract") or ""))),
            "url": _clean_ws(item.get("URL")),
            "venue": _clean_ws(containers[0] if containers else ""),
            "source": "crossref",
            "external_id": doi,
            "doi": doi,
        })
    return papers


def _dblp_authors(value: Any) -> List[str]:
    if isinstance(value, dict):
        value = value.get("author")
    if isinstance(value, str):
        return [_clean_ws(value)] if _clean_ws(value) else []
    if isinstance(value, dict):
        value = [value]
    return [
        _clean_ws(item.get("text") if isinstance(item, dict) else item)
        for item in (value or [])
        if _clean_ws(item.get("text") if isinstance(item, dict) else item)
    ]


def _dblp_search(query: str, max_results: int = MAX_RESULTS,
                 timeout: float = TIMEOUT) -> List[Dict[str, Any]]:
    """DBLP publication search API 检索并标准化。"""
    params = {"q": query, "h": max_results, "format": "json"}
    resp = _http().get(DBLP_API, params=params, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    hits = (((data.get("result") or {}).get("hits") or {}).get("hit") or []) \
        if isinstance(data, dict) else []
    papers: List[Dict[str, Any]] = []
    for hit in hits:
        info = hit.get("info") if isinstance(hit, dict) else None
        if not isinstance(info, dict):
            continue
        title = _clean_ws(re.sub(r"<[^>]+>", " ", str(info.get("title") or "")))
        if not title:
            continue
        doi = _clean_ws(info.get("doi")).lower()
        papers.append({
            "title": title,
            "authors": _dblp_authors(info.get("authors")),
            "year": _year_from(info.get("year")),
            "abstract": "",
            "url": _clean_ws(info.get("ee") or info.get("url")),
            "venue": _clean_ws(info.get("venue")),
            "source": "dblp",
            "external_id": doi or _clean_ws(info.get("key")),
            "doi": doi,
        })
    return papers


# ---- 本地缓存（literature_cache/）----

def _cache_key(query: str) -> str:
    material = "v{}:{}".format(CACHE_SCHEMA_VERSION, _clean_ws(query).lower())
    return hashlib.sha1(material.encode("utf-8")).hexdigest()


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
    if data.get("_schema") != CACHE_SCHEMA or data.get("_schema_version") != CACHE_SCHEMA_VERSION:
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


# ---- 单轮检索（缓存优先，五源 best-effort）----

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
    searchers = (
        ("arxiv", _arxiv_search),
        ("semantic_scholar", _s2_search),
        ("openalex", _openalex_search),
        ("crossref", _crossref_search),
        ("dblp", _dblp_search),
    )
    with ThreadPoolExecutor(max_workers=len(searchers)) as pool:
        futures = [(name, pool.submit(fn, query)) for name, fn in searchers]
        for name, future in futures:
            try:
                batch = future.result()
            except Exception:
                # 网络 / 限流 / 解析失败：该源降级跳过，不崩整个检索
                continue
            if isinstance(batch, list) and batch:
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
        result = complete_fast(llm, _REWRITE_SYSTEM, user, QUERY_REWRITE_SCHEMA, temperature=0.3)
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
    "你是 papermine 的「检索查询翻译器」。把给定的中文查询提炼为英文学术检索关键词。\n"
    "规则：\n"
    "1. english_query 用 3~6 个核心学术术语组合（术语串），不要完整句子、不要宽泛短语；\n"
    "2. 保留领域限定词（对象/场景/方法），如『剩余寿命预测』→ remaining useful life prediction，"
    "而非仅剩 life 这类泛词（易词面误命中）；\n"
    "3. 去除『研究/方法/系统/如何/一个』等泛词；\n"
    "4. keywords 输出与 english_query 对应的英文核心术语列表（供相关性过滤）。\n"
    "只输出 JSON，严格满足给定 schema。"
)


def _translate_query(llm: Optional[LLMProvider], query: str) -> tuple:
    """把含中文的查询翻译/提炼为英文学术关键词，返回 ``(english_query, keywords)``。

    - 有 LLM 且含中文：产出聚焦关键词查询 + 核心术语列表（M10 要点 1）；
    - 无中文 / 无 LLM / 失败：原样返回 query，关键词退化为确定性拆词。
    """
    if llm is None:
        return query, _extract_keywords(query)
    if not any("\u4e00" <= ch <= "\u9fff" for ch in query):
        return query, _extract_keywords(query)
    user = json.dumps({"query": query}, ensure_ascii=False)
    try:
        result = complete_fast(llm, _TRANSLATE_SYSTEM, user, TRANSLATE_SCHEMA, temperature=0.2)
    except (LLMError, SchemaError):
        return query, _extract_keywords(query)
    if not isinstance(result, dict):
        return query, _extract_keywords(query)
    en = _clean_ws(result.get("english_query"))
    keywords = _dedup_strings(result.get("keywords"))
    if not en:
        return query, _extract_keywords(query)
    return en, (keywords or _extract_keywords(en))


def _retrieve_with_rewrite(query: str, cache_dir: Any, llm: Optional[LLMProvider]) -> tuple:
    """对一个（已翻译的英文）查询跑查询改写循环（最多 MAX_ROUNDS 轮），合并去重各轮论文。"""
    merged: Dict[str, Dict[str, Any]] = {}
    sources: List[str] = []
    current = query
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


# ---- 相关性过滤（M10 要点 3）：LLM 判相关，降级为关键词匹配 ----

_RELEVANCE_SYSTEM = (
    "你是 papermine 的「文献相关性过滤器」。给定检索查询与候选论文列表，"
    "判断每篇论文是否与查询目标真正相关，只保留相关论文。\n"
    "判据：论文的研究对象/方法/场景须与查询的核心学术主题一致；"
    "仅因个别词语相同而命中不算相关（例如查询含 life，但『LIFE 太空望远镜』项目不算）。\n"
    "输出 relevant_titles 列表，标题必须逐字等于给定论文的 title，不要增删改或改写。\n"
    "只输出 JSON，严格满足给定 schema。"
)


def _llm_relevance(llm: Optional[LLMProvider], query: str,
                   papers: List[Dict[str, Any]]) -> Optional[List[str]]:
    """让 LLM 判定每篇论文是否相关，返回应保留的标题列表；无 LLM / 无论文 / 失败返回 None。"""
    if llm is None or not papers:
        return None
    user = json.dumps({
        "query": query,
        "papers": [
            {"title": p.get("title", ""), "abstract": (p.get("abstract") or "")[:300]}
            for p in papers[:12]
        ],
    }, ensure_ascii=False)
    try:
        result = complete_fast(llm, _RELEVANCE_SYSTEM, user, RELEVANCE_SCHEMA, temperature=0.0)
    except (LLMError, SchemaError):
        return None
    if not isinstance(result, dict):
        return None
    raw = result.get("relevant_titles")
    if not isinstance(raw, list):
        return None
    return [t for t in raw if isinstance(t, str)]


def _keyword_relevance(query: str, keywords: Any,
                       papers: List[Dict[str, Any]]) -> List[str]:
    """确定性相关性过滤：论文标题+摘要须命中查询的显著术语。

    - 术语来自翻译产出的 keywords，缺失时退化为对 query 确定性拆词；
    - 命中判定：短语/带连字符术语用子串匹配，单词用词首边界匹配（前缀，容忍词形变化）；
    - 阈值：术语数为 1 时命中 ≥1，术语数 ≥2 时命中 ≥2（多词共现，抑制单泛词误命中）；
    - 无显著术语时不过滤（返回全部标题）。
    """
    terms = [(_clean_ws(k) or "").lower() for k in (keywords or [])]
    terms = [t for t in terms if t]
    if not terms:
        terms = _extract_keywords(query)
    if not terms:
        return [p.get("title") for p in papers if p.get("title")]

    threshold = 1 if len(terms) == 1 else 2
    kept: List[str] = []
    for p in papers:
        title = _clean_ws(p.get("title"))
        if not title:
            continue
        text = " ".join([title, _clean_ws(p.get("abstract"))]).lower()
        hits = 0
        for t in terms:
            if " " in t or "-" in t:
                if t in text:
                    hits += 1
            elif re.search(r"\b" + re.escape(t), text):
                hits += 1
        if hits >= threshold:
            kept.append(title)
    return kept


def _filter_relevant(llm: Optional[LLMProvider], query: str, keywords: Any,
                     papers: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """检索后相关性过滤：先 LLM 判相关，失败/无 LLM 时降级为关键词匹配。

    只保留被判定相关的论文；LLM 显式判全不相关（空列表）或关键词全不匹配时返回空列表。
    """
    if not papers:
        return papers
    relevant = _llm_relevance(llm, query, papers) if llm is not None else None
    if relevant is None:
        relevant = _keyword_relevance(query, keywords, papers)
    if relevant is None:
        return papers
    keep = {_clean_ws(t).lower() for t in relevant if _clean_ws(t)}
    return [p for p in papers if _clean_ws(p.get("title")).lower() in keep]


# ---- M29 分层召回：高度相关优先，不足时补部分相关 ----

_QUERY_EXPANSIONS = {
    "remaining useful life": "prognostics health management condition monitoring",
    "obstacle avoidance": "collision avoidance path planning motion planning",
    "collision avoidance": "conflict detection resolution path planning",
    "prescribed performance": "nonlinear robust tracking control",
    "trajectory tracking": "path following guidance control",
    "anomaly detection": "fault detection condition monitoring",
    "time series": "temporal data sequence modeling",
    "evtol": "urban air mobility aircraft control",
}


def _normalized_terms(query: str, keywords: Any) -> List[str]:
    terms = [_clean_ws(item).lower() for item in (keywords or []) if _clean_ws(item)]
    return terms or _extract_keywords(query)


def _paper_term_hits(paper: Dict[str, Any], terms: List[str]) -> List[str]:
    text = " ".join([
        _clean_ws(paper.get("title")),
        _clean_ws(paper.get("abstract")),
    ]).lower()
    hits: List[str] = []
    for term in terms:
        if " " in term or "-" in term:
            matched = term in text
        else:
            matched = re.search(r"\b" + re.escape(term), text) is not None
        if matched:
            hits.append(term)
    return hits


def _select_tiered_papers(query: str, keywords: Any,
                          papers: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按术语覆盖把候选分成 high/partial，并返回最多 12 篇。"""
    terms = _normalized_terms(query, keywords)
    high: List[Dict[str, Any]] = []
    partial: List[Dict[str, Any]] = []
    for paper in _dedup_papers(papers):
        hits = _paper_term_hits(paper, terms)
        if not terms:
            level = "partial"
        elif len(terms) == 1:
            level = "high" if hits else ""
        else:
            level = "high" if len(hits) >= 2 else ("partial" if hits else "")
        if not level:
            continue
        item = dict(paper)
        item["relevance_level"] = level
        if hits:
            item["match_reason"] = "命中核心术语：{}".format("、".join(hits[:4]))
        else:
            item["match_reason"] = "扩展检索获得，需结合摘要人工核验"
        (high if level == "high" else partial).append(item)
    return (high + partial)[:MAX_PAPERS]


def _expansion_queries(query: str, keywords: Any) -> List[str]:
    """确定性生成由窄到宽的扩展查询，不增加 LLM 调用。"""
    terms = _normalized_terms(query, keywords)
    candidates: List[str] = []
    words = _extract_keywords(query)
    if len(words) >= 4:
        candidates.append(" ".join(words[:4]))
    if len(words) >= 3:
        candidates.append(" ".join(words[:3]))
    lowered = _clean_ws(query).lower()
    for phrase, expansion in _QUERY_EXPANSIONS.items():
        if phrase in lowered or phrase in terms:
            candidates.append(expansion)
    if terms:
        candidates.append(" ".join(terms[:2]))
    original = lowered
    return [item for item in _dedup_strings(candidates) if item.lower() != original][:3]


def _retrieve_tiered(query: str, keywords: Any, cache_dir: Any,
                     llm: Optional[LLMProvider]) -> tuple:
    """先跑原始/改写检索，不足目标时用确定性宽化查询补召回。"""
    papers, sources = _retrieve_with_rewrite(query, cache_dir, llm)
    selected = _select_tiered_papers(query, keywords, papers)
    for expanded in _expansion_queries(query, keywords):
        if len(selected) >= TARGET_PAPERS:
            break
        batch, batch_sources = _search_once(expanded, cache_dir)
        papers = _dedup_papers(papers + batch)
        for source in batch_sources:
            if source not in sources:
                sources.append(source)
        selected = _select_tiered_papers(query, keywords, papers)
    return selected, sources


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
        result = complete_fast(llm, _GAP_SYSTEM, user, GAP_SCHEMA, temperature=0.2)
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
    """对一组查询做检索（含查询改写循环 + 相关性过滤 + 缓存 + 降级），返回 literature[]。

    冻结契约（docs/build-plan.md §3.3 / §4 M5）：

        def search_literature(queries, cache_dir, llm) -> list[dict]

    - 每个查询返回一条 ``{query, papers, gap_note, sources}``；
    - 先翻译为聚焦英文学术关键词，再检索 + 相关性过滤（LLM 判相关 / 关键词匹配）；
    - 网络不可用时 papers 留空、gap_note 标离线，不抛异常；
    - ``llm`` 为 None 或 NullProvider 时跳过翻译/改写/LLM 过滤与 LLM gap，走确定性路径。
    """
    apply_proxy()
    entries: List[dict] = []
    for query in _dedup_strings(queries):
        en_query, keywords = _translate_query(llm, query)
        papers, sources = _retrieve_tiered(en_query, keywords, cache_dir, llm)
        gap_note = _llm_gap_note(llm, en_query, papers) or _default_gap_note(papers, sources)
        high_count = len([p for p in papers if p.get("relevance_level") == "high"])
        partial_count = len(papers) - high_count
        entries.append({
            "query": query,
            "papers": papers,
            "gap_note": gap_note,
            "sources": sorted(set(sources)),
            "target_count": TARGET_PAPERS,
            "high_count": high_count,
            "partial_count": partial_count,
            "coverage_status": "sufficient" if len(papers) >= 7 else "insufficient",
        })
    return entries
