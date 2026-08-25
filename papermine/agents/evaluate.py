"""⑤ 可行性评估 Agent：ideas -> evaluations（证据驱动）。

对应 docs/build-plan.md §3.3 / §4 M6 / §4 M11 与 docs/architecture.md §5 ⑤：

评估是**证据驱动**的，不是 LLM 自评（architecture §8「LLM 自评不可靠」）：

+-------------+--------------------------------+---------------------------+
| 维度        | 证据来源                       | 谁算                      |
+=============+================================+===========================+
| novelty     | literature.gap_note 对拍       | LLM 解释 + 检索事实        |
| 数据可得性  | assets.facts.data / metrics    | 确定性规则                |
| 工作量      | idea 复杂度 + 证据量           | LLM 估计（带确定性兜底）  |
| 档位        | 检索论文的 venue 分布          | 规则 + 静态档位库         |
| 风险        | metrics / baseline 缺失        | 规则                      |
+-------------+--------------------------------+---------------------------+

M11 升级：novelty 从单一 0~5 分改为**多维加权评分**（docs/build-plan.md §4 M11）：

- 5 个维度各 0~5，加权归一后总分 0~100：``总分 = Σ(权重 × 维度分) / 5``（权重合计 100）；
- 分数段映射旧 verdict：Reject / Weak Reject → drop、Revise → rework、Accept / Priority → proceed；
- 每维分数必须给出**差异化理由**（引用 gap_note / 文献证据），从机制上避免趋同；
- 无 LLM 时按维度规则粗估（gap 信号强弱 / 方法组合度 / 通用性等），标低置信。

M12 升级：与 novelty 评分**并列**加入「证据强度」子审查（docs/build-plan.md §4 M12）：

- 调 ``agents/evidence.validate_evidence`` 对每个 idea 做 4 维证据审查
  （文献对拍 / 理论支撑 / 实验设计支持 / claim 强度校准），输出 weak / medium / strong + 理由；
- ``evidence=weak`` 时把 verdict 下调为 ``rework``，随 verdict 一起回炉到 ④ 细化 claim；
- 结果写入每条 evaluation 的 ``evidence_validation`` 子对象，并挂进 evidence 证据链。

verdict ∈ {proceed, rework, drop}；每条评估必须挂 `evidence`（provenance 强制）。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..dossier import Dossier
from ..llm import LLMError, LLMProvider, SchemaError
from .evidence import CHECK_DIMENSIONS, validate_evidence

__all__ = [
    "run",
    "EVALUATE_SCHEMA",
    "NOVELTY_DIMENSIONS",
    "_data_feasibility",
    "_deterministic_dimensions",
    "_deterministic_workload",
    "_weighted_total",
    "_score_band",
    "_decide_verdict",
    "_rework_reason",
    "_append_evidence_validation",
    "_guess_venue",
    "_tier_of",
    "_venue_distribution",
]

# 本 Agent prompt 版本：优先读 prompts/evaluate.md 头的 version，缺失时用此兜底
_PROMPT_VERSION = "v2"
_PROMPT_FILENAME = "evaluate.md"
_PROMPT_VERSION_RE = re.compile(r"<!--\s*version:\s*(\d+)\s*-->")

# M11 多维加权 novelty 评分体系：维度名 -> (中文标签, 权重)。
# 权重合计 100；各维度分 0~5；总分 = Σ(权重 × 维度分) / 5 ∈ [0, 100]。
NOVELTY_DIMENSIONS: Tuple[Tuple[str, str, int], ...] = (
    ("problem_novelty", "问题新颖性", 20),   # 是否提出了过去未被充分解决的问题？
    ("method_novelty", "方法新颖性", 35),    # 核心方法是否有新机制，而非简单组合已有模块？
    ("technical_depth", "技术突破性", 20),   # 是否解决了关键技术瓶颈？
    ("gap", "与已有工作的差异程度", 15),     # 相比 SOTA 是否有明确区别？
    ("generalization", "可推广价值", 10),    # 能否迁移到其他任务？
)
_DIM_KEYS: Tuple[str, ...] = tuple(k for k, _l, _w in NOVELTY_DIMENSIONS)
_DIM_LABELS: Dict[str, str] = {k: label for k, label, _w in NOVELTY_DIMENSIONS}
_DIM_WEIGHTS: Dict[str, int] = {k: w for k, _l, w in NOVELTY_DIMENSIONS}

# M12 证据审查的 4 个维度（来自 agents/evidence.py，供报告/证据链复用）
_EVIDENCE_CHECK_LABELS: Tuple[Tuple[str, str], ...] = tuple(CHECK_DIMENSIONS)

# 静态档位库：检索到的 venue 名称 -> 档位（architecture §5 ⑤「规则 + 静态档位库」的 MVP 子集）
_VENUE_TIERS = {
    # CCF-A 顶会 / 顶刊
    "neurips": "CCF-A", "nips": "CCF-A", "icml": "CCF-A", "iclr": "CCF-A",
    "cvpr": "CCF-A", "iccv": "CCF-A", "eccv": "CCF-A", "acl": "CCF-A",
    "aaai": "CCF-A", "ijcai": "CCF-A", "kdd": "CCF-A", "sigmod": "CCF-A",
    "vldb": "CCF-A", "icde": "CCF-A", "sigir": "CCF-A", "www": "CCF-A",
    "tkde": "CCF-A", "tpami": "CCF-A",
    # CCF-B
    "icdm": "CCF-B", "sdm": "CCF-B", "cikm": "CCF-B", "ecml": "CCF-B",
    "pkdd": "CCF-B", "icassp": "CCF-B", "emnlp": "CCF-B", "coling": "CCF-B",
    "naacl": "CCF-B", "icpr": "CCF-B", "dasfaa": "CCF-B", "ecai": "CCF-B",
    "tkdd": "CCF-B", "tist": "CCF-B", "kais": "CCF-B",
    # CCF-C
    "pakdd": "CCF-C", "apweb": "CCF-C", "waim": "CCF-C", "adma": "CCF-C",
    "dexa": "CCF-C",
    # 预印本（未分级）
    "arxiv": "预印本（arXiv）", "semantic scholar": "预印本",
    # 中文核心
    "计算机学报": "中文核心（A类）", "软件学报": "中文核心（A类）",
    "自动化学报": "中文核心（A类）", "电子学报": "中文核心（A类）",
    "计算机研究与发展": "中文核心（A类）", "中文信息学报": "中文核心",
}


def _dim_schema() -> Dict[str, Any]:
    """单个评分维度的输出契约：0~5 分数 + 差异化理由。"""
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["score", "reason"],
        "properties": {
            "score": {"type": "number"},
            "reason": {"type": "string"},
        },
    }


# 本 Agent 的 LLM 输出契约（schema 校验走 papermine/llm.py 的极简子集）
EVALUATE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "novelty_dimensions", "workload_hours",
        "verdict_suggestion", "rework_reason",
    ],
    "properties": {
        "novelty_dimensions": {
            "type": "object",
            "additionalProperties": False,
            "required": list(_DIM_KEYS),
            "properties": {k: _dim_schema() for k in _DIM_KEYS},
        },
        "workload_hours": {"type": "number"},
        "verdict_suggestion": {
            "type": "string", "enum": ["proceed", "rework", "drop"],
        },
        "rework_reason": {"type": ["string", "null"]},
    },
}

_SYSTEM_PROMPT_FALLBACK = (
    "你是 papermine 的「可行性评估 Agent」。对候选创新点做证据驱动的可行性评估："
    "novelty 从 5 个维度分别打分（problem_novelty / method_novelty / technical_depth / "
    "gap / generalization，各 0~5，每维必须给出引用 gap_note 的差异化理由），估计工作量，"
    "给出 verdict_suggestion∈{proceed,rework,drop}，非 proceed 时给出 rework_reason。"
    "只输出符合 schema 的 JSON 对象。"
)


def _prompt_dir() -> Path:
    """返回包内 prompts 目录（papermine/prompts）。"""
    return Path(__file__).resolve().parent.parent / "prompts"


def _load_prompt() -> tuple:
    """读取 prompts/evaluate.md，返回 (system_prompt_text, version)。文件缺失时用内联兜底。"""
    path = _prompt_dir() / _PROMPT_FILENAME
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return _SYSTEM_PROMPT_FALLBACK, _PROMPT_VERSION
    m = _PROMPT_VERSION_RE.search(text)
    version = "v{}".format(m.group(1)) if m else _PROMPT_VERSION
    return text, version


# ---------------------------------------------------------------------------
# 确定性信号：数据可得性 / 检索 venue 分布 / gap 笔记
# ---------------------------------------------------------------------------

def _data_feasibility(facts: Dict[str, Any]) -> str:
    """数据可得性（确定性规则）：high = 有数据且有指标；medium = 有数据缺指标；low = 无数据。"""
    data = facts.get("data") or []
    metrics = facts.get("metrics") or []
    if data and metrics:
        return "high"
    if data:
        return "medium"
    return "low"


def _venue_distribution(literature: List[dict]) -> Dict[str, int]:
    """统计检索论文的 venue 分布（venue -> 出现次数）。"""
    dist: Dict[str, int] = {}
    for lit in literature or []:
        if not isinstance(lit, dict):
            continue
        for paper in lit.get("papers") or []:
            if not isinstance(paper, dict):
                continue
            venue = (
                paper.get("venue") or paper.get("journal")
                or paper.get("conference") or paper.get("venue_name")
                or paper.get("source")
            )
            if venue and str(venue).strip():
                v = str(venue).strip()
                dist[v] = dist.get(v, 0) + 1
    return dist


def _all_gap_notes(literature: List[dict]) -> List[str]:
    """收集全部 gap_note（去重保序），作为 novelty 对拍的证据。"""
    notes: List[str] = []
    for lit in literature or []:
        if not isinstance(lit, dict):
            continue
        gap = lit.get("gap_note")
        if gap and str(gap).strip() and gap not in notes:
            notes.append(gap)
    return notes


def _format_venue_distribution(dist: Dict[str, int]) -> str:
    if not dist:
        return "（检索论文未提供 venue 信息）"
    items = sorted(dist.items(), key=lambda kv: (-kv[1], kv[0]))
    return "，".join("{}×{}".format(v, k) for k, v in items)


def _tier_of(venue: str) -> str:
    """把单个 venue 名称映射到档位（静态档位库 + 兜底）。"""
    v = (venue or "").strip().lower()
    if not v:
        return "未知档位"
    for key, tier in _VENUE_TIERS.items():
        if key in v:
            return tier
    return "未分级（{}）".format((venue or "").strip()[:30] or "未知")


# ---------------------------------------------------------------------------
# 确定性兜底估算：novelty（多维粗估）/ workload / 档位
# ---------------------------------------------------------------------------

def _gap_signal_strength(gap_notes: List[str]) -> int:
    """gap 信号强度：0 = 无 gap（无法对拍）；1 = 弱信号；2 = 强信号（明确缺口）。"""
    if not gap_notes:
        return 0
    low = " ".join(str(g) for g in gap_notes).lower()
    strong = ("尚未", "没有系统", "缺乏系统", "no systematic", "rarely",
              "open problem", "未解决", "缺口", "未有", "少有")
    weak = ("gap", "不足", "limited", "missing", "challenge", "缺乏")
    if any(m in low for m in strong):
        return 2
    if any(m in low for m in weak):
        return 1
    return 0


def _deterministic_dimensions(idea: dict, gap_notes: List[str],
                              facts: Dict[str, Any]) -> Dict[str, Any]:
    """无 LLM 时的多维粗估（低置信，报告会标注）：按维度规则给分，保证维度间有区分度。

    规则信号（documented in build-plan §4 M11 要点 4）：
    - gap 信号强弱 → 问题新颖性 / 与已有工作的差异度；
    - 方法组合度 + 新机制关键词 → 方法新颖性；
    - 重方法 / 方法复杂度 → 技术突破性；
    - 通用 / 可复用主张 → 可推广价值。
    """
    methods = facts.get("methods") or []
    claim = " ".join(str(idea.get(k) or "") for k in ("claim", "novelty_hypothesis"))
    strength = _gap_signal_strength(gap_notes)
    gap_desc = {0: "无 gap_note，无法对拍", 1: "gap 信号弱", 2: "gap 信号强（明确缺口）"}[strength]

    # 1) 问题新颖性：gap 越强 → 问题越未被充分解决（无 gap 保守 2.0）
    problem_score = min(5.0, 2.0 + 0.5 * strength)

    # 2) 方法新颖性：新机制信号 vs 简单组合信号
    has_new_mechanism = any(k in claim for k in (
        "新机制", "自适应", "端到端", "可微", "联合优化", "自监督", "对比学习", "可学习"))
    has_combination = any(k in claim for k in ("结合", "组合", "集成", "混合", "融合"))
    if has_new_mechanism and not has_combination:
        method_score = 4.0
    elif has_new_mechanism:
        method_score = 3.5
    elif has_combination:
        method_score = 2.0
    else:
        method_score = 2.5

    # 3) 技术突破性：重方法 / 有方法信号
    heavy = any(m in ("深度学习", "集成学习", "随机森林", "XGBoost", "LSTM",
                      "Transformer", "图神经网络") for m in methods)
    tech_score = 3.5 if heavy else (2.5 if methods else 2.0)

    # 4) 与已有工作的差异度：直接映射 gap 强度（比问题新颖性更敏感）
    gap_score = min(5.0, 1.0 + 0.75 * strength)

    # 5) 可推广价值：通用 / 框架主张
    general_claim = any(k in claim for k in ("通用", "框架", "可复用", "平台", "工具"))
    gen_score = 4.0 if general_claim else 2.0

    gap_ref = str(gap_notes[0])[:120] if gap_notes else "（空）"
    return {
        "problem_novelty": {
            "score": round(problem_score, 1),
            "reason": "规则粗估（问题新颖性）：{}；gap_note={}".format(gap_desc, gap_ref),
        },
        "method_novelty": {
            "score": round(method_score, 1),
            "reason": "规则粗估（方法新颖性）：claim 新机制信号={}、简单组合信号={}，facts 方法数={}".format(
                has_new_mechanism, has_combination, len(set(methods))),
        },
        "technical_depth": {
            "score": round(tech_score, 1),
            "reason": "规则粗估（技术突破性）：facts.methods={}".format(
                "、".join(methods) if methods else "空"),
        },
        "gap": {
            "score": round(gap_score, 1),
            "reason": "规则粗估（与已有工作差异）：gap 信号强度={}".format(strength),
        },
        "generalization": {
            "score": round(gen_score, 1),
            "reason": "规则粗估（可推广价值）：通用/复用主张={}".format(general_claim),
        },
    }


def _weighted_total(dims: Dict[str, Any]) -> float:
    """加权合成 0~100 总分：``总分 = Σ(权重 × 维度分) / 5``（权重合计 100、维度分 0~5）。"""
    total = 0.0
    for key, _label, weight in NOVELTY_DIMENSIONS:
        item = (dims or {}).get(key)
        score = _coerce_number(item.get("score"), 0.0, 0.0, 5.0) \
            if isinstance(item, dict) else 0.0
        total += weight * float(score)
    return round(total / 5.0, 1)


def _score_band(score: float) -> Tuple[str, str]:
    """总分 → (Agent 建议动作标签, 旧 verdict)。

    分数段（docs/build-plan.md §4 M11，区间约定为左闭右开）：
        (-∞, 40)   Reject      → drop
        [40, 60)   Weak Reject → drop
        [60, 70)   Revise      → rework
        [70, 80]   Accept      → proceed
        (80, +∞)   Priority    → proceed
    """
    if score < 40:
        return ("Reject", "drop")
    if score < 60:
        return ("Weak Reject", "drop")
    if score < 70:
        return ("Revise", "rework")
    if score <= 80:
        return ("Accept", "proceed")
    return ("Priority", "proceed")


def _deterministic_workload(idea: dict, facts: Dict[str, Any]) -> int:
    """确定性工作量兜底：依据 facts 丰富度估算（有指标减负、缺指标加负、复杂方法加负）。"""
    methods = facts.get("methods") or []
    metrics = facts.get("metrics") or []
    base = 60
    if metrics:
        base -= 10
    else:
        base += 20
    base += min(len(methods), 4) * 10
    heavy = {"深度学习", "集成学习", "随机森林", "XGBoost"}
    if any(m in heavy for m in methods):
        base += 20
    return max(20, min(400, base))


def _is_tool_claim(claim: str) -> bool:
    """判定 idea 是否在主张「做工具/框架」，用强信号避免把方法标签「流水线/框架」误判为工具。"""
    if any(k in claim for k in ("工具", "平台", "通用框架", "可复用组件")):
        return True
    return ("组件" in claim) and ("抽象" in claim or "复用" in claim)


def _rule_venue_guess(facts: Dict[str, Any], idea: dict, novelty: float) -> str:
    """无检索 venue 时的规则档位猜测（复用 v0.1 mining 思路，依据 idea 类型 + novelty 总分）。"""
    claim = str(idea.get("claim") or "")
    if _is_tool_claim(claim):
        return "中文核心 / EI 会议（系统/工具类）"
    if "实证" in claim:
        return "中文核心 / 应用类期刊"
    if novelty >= 80:
        return "CCF-B / 中文核心或 EI 会议"
    if novelty >= 60:
        return "CCF-C / 中文核心"
    return "EI 会议 / 中文核心（创新度有限）"


def _guess_venue(facts: Dict[str, Any], idea: dict,
                 venue_dist: Dict[str, int], novelty: float) -> str:
    """档位匹配（确定性规则）：有检索 venue 分布则对照分布，否则退规则档位库。"""
    if venue_dist:
        tier_counts: Dict[str, int] = {}
        for venue, cnt in venue_dist.items():
            tier = _tier_of(venue)
            tier_counts[tier] = tier_counts.get(tier, 0) + cnt
        dominant = max(tier_counts.items(), key=lambda kv: (kv[1], kv[0]))[0]
        if "预印本" in dominant or "arXiv" in dominant:
            return "检索论文多为预印本，方向尚新；建议从 EI 会议 / 中文核心起步"
        return "检索论文档位集中（{}），建议对照 {}".format(
            _format_venue_distribution(venue_dist), dominant)
    return _rule_venue_guess(facts, idea, novelty)


# ---------------------------------------------------------------------------
# verdict 决策（分数段映射 + 证据驱动硬护栏 + LLM 建议）
# ---------------------------------------------------------------------------

def _decide_verdict(novelty: float, data_feasibility: str, workload: float,
                    suggestion: Optional[str], evidence: str = "medium") -> str:
    """综合 verdict：分数段映射旧 verdict 为基础，证据硬护栏优先，LLM 建议仅作下调参考。

    M12：``evidence=weak``（证据不足以支撑 claim）时下调为 ``rework``（回炉到 ④ 细化 claim），
    但**不覆盖 ``drop``**（新颖性不足判死时优先放弃）。
    """
    _band, base = _score_band(novelty)
    if data_feasibility == "low":
        return "rework"        # 无数据支撑，回炉补数据
    if workload > 400:
        return "rework"        # 工作量过大，需拆分或回炉
    if data_feasibility == "medium" and base == "drop":
        return "rework"        # 中低新颖性 + 数据不完整
    # M12：证据强度弱 → 回炉细化 claim（仅下调，不把 drop 上调）
    if evidence == "weak" and base != "drop":
        return "rework"
    # LLM 建议仅可下调（proceed -> rework/drop），不可把 drop 上调
    if suggestion in ("drop", "rework") and base == "proceed":
        return suggestion
    return base


def _rework_reason(verdict: str, novelty: float, data_feasibility: str,
                   workload: float, llm_reason: Optional[str] = None,
                   evidence_strength: Optional[str] = None,
                   evidence_reason: Optional[str] = None) -> Optional[str]:
    """生成 rework_reason（proceed 时为 None）。M12：evidence=weak 时给出证据不足的专属理由。"""
    if verdict == "proceed":
        return None
    band_label, _ = _score_band(novelty)
    if verdict == "drop":
        return "新颖性不足：novelty_score={}（{}），与文献 gap 对拍无明显差异，建议放弃该创新点".format(
            novelty, band_label)
    if data_feasibility == "low":
        return "数据可得性低：assets.facts 未识别到数据/指标，需回炉补充评测数据（回退①项目理解补采集）"
    if evidence_strength == "weak":
        return "证据不足：evidence=weak；{}（建议回炉到④细化 claim）".format(
            evidence_reason or "需补文献对拍/理论依据/实验设计")
    if novelty < 70:
        return "新颖性偏低：novelty_score={}（{}），建议回炉到②问题抽象/④创新点生成以强化 novelty".format(
            novelty, band_label)
    if workload > 400:
        return "工作量过大：workload={}h，建议拆分范围或回炉缩小目标".format(workload)
    if llm_reason:
        return str(llm_reason)
    return "需回炉打磨（详见 evidence）"


def _coerce_number(value: Any, default: Optional[float],
                   lo: Optional[float] = None, hi: Optional[float] = None) -> Optional[float]:
    """把值安全转为 float 并夹取区间；非数字返回 default。"""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        num = float(value)
        if lo is not None:
            num = max(lo, num)
        if hi is not None:
            num = min(hi, num)
        return num
    return default


# ---------------------------------------------------------------------------
# LLM 调用与证据装配
# ---------------------------------------------------------------------------

def _build_user_prompt(idea: dict, gap_notes: List[str],
                       venue_summary: str, facts: Dict[str, Any]) -> str:
    payload = {
        "idea": {
            "idea_id": idea.get("idea_id"),
            "claim": idea.get("claim"),
            "novelty_hypothesis": idea.get("novelty_hypothesis"),
            "problem_ref": idea.get("problem_ref"),
            "literature_refs": idea.get("literature_refs"),
        },
        "gap_notes": gap_notes,
        "venue_distribution": venue_summary,
        "facts": facts,
    }
    return "以下是一个候选创新点及其证据，请做证据驱动的可行性评估：\n" + json.dumps(
        payload, ensure_ascii=False)


def _call_llm(llm: LLMProvider, system_prompt: str, idea: dict,
              gap_notes: List[str], venue_summary: str,
              facts: Dict[str, Any]) -> Dict[str, Any]:
    """调用 LLM；任何失败/空结果都返回空 dict，由上层降级。"""
    result: Dict[str, Any] = {}
    try:
        result = llm.complete(
            system_prompt,
            _build_user_prompt(idea, gap_notes, venue_summary, facts),
            EVALUATE_SCHEMA, temperature=0.2,
        )
    except (LLMError, SchemaError):
        result = {}
    if not isinstance(result, dict):
        result = {}
    return result


def _extract_dimensions(raw: Any) -> Optional[Dict[str, Any]]:
    """从 LLM 输出提取并规范化 5 维度分；任一维度缺失/非法 → 返回 None（触发确定性兜底）。"""
    if not isinstance(raw, dict):
        return None
    dims: Dict[str, Any] = {}
    for key in _DIM_KEYS:
        item = raw.get(key)
        if not isinstance(item, dict):
            return None
        score = _coerce_number(item.get("score"), None, 0.0, 5.0)
        if score is None:
            return None
        reason = str(item.get("reason") or "").strip()
        if not reason:
            return None
        dims[key] = {"score": round(float(score), 1), "reason": reason}
    return dims


def _dims_converged(dims: Dict[str, Any]) -> bool:
    """判断 5 个维度分是否完全相等（LLM 趋同信号，用于报告可见性标注）。"""
    scores: List[float] = []
    for key in _DIM_KEYS:
        item = (dims or {}).get(key)
        if isinstance(item, dict) and isinstance(item.get("score"), (int, float)) \
                and not isinstance(item.get("score"), bool):
            scores.append(round(float(item["score"]), 1))
    return len(scores) == len(_DIM_KEYS) and len(set(scores)) == 1


def _assemble_evidence(gap_notes: List[str], facts: Dict[str, Any],
                       venue_dist: Dict[str, int], dims: Dict[str, Any],
                       degraded: bool, converged: bool) -> List[dict]:
    """装配评估证据链（provenance 强制：每条结论挂证据源，含 M11 分维度明细）。"""
    evidence: List[dict] = []
    if gap_notes:
        for g in gap_notes:
            evidence.append({"source": "literature.gap_note", "note": str(g)[:200]})
    else:
        evidence.append({"source": "literature.gap_note",
                         "note": "文献为空，novelty 无法对照，按规则保守估计"})
    data = facts.get("data") or []
    if data:
        evidence.append({"source": "assets.facts.data", "note": "数据标签：" + "、".join(data)})
    metrics = facts.get("metrics") or []
    if metrics:
        evidence.append({"source": "assets.facts.metrics", "note": "指标标签：" + "、".join(metrics)})
    if venue_dist:
        evidence.append({"source": "literature.venues",
                         "note": "检索论文档位分布：" + _format_venue_distribution(venue_dist)})
    # 分维度明细：每维分数 + 理由挂进证据链（M11）
    for key, label, _weight in NOVELTY_DIMENSIONS:
        item = (dims or {}).get(key)
        if isinstance(item, dict):
            reason = str(item.get("reason") or "").strip()
            if reason:
                evidence.append({
                    "source": "novelty_dimensions.{}".format(key),
                    "note": "{}={}，{}".format(label, item.get("score"), reason[:160]),
                })
    if degraded:
        evidence.append({
            "source": "degradation",
            "note": "novelty 维度分为确定性规则粗估（无 LLM 或 LLM 输出非法），低置信",
        })
    if converged:
        evidence.append({
            "source": "degradation",
            "note": "各维度分数趋同（区分度不足），建议人工复核",
        })
    return evidence


def _append_evidence_validation(evidence: List[dict],
                                ev_validation: Dict[str, Any]) -> None:
    """把 M12 证据强度 + 4 维检查挂进评估证据链（provenance 强制）。"""
    strength = ev_validation.get("evidence", "medium")
    reason = str(ev_validation.get("reason") or "").strip()
    evidence.append({
        "source": "evidence_validation",
        "note": "证据强度={}；{}".format(strength, reason[:200] or "（无理由）"),
    })
    for key, label in _EVIDENCE_CHECK_LABELS:
        item = (ev_validation.get("checks") or {}).get(key)
        if isinstance(item, dict):
            status = str(item.get("status") or "").strip()
            note = str(item.get("note") or "").strip()
            if status and note:
                evidence.append({
                    "source": "evidence_validation.{}".format(key),
                    "note": "{}={}，{}".format(label, status, note[:160]),
                })
    if ev_validation.get("degraded"):
        evidence.append({
            "source": "degradation",
            "note": "证据强度为确定性规则粗估（无 LLM 或 LLM 输出非法），低置信",
        })


def _evaluate_idea(idea: dict, facts: Dict[str, Any], gap_notes: List[str],
                   venue_dist: Dict[str, int], venue_summary: str,
                   data_feasibility: str, literature: List[dict],
                   llm: LLMProvider, system_prompt: str) -> dict:
    """对单个 idea 做证据驱动评估，返回一条 evaluation dict（含 M11 多维 novelty + M12 证据强度）。"""
    idea_id = str(idea.get("idea_id") or "").strip()
    out = _call_llm(llm, system_prompt, idea, gap_notes, venue_summary, facts)

    # M11：5 维度分（优先 LLM，否则确定性粗估），加权合成 0~100 总分
    dims = _extract_dimensions(out.get("novelty_dimensions"))
    degraded = dims is None
    if degraded:
        dims = _deterministic_dimensions(idea, gap_notes, facts)
    novelty = _weighted_total(dims)
    band_label, _ = _score_band(novelty)
    converged = _dims_converged(dims)

    workload = _coerce_number(out.get("workload_hours"), None, 10.0, 1000.0)
    if workload is None:
        workload = float(_deterministic_workload(idea, facts))
    workload = int(round(workload))

    suggestion = out.get("verdict_suggestion")
    if suggestion not in ("proceed", "rework", "drop"):
        suggestion = None

    llm_rework_reason = out.get("rework_reason")
    if not isinstance(llm_rework_reason, str) or not llm_rework_reason.strip():
        llm_rework_reason = None

    # M12：证据强度子审查（与 novelty 并列），不跑实验，只审证据
    ev_validation = validate_evidence(idea, literature, llm, facts)
    evidence_strength = ev_validation.get("evidence", "medium")
    evidence_reason = str(ev_validation.get("reason") or "").strip() or None

    venue_guess = _guess_venue(facts, idea, venue_dist, novelty)
    verdict = _decide_verdict(novelty, data_feasibility, float(workload),
                              suggestion, evidence=evidence_strength)
    rework_reason = _rework_reason(verdict, novelty, data_feasibility,
                                   float(workload), llm_rework_reason,
                                   evidence_strength=evidence_strength,
                                   evidence_reason=evidence_reason)
    evidence = _assemble_evidence(gap_notes, facts, venue_dist, dims, degraded, converged)
    _append_evidence_validation(evidence, ev_validation)

    return {
        "idea_ref": idea_id,
        "novelty_score": novelty,
        "novelty_band": band_label,
        "novelty_dimensions": dims,
        "evidence_validation": {
            "evidence": evidence_strength,
            "reason": evidence_reason or "",
            "checks": ev_validation.get("checks", {}),
            "degraded": bool(ev_validation.get("degraded")),
        },
        "data_feasibility": data_feasibility,
        "workload_hours": workload,
        "venue_guess": venue_guess,
        "verdict": verdict,
        "rework_reason": rework_reason,
        "evidence": evidence,
    }


# ---------------------------------------------------------------------------
# 入口（冻结契约）
# ---------------------------------------------------------------------------

def run(dossier: Dossier, llm: LLMProvider) -> None:
    """ideas -> evaluations（证据驱动，M11 多维加权 + M12 证据强度），原地写 dossier.evaluations。

    冻结契约（docs/build-plan.md §3.3）：
        def run(dossier: Dossier, llm: LLMProvider) -> None
    """
    assets = dossier.assets if isinstance(dossier.assets, dict) else {}
    facts = assets.get("facts") if isinstance(assets.get("facts"), dict) else {}
    literature = list(dossier.literature or [])
    ideas = list(dossier.ideas or [])

    gap_notes = _all_gap_notes(literature)
    venue_dist = _venue_distribution(literature)
    venue_summary = _format_venue_distribution(venue_dist)
    data_feasibility = _data_feasibility(facts)
    system_prompt, version = _load_prompt()

    evaluations: List[dict] = []
    for idea in ideas:
        if not isinstance(idea, dict) or not (idea.get("idea_id") or "").strip():
            continue
        evaluations.append(_evaluate_idea(
            idea, facts, gap_notes, venue_dist, venue_summary,
            data_feasibility, literature, llm, system_prompt,
        ))

    dossier.evaluations = evaluations
    dossier.meta.setdefault("prompt_versions", {})["evaluate"] = version
