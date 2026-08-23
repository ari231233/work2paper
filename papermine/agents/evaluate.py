"""⑤ 可行性评估 Agent：ideas -> evaluations（证据驱动）。

对应 docs/build-plan.md §3.3 / §4 M6 与 docs/architecture.md §5 ⑤：

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

verdict ∈ {proceed, rework, drop}；每条评估必须挂 `evidence`（provenance 强制）。

降级路径：无 key（NullProvider 空结果）、LLMError、SchemaError 时，novelty / workload
降级为确定性规则估算；数据可得性 / 档位**始终**由确定性规则计算，不依赖 LLM。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..dossier import Dossier
from ..llm import LLMError, LLMProvider, SchemaError

__all__ = [
    "run",
    "EVALUATE_SCHEMA",
    "_data_feasibility",
    "_deterministic_novelty",
    "_deterministic_workload",
    "_decide_verdict",
    "_rework_reason",
    "_guess_venue",
    "_tier_of",
    "_venue_distribution",
]

# 本 Agent prompt 版本：优先读 prompts/evaluate.md 头的 version，缺失时用此兜底
_PROMPT_VERSION = "v1"
_PROMPT_FILENAME = "evaluate.md"
_PROMPT_VERSION_RE = re.compile(r"<!--\s*version:\s*(\d+)\s*-->")

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

# 本 Agent 的 LLM 输出契约（schema 校验走 papermine/llm.py 的极简子集）
EVALUATE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "novelty_score", "novelty_reason", "workload_hours",
        "verdict_suggestion", "rework_reason",
    ],
    "properties": {
        "novelty_score": {"type": "number"},
        "novelty_reason": {"type": "string"},
        "workload_hours": {"type": "number"},
        "verdict_suggestion": {
            "type": "string", "enum": ["proceed", "rework", "drop"],
        },
        "rework_reason": {"type": ["string", "null"]},
    },
}

_SYSTEM_PROMPT_FALLBACK = (
    "你是 papermine 的「可行性评估 Agent」。对候选创新点做证据驱动的可行性评估："
    "novelty 对照文献 gap_note 打分（0~5），估计工作量，给出 verdict_suggestion∈{proceed,rework,drop}，"
    "非 proceed 时给出 rework_reason。只输出符合 schema 的 JSON 对象。"
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
# 确定性兜底估算：novelty / workload / 档位
# ---------------------------------------------------------------------------

def _shared_3grams(a: str, b: str) -> int:
    """计算两个文本在去掉标点后共享的三字串数量（容忍中文分词差异）。"""
    a = re.sub(r"[\s,，。;；:：、()（）\[\]【】]+", "", a)
    b = re.sub(r"[\s,，。;；:：、()（）\[\]【】]+", "", b)
    if len(a) < 3 or len(b) < 3:
        return 0
    grams = {a[i:i + 3] for i in range(len(a) - 2)}
    return sum(1 for g in grams if g in b)


def _deterministic_novelty(idea: dict, gap_notes: List[str]) -> float:
    """确定性 novelty 兜底：对照 gap_note 文本给保守估计（LLM 缺失时用，标低置信）。

    无 gap_note -> 2.5（无法对拍）；gap 含强/弱「缺口」信号 -> 加分；idea 与 gap 文本
    重合度高 -> 再略加。上限 5.0。注意：确定性兜底不逐 idea 匹配 gap 相关性，LLM 路径更准。
    """
    if not gap_notes:
        return 2.5
    joined = " ".join(str(g) for g in gap_notes)
    low = joined.lower()
    score = 2.0
    strong = ("尚未", "没有系统", "缺乏系统", "no systematic", "rarely",
              "open problem", "未解决", "缺口")
    weak = ("gap", "不足", "limited", "missing", "challenge", "缺乏")
    if any(m in low for m in strong):
        score += 1.0
    elif any(m in low for m in weak):
        score += 0.5
    idea_text = " ".join(str(idea.get(k) or "") for k in ("claim", "novelty_hypothesis"))
    if _shared_3grams(idea_text, joined) >= 2:
        score += 0.5
    return round(min(5.0, score), 1)


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
    """无检索 venue 时的规则档位猜测（复用 v0.1 mining 思路，依据 idea 类型 + novelty）。"""
    claim = str(idea.get("claim") or "")
    if _is_tool_claim(claim):
        return "中文核心 / EI 会议（系统/工具类）"
    if "实证" in claim:
        return "中文核心 / 应用类期刊"
    if novelty >= 4.0:
        return "CCF-B / 中文核心或 EI 会议"
    return "CCF-C / 中文核心"


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
# verdict 决策（证据驱动硬护栏 + LLM 建议）
# ---------------------------------------------------------------------------

def _decide_verdict(novelty: float, data_feasibility: str, workload: float,
                    suggestion: Optional[str]) -> str:
    """综合 verdict：硬护栏优先（不依赖 LLM 自评），LLM 建议仅作参考。"""
    if novelty < 2.0:
        return "drop"          # 新颖性不足，与文献对拍无差异
    if data_feasibility == "low":
        return "rework"        # 无数据支撑，回炉补数据
    if workload > 400:
        return "rework"        # 工作量过大，需拆分或回炉
    if data_feasibility == "medium" and novelty < 3.0:
        return "rework"        # 中低新颖性 + 数据不完整
    if suggestion in ("drop", "rework"):
        return suggestion
    if novelty >= 3.0:
        return "proceed"
    return "rework"


def _rework_reason(verdict: str, novelty: float, data_feasibility: str,
                   workload: float, llm_reason: Optional[str] = None) -> Optional[str]:
    """生成 rework_reason（proceed 时为 None）。"""
    if verdict == "proceed":
        return None
    if verdict == "drop":
        return "新颖性不足：novelty={} 与文献 gap 对拍无明显差异，建议放弃该创新点".format(novelty)
    if data_feasibility == "low":
        return "数据可得性低：assets.facts 未识别到数据/指标，需回炉补充评测数据（回退①项目理解补采集）"
    if novelty < 3.0:
        return "新颖性偏低：novelty={}，建议回炉到②问题抽象/④创新点生成以强化 novelty".format(novelty)
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


def _assemble_evidence(idea: dict, gap_notes: List[str], facts: Dict[str, Any],
                       venue_dist: Dict[str, int], novelty_reason: str) -> List[dict]:
    """装配评估证据链（provenance 强制：每条结论挂证据源）。"""
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
    if novelty_reason:
        evidence.append({"source": "llm", "note": novelty_reason[:200]})
    return evidence


def _evaluate_idea(idea: dict, facts: Dict[str, Any], gap_notes: List[str],
                   venue_dist: Dict[str, int], venue_summary: str,
                   data_feasibility: str, llm: LLMProvider,
                   system_prompt: str) -> dict:
    """对单个 idea 做证据驱动评估，返回一条 evaluation dict。"""
    idea_id = str(idea.get("idea_id") or "").strip()
    out = _call_llm(llm, system_prompt, idea, gap_notes, venue_summary, facts)

    novelty = _coerce_number(out.get("novelty_score"), None, 0.0, 5.0)
    if novelty is None:
        novelty = _deterministic_novelty(idea, gap_notes)
    novelty = round(float(novelty), 1)

    workload = _coerce_number(out.get("workload_hours"), None, 10.0, 1000.0)
    if workload is None:
        workload = float(_deterministic_workload(idea, facts))
    workload = int(round(workload))

    suggestion = out.get("verdict_suggestion")
    if suggestion not in ("proceed", "rework", "drop"):
        suggestion = None

    novelty_reason = str(out.get("novelty_reason") or "").strip()
    llm_rework_reason = out.get("rework_reason")
    if not isinstance(llm_rework_reason, str) or not llm_rework_reason.strip():
        llm_rework_reason = None

    venue_guess = _guess_venue(facts, idea, venue_dist, novelty)
    verdict = _decide_verdict(novelty, data_feasibility, float(workload), suggestion)
    rework_reason = _rework_reason(verdict, novelty, data_feasibility,
                                   float(workload), llm_rework_reason)
    evidence = _assemble_evidence(idea, gap_notes, facts, venue_dist, novelty_reason)

    return {
        "idea_ref": idea_id,
        "novelty_score": novelty,
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
    """ideas -> evaluations（证据驱动），原地写 dossier.evaluations。

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
            data_feasibility, llm, system_prompt,
        ))

    dossier.evaluations = evaluations
    dossier.meta.setdefault("prompt_versions", {})["evaluate"] = version
