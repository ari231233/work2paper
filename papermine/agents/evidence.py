"""M12 — 证据验证 Agent（Evidence Validation）。

对应 docs/build-plan.md §4 M12 与 docs/architecture.md §5 ⑤（证据驱动评估）：

- **职责**：验证候选 idea 的 claim 是否有**足够证据**支撑，输出「证据强度 + 理由」，
  帮学生判断「这个点子站不站得住」。**只做证据审查，不跑实验**。
- **流程位置**：⑤ 可行性评估（EVALUATE）内部，与 M11 的 novelty 多维评分**并列**，
  作为「证据强度」子审查；``evidence=weak`` 时随 verdict 一起回炉到 ④ 细化 claim
  （由 evaluate.py 落地为 ``verdict=rework``）。
- **输入**：``idea``（claim + novelty_hypothesis + literature_refs）＋ 文献对拍依据
  （M5 检索 + M5 v2 结构化理解 / gap）＋ 项目 facts（数据 / 指标，用于「能否被验证」）。
- **输出**：``evidence``（weak / medium / strong）＋ ``reason``（为什么弱、如何强化）。

四项检查维度（均为「证据审查」，而非「实验执行」）：

1. ``similar_work``（文献对拍）：有没有类似论文可以对拍？
2. ``theory_basis``（理论支撑）：有没有理论依据？
3. ``experiment_support``（实验设计支持）：别人做过什么、这个 claim 能否被验证？
4. ``claim_strength``（claim 强度校准）：这个 claim 是否过强？

降级路径（architecture §7 / §8）：无 LLM（NullProvider 返回空）、LLMError、SchemaError、
或 LLM 返回结构非法时，退化为**确定性规则**（按词面信号给四个维度定性，再聚合强度），
并标注 ``degraded=True``（低置信）。

本模块**不改 Dossier 顶层字段、不改冻结接口**（§3.2 / §3.3）：它只提供
``validate_evidence(idea, literature, llm, facts)`` 这一纯函数，由 evaluate.py 在
EVALUATE 内部调用并把结果写入每条 evaluation 的 ``evidence_validation`` 子对象。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..llm import LLMError, LLMProvider, SchemaError, complete_fast

__all__ = [
    "validate_evidence",
    "validate_evidence_batch",
    "EVIDENCE_SCHEMA",
    "EVIDENCE_BATCH_SCHEMA",
    "EVIDENCE_LEVELS",
    "CHECK_DIMENSIONS",
    "_deterministic_checks",
    "_aggregate_evidence",
    "_extract_checks",
    "_build_user_prompt",
    "_build_batch_user_prompt",
    "_call_llm",
    "_finalize_evidence",
]

# 本 Agent prompt 版本：优先读 prompts/evidence.md 头的 version，缺失时用此兜底
_PROMPT_VERSION = "v1"
_PROMPT_FILENAME = "evidence.md"
_PROMPT_VERSION_RE = re.compile(r"<!--\s*version:\s*(\d+)\s*-->")

# 证据强度三档
EVIDENCE_LEVELS: Tuple[str, ...] = ("weak", "medium", "strong")

# 四项检查维度：维度键 -> 中文标签（顺序即报告展示顺序）
CHECK_DIMENSIONS: Tuple[Tuple[str, str], ...] = (
    ("similar_work", "文献对拍"),
    ("theory_basis", "理论支撑"),
    ("experiment_support", "实验设计支持"),
    ("claim_strength", "claim 强度校准"),
)
_CHECK_KEYS: Tuple[str, ...] = tuple(k for k, _l in CHECK_DIMENSIONS)
_CHECK_LABELS: Dict[str, str] = {k: label for k, label in CHECK_DIMENSIONS}

# 单项检查的定性结论：ok = 该维度证据到位；concern = 有信号但不足/需明确；missing = 缺失
_CHECK_STATUSES: Tuple[str, ...] = ("ok", "concern", "missing")

_SYSTEM_PROMPT_FALLBACK = (
    "你是 papermine 的「证据验证 Agent」。对候选创新点（idea）的 claim 做**证据审查**："
    "判断「这个点子站不站得住」。你不跑实验、不虚构文献，只基于给定证据材料判断证据强度。\n"
    "从四个维度检查：similar_work（文献对拍：有没有类似论文、是否明确区别）、"
    "theory_basis（理论支撑：有没有理论依据）、experiment_support（实验设计支持："
    "别人做过什么、claim 能否被验证）、claim_strength（claim 是否过强）。\n"
    "每维给出 status∈{ok,concern,missing} + note；再给出整体 evidence∈{weak,medium,strong} "
    "与 reason（为什么弱、如何强化）。只输出符合 schema 的 JSON 对象。"
)

# 确定性信号词典：claim 过强 / 校准 / 差异化 / 理论支撑 的词面信号
_OVERSTRONG_MARKERS: Tuple[str, ...] = (
    "首创", "首个", "首次", "完全解决", "彻底解决", "超越所有", "全面超越",
    "颠覆", "革命性", "最优", "最强", "完美", "绝对", "state-of-the-art", "sota",
)
_HEDGE_MARKERS: Tuple[str, ...] = (
    "改进", "提升", "缓解", "减轻", "降低", "减少", "初步", "探索", "尝试",
    "轻量", "有限", "面向", "针对", "场景", "约束", "假设", "需核验",
    "需验证", "待验证", "特定",
)
_DIFF_MARKERS: Tuple[str, ...] = (
    "不同于", "区别于", "相较", "相比之下", "而非", "而不是", "而非简单",
    "未覆盖", "尚未", "缺口", "gap", "本文", "本项目", "我们提出",
    "现有方法", "现有工作", "与现有",
)
_THEORY_MARKERS: Tuple[str, ...] = (
    "机制", "原理", "理论", "定理", "因果", "归纳偏置", "可证伪", "if-then",
    "falsification", "收敛性", "最优性", "可证明",
)


def _prompt_dir() -> Path:
    """返回包内 prompts 目录（papermine/prompts）。"""
    return Path(__file__).resolve().parent.parent / "prompts"


def _load_prompt() -> Tuple[str, str]:
    """读取 prompts/evidence.md，返回 (system_prompt_text, version)。文件缺失时用内联兜底。"""
    path = _prompt_dir() / _PROMPT_FILENAME
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return _SYSTEM_PROMPT_FALLBACK, _PROMPT_VERSION
    m = _PROMPT_VERSION_RE.search(text)
    version = "v{}".format(m.group(1)) if m else _PROMPT_VERSION
    return text, version


# ---------------------------------------------------------------------------
# 结构化输出契约（schema 校验走 papermine/llm.py 的极简子集）
# ---------------------------------------------------------------------------

def _check_object() -> Dict[str, Any]:
    """单个检查维度的输出契约：status（ok/concern/missing）+ note（结论与依据）。"""
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["status", "note"],
        "properties": {
            "status": {"type": "string", "enum": list(_CHECK_STATUSES)},
            "note": {"type": "string"},
        },
    }


EVIDENCE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["evidence", "reason", "checks"],
    "properties": {
        "evidence": {"type": "string", "enum": list(EVIDENCE_LEVELS)},
        "reason": {"type": "string"},
        "checks": {
            "type": "object",
            "additionalProperties": False,
            "required": list(_CHECK_KEYS),
            "properties": {k: _check_object() for k in _CHECK_KEYS},
        },
    },
}

# M15 方向④：批量证据审查——一次 LLM 调用返回多个 idea 的证据审查结果。
EVIDENCE_BATCH_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["results"],
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["idea_id", "evidence", "reason", "checks"],
                "properties": {
                    "idea_id": {"type": "string"},
                    "evidence": {"type": "string", "enum": list(EVIDENCE_LEVELS)},
                    "reason": {"type": "string"},
                    "checks": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": list(_CHECK_KEYS),
                        "properties": {k: _check_object() for k in _CHECK_KEYS},
                    },
                },
            },
        },
    },
}


# ---------------------------------------------------------------------------
# 输入装配
# ---------------------------------------------------------------------------

def _clean(s: Any) -> str:
    return " ".join(str(s or "").split())


def _all_papers(literature: List[dict]) -> List[dict]:
    """收集全部检索论文（跨文献条目，去重保序）。"""
    papers: List[dict] = []
    seen: set = set()
    for entry in literature or []:
        if not isinstance(entry, dict):
            continue
        for p in entry.get("papers") or []:
            if not isinstance(p, dict):
                continue
            title = _clean(p.get("title"))
            if not title or title in seen:
                continue
            seen.add(title)
            papers.append(p)
    return papers


def _literature_summary(literature: List[dict]) -> List[dict]:
    """构造发给 LLM 的文献摘要（标题/摘要/venue/结构化理解，不含全文）。"""
    out: List[dict] = []
    for entry in literature or []:
        if not isinstance(entry, dict):
            continue
        papers: List[dict] = []
        for p in entry.get("papers") or []:
            if not isinstance(p, dict):
                continue
            papers.append({
                "title": _clean(p.get("title")),
                "abstract": _clean(p.get("abstract"))[:300],
                "venue": _clean(p.get("venue")),
                "year": p.get("year"),
                "understanding": p.get("understanding") or {},
            })
        out.append({
            "query": _clean(entry.get("query")),
            "gap_note": _clean(entry.get("gap_note")),
            "papers": papers,
        })
    return out


def _build_user_prompt(idea: dict, literature: List[dict],
                       facts: Dict[str, Any]) -> str:
    """构造脱敏输入：idea + 文献摘要 + 项目事实（数据/指标等），供证据审查。"""
    payload = {
        "idea": {
            "idea_id": idea.get("idea_id"),
            "claim": idea.get("claim"),
            "novelty_hypothesis": idea.get("novelty_hypothesis"),
            "problem_ref": idea.get("problem_ref"),
            "literature_refs": idea.get("literature_refs"),
            "hypothesis_refs": idea.get("hypothesis_refs"),
            "gap_refs": idea.get("gap_refs"),
        },
        "literature": _literature_summary(literature),
        "facts": {
            "data": (facts or {}).get("data"),
            "metrics": (facts or {}).get("metrics"),
            "methods": (facts or {}).get("methods"),
            "scenarios": (facts or {}).get("scenarios"),
        },
    }
    return (
        "以下是一个候选创新点及其可用的证据材料，请做证据审查"
        "（不跑实验，只判断「这个 claim 站不站得住」）：\n"
        + json.dumps(payload, ensure_ascii=False)
    )


# ---------------------------------------------------------------------------
# LLM 调用与解析
# ---------------------------------------------------------------------------

def _call_llm(llm: Optional[LLMProvider], system: str, idea: dict,
              literature: List[dict], facts: Dict[str, Any]) -> Dict[str, Any]:
    """调用 LLM（M15：证据审查属「简单校验」，走便宜快模型）；失败/空结果返回空 dict。"""
    if llm is None:
        return {}
    result: Dict[str, Any] = {}
    try:
        result = complete_fast(
            llm, system, _build_user_prompt(idea, literature, facts),
            EVIDENCE_SCHEMA, temperature=0.2,
        )
    except (LLMError, SchemaError):
        result = {}
    if not isinstance(result, dict):
        result = {}
    return result


def _build_batch_user_prompt(ideas: List[dict], literature: List[dict],
                             facts: Dict[str, Any]) -> str:
    """构造批量证据审查的脱敏输入：一组 idea + 共享文献摘要 + 项目事实。"""
    payload = {
        "ideas": [
            {
                "idea_id": idea.get("idea_id"),
                "claim": idea.get("claim"),
                "novelty_hypothesis": idea.get("novelty_hypothesis"),
                "problem_ref": idea.get("problem_ref"),
                "literature_refs": idea.get("literature_refs"),
                "hypothesis_refs": idea.get("hypothesis_refs"),
                "gap_refs": idea.get("gap_refs"),
            }
            for idea in ideas
            if isinstance(idea, dict)
        ],
        "literature": _literature_summary(literature),
        "facts": {
            "data": (facts or {}).get("data"),
            "metrics": (facts or {}).get("metrics"),
            "methods": (facts or {}).get("methods"),
            "scenarios": (facts or {}).get("scenarios"),
        },
    }
    return (
        "以下是一组候选创新点及其可用的证据材料，请对每个 idea 分别做证据审查"
        "（不跑实验，只判断「这个 claim 站不站得住」）：\n"
        + json.dumps(payload, ensure_ascii=False)
    )


def _extract_checks(raw: Any) -> Optional[Dict[str, Dict[str, str]]]:
    """从 LLM 输出提取并规范化 4 个检查维度；任一维度缺失/非法 → 返回 None（触发确定性兜底）。"""
    if not isinstance(raw, dict):
        return None
    checks: Dict[str, Dict[str, str]] = {}
    for key in _CHECK_KEYS:
        item = raw.get(key)
        if not isinstance(item, dict):
            return None
        status = _clean(item.get("status"))
        note = _clean(item.get("note"))
        if status not in _CHECK_STATUSES or not note:
            return None
        checks[key] = {"status": status, "note": note}
    return checks


# ---------------------------------------------------------------------------
# 确定性降级：四个维度的词面信号
# ---------------------------------------------------------------------------

def _text_of(idea: dict) -> str:
    return " ".join([
        _clean(idea.get("claim")), _clean(idea.get("novelty_hypothesis")),
    ]).lower()


def _claim_overstrong(idea: dict) -> bool:
    text = _text_of(idea)
    return any(m in text for m in _OVERSTRONG_MARKERS)


def _claim_hedged(idea: dict) -> bool:
    text = _text_of(idea)
    return any(m in text for m in _HEDGE_MARKERS)


def _differentiation(idea: dict) -> bool:
    text = _text_of(idea)
    return any(m in text for m in _DIFF_MARKERS)


def _theory_signal(idea: dict) -> bool:
    text = _text_of(idea)
    return any(m in text for m in _THEORY_MARKERS)


def _deterministic_checks(idea: dict, literature: List[dict],
                          facts: Dict[str, Any]) -> Dict[str, Dict[str, str]]:
    """无 LLM 时的确定性证据审查（词面信号，低置信，报告会标注）。

    规则（documented in build-plan §4 M12 检查维度）：
    - similar_work：无论文 → 无法对拍（missing）；有论文且 idea 引用并给出差异化 → ok；
      有论文但差异不明确 / 未引用 → concern；
    - theory_basis：追溯到 M5 v2 可证伪假设或含理论/机制表述 → ok；有文献可借鉴但
      idea 自身无理论 → concern；否则 missing；
    - experiment_support：有数据且有指标 → 可验证（ok）；仅其一 → concern；均无 → missing；
    - claim_strength：含绝对化/过强表述 → missing（过强）；含限定/校准表述 → ok；否则 concern。
    """
    papers = _all_papers(literature)
    refs = [x for x in (idea.get("literature_refs") or []) if _clean(x)]
    facts = facts or {}
    data = facts.get("data") or []
    metrics = facts.get("metrics") or []

    # 1) 文献对拍
    if not papers:
        similar = {
            "status": "missing",
            "note": "未检索到可比文献，无法对拍（证据强度受限，需人工补检索）",
        }
    elif refs and _differentiation(idea):
        similar = {
            "status": "ok",
            "note": "有 {} 篇可比文献，idea 引用了真实文献并给出差异化定位".format(len(papers)),
        }
    elif refs:
        similar = {
            "status": "concern",
            "note": "有 {} 篇可比文献且 idea 有引用，但差异点需更明确（如 memory → adaptive policy memory）".format(len(papers)),
        }
    else:
        similar = {
            "status": "concern",
            "note": "有 {} 篇可比文献，但 idea 未引用任何文献，无法确认已做过对拍".format(len(papers)),
        }

    # 2) 理论支撑
    if idea.get("hypothesis_refs"):
        theory = {
            "status": "ok",
            "note": "idea 追溯到 M5 v2 的可证伪假设（hypothesis_refs 非空），有 if-then 假设支撑",
        }
    elif _theory_signal(idea):
        theory = {
            "status": "ok",
            "note": "claim/假设含理论或机制性表述（机制/原理/可证伪等），有理论支撑信号",
        }
    elif papers and any(p.get("understanding") for p in papers):
        theory = {
            "status": "concern",
            "note": "有相关文献可借鉴，但 idea 自身未给出明确理论依据",
        }
    else:
        theory = {
            "status": "missing",
            "note": "未识别到理论依据（无机制/原理/可证伪假设表述）",
        }

    # 3) 实验设计支持（别人做过什么 + claim 能否被验证）
    if data and metrics:
        experiment = {
            "status": "ok",
            "note": "项目有数据（{}）且有指标（{}），claim 可被验证；{} 篇文献提供实验设计参照".format(
                "、".join(data)[:40], "、".join(metrics)[:40],
                len(papers) if papers else "无"),
        }
    elif data or metrics:
        experiment = {
            "status": "concern",
            "note": "项目仅有数据或仅有指标，验证方案不完整（缺对照实验设计）",
        }
    else:
        experiment = {
            "status": "missing",
            "note": "未识别到数据/指标，claim 当前无法被验证（需补实验设计）",
        }

    # 4) claim 强度校准
    if _claim_overstrong(idea):
        strength = {
            "status": "missing",
            "note": "claim 含绝对化/过强表述（如 首创/完全解决/超越所有），需弱化为可检验的限定主张",
        }
    elif _claim_hedged(idea):
        strength = {
            "status": "ok",
            "note": "claim 含限定/校准表述（场景/约束/改进等），主张范围相对克制",
        }
    else:
        strength = {
            "status": "concern",
            "note": "claim 未明显限定范围，建议补充适用条件与比较基准",
        }

    return {
        "similar_work": similar,
        "theory_basis": theory,
        "experiment_support": experiment,
        "claim_strength": strength,
    }


# ---------------------------------------------------------------------------
# 证据强度聚合
# ---------------------------------------------------------------------------

def _aggregate_evidence(checks: Dict[str, Dict[str, str]]) -> Tuple[str, str]:
    """把 4 个检查维度聚合成 (evidence, reason)。确定性兜底 + LLM 缺 reason 时的兜底。"""
    score = {"ok": 2, "concern": 1, "missing": 0}
    total = sum(score[c["status"]] for c in checks.values())
    similar = checks["similar_work"]["status"]
    theory = checks["theory_basis"]["status"]
    strength = checks["claim_strength"]["status"]

    if strength == "missing":
        return ("weak", "claim 强度过强（绝对化/不可证伪表述），需弱化为可检验的限定主张后再评估")
    if similar == "missing" and theory == "missing":
        return ("weak", "既无文献对拍依据又无理论支撑，证据不足以支撑 claim，需回炉补充文献/理论")
    if total <= 3:
        return ("weak", "多个证据维度缺失，claim 证据强度不足，需回炉细化")
    if total >= 7 and similar == "ok" and strength == "ok":
        return ("strong", "文献对拍、理论依据、可验证性、claim 校准基本到位，证据较充分")
    return ("medium", "有一定证据支撑，但{}仍需补强（如明确与已有工作的区别 / 补理论依据 / 补实验设计）".format(
        _weak_dims_text(checks)))


def _weak_dims_text(checks: Dict[str, Dict[str, str]]) -> str:
    """列出 concern / missing 维度的中文标签，用于拼接「如何强化」建议。"""
    dims = [
        _CHECK_LABELS[k]
        for k, _label in CHECK_DIMENSIONS
        if checks[k]["status"] in ("concern", "missing")
    ]
    return "、".join(dims) if dims else "各维度"


# ---------------------------------------------------------------------------
# 冻结入口（纯函数，供 evaluate.py 在 EVALUATE 内部调用）
# ---------------------------------------------------------------------------

def _finalize_evidence(raw: Any, idea: dict, literature: List[dict],
                       facts: Dict[str, Any]) -> Dict[str, Any]:
    """把一条 LLM 原始输出（或空）规范化为 ``{evidence, reason, checks, degraded}``。

    与 ``validate_evidence`` 的收尾逻辑一致：checks 非法 / 缺失 → 确定性兜底（degraded=True）；
    evidence 非法 → 用 checks 重新聚合；reason 缺失 → 兜底补。单条与批量路径共用，保证二者语义一致。
    """
    facts = facts or {}
    raw = raw if isinstance(raw, dict) else {}
    checks = _extract_checks(raw.get("checks"))
    if checks is None:
        checks = _deterministic_checks(idea, literature, facts)
        evidence, reason = _aggregate_evidence(checks)
        return {"evidence": evidence, "reason": reason, "checks": checks, "degraded": True}

    evidence = raw.get("evidence")
    if evidence not in EVIDENCE_LEVELS:
        evidence, _ = _aggregate_evidence(checks)
    reason = _clean(raw.get("reason"))
    if not reason:
        _, reason = _aggregate_evidence(checks)

    return {"evidence": evidence, "reason": reason, "checks": checks, "degraded": False}


def validate_evidence(idea: dict, literature: List[dict],
                      llm: Optional[LLMProvider],
                      facts: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """对单个 idea 做证据审查，返回 ``{evidence, reason, checks, degraded}``。

    - ``evidence`` ∈ {weak, medium, strong}；
    - ``reason``：为什么弱 / 如何强化；
    - ``checks``：4 个检查维度的 ``{status, note}``；
    - ``degraded``：True 表示走了确定性兜底（无 LLM / LLM 输出非法），低置信。

    本函数不抛异常、不改 Dossier，是 evaluate.py 在 EVALUATE 内部调用的子审查单元。
    """
    system, _version = _load_prompt()
    raw = _call_llm(llm, system, idea, literature, facts or {})
    return _finalize_evidence(raw, idea, literature, facts or {})


def validate_evidence_batch(ideas: List[dict], literature: List[dict],
                            llm: Optional[LLMProvider],
                            facts: Optional[Dict[str, Any]] = None) -> Dict[str, Dict[str, Any]]:
    """M15 方向④：批量证据审查——一次 LLM 调用审查多个 idea，返回 ``{idea_id: 结果}``。

    - 返回每个 idea 的 ``{evidence, reason, checks, degraded}``；
    - 失败 / 空结果 / 某 idea 缺失 → 该 idea 不在返回 dict 中（由 evaluate.py 回退单条路径）；
    - 与 ``validate_evidence`` 语义一致（共用 ``_finalize_evidence``），绝不抛异常。
    """
    if llm is None or not ideas:
        return {}
    facts = facts or {}
    idea_by_id: Dict[str, dict] = {}
    for idea in ideas:
        if isinstance(idea, dict) and _clean(idea.get("idea_id")):
            idea_by_id[_clean(idea.get("idea_id"))] = idea

    system, _version = _load_prompt()
    result: Dict[str, Any] = {}
    try:
        result = complete_fast(
            llm, system, _build_batch_user_prompt(ideas, literature, facts),
            EVIDENCE_BATCH_SCHEMA, temperature=0.2,
        )
    except (LLMError, SchemaError):
        return {}
    if not isinstance(result, dict):
        return {}
    raw = result.get("results")
    if not isinstance(raw, list):
        return {}

    out: Dict[str, Dict[str, Any]] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        idea_id = _clean(item.get("idea_id"))
        idea = idea_by_id.get(idea_id)
        if not idea_id or idea is None:
            continue
        out[idea_id] = _finalize_evidence(item, idea, literature, facts)
    return out
