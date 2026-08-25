"""M5 v2：文献理解 + 矛盾/gap 挖掘 + 假设生成。

把 M5 的「检索 → 创新」加深为六步流水线的中间三步（docs/build-plan.md §4 M5 v2）：

    problems → Retrieval(retrieval.py) → literature
             → Understanding(本模块)   → 每篇命中论文附结构化理解
             → Contradiction/Gap Mining(本模块) → 每条 literature 附 contradiction_graph
             → Hypothesis Generation(本模块) → 每条 literature 附 hypotheses
             → Idea Generation(agents/ideate.py) → ideas

数据落点：全部写在 ``literature[]`` 条目内部（architecture §4 的 literature 条目为
``additionalProperties: true``），**不改 Dossier 顶层字段、不改冻结接口**（§3.2 / §3.3）。

- 每篇论文新增 ``understanding``：{claim / method / conclusion / applicability / limitations}。
- 每条 literature 新增 ``contradiction_graph``：{nodes / edges / gaps}，其中 gaps 的
  ``type`` ∈ {contradiction, gap}，矛盾（contradiction）同时给出节点间 contradiction 边。
- 每条 literature 新增 ``hypotheses``：每条 gap 对应一条可证伪假设（if-then + falsification）。

降级路径（architecture §7 / §8）：无 LLM（NullProvider）、LLMError、SchemaError、或 LLM
返回结构非法时，退化为确定性规则（词面级 understanding / 每条目一条 gap / 每条 gap 一条
if-then 假设），保证六步流水线在离线/异常下也能产出可追溯的 gap → hypothesis → idea 链。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

from .llm import LLMError, LLMProvider, SchemaError

__all__ = [
    "analyze_literature",
    "BATCH_UNDERSTANDING_SCHEMA",
    "MINING_SCHEMA",
    "HYPOTHESIS_SCHEMA",
    "MAX_UNDERSTAND_PAPERS",
    "MAX_GAPS",
    "_understand_deterministic",
    "_mine_deterministic",
    "_hypothesize_deterministic",
    "_build_graph",
    "_assign_gap_ids",
    "_entry_gaps",
    "_entry_hypotheses",
]

# 每个条目送入 LLM 理解/挖掘的论文数上限（控制 token 预算）；超出的论文走确定性理解
MAX_UNDERSTAND_PAPERS = 8
# 每条文献条目最多保留的 gap/矛盾数量（防止图无限膨胀）
MAX_GAPS = 8

# ---------------------------------------------------------------------------
# 结构化输出契约（schema 校验走 papermine/llm.py 的极简子集）
# ---------------------------------------------------------------------------

# 单篇论文的结构化理解五元组
_UNDERSTANDING_OBJECT: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["claim", "method", "conclusion", "applicability", "limitations"],
    "properties": {
        "claim": {"type": "string"},
        "method": {"type": "string"},
        "conclusion": {"type": "string"},
        "applicability": {"type": "string"},
        "limitations": {"type": "string"},
    },
}

# 批量理解：一次 LLM 调用返回该条目下多篇论文的理解
BATCH_UNDERSTANDING_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["papers"],
    "properties": {
        "papers": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["title", "understanding"],
                "properties": {
                    "title": {"type": "string"},
                    "understanding": _UNDERSTANDING_OBJECT,
                },
            },
        },
    },
}

# 矛盾/缺口挖掘：gaps = 无人覆盖的角度；contradictions = 同一结论点结论冲突的论文对
MINING_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["gaps", "contradictions"],
    "properties": {
        "gaps": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["claim_point", "description", "angle"],
                "properties": {
                    "claim_point": {"type": "string"},
                    "description": {"type": "string"},
                    "angle": {"type": "string"},
                },
            },
        },
        "contradictions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["claim_point", "description", "paper_a", "paper_b"],
                "properties": {
                    "claim_point": {"type": "string"},
                    "description": {"type": "string"},
                    "paper_a": {"type": "string"},
                    "paper_b": {"type": "string"},
                },
            },
        },
    },
}

# 假设生成：与给定 gaps 顺序对齐的 if-then 可证伪假设
HYPOTHESIS_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["hypotheses"],
    "properties": {
        "hypotheses": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["if_then", "falsification"],
                "properties": {
                    "if_then": {"type": "string"},
                    "falsification": {"type": "string"},
                },
            },
        },
    },
}

# ---------------------------------------------------------------------------
# System 提示词
# ---------------------------------------------------------------------------

_UNDERSTAND_SYSTEM = (
    "你是 papermine 的「文献理解器」。给定一个检索查询与命中的若干论文（仅标题/摘要/年份/来源），"
    "对每篇论文提取结构化理解。\n"
    "规则：\n"
    "1. claim：论文的核心主张（1 句话）；\n"
    "2. method：用到的方法/模型（简洁术语）；\n"
    "3. conclusion：论文得出的结论；\n"
    "4. applicability：该结论的适用条件/场景（如数据分布、领域、规模假设）；\n"
    "5. limitations：论文自述或可见的局限/边界。\n"
    "只基于给定信息，不得编造全文内容。输出 papers 列表，每项的 title 必须逐字等于给定论文标题，"
    "不要增删改；understanding 为五字段对象。只输出 JSON，严格满足给定 schema。"
)

_MINING_SYSTEM = (
    "你是 papermine 的「矛盾与缺口挖掘器」。给定同一检索查询命中的多篇论文及其结构化理解，"
    "跨论文比较以找出可成为创新点的矛盾与缺口。\n"
    "规则：\n"
    "1. gaps：找出这些论文都未覆盖的角度（例如某种约束、场景、数据设定、评测维度没人做），"
    "每项给 claim_point（结论点/主题）、description（为何是缺口）、angle（无人覆盖的具体角度）；\n"
    "2. contradictions：找出同一结论点上结论互相冲突的论文对，每项给 claim_point、"
    "description（冲突在哪）、paper_a 与 paper_b（两篇论文标题，必须逐字等于给定标题）。\n"
    "宁缺毋滥，无矛盾或无缺口时给空数组。只输出 JSON，严格满足给定 schema。"
)

_HYPOTHESIS_SYSTEM = (
    "你是 papermine 的「可证伪假设生成器」。给定若干 gap/矛盾（含 gap_id / claim_point / "
    "description），为每个生成一条可证伪的 if-then 假设，作为候选创新点的前置。\n"
    "规则：\n"
    "1. if_then：用「若…则…」形式写一条可检验的因果/方向假设，明确触发条件与预期结果；\n"
    "2. falsification：写清什么观察结果会证伪该假设（必须可判真伪，禁止空泛）。\n"
    "hypotheses 数组与给定 gaps 顺序一一对应。只输出 JSON，严格满足给定 schema。"
)

# ---------------------------------------------------------------------------
# 确定性降级：方法术语表
# ---------------------------------------------------------------------------

_METHOD_KEYWORDS: Tuple[Tuple[str, str], ...] = (
    ("lstm", "LSTM/循环神经网络"),
    ("transformer", "Transformer"),
    ("isolation forest", "孤立森林"),
    ("random forest", "随机森林"),
    ("xgboost", "XGBoost"),
    ("gradient boost", "梯度提升"),
    ("graph neural", "图神经网络"),
    ("gnn", "图神经网络"),
    ("convolution", "卷积神经网络"),
    ("cnn", "卷积神经网络"),
    ("reinforcement", "强化学习"),
    ("diffusion", "扩散模型"),
    ("variational", "变分推断"),
    ("bayesian", "贝叶斯方法"),
    ("ensemble", "集成学习"),
    ("anomaly detection", "异常检测"),
    ("remaining useful life", "剩余寿命预测"),
    ("prognostics", "寿命预测"),
    ("time series", "时序建模"),
)


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def _clean(s: Any) -> str:
    return " ".join(str(s or "").split())


def _clip(s: Any, n: int) -> str:
    s = _clean(s)
    if not s:
        return ""
    return s[:n] + ("…" if len(s) > n else "")


def _paper_title(p: Any) -> str:
    if not isinstance(p, dict):
        return ""
    return _clean(p.get("title"))


def _valid_understanding(u: Any) -> bool:
    """判断 understanding 是否为含五个非空字段的合法对象。"""
    if not isinstance(u, dict):
        return False
    for key in ("claim", "method", "conclusion", "applicability", "limitations"):
        if not _clean(u.get(key)):
            return False
    return True


# ---------------------------------------------------------------------------
# ① 文献理解（LLM 批量 + 确定性降级）
# ---------------------------------------------------------------------------

def _understand_deterministic(paper: Dict[str, Any]) -> Dict[str, str]:
    """无 LLM 时的词面级理解：从标题/摘要抽取方法术语，其余字段标离线降级。"""
    title = _paper_title(paper)
    abstract = _clean(paper.get("abstract"))
    text = (title + " " + abstract).lower()
    methods = [name for key, name in _METHOD_KEYWORDS if key in text]
    method = "、".join(methods[:3]) if methods else "未识别（离线降级）"
    return {
        "claim": "本文围绕「{}」展开（离线降级，仅基于标题/摘要词面信息）".format(title or "未知主题"),
        "method": method,
        "conclusion": abstract[:160] if abstract else "未识别（离线降级，需人工阅读全文）",
        "applicability": "适用条件需人工核验（离线降级）",
        "limitations": "未识别（离线降级，需人工阅读全文）",
    }


def _understand_with_llm(entry: Dict[str, Any], papers: List[Dict[str, Any]],
                         llm: Optional[LLMProvider]) -> Optional[Dict[str, Dict[str, str]]]:
    """LLM 批量理解，返回 {论文标题: understanding}；失败/无 LLM/结构非法返回 None。"""
    if llm is None or not papers:
        return None
    subset = papers[:MAX_UNDERSTAND_PAPERS]
    user = json.dumps({
        "query": entry.get("query", ""),
        "papers": [
            {
                "title": _paper_title(p),
                "abstract": _clip(p.get("abstract"), 400),
                "venue": p.get("venue", ""),
                "year": p.get("year"),
            }
            for p in subset
        ],
    }, ensure_ascii=False)
    try:
        result = llm.complete(_UNDERSTAND_SYSTEM, user, BATCH_UNDERSTANDING_SCHEMA, temperature=0.2)
    except (LLMError, SchemaError):
        return None
    if not isinstance(result, dict):
        return None
    raw = result.get("papers")
    if not isinstance(raw, list):
        return None
    out: Dict[str, Dict[str, str]] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        title = _clean(item.get("title"))
        u = item.get("understanding")
        if title and _valid_understanding(u):
            out[title] = {
                "claim": _clean(u.get("claim")),
                "method": _clean(u.get("method")),
                "conclusion": _clean(u.get("conclusion")),
                "applicability": _clean(u.get("applicability")),
                "limitations": _clean(u.get("limitations")),
            }
    return out


def _understand_entry(entry: Dict[str, Any], papers: List[Dict[str, Any]],
                      llm: Optional[LLMProvider]) -> None:
    """对条目内所有论文写入 understanding（LLM 命中用 LLM，未命中/失败走确定性）。"""
    understood = _understand_with_llm(entry, papers, llm)
    for paper in papers:
        title = _paper_title(paper)
        u = (understood or {}).get(title) if title else None
        paper["understanding"] = u if _valid_understanding(u) else _understand_deterministic(paper)


# ---------------------------------------------------------------------------
# ② 矛盾/gap 挖掘（LLM + 确定性降级）
# ---------------------------------------------------------------------------

def _sanitize_gaps(gaps: Any) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for g in gaps or []:
        if not isinstance(g, dict):
            continue
        point = _clean(g.get("claim_point"))
        desc = _clean(g.get("description"))
        if not point or not desc:
            continue
        out.append({"claim_point": point, "description": desc, "angle": _clean(g.get("angle"))})
    return out


def _sanitize_contradictions(conts: Any, papers: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """只保留 paper_a/paper_b 均能匹配到真实论文标题的矛盾（杜绝编造边）。"""
    titles = {_paper_title(p).lower(): _paper_title(p) for p in papers if _paper_title(p)}
    out: List[Dict[str, str]] = []
    for c in conts or []:
        if not isinstance(c, dict):
            continue
        point = _clean(c.get("claim_point"))
        desc = _clean(c.get("description"))
        if not point or not desc:
            continue
        a = titles.get(_clean(c.get("paper_a")).lower())
        b = titles.get(_clean(c.get("paper_b")).lower())
        if not a or not b:
            continue
        out.append({"claim_point": point, "description": desc, "paper_a": a, "paper_b": b})
    return out


def _mine_with_llm(entry: Dict[str, Any], papers: List[Dict[str, Any]],
                   llm: Optional[LLMProvider]) -> Tuple[Optional[List[Dict[str, str]]],
                                                         Optional[List[Dict[str, str]]]]:
    """LLM 跨论文挖掘，返回 (gaps, contradictions)；失败/无 LLM/结构非法返回 (None, None)。"""
    if llm is None or not papers:
        return None, None
    subset = papers[:MAX_UNDERSTAND_PAPERS]
    user = json.dumps({
        "query": entry.get("query", ""),
        "papers": [
            {
                "title": _paper_title(p),
                "understanding": p.get("understanding") or {},
                "abstract": _clip(p.get("abstract"), 200),
            }
            for p in subset
        ],
    }, ensure_ascii=False)
    try:
        result = llm.complete(_MINING_SYSTEM, user, MINING_SCHEMA, temperature=0.3)
    except (LLMError, SchemaError):
        return None, None
    if not isinstance(result, dict):
        return None, None
    gaps, conts = result.get("gaps"), result.get("contradictions")
    if not isinstance(gaps, list) or not isinstance(conts, list):
        return None, None
    return _sanitize_gaps(gaps), _sanitize_contradictions(conts, papers)


def _mine_deterministic(entry: Dict[str, Any],
                        papers: List[Dict[str, Any]]) -> Tuple[List[Dict[str, str]], List[Dict[str, str]]]:
    """无 LLM 时的确定性挖掘：每条文献条目产出一条「无人覆盖的角度」gap（诚实标注离线降级）。"""
    if not papers:
        return [], []
    titles = [t for t in (_paper_title(p) for p in papers) if t]
    angle = _clean(entry.get("query")) or "本项目关注的角度"
    desc = (
        "检索到 {} 篇相关文献（{}）；未识别到明确的结论冲突，{} 这一角度在检索结果中"
        "缺少显式覆盖（离线降级，需人工核验）".format(len(papers), "、".join(titles[:3]) or "无标题", angle)
    )
    return [{"claim_point": angle, "description": desc, "angle": angle}], []


def _assign_gap_ids(gap_records: List[Dict[str, Any]], start: int) -> List[Dict[str, Any]]:
    """按全局计数器给 gap 记录赋唯一 id（g1, g2, ...）。"""
    for i, g in enumerate(gap_records):
        g["gap_id"] = "g{}".format(start + i + 1)
    return gap_records


def _build_graph(papers: List[Dict[str, Any]], gaps: List[Dict[str, str]],
                 contradictions: List[Dict[str, str]], start_id: int) -> Dict[str, Any]:
    """把 gap/矛盾记录组装成 contradiction_graph（nodes/edges/gaps），gap_id 全局唯一。"""
    titles = [t for t in (_paper_title(p) for p in papers) if t]
    nodes = [{"id": "p:{}".format(i), "label": t, "kind": "paper"} for i, t in enumerate(titles)]
    index = {t: "p:{}".format(i) for i, t in enumerate(titles)}

    edges: List[Dict[str, str]] = []
    gap_records: List[Dict[str, Any]] = []

    for g in gaps[:MAX_GAPS]:
        gap_records.append({
            "gap_id": "",
            "type": "gap",
            "claim_point": g.get("claim_point", ""),
            "description": g.get("description", ""),
            "angle": g.get("angle", ""),
            "paper_refs": [],
        })

    for c in contradictions[:MAX_GAPS]:
        a, b = c.get("paper_a", ""), c.get("paper_b", "")
        gap_records.append({
            "gap_id": "",
            "type": "contradiction",
            "claim_point": c.get("claim_point", ""),
            "description": c.get("description", ""),
            "angle": "",
            "paper_refs": [a, b],
        })
        if a in index and b in index:
            edges.append({
                "source": index[a],
                "target": index[b],
                "kind": "contradiction",
                "claim_point": c.get("claim_point", ""),
            })

    _assign_gap_ids(gap_records, start_id)
    return {"nodes": nodes, "edges": edges, "gaps": gap_records}


def _mine_entry(entry: Dict[str, Any], papers: List[Dict[str, Any]],
                llm: Optional[LLMProvider], start_id: int) -> Dict[str, Any]:
    if not papers:
        return {"nodes": [], "edges": [], "gaps": []}
    gaps, conts = _mine_with_llm(entry, papers, llm)
    if gaps is None or conts is None:
        gaps, conts = _mine_deterministic(entry, papers)
    return _build_graph(papers, gaps, conts, start_id)


# ---------------------------------------------------------------------------
# ③ 假设生成（if-then 可证伪假设）
# ---------------------------------------------------------------------------

def _hypothesize_deterministic(gap: Dict[str, Any]) -> Dict[str, str]:
    """无 LLM 时的确定性 if-then 假设（诚实标注离线降级）。"""
    point = _clean(gap.get("claim_point")) or _clean(gap.get("angle")) or "该问题"
    if gap.get("type") == "contradiction":
        statement = (
            "若在相同适用条件下复现「{}」，则能判定冲突结论中至少一方不可靠，"
            "从而明确本项目的创新落点（离线降级，需人工细化）".format(point)
        )
    else:
        statement = (
            "若针对「{}」提出一种显式的方法或评测方案，则在给定数据与指标约束下"
            "能检验其相对现有文献的优势（离线降级，需人工细化）".format(point)
        )
    return {
        "statement": statement,
        "falsification": "若与现有文献结论无显著差异、或无法构造可对照的验证条件，则该假设被证伪",
    }


def _hypothesize_with_llm(entry: Dict[str, Any], gaps: List[Dict[str, Any]],
                          llm: Optional[LLMProvider]) -> Optional[List[Dict[str, str]]]:
    """LLM 为每条 gap 生成 if-then 假设；失败/结构非法返回 None。"""
    if llm is None or not gaps:
        return None
    user = json.dumps({
        "query": entry.get("query", ""),
        "gaps": [
            {
                "gap_id": g.get("gap_id"),
                "type": g.get("type"),
                "claim_point": g.get("claim_point"),
                "description": g.get("description"),
                "angle": g.get("angle"),
            }
            for g in gaps
        ],
    }, ensure_ascii=False)
    try:
        result = llm.complete(_HYPOTHESIS_SYSTEM, user, HYPOTHESIS_SCHEMA, temperature=0.4)
    except (LLMError, SchemaError):
        return None
    if not isinstance(result, dict):
        return None
    raw = result.get("hypotheses")
    if not isinstance(raw, list):
        return None
    out: List[Dict[str, str]] = []
    for h in raw:
        if not isinstance(h, dict):
            out.append({})
            continue
        if_then = _clean(h.get("if_then"))
        fals = _clean(h.get("falsification"))
        out.append({"statement": if_then, "falsification": fals} if if_then else {})
    return out


def _hypothesize_entry(entry: Dict[str, Any], gaps: List[Dict[str, Any]],
                       llm: Optional[LLMProvider], start_id: int) -> List[Dict[str, Any]]:
    """为每条 gap 生成一条假设（LLM 命中用 LLM，否则确定性），hypothesis_id 全局唯一。"""
    if not gaps:
        return []
    statements = _hypothesize_with_llm(entry, gaps, llm)
    out: List[Dict[str, Any]] = []
    for i, gap in enumerate(gaps):
        stmt = statements[i] if (statements is not None and i < len(statements)) else None
        if not stmt or not stmt.get("statement"):
            stmt = _hypothesize_deterministic(gap)
        out.append({
            "hypothesis_id": "h{}".format(start_id + i + 1),
            "gap_ref": gap.get("gap_id"),
            "statement": stmt["statement"],
            "falsification": stmt.get("falsification", ""),
        })
    return out


# ---------------------------------------------------------------------------
# 读取工具（供 ideate / 测试复用）
# ---------------------------------------------------------------------------

def _entry_gaps(entry: Dict[str, Any]) -> List[Dict[str, Any]]:
    graph = entry.get("contradiction_graph") or {}
    return [g for g in (graph.get("gaps") or []) if isinstance(g, dict) and g.get("gap_id")]


def _entry_hypotheses(entry: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [h for h in (entry.get("hypotheses") or []) if isinstance(h, dict) and h.get("hypothesis_id")]


# ---------------------------------------------------------------------------
# 冻结入口（本模块内部编排，不改检索/ideate 的冻结签名）
# ---------------------------------------------------------------------------

def analyze_literature(literature: List[dict], llm: LLMProvider) -> List[dict]:
    """对每条 literature 条目依次执行 理解 → 矛盾/gap 挖掘 → 假设生成，原地丰富并返回。

    - 每篇论文写 ``understanding``；
    - 每条目写 ``contradiction_graph``（nodes/edges/gaps，gaps 的 type ∈ {gap, contradiction}）；
    - 每条目写 ``hypotheses``（每条 gap 一条 if-then 可证伪假设）；
    - gap_id / hypothesis_id 全局唯一（跨条目不重号），供 idea 追溯引用。

    无 LLM / 异常时退化为确定性规则，绝不抛异常。
    """
    gap_counter = 0
    hyp_counter = 0
    for entry in literature or []:
        if not isinstance(entry, dict):
            continue
        papers = [p for p in (entry.get("papers") or []) if isinstance(p, dict)]

        # ① 文献理解
        _understand_entry(entry, papers, llm)

        # ② 矛盾/gap 挖掘
        graph = _mine_entry(entry, papers, llm, start_id=gap_counter)
        entry["contradiction_graph"] = graph
        gaps = graph.get("gaps") or []
        gap_counter += len(gaps)

        # ③ 假设生成
        hypotheses = _hypothesize_entry(entry, gaps, llm, start_id=hyp_counter)
        entry["hypotheses"] = hypotheses
        hyp_counter += len(hypotheses)

    return literature
