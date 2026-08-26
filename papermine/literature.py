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
- 每篇论文新增 ``evidence_card``（M19 论文级证据卡）：{title / dataset / baseline / metric /
  main_gain / limitation / claim_strength / evidence_source}。**字段提取不到一律标 null，
  禁止 LLM 编造**（否则 novelty 会漂）；``evidence_source`` ∈ {abstract, fulltext, table}
  由系统按论文实际可用的证据层级确定性回填（当前检索只给摘要 → 恒为 abstract，fulltext /
  table 需后续「全文下载 + 表格解析」另立项）。
- 每条 literature 新增 ``contradiction_graph``：{nodes / edges / gaps}，其中 gaps 的
  ``type`` ∈ {contradiction, gap}，矛盾（contradiction）同时给出节点间 contradiction 边。
- 每条 literature 新增 ``hypotheses``：每条 gap 对应一条可证伪假设（if-then + falsification）。

降级路径（architecture §7 / §8）：无 LLM（NullProvider）、LLMError、SchemaError、或 LLM
返回结构非法时，退化为确定性规则（词面级 understanding / 证据卡只做保守关键词抽取且
baseline·gain·limitation·claim_strength 一律 null / 每条目一条 gap / 每条 gap 一条
if-then 假设），保证六步流水线在离线/异常下也能产出可追溯的 gap → hypothesis → idea 链。
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from .llm import LLMError, LLMProvider, SchemaError
from .parallel import map_parallel

__all__ = [
    "analyze_literature",
    "BATCH_UNDERSTANDING_SCHEMA",
    "MINING_SCHEMA",
    "HYPOTHESIS_SCHEMA",
    "EVIDENCE_CARD_SCHEMA",
    "BATCH_EVIDENCE_CARD_SCHEMA",
    "EVIDENCE_SOURCES",
    "MAX_UNDERSTAND_PAPERS",
    "MAX_GAPS",
    "_understand_deterministic",
    "_mine_deterministic",
    "_hypothesize_deterministic",
    "_evidence_source_for",
    "_evidence_card_deterministic",
    "_sanitize_evidence_card",
    "_extract_evidence_cards_with_llm",
    "_extract_evidence_entry",
    "_build_graph",
    "_assign_gap_ids",
    "_entry_gaps",
    "_entry_hypotheses",
]

# 每个条目送入 LLM 理解/挖掘的论文数上限（控制 token 预算）；超出的论文走确定性理解
MAX_UNDERSTAND_PAPERS = 8
# 每条文献条目最多保留的 gap/矛盾数量（防止图无限膨胀）
MAX_GAPS = 8

# 论文级证据卡的证据来源层级（docs/build-plan.md §4 M19）：abstract=弱证据，fulltext/table=强证据
EVIDENCE_SOURCES: Tuple[str, ...] = ("abstract", "fulltext", "table")
# 证据卡 claim_strength 的取值：strong=绝对化/全称断言，moderate=有对比的改进主张，weak=探索性/受限主张
_CLAIM_STRENGTHS: Tuple[str, ...] = ("strong", "moderate", "weak")
# 证据卡中由 LLM 从摘要提取的可空字段（缺失一律 null，绝不编造）；title / evidence_source 由系统确定性回填
_EVIDENCE_FIELDS: Tuple[str, ...] = (
    "dataset", "baseline", "metric", "main_gain", "limitation", "claim_strength",
)

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

# 论文级证据卡（docs/build-plan.md §4 M19）：每篇论文固定 8 字段。
# title / evidence_source 由系统确定性回填（title 取论文真实标题、evidence_source 按可用证据层级）；
# 其余 6 字段由 LLM 从摘要提取，提取不到一律 null，绝不编造（否则 novelty 会漂）。
EVIDENCE_CARD_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "title", "dataset", "baseline", "metric",
        "main_gain", "limitation", "claim_strength", "evidence_source",
    ],
    "properties": {
        "title": {"type": "string"},
        "dataset": {"type": ["string", "null"]},
        "baseline": {"type": ["string", "null"]},
        "metric": {"type": ["string", "null"]},
        "main_gain": {"type": ["string", "null"]},
        "limitation": {"type": ["string", "null"]},
        "claim_strength": {
            "type": ["string", "null"],
            "enum": list(_CLAIM_STRENGTHS) + [None],
        },
        "evidence_source": {"type": "string", "enum": list(EVIDENCE_SOURCES)},
    },
}

# 批量证据卡提取：一次 LLM 调用返回该条目下多篇论文的证据卡
BATCH_EVIDENCE_CARD_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["papers"],
    "properties": {
        "papers": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["title", "evidence_card"],
                "properties": {
                    "title": {"type": "string"},
                    "evidence_card": EVIDENCE_CARD_SCHEMA,
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

_EVIDENCE_SYSTEM = (
    "你是 papermine 的「论文级证据卡提取器」。给定检索命中的论文（仅标题/摘要），"
    "为每篇论文提取一张证据卡，作为 novelty 判断的证据基础。\n"
    "字段说明（除 title / evidence_source 外均可空）：\n"
    "1. dataset：论文实验使用的数据集/基准名（如 C-MAPSS、ImageNet）；摘要未明确提及 → null；\n"
    "2. baseline：论文对比的基线方法（如 LSTM、SVM）；摘要未明确提及 → null；\n"
    "3. metric：论文使用的评测指标（如 RMSE、accuracy、F1）；摘要未明确提及 → null；\n"
    "4. main_gain：论文声称的主要提升/收益（如「精度相对基线提升 5%」）；摘要未明确给出 → null；\n"
    "5. limitation：论文自述或可见的局限/边界；摘要未提及 → null；\n"
    "6. claim_strength：论文主张强度，只能取 strong（绝对化/全称断言，如「首次」「最优」「state-of-the-art」）、"
    "moderate（有对比的改进主张）、weak（探索性/受限主张）；无法判断 → null；\n"
    "7. evidence_source：一律填 abstract（当前检索仅提供摘要；系统会据实回填，本字段仅供占位）。\n"
    "铁律：只提取给定文本中【明确出现】的信息；任何未明确出现的字段一律填 null，"
    "绝不根据常识或领域背景补全、绝不编造 baseline / gain。title 必须逐字等于给定论文标题。"
    "只输出 JSON，严格满足给定 schema。"
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

# 证据卡确定性降级用的保守词典（词首边界匹配，宁可漏提不可误提）：
# 仅收录可无歧义识别的数据集名 / 评测指标；baseline / main_gain / limitation / claim_strength
# 不在此列（无 LLM 时无法可靠提取，一律 null，绝不编造）。
_DATASET_TERMS: Tuple[str, ...] = (
    "C-MAPSS", "Turbofan", "PHM", "MNIST", "Fashion-MNIST",
    "CIFAR-10", "CIFAR-100", "ImageNet", "COCO", "SQuAD", "LibriSpeech",
)
_METRIC_TERMS: Tuple[str, ...] = (
    "RMSE", "MAE", "MAPE", "MSE", "accuracy", "F1", "AUC", "AUROC",
    "precision", "recall", "R-squared", "R2",
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
# ①½ 论文级证据卡（M19）：LLM 提取 + 确定性降级，字段缺失一律 null，绝不编造
# ---------------------------------------------------------------------------

def _nullable(s: Any) -> Optional[str]:
    """把任意值折叠为单行文本；空/None/纯空白 → None（证据卡「未提取」的规范表示）。"""
    return _clean(s) or None


def _evidence_source_for(paper: Dict[str, Any]) -> str:
    """按论文实际可用的证据层级确定性回填 evidence_source。

    层级（docs/build-plan.md §4 M19）：table（已解析表格，最强）> fulltext（全文，强）>
    abstract（仅摘要，弱）。当前检索（arXiv + Semantic Scholar）只给摘要，故 v1 恒为
    abstract；fulltext / table 需后续「全文下载 + 表格解析」另立项后才会命中。
    """
    if not isinstance(paper, dict):
        return "abstract"
    tables = paper.get("tables")
    if isinstance(tables, (list, tuple)) and len(tables) > 0:
        return "table"
    if _clean(paper.get("fulltext")):
        return "fulltext"
    return "abstract"


def _match_terms(text: str, terms: Tuple[str, ...]) -> List[str]:
    """保守的词首边界匹配（忽略大小写），返回命中的术语；宁缺毋滥。"""
    out: List[str] = []
    for t in terms:
        if re.search(r"\b" + re.escape(t) + r"\b", text, re.IGNORECASE):
            out.append(t)
    return out


def _join_or_none(items: List[str]) -> Optional[str]:
    """列表非空 → 「、」连接字符串；空 → None。"""
    return "、".join(items) if items else None


def _evidence_card_deterministic(paper: Dict[str, Any]) -> Dict[str, Any]:
    """无 LLM 时的证据卡确定性降级：仅做保守的 dataset/metric 关键词抽取，其余字段一律 null。

    baseline / main_gain / limitation / claim_strength 无法用词面规则可靠提取，按 M19 铁律
    标 null（**不编造**）；dataset / metric 只在命中明确词典术语时取值，否则也 null。
    """
    title = _paper_title(paper)
    text = (title + " " + _clean(paper.get("abstract"))).lower()
    return {
        "title": title,
        "dataset": _join_or_none(_match_terms(text, _DATASET_TERMS)),
        "baseline": None,
        "metric": _join_or_none(_match_terms(text, _METRIC_TERMS)),
        "main_gain": None,
        "limitation": None,
        "claim_strength": None,
        "evidence_source": _evidence_source_for(paper),
    }


def _sanitize_evidence_card(raw: Any, title: str, paper: Dict[str, Any]) -> Dict[str, Any]:
    """把 LLM 原始证据卡规范化为最终 8 字段卡；任何非法输入退化为确定性降级。

    - ``title`` 恒取论文真实标题（杜绝标题编造）；
    - ``evidence_source`` 恒由 ``_evidence_source_for`` 确定性回填（杜绝来源虚标）；
    - 6 个可空字段：非字符串 / 空串 → null；claim_strength 非法枚举 → null。
    """
    if not isinstance(raw, dict):
        return _evidence_card_deterministic(paper)
    card: Dict[str, Any] = {"title": title}
    for field in _EVIDENCE_FIELDS:
        val = raw.get(field)
        if field == "claim_strength":
            card[field] = val if val in _CLAIM_STRENGTHS else None
        else:
            card[field] = _nullable(val)
    card["evidence_source"] = _evidence_source_for(paper)
    return card


def _extract_evidence_cards_with_llm(entry: Dict[str, Any], papers: List[Dict[str, Any]],
                                     llm: Optional[LLMProvider]) -> Optional[Dict[str, Dict[str, Any]]]:
    """LLM 批量提取证据卡，返回 {论文标题: 原始证据卡}；失败/无 LLM/结构非法返回 None。"""
    if llm is None or not papers:
        return None
    subset = papers[:MAX_UNDERSTAND_PAPERS]
    user = json.dumps({
        "query": entry.get("query", ""),
        "papers": [
            {
                "title": _paper_title(p),
                "abstract": _clip(p.get("abstract"), 500),
                "venue": p.get("venue", ""),
                "year": p.get("year"),
            }
            for p in subset
        ],
    }, ensure_ascii=False)
    try:
        result = llm.complete(_EVIDENCE_SYSTEM, user, BATCH_EVIDENCE_CARD_SCHEMA, temperature=0.1)
    except (LLMError, SchemaError):
        return None
    if not isinstance(result, dict):
        return None
    raw = result.get("papers")
    if not isinstance(raw, list):
        return None
    out: Dict[str, Dict[str, Any]] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        title = _clean(item.get("title"))
        card = item.get("evidence_card")
        if title and isinstance(card, dict):
            out[title] = card
    return out


def _extract_evidence_entry(entry: Dict[str, Any], papers: List[Dict[str, Any]],
                            llm: Optional[LLMProvider]) -> None:
    """对条目内所有论文写入 evidence_card（LLM 命中用 LLM，未命中/失败走确定性降级）。"""
    cards = _extract_evidence_cards_with_llm(entry, papers, llm)
    for paper in papers:
        title = _paper_title(paper)
        raw = (cards or {}).get(title) if title else None
        paper["evidence_card"] = _sanitize_evidence_card(raw, title, paper)


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


def _finalize_hypotheses(gaps: List[Dict[str, Any]],
                         statements: Optional[List[Dict[str, str]]],
                         start_id: int) -> List[Dict[str, Any]]:
    """把 LLM/确定性产出的假设语句按 gap 顺序装配，赋全局唯一 hypothesis_id。

    - ``statements`` 为 None（LLM 失败/无 LLM）或某条缺失/非法时，该条退化为确定性 if-then；
    - ``hypothesis_id`` 从 ``start_id`` 起递增，``gap_ref`` 回指对应 gap 的 ``gap_id``。
    """
    if not gaps:
        return []
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
    """对每条 literature 条目依次执行 理解 → 证据卡(M19) → 矛盾/gap 挖掘 → 假设生成，原地丰富并返回。

    - 每篇论文写 ``understanding``；
    - 每篇论文写 ``evidence_card``（M19 论文级证据卡，8 字段；提取不到一律 null，绝不编造，
      ``evidence_source`` 确定性回填）；
    - 每条目写 ``contradiction_graph``（nodes/edges/gaps，gaps 的 type ∈ {gap, contradiction}）；
    - 每条目写 ``hypotheses``（每条 gap 一条 if-then 可证伪假设）；
    - gap_id / hypothesis_id 全局唯一（跨条目不重号），供 idea 追溯引用。

    无 LLM / 异常时退化为确定性规则，绝不抛异常。

    M16 方向⑥：①理解 + ①½证据卡 + ②挖掘（跨条目独立）与③假设生成（跨条目独立）**并行执行**；
    赋全局唯一 gap_id / hypothesis_id 的两步为顺序（纯确定性、无网络 I/O），保证编号
    严格按「条目序 + 条目内序」可复现。
    """
    entries = [e for e in (literature or []) if isinstance(e, dict)]
    if not entries:
        return literature

    # ① 文献理解 + ①½ 论文级证据卡（M19）+ ② 矛盾/gap 挖掘：跨条目不共享状态 → 并行。
    def _understand_and_mine(entry: Dict[str, Any]) -> Tuple[List[Dict[str, Any]],
                                                              List[Dict[str, str]],
                                                              List[Dict[str, str]]]:
        papers = [p for p in (entry.get("papers") or []) if isinstance(p, dict)]
        _understand_entry(entry, papers, llm)
        _extract_evidence_entry(entry, papers, llm)
        gaps, conts = _mine_with_llm(entry, papers, llm)
        if gaps is None or conts is None:
            gaps, conts = _mine_deterministic(entry, papers)
        return papers, gaps, conts

    mined = map_parallel(_understand_and_mine, entries)

    # 顺序赋全局唯一 gap_id（确定性，无网络 I/O）。
    gap_counter = 0
    for entry, (papers, gaps, conts) in zip(entries, mined):
        graph = _build_graph(papers, gaps, conts, start_id=gap_counter)
        entry["contradiction_graph"] = graph
        gap_counter += len(graph.get("gaps") or [])

    # ③ 假设生成：跨条目不共享状态 → 并行（用已赋的 gap_id）。
    def _hypothesize(entry: Dict[str, Any]) -> Tuple[List[Dict[str, Any]],
                                                     Optional[List[Dict[str, str]]]]:
        graph = entry.get("contradiction_graph") or {}
        gaps = [g for g in (graph.get("gaps") or [])
                if isinstance(g, dict) and g.get("gap_id")]
        statements = _hypothesize_with_llm(entry, gaps, llm)
        return gaps, statements

    hypothesized = map_parallel(_hypothesize, entries)

    # 顺序赋全局唯一 hypothesis_id（确定性）。
    hyp_counter = 0
    for entry, (gaps, statements) in zip(entries, hypothesized):
        hypotheses = _finalize_hypotheses(gaps, statements, start_id=hyp_counter)
        entry["hypotheses"] = hypotheses
        hyp_counter += len(hypotheses)

    return literature
