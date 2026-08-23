"""⑦ 经验沉淀 Agent：把一次运行蒸馏成经验条目，写 experience/semantic.jsonl。

对应 docs/build-plan.md §3.3 / §4 M7 与 docs/architecture.md §3.7：

- **输入**：Dossier（含 ``human_decisions`` 的 F1 反馈 + roadmap/evaluations 结果）+ F2 过程信号。
- **输出**：``experience_entries[]``（status = candidate；人工确认或 ``support_count`` 达标后 active）。
- **时机**：⑥ 结束后由编排器自动触发一次（也可人工触发）。

降级路径：无 key（NullProvider 空结果）、LLMError、SchemaError 时，降级为确定性规则，
按 facts 的 task/scenario 生成至少一条经验条目（保证「结束写出一条经验」的验收）。

经验条目 schema 与 architecture §3.6 对齐（``confidence / support_count``），
落盘走 ``papermine.experience.append_semantic``（去重 + 支持数累加 + 晋升）。

冻结接口（docs/build-plan.md §3.3 / §4 M7）：

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
]

# 本 Agent prompt 版本：单一事实源 = prompts/reflect.md 的 `<!-- version: N -->` 头
_PROMPT_VERSION = "v1"
_PROMPT_FILE = Path(__file__).resolve().parent.parent / "prompts" / "reflect.md"

# 本 Agent 的 LLM 输出契约（schema 校验走 papermine/llm.py 的极简子集）
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
                "required": ["scope", "trigger", "insight", "action", "confidence"],
                "properties": {
                    "scope": {"type": "string"},
                    "trigger": {"type": "string"},
                    "insight": {"type": "string"},
                    "action": {"type": "string"},
                    "confidence": {"type": "number"},
                },
            },
        }
    },
}

_FALLBACK_SYSTEM = (
    "你是 papermine 的「经验沉淀 Agent」。把一次分析运行蒸馏成 1~3 条跨项目可复用的经验条目："
    "scope（global/domain:.../task:...）、trigger、insight、action、confidence（0~1）。"
    "只输出符合 schema 的 JSON 对象。"
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


def _deterministic_entries(dossier: Dossier) -> List[dict]:
    """无 LLM 时的确定性降级：按 task/scenario 生成至少一条经验条目。"""
    facts = _facts(dossier)
    task = facts["tasks"][0] if facts["tasks"] else ""
    scenario = facts["scenarios"][0] if facts["scenarios"] else "跨项目"

    scope = "task:{}".format(task) if task else ("domain:{}".format(scenario) if scenario else "global")
    if task:
        trigger = "项目含{}场景 + {}任务信号".format(scenario, task)
        insight = "{}类横向项目通常能抽象出可发表的研究问题".format(task)
        action = "问题抽象阶段优先围绕「{}」方向检索与立项".format(task)
    else:
        trigger = "项目含{}场景信号".format(scenario)
        insight = "{}场景下的横向工作具备提炼研究问题的潜力".format(scenario)
        action = "问题抽象阶段结合场景与任务信号，先明确可研究的方法问题"

    return [{
        "experience_id": experience.new_experience_id(),
        "type": "pattern",
        "scope": scope,
        "trigger": trigger,
        "insight": insight,
        "action": action,
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


def _normalize_entries(raw: List[dict], dossier: Dossier) -> List[dict]:
    """把 LLM / 确定性产出的原始条目规范化为经验条目 schema，并带 support_count/status。"""
    run_id = _run_id(dossier)
    has_accept = _has_accept(dossier)
    out: List[dict] = []
    seen: set = set()
    for r in raw or []:
        if not isinstance(r, dict):
            continue
        scope = " ".join(str(r.get("scope") or "").split()) or "global"
        insight = " ".join(str(r.get("insight") or "").split())
        if not insight or insight in seen:
            continue
        seen.add(insight)
        out.append({
            "experience_id": experience.new_experience_id(),
            "type": "pattern",
            "scope": scope,
            "trigger": " ".join(str(r.get("trigger") or "").split()),
            "insight": insight,
            "action": " ".join(str(r.get("action") or "").split()),
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

    冻结契约（docs/build-plan.md §3.3 / §4 M7）：
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
