"""⑦ 经验沉淀 Agent：把一次运行蒸馏成经验条目，写 experience/semantic.jsonl。

对应 docs/build-plan.md §4 M8 与 docs/architecture.md §3.6 / §3.7 / §3.9：

- **输入**：Dossier（含 ``human_decisions`` 的 F1 反馈 + roadmap/evaluations 结果）+ F2 过程信号。
- **输出**：``experience_entries[]``（status = candidate；人工确认后 active）。
- **关键动作（M8）**：蒸馏时**去领域化**——把领域特例抽象成领域无关的 ``principle``，
  并生成结构化 ``policy``（target + directive），同时填充 ``effect``（初始取 F1 人工 review 信号，
  F3 结果信号后续由 ``experience.record_effect`` 更新）。

降级路径：无 key（NullProvider 空结果）、LLMError、SchemaError 时，降级为确定性规则，
按 facts 的 task/scenario 生成至少一条经验条目（保证「结束写出一条经验」的验收）。

经验条目 schema 与 architecture §3.6 对齐，落盘走 ``papermine.experience.append_semantic``
（去重键 = principle + applicability，支持数累加，晋升由 support_count + effect 驱动）。

冻结接口（docs/build-plan.md §3.3）：

    def run(dossier, llm) -> None
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from .. import experience
from ..dossier import Dossier
from ..llm import LLMError, LLMProvider, SchemaError

__all__ = [
    "run",
    "REFLECT_SCHEMA",
    "_deterministic_entries",
    "_normalize_entries",
    "_initial_effect",
]

# 本 Agent prompt 版本：单一事实源 = prompts/reflect.md 的 `<!-- version: N -->` 头
_PROMPT_VERSION = "v1"
_PROMPT_FILE = Path(__file__).resolve().parent.parent / "prompts" / "reflect.md"

# 本 Agent 的 LLM 输出契约（schema 校验走 papermine/llm.py 的极简子集）
# 注意：effect 不由 LLM 输出（§3.8 护栏 #3：不由 LLM 自评），由 reflect 确定性填充。
REFLECT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["entries"],
    "properties": {
        "entries": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["source_domain", "applicability", "principle", "policy", "confidence"],
                "properties": {
                    "source_domain": {"type": "string"},
                    "applicability": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "domains": {"type": "array", "items": {"type": "string"}},
                            "task_types": {"type": "array", "items": {"type": "string"}},
                            "preconditions": {"type": "array", "items": {"type": "string"}},
                        },
                    },
                    "principle": {"type": "string"},
                    "policy": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["target", "directive"],
                        "properties": {
                            "target": {
                                "type": "string",
                                "enum": ["prompt", "planning", "search", "evaluation"],
                            },
                            "directive": {"type": "string"},
                        },
                    },
                    "confidence": {"type": "number"},
                },
            },
        }
    },
}

_FALLBACK_SYSTEM = (
    "你是 papermine 的「经验沉淀 Agent」。把一次分析运行蒸馏成 1~3 条跨项目可复用的经验条目。"
    "每条必须：去领域化地给出 source_domain（来源域）与 principle（领域无关的抽象原则）、"
    "applicability（domains/task_types/preconditions 适用边界）、policy（target∈{prompt,planning,search,evaluation}"
    " + directive 行为约束）、confidence（0~1）。只输出符合 schema 的 JSON 对象。"
)


def _load_prompt() -> tuple:
    """从 prompts/reflect.md 读取 (system, version)；文件缺失时回退内置默认。"""
    if _PROMPT_FILE.exists():
        text = _PROMPT_FILE.read_text(encoding="utf-8")
        m = re.search(r"<!--\s*version:\s*(\d+)\s*-->", text)
        version = "v{}".format(m.group(1)) if m else _PROMPT_VERSION
        system = re.sub(r"<!--.*?-->\s*", "", text).strip()
        return version, system
    return _PROMPT_VERSION, _FALLBACK_SYSTEM


# ---------------------------------------------------------------------------
# 输入装配
# ---------------------------------------------------------------------------

def _first(items: Any, n: int = 3) -> List[str]:
    out: List[str] = []
    for it in items or []:
        s = " ".join(str(it).split())
        if s and s not in out:
            out.append(s)
        if len(out) >= n:
            break
    return out


def _facts(dossier: Dossier) -> Dict[str, Any]:
    assets = dossier.assets if isinstance(dossier.assets, dict) else {}
    facts = assets.get("facts") if isinstance(assets.get("facts"), dict) else {}
    return {
        "tasks": _first(facts.get("tasks")),
        "methods": _first(facts.get("methods")),
        "data": _first(facts.get("data")),
        "scenarios": _first(facts.get("scenarios")),
        "metrics": _first(facts.get("metrics")),
    }


def _has_accept(dossier: Dossier) -> bool:
    return any(
        isinstance(d, dict) and d.get("decision") in ("accept", "note")
        for d in (dossier.human_decisions or [])
    )


def _proceed_count(dossier: Dossier) -> int:
    return sum(
        1 for ev in (dossier.evaluations or [])
        if isinstance(ev, dict) and ev.get("verdict") == "proceed"
    )


def _run_id(dossier: Dossier) -> str:
    meta = dossier.meta if isinstance(dossier.meta, dict) else {}
    return str(meta.get("run_id") or meta.get("project_id") or "run")


def _build_user_prompt(dossier: Dossier) -> str:
    payload = {
        "project_facts": _facts(dossier),
        "problems": [
            {"title": p.get("title"), "formulation": p.get("formulation")}
            for p in (dossier.problems or [])
            if isinstance(p, dict)
        ][:5],
        "roadmap": {
            "selected_idea": (dossier.roadmap or {}).get("selected_idea"),
            "paper_type": (dossier.roadmap or {}).get("paper_type"),
        },
        "evaluations": [
            {"idea_ref": ev.get("idea_ref"), "verdict": ev.get("verdict")}
            for ev in (dossier.evaluations or [])
            if isinstance(ev, dict)
        ],
        "human_decisions": [
            {"checkpoint": d.get("checkpoint"), "decision": d.get("decision"), "note": d.get("note")}
            for d in (dossier.human_decisions or [])
            if isinstance(d, dict)
        ],
        "process_signals": (dossier.meta or {}).get("process_signals") or {},
    }
    return "以下是一次分析运行的产出，请蒸馏成可复用的经验条目：\n" + json.dumps(
        payload, ensure_ascii=False)


# ---------------------------------------------------------------------------
# LLM 路径
# ---------------------------------------------------------------------------

def _try_llm(dossier: Dossier, llm: LLMProvider, system: str) -> Optional[List[dict]]:
    result: Dict[str, Any] = {}
    try:
        result = llm.complete(system, _build_user_prompt(dossier), REFLECT_SCHEMA, temperature=0.3)
    except (LLMError, SchemaError):
        return None
    if not isinstance(result, dict):
        return None
    entries = result.get("entries")
    if not isinstance(entries, list):
        return None
    return [e for e in entries if isinstance(e, dict)]


# ---------------------------------------------------------------------------
# 确定性降级
# ---------------------------------------------------------------------------

def _confidence(dossier: Dossier) -> float:
    """依据结果信号给一个保守置信度：有 proceed 高分；有 accept 中分；全 rework 低分。"""
    if _proceed_count(dossier) > 0:
        return 0.8
    if _has_accept(dossier):
        return 0.6
    return 0.4


def _initial_effect(dossier: Dossier) -> Dict[str, Any]:
    """reflect 阶段的 effect 初值（确定性，取 F1 人工 review 信号，不由 LLM 自评）。

    F3 结果信号（论文是否发表/被采纳）后续由 ``experience.record_effect`` 覆盖更新。
    """
    if _has_accept(dossier):
        return {
            "outcome": "positive",
            "measured_by": "human_review",
            "note": "检查点获人工认可（F1 初始信号）",
            "updated_at": None,
        }
    return {
        "outcome": "neutral",
        "measured_by": "human_review",
        "note": "暂无人工正反馈，待 F3 结果信号校准",
        "updated_at": None,
    }


def _deterministic_entries(dossier: Dossier) -> List[dict]:
    """无 LLM 时的确定性降级：去领域化 principle + 结构化 policy + applicability + effect。"""
    facts = _facts(dossier)
    task = facts["tasks"][0] if facts["tasks"] else ""
    scenario = facts["scenarios"][0] if facts["scenarios"] else ""
    domain = scenario or "*"

    # 去领域化：principle 不写具体项目/领域名，写可迁移规律
    if task:
        principle = "建模/方法类横向任务通常能抽象出可发表的研究问题（超越一次性工程交付）"
        directive = "问题抽象阶段优先围绕可迁移的方法问题立项，并强制论证「为何不是纯工程」"
        target = "prompt"
        applicability = {
            "domains": [domain] if scenario else ["*"],
            "task_types": [task],
            "preconditions": ["项目包含任务：{}".format(task)],
        }
    else:
        principle = "具备明确场景的横向工作具备提炼研究问题的潜力"
        directive = "问题抽象阶段结合场景与任务信号，先明确可研究的方法问题"
        target = "prompt"
        applicability = {
            "domains": [domain] if scenario else ["*"],
            "task_types": ["*"],
            "preconditions": [],
        }

    return [{
        "experience_id": experience.new_experience_id(),
        "type": "pattern",
        "source_domain": domain,
        "applicability": applicability,
        "principle": principle,
        "policy": {"target": target, "directive": directive},
        "effect": _initial_effect(dossier),
        "confidence": _confidence(dossier),
        "support_count": 1,
        "source_runs": [_run_id(dossier)],
        "status": "active" if _has_accept(dossier) else "candidate",
        "created_at": None,
        "updated_at": None,
    }]


# ---------------------------------------------------------------------------
# 规范化 + 落盘
# ---------------------------------------------------------------------------

def _coerce_confidence(value: Any) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return round(max(0.0, min(1.0, float(value))), 2)
    return 0.5


def _clean_strings(value: Any) -> List[str]:
    out: List[str] = []
    seen: set = set()
    if not isinstance(value, list):
        return out
    for v in value:
        s = " ".join(str(v).split())
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _normalize_applicability(value: Any) -> Dict[str, Any]:
    app = value if isinstance(value, dict) else {}
    domains = _clean_strings(app.get("domains")) or ["*"]
    task_types = _clean_strings(app.get("task_types")) or ["*"]
    preconditions = _clean_strings(app.get("preconditions"))
    return {"domains": domains, "task_types": task_types, "preconditions": preconditions}


def _normalize_policy(value: Any) -> Dict[str, str]:
    p = value if isinstance(value, dict) else {}
    target = " ".join(str(p.get("target") or "").split())
    if target not in experience.TARGETS:
        target = "prompt"
    return {"target": target, "directive": " ".join(str(p.get("directive") or "").split())}


def _normalize_entries(raw: List[dict], dossier: Dossier) -> List[dict]:
    """把 LLM / 确定性产出的原始条目规范化为经验条目 schema，并带 support_count/status/effect。"""
    run_id = _run_id(dossier)
    has_accept = _has_accept(dossier)
    effect = _initial_effect(dossier)
    out: List[dict] = []
    seen: set = set()
    for r in raw or []:
        if not isinstance(r, dict):
            continue
        principle = " ".join(str(r.get("principle") or "").split())
        if not principle or principle in seen:
            continue
        seen.add(principle)
        out.append({
            "experience_id": experience.new_experience_id(),
            "type": "pattern",
            "source_domain": " ".join(str(r.get("source_domain") or "").split()) or "*",
            "applicability": _normalize_applicability(r.get("applicability")),
            "principle": principle,
            "policy": _normalize_policy(r.get("policy")),
            "effect": effect,
            "confidence": _coerce_confidence(r.get("confidence")),
            "support_count": 1,
            "source_runs": [run_id],
            "status": "active" if has_accept else "candidate",
            "created_at": None,
            "updated_at": None,
        })
    return out


# ---------------------------------------------------------------------------
# 入口（冻结契约）
# ---------------------------------------------------------------------------

def run(dossier: Dossier, llm: LLMProvider) -> None:
    """蒸馏本次运行的经验，写 experience/semantic.jsonl（原地副作用在经验库）。

    冻结契约（docs/build-plan.md §3.3）：
        def run(dossier, llm) -> None

    只写长期记忆（经验库），不写 dossier 字段（prompt 版本除外）。
    """
    version, system = _load_prompt()
    dossier.meta.setdefault("prompt_versions", {})["reflect"] = version

    raw: Optional[List[dict]] = None
    if llm is not None:
        raw = _try_llm(dossier, llm, system)
    if not raw:
        raw = _deterministic_entries(dossier)

    for entry in _normalize_entries(raw, dossier):
        experience.append_semantic(entry)
