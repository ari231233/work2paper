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
  main_gain / limitation / claim_strength / evidence_source}。``evidence_source`` ∈
  {abstract, fulltext, table} 由系统按论文实际可用的证据层级确定性回填（当前检索只给摘要 →
  恒为 abstract，fulltext / table 需后续「全文下载 + 表格解析」另立项）。
  **M19 v2（正向约束防过度保守）**：提取指令从「默认 null」改为「默认提取、提取不到才 null」——
  摘要中**明确出现**的数据集 / 指标 / baseline / gain 必须提取，只有摘要里**完全没有**相关
  信息的字段才标 null；并新增 ``_positive_backfill``，用保守词典把 LLM 误判成 null 的
  dataset / metric 确定性回填（绝不编造摘要里没有的值），兜住「过度保守」的尾部风险。
- 每条 literature 新增 ``contradiction_graph``：{nodes / edges / gaps}，其中 gaps 的
  ``type`` ∈ {contradiction, gap}，矛盾（contradiction）同时给出节点间 contradiction 边。
- 每条 literature 新增 ``hypotheses``：每条 gap 对应一条可证伪假设（if-then + falsification）。
- **M18（证据级别）**：每条 ``type="gap"`` 记录附 ``gap_hypothesis``
  （{claim / evidence_level / basis / scope}），把「无人覆盖」从事实断言降级为
  **证据有界的假设**。``evidence_level`` ∈ {weak, moderate, strong} 由检索样本量、
  系统性、相关性、有无反例**确定性计算**（architecture §8「LLM 自评不可靠」），
  禁止全称断言（**absence of evidence ≠ evidence of absence**）。矛盾
  （``type="contradiction"``）是**正证据**（两篇论文结论冲突），``evidence_level`` 恒为 strong。

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
    "EVIDENCE_LEVELS",
    "GAP_HYPOTHESIS_SCHEMA",
    "MAX_UNDERSTAND_PAPERS",
    "MAX_GAPS",
    "_compute_evidence_level",
    "_soften_universal",
    "_hypothesis_claim",
    "_hypothesis_basis",
    "_hypothesis_scope",
    "_build_gap_hypothesis",
    "_gap_evidence_level",
    "_entry_gap_evidence_levels",
    "_literature_gap_evidence_levels",
    "_understand_deterministic",
    "_mine_deterministic",
    "_hypothesize_deterministic",
    "_evidence_source_for",
    "_evidence_card_deterministic",
    "_positive_backfill",
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
# 证据卡中由 LLM 从摘要提取的可空字段（摘要真没有才 null，绝不编造）；title / evidence_source
# 由系统确定性回填；dataset / metric 在 LLM 误判为 null 时由 _positive_backfill 用词典兜底回填。
_EVIDENCE_FIELDS: Tuple[str, ...] = (
    "dataset", "baseline", "metric", "main_gain", "limitation", "claim_strength",
)

# M18：gap 假设的证据级别（注意与 M12 的 evidence∈{weak,medium,strong} 是两套不同概念：
# 本枚举描述「gap 假设本身有多少证据」，M12 描述「idea 的 claim 有多少证据」）。
EVIDENCE_LEVELS: Tuple[str, ...] = ("weak", "moderate", "strong")

# 检索源 -> 展示名（供 gap_hypothesis.scope 渲染）
_SOURCE_DISPLAY: Dict[str, str] = {
    "arxiv": "arXiv",
    "semantic_scholar": "Semantic Scholar",
}

# 全称断言标记（M18 禁止 gap 输出此类断言）：命中即软化为「基于本次检索」的假设式表述。
# 排列顺序：更具体/更长的标记在前，避免部分替换导致歧义。
_UNIVERSAL_MARKERS: Tuple[Tuple[str, str], ...] = (
    ("所有论文均未", "检索到的论文均未"),
    ("所有工作均未", "检索到的工作均未"),
    ("领域内无人", "检索范围内未发现有人"),
    ("领域无人", "检索范围内未发现有人"),
    ("没有人做", "检索范围内未发现有人做"),
    ("没人做过", "检索范围内未发现有人做过"),
    ("没人做", "检索范围内未发现有人做"),
    ("无人做过", "检索范围内未发现有人做过"),
    ("无人研究", "检索范围内未发现相关研究"),
    ("无人涉足", "检索范围内未发现相关工作"),
    ("从未有人", "检索范围内未发现有人"),
    ("从未提出", "检索范围内未发现提出"),
    ("没有任何", "检索范围内未发现"),
    ("尚无任何", "检索范围内未发现"),
    ("缺乏任何", "检索范围内未发现"),
    ("未见任何", "检索范围内未发现"),
    ("整个领域", "检索范围"),
    ("领域空白", "检索范围内未见明确覆盖"),
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

# gap 假设（M18）：由系统确定性构造（非 LLM 输出），作为 gap 记录的 ``gap_hypothesis`` 子对象。
# claim 恒为「尚未发现…（假设，非事实）」形式；evidence_level 由检索样本量/系统性/相关性/反例
# 确定性计算；basis/scope 标注证据边界。此 schema 仅作文档与测试契约，不参与 LLM 校验。
GAP_HYPOTHESIS_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["claim", "evidence_level", "basis", "scope"],
    "properties": {
        "claim": {"type": "string"},
        "evidence_level": {"type": "string", "enum": list(EVIDENCE_LEVELS)},
        "basis": {"type": "string"},
        "scope": {"type": "string"},
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
    "字段说明（title 与 evidence_source 由系统据实回填，你只需填其余 6 字段）：\n"
    "1. dataset：论文实验使用的数据集/基准名（如 C-MAPSS、SWaT、WADI、ImageNet）；\n"
    "2. baseline：论文对比的基线方法（如 LSTM、SVM、MLP）；\n"
    "3. metric：论文使用的评测指标（如 RMSE、accuracy、F1、precision、recall）；\n"
    "4. main_gain：论文声称的主要提升/收益（如「精度相对基线提升 5%」「outperform … by …」）；\n"
    "5. limitation：论文自述或可见的局限/边界；\n"
    "6. claim_strength：论文主张强度，只能取 strong（绝对化/全称断言，如「首次」「最优」「state-of-the-art」）、"
    "moderate（有对比的改进主张）、weak（探索性/受限主张）；无法判断才 null；\n"
    "7. evidence_source：一律填 abstract（当前检索仅提供摘要；系统会据实回填，本字段仅供占位）。\n"
    "提取规则（正向优先，务必遵守）：\n"
    "1. 摘要中【明确出现】的数据集名 / 指标名 / baseline / gain / 局限，必须提取、逐字填入对应字段；\n"
    "2. 默认提取：不要因为「怕编造」就把摘要里明确写着的值留成 null——只有摘要中【完全没有】"
    "某个字段对应信息的字段才填 null；\n"
    "3. 值必须逐字来自给定文本（可规整大小写，如 lstm → LSTM、f1 → F1），不得改写为别的名称；\n"
    "4. 绝不根据常识或领域背景补全、绝不编造摘要中未出现的 baseline / gain。\n"
    "title 必须逐字等于给定论文标题。只输出 JSON，严格满足给定 schema。"
)

_MINING_SYSTEM = (
    "你是 papermine 的「矛盾与缺口挖掘器」。给定同一检索查询命中的多篇论文及其结构化理解，"
    "跨论文比较以找出可成为创新点的矛盾与缺口。\n"
    "规则：\n"
    "1. gaps：找出这些论文都未覆盖的角度（例如某种约束、场景、数据设定、评测维度），"
    "每项给 claim_point（结论点/主题）、description（为何是缺口）、angle（未被覆盖的具体角度）。"
    "description 必须写成「基于检索到的论文，未发现…」的**证据有界假设**，"
    "只能断言「检索到的这些论文没做」，**禁止全称断言**（如「整个领域无人做」「所有论文均未」「没有任何工作」）；"
    "证据级别（evidence_level）由系统按检索样本量/系统性/相关性/反例确定性计算，你无需输出；\n"
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

# 证据卡确定性降级 / 正向回填共用的保守词典（词首边界匹配，宁可漏提不可误提）：
# 仅收录可无歧义识别的数据集名 / 评测指标；baseline / main_gain / limitation / claim_strength
# 不在此列（无 LLM 时无法可靠提取，一律 null，绝不编造）。
# M19 v2：加入工业时序 / 异常检测常用数据集 SWaT、WADI、CMAPSS（去连字符变体），
# 使「摘要明确提到 SWaT/WADI」的正向对照在 LLM 误判 null 时也能被确定性回填。
_DATASET_TERMS: Tuple[str, ...] = (
    "C-MAPSS", "CMAPSS", "Turbofan", "PHM", "MNIST", "Fashion-MNIST",
    "CIFAR-10", "CIFAR-100", "ImageNet", "COCO", "SQuAD", "LibriSpeech",
    "SWaT", "WADI",
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
# ①½ 论文级证据卡（M19 / M19 v2）：LLM 正向提取 + 确定性降级；摘要明确提到必须提取、
# 提取不到才 null（绝不编造），dataset/metric 在 LLM 误判 null 时由词典确定性回填。
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


def _positive_backfill(card: Dict[str, Any], paper: Dict[str, Any]) -> Dict[str, Any]:
    """M19 v2 正向对照兜底：LLM 把摘要中明确出现的 dataset/metric 误判为 null 时，用保守词典回填。

    - 只在字段为 null 时回填，绝不覆盖 LLM 已提取的正确值；
    - dataset / metric 用无歧义词典做词首边界匹配（宁可漏提不可误提），故回填安全——绝不可能
      编造摘要里没有的值；
    - baseline / main_gain / limitation / claim_strength 无法用词面规则可靠回填，保持 null
      （由提示词正向约束 LLM 提取，见 ``_EVIDENCE_SYSTEM``）。
    """
    title = _paper_title(paper)
    text = (title + " " + _clean(paper.get("abstract"))).lower()
    if card.get("dataset") is None:
        card["dataset"] = _join_or_none(_match_terms(text, _DATASET_TERMS))
    if card.get("metric") is None:
        card["metric"] = _join_or_none(_match_terms(text, _METRIC_TERMS))
    return card


def _sanitize_evidence_card(raw: Any, title: str, paper: Dict[str, Any]) -> Dict[str, Any]:
    """把 LLM 原始证据卡规范化为最终 8 字段卡；任何非法输入退化为确定性降级。

    - ``title`` 恒取论文真实标题（杜绝标题编造）；
    - ``evidence_source`` 恒由 ``_evidence_source_for`` 确定性回填（杜绝来源虚标）；
    - 6 个可空字段：非字符串 / 空串 → null；claim_strength 非法枚举 → null；
    - M19 v2：最终卡片统一过一遍 ``_positive_backfill``，把 LLM 误判成 null 的 dataset / metric
      用保守词典回填（只填 null、绝不编造）。
    """
    if not isinstance(raw, dict):
        card = _evidence_card_deterministic(paper)
    else:
        card = {"title": title}
        for field in _EVIDENCE_FIELDS:
            val = raw.get(field)
            if field == "claim_strength":
                card[field] = val if val in _CLAIM_STRENGTHS else None
            else:
                card[field] = _nullable(val)
        card["evidence_source"] = _evidence_source_for(paper)
    return _positive_backfill(card, paper)


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
        "基于检索到的 {} 篇论文（{}），尚未发现「{}」这一角度被明确覆盖"
        "（假设，非事实，离线降级，需人工核验）".format(len(papers), "、".join(titles[:3]) or "无标题", angle)
    )
    return [{"claim_point": angle, "description": desc, "angle": angle}], []


def _assign_gap_ids(gap_records: List[Dict[str, Any]], start: int) -> List[Dict[str, Any]]:
    """按全局计数器给 gap 记录赋唯一 id（g1, g2, ...）。"""
    for i, g in enumerate(gap_records):
        g["gap_id"] = "g{}".format(start + i + 1)
    return gap_records


# ---------------------------------------------------------------------------
# M18：gap 假设 + 证据级别（evidence_level）
# ---------------------------------------------------------------------------

def _soften_universal(text: str) -> str:
    """把 gap 描述里的全称断言软化为「基于本次检索」的假设式表述（M18 核心原则）。

    absence of evidence ≠ evidence of absence：LLM 只能证明「检索到的论文没做」，
    不能证明「整个领域没人做」。命中全称断言标记时替换为检索范围内的观察，并追加
    「假设，非事实，仅基于本次检索」提示。
    """
    s = _clean(text)
    if not s:
        return s
    changed = False
    for bad, good in _UNIVERSAL_MARKERS:
        if bad in s:
            s = s.replace(bad, good)
            changed = True
    if changed:
        s += "（假设，非事实，仅基于本次检索）"
    return s


def _source_names(sources: Any) -> str:
    """把 sources（如 ['arxiv','semantic_scholar']）渲染为展示名（arXiv、Semantic Scholar）。"""
    names: List[str] = []
    for s in (sources or []):
        key = str(s).strip().lower()
        names.append(_SOURCE_DISPLAY.get(key, str(s).strip()))
    names = [n for n in names if n]
    return "、".join(names) if names else "未知来源"


def _gap_terms(text: Any) -> List[str]:
    """把 claim_point/angle 拆成显著词（ASCII 词 + CJK 双字词），用于相关性/反例的确定性判定。"""
    s = _clean(text).lower()
    if not s:
        return []
    terms: List[str] = list(re.findall(r"[a-z0-9][a-z0-9\-]{1,}", s))
    cjk = "".join(ch for ch in s if "\u4e00" <= ch <= "\u9fff")
    terms.extend(cjk[i:i + 2] for i in range(len(cjk) - 1))
    return terms


def _paper_corpus(paper: Dict[str, Any]) -> str:
    """论文的可检索文本：标题 + 摘要 + 结构化理解的 claim/method/conclusion。"""
    parts = [_paper_title(paper), _clean(paper.get("abstract"))]
    u = paper.get("understanding")
    if isinstance(u, dict):
        parts.extend([_clean(u.get("claim")), _clean(u.get("method")), _clean(u.get("conclusion"))])
    return " ".join(p for p in parts if p).lower()


def _gap_evidence_stats(entry: Dict[str, Any], papers: List[Dict[str, Any]],
                        gap: Dict[str, Any]) -> Tuple[int, bool]:
    """确定性地估算 gap 的相关论文数与反例（M18 evidence_level 的输入信号）。

    - ``n_relevant``：标题/摘要/理解与 gap 的 claim_point 共享显著词的论文数（相关性代理）；
    - ``counterexample``：angle 与 claim_point 不同、且某篇论文正文直接命中 angle 显著词
      （提示该「无人覆盖的角度」可能已被覆盖，声明被削弱）。
    """
    point_terms = _gap_terms(gap.get("claim_point")) or _gap_terms(gap.get("angle"))
    angle_terms = _gap_terms(gap.get("angle"))
    point = _clean(gap.get("claim_point"))
    angle = _clean(gap.get("angle"))
    n_relevant = 0
    counterexample = False
    for p in papers:
        corpus = _paper_corpus(p)
        if not corpus:
            continue
        if any(t in corpus for t in point_terms):
            n_relevant += 1
        if angle and angle != point and angle_terms and any(t in corpus for t in angle_terms):
            counterexample = True
    return n_relevant, counterexample


def _compute_evidence_level(n_papers: int, n_sources: int,
                            n_relevant: int, counterexample: bool = False) -> str:
    """M18：由检索样本量、系统性、相关性、有无反例确定性计算 gap 假设的证据级别。

    规则（docs/build-plan.md §4 M18）：
    - 反例（检索到直接覆盖该 gap 角度的论文）或零样本 → weak（假设被削弱 / 无证据）；
    - 样本量：N ≥ 6 → strong 档，3 ≤ N < 6 → moderate 档，N < 3 → weak 档；
    - 系统性：≥ 2 个检索源（arXiv + Semantic Scholar）升一档（封顶 strong）；
    - 相关性：与 gap 角度相关的论文 < 2 篇 → 降一档（样本虽多但不相关，证据弱）。
    """
    if counterexample or n_papers <= 0:
        return "weak"
    if n_papers >= 6:
        level = 2
    elif n_papers >= 3:
        level = 1
    else:
        level = 0
    if n_sources >= 2:
        level = min(2, level + 1)
    if n_relevant < 2:
        level = max(0, level - 1)
    return EVIDENCE_LEVELS[level]


def _hypothesis_claim(claim_point: Any, angle: Any) -> str:
    """构造 gap 假设的 claim（恒为「尚未发现…（假设，非事实）」形式，杜绝全称断言）。"""
    point = _clean(claim_point) or "该方向"
    ang = _clean(angle)
    if not ang or ang == point:
        return "尚未发现{}（假设，非事实，仅基于本次检索）".format(point)
    return "尚未发现{}方面的{}（假设，非事实，仅基于本次检索）".format(point, ang)


def _hypothesis_basis(n_papers: int, n_relevant: int,
                      claim_point: Any, angle: Any) -> str:
    """构造 gap 假设的 basis（「基于 N 篇论文…均未…」，证据边界显式可见）。"""
    point = _clean(claim_point) or "该方向"
    ang = _clean(angle)
    target = ang if (ang and ang != point) else point
    if n_relevant >= 1:
        return "基于检索到的 {} 篇论文，其中 {} 篇与「{}」相关，但均未明确提出「{}」".format(
            n_papers, n_relevant, point, target)
    return "基于检索到的 {} 篇论文，未发现明确覆盖「{}」的工作".format(n_papers, target)


def _hypothesis_scope(query: Any, sources: Any, n_papers: int) -> str:
    """构造 gap 假设的 scope（检索范围 + query + 论文数，界定证据边界）。"""
    return "检索范围：{}，query {}，共 {} 篇".format(
        _source_names(sources), _clean(query) or "（未命名）", n_papers)


def _build_gap_hypothesis(entry: Dict[str, Any], papers: List[Dict[str, Any]],
                          gap: Dict[str, Any]) -> Dict[str, Any]:
    """为一条 gap 记录构造 gap_hypothesis（claim/evidence_level/basis/scope），全部确定性计算。"""
    n_papers = len(papers)
    sources = [s for s in (entry.get("sources") or []) if _clean(s)]
    n_relevant, counterexample = _gap_evidence_stats(entry, papers, gap)
    return {
        "claim": _hypothesis_claim(gap.get("claim_point"), gap.get("angle")),
        "evidence_level": _compute_evidence_level(
            n_papers, len(sources), n_relevant, counterexample),
        "basis": _hypothesis_basis(n_papers, n_relevant, gap.get("claim_point"), gap.get("angle")),
        "scope": _hypothesis_scope(entry.get("query"), sources, n_papers),
    }


def _build_graph(entry: Dict[str, Any], papers: List[Dict[str, Any]],
                 gaps: List[Dict[str, str]], contradictions: List[Dict[str, str]],
                 start_id: int) -> Dict[str, Any]:
    """把 gap/矛盾记录组装成 contradiction_graph（nodes/edges/gaps），gap_id 全局唯一。

    M18：gap 型记录附 ``gap_hypothesis``（claim/evidence_level/basis/scope，确定性构造）、
    ``description`` 经全称断言软化；矛盾型记录是正证据，``evidence_level`` 恒为 strong。
    """
    titles = [t for t in (_paper_title(p) for p in papers) if t]
    nodes = [{"id": "p:{}".format(i), "label": t, "kind": "paper"} for i, t in enumerate(titles)]
    index = {t: "p:{}".format(i) for i, t in enumerate(titles)}

    edges: List[Dict[str, str]] = []
    gap_records: List[Dict[str, Any]] = []

    for g in gaps[:MAX_GAPS]:
        gap_records.append({
            "gap_id": "",
            "type": "gap",
            "claim_point": _clean(g.get("claim_point")),
            "description": _soften_universal(g.get("description", "")),
            "angle": _clean(g.get("angle")),
            "paper_refs": [],
        })

    for c in contradictions[:MAX_GAPS]:
        a, b = c.get("paper_a", ""), c.get("paper_b", "")
        gap_records.append({
            "gap_id": "",
            "type": "contradiction",
            "claim_point": _clean(c.get("claim_point")),
            "description": _soften_universal(c.get("description", "")),
            "angle": "",
            "paper_refs": [a, b],
            # 矛盾 = 正证据（两篇论文结论冲突），证据级别天然 strong
            "evidence_level": "strong",
        })
        if a in index and b in index:
            edges.append({
                "source": index[a],
                "target": index[b],
                "kind": "contradiction",
                "claim_point": c.get("claim_point", ""),
            })

    _assign_gap_ids(gap_records, start_id)
    for gr in gap_records:
        if gr.get("type") == "gap":
            gr["gap_hypothesis"] = _build_gap_hypothesis(entry, papers, gr)
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


def _gap_evidence_level(gap: Dict[str, Any]) -> Optional[str]:
    """读取一条 gap/矛盾记录的 evidence_level。

    - gap 型记录在 ``gap_hypothesis.evidence_level`` 内；
    - 矛盾型记录在顶层 ``evidence_level``（正证据，恒 strong）；
    - 旧格式（无 M18 字段）返回 None。
    """
    if not isinstance(gap, dict):
        return None
    gh = gap.get("gap_hypothesis")
    if isinstance(gh, dict) and gh.get("evidence_level") in EVIDENCE_LEVELS:
        return gh["evidence_level"]
    lv = gap.get("evidence_level")
    return lv if lv in EVIDENCE_LEVELS else None


def _entry_gap_evidence_levels(entry: Dict[str, Any]) -> List[str]:
    """收集一个文献条目内全部 gap/矛盾的 evidence_level（保序、去空）。"""
    out: List[str] = []
    for g in _entry_gaps(entry):
        lv = _gap_evidence_level(g)
        if lv:
            out.append(lv)
    return out


def _literature_gap_evidence_levels(literature: Any) -> List[str]:
    """跨文献条目收集全部 gap/矛盾的 evidence_level（供 M11/M12 消费）。"""
    out: List[str] = []
    for entry in (literature or []):
        if isinstance(entry, dict):
            out.extend(_entry_gap_evidence_levels(entry))
    return out


# ---------------------------------------------------------------------------
# 冻结入口（本模块内部编排，不改检索/ideate 的冻结签名）
# ---------------------------------------------------------------------------

def analyze_literature(literature: List[dict], llm: LLMProvider) -> List[dict]:
    """对每条 literature 条目依次执行 理解 → 证据卡(M19) → 矛盾/gap 挖掘 → 假设生成，原地丰富并返回。

    - 每篇论文写 ``understanding``；
    - 每篇论文写 ``evidence_card``（M19 论文级证据卡，8 字段；M19 v2：摘要明确提到必须提取、
      提取不到才 null，绝不编造；dataset/metric 在 LLM 误判 null 时由词典确定性回填，
      ``evidence_source`` 确定性回填）；
    - 每条目写 ``contradiction_graph``（nodes/edges/gaps，gaps 的 type ∈ {gap, contradiction}；
      gap 型附 ``gap_hypothesis``＝{claim/evidence_level/basis/scope}，矛盾型 ``evidence_level``=strong）；
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
        graph = _build_graph(entry, papers, gaps, conts, start_id=gap_counter)
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
