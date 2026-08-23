"""经验库：跨项目记忆（Evolution Layer 的数据层）——M8 升级为「策略」版。

对应 docs/architecture.md §3.6 / §3.7 与 docs/build-plan.md §4 M8：

- 三种记忆文件（JSONL，append-only，位于 ``~/.papermine/experience/``）：
  - ``episodic.jsonl``     案例记忆（人类检查点决策 + 运行摘要，F1 反馈源）。
  - ``semantic.jsonl``     语义记忆（⑦ 蒸馏的经验条目，经验 = 去领域化 principle + 结构化 policy）。
  - ``calibration.jsonl``  校准记忆（评估预测 vs 实际结果，预留）。
- 经验条目 schema 与 architecture §3.6 对齐（M8 迁移 delta 已生效）：
  ``scope -> source_domain + applicability``、``trigger -> applicability.preconditions``、
  ``insight -> principle``、``action -> policy.target + policy.directive``、
  新增 ``effect``（F3 落点）、``status`` 增加 ``degraded`` / ``retired``。
- 去重键：``principle + applicability``（旧为 ``scope + insight``）。
- 检索注入（M1）：按 ``applicability`` 门控（不覆盖当前任务不注入），只返回 ``active``。
- 生命周期 ``candidate -> active -> degraded -> retired``，由 ``support_count + effect`` 驱动：
  - 晋升：``support_count >= PROMOTE_THRESHOLD`` 且（人工确认 或 ``effect.outcome == positive``）。
  - 降级：``effect.outcome == negative`` 累积 -> confidence 下降 -> ``degraded``。
  - 退役：``retire()`` 后不参与注入，保留审计。

冻结接口（docs/build-plan.md §4 M7，M8 依据 delta 更新 ``retrieve``）：

    def record_decision(run_id, checkpoint, decision, note) -> None
    def retrieve(applicability: dict, k: int = 3) -> list[dict]   # scope -> applicability 门控
"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import storage

__all__ = [
    "PROMOTE_THRESHOLD",
    "DEGRADE_CONFIDENCE_FLOOR",
    "CONFIDENCE_STEP",
    "TARGETS",
    "OUTCOMES",
    "STATUSES",
    "EPISODIC_FILENAME",
    "SEMANTIC_FILENAME",
    "CALIBRATION_FILENAME",
    "record_decision",
    "retrieve",
    "record_effect",
    "retire",
    "append_semantic",
    "read_semantic",
    "append_episodic",
    "new_experience_id",
    "_applicability_matches",
    "_dedup_key",
]

# 三种记忆的文件名（相对 experience/ 目录）
EPISODIC_FILENAME = "episodic.jsonl"
SEMANTIC_FILENAME = "semantic.jsonl"
CALIBRATION_FILENAME = "calibration.jsonl"

# 晋升门槛：support_count 达到该值即具备晋升资格（还需人工确认 或 effect positive）
PROMOTE_THRESHOLD = 2

# 降级阈值与步长（由 effect.outcome=negative 累积驱动，见 §3.7）
DEGRADE_CONFIDENCE_FLOOR = 0.3
CONFIDENCE_STEP = 0.25

# policy.target 的合法取值（architecture §3.1 / §3.6：四个行为环节）
TARGETS = ("prompt", "planning", "search", "evaluation")
# effect.outcome 的合法取值（F3 落点）
OUTCOMES = ("positive", "neutral", "negative")
# status 生命周期（§3.7）
STATUSES = ("candidate", "active", "degraded", "retired")

# 语义条目逻辑键（与 architecture §3.6 对齐；``type`` 为内部实现字段）
_SEMANTIC_KEYS = (
    "experience_id", "type", "source_domain", "applicability", "principle",
    "policy", "effect", "confidence", "support_count", "status",
    "source_runs", "created_at", "updated_at",
)


def _path(filename: str) -> Path:
    """返回 experience/ 目录下某记忆文件的路径（目录不存在时先建）。"""
    return storage.experience_dir() / filename


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def new_experience_id(prefix: str = "exp") -> str:
    """生成稳定的经验条目 id（experience_id）。"""
    return "{}_{}".format(prefix, uuid.uuid4().hex[:10])


def _norm(text: Any) -> str:
    """折叠空白，用于文本字段比较。"""
    return " ".join(str(text or "").split())


def _dedup(items: Any) -> List[str]:
    out: List[str] = []
    seen: set = set()
    for it in items or []:
        s = _norm(it)
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _coerce_confidence(value: Any) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return round(max(0.0, min(1.0, float(value))), 2)
    return 0.5


# ---------------------------------------------------------------------------
# schema 规范化（含旧 schema -> 新 schema 的读取迁移）
# ---------------------------------------------------------------------------

def _legacy_applicability(entry: Dict[str, Any]) -> Dict[str, Any]:
    """旧字段 scope / trigger -> 新 applicability（读取旧 semantic.jsonl 时迁移）。"""
    scope = _norm(entry.get("scope"))
    trigger = _norm(entry.get("trigger"))
    domains: List[str] = ["*"]
    task_types: List[str] = ["*"]
    if scope and scope != "global":
        if scope.startswith("domain:"):
            domains = [scope.split(":", 1)[1]]
        elif scope.startswith("task:"):
            task_types = [scope.split(":", 1)[1]]
    preconditions = [trigger] if trigger else []
    return {"domains": domains, "task_types": task_types, "preconditions": preconditions}


def _normalize_applicability(value: Any) -> Dict[str, Any]:
    app = value if isinstance(value, dict) else {}
    domains = _dedup(app.get("domains")) or ["*"]
    task_types = _dedup(app.get("task_types")) or ["*"]
    preconditions = _dedup(app.get("preconditions"))
    return {"domains": domains, "task_types": task_types, "preconditions": preconditions}


def _normalize_policy(value: Any) -> Dict[str, str]:
    p = value if isinstance(value, dict) else {}
    target = _norm(p.get("target")) or "prompt"
    if target not in TARGETS:
        target = "prompt"
    return {"target": target, "directive": _norm(p.get("directive"))}


def _normalize_effect(value: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(value, dict) or not value:
        return None
    outcome = _norm(value.get("outcome"))
    if outcome not in OUTCOMES:
        outcome = "neutral"
    return {
        "outcome": outcome,
        "measured_by": _norm(value.get("measured_by")) or "human_review",
        "note": _norm(value.get("note")),
        "updated_at": _norm(value.get("updated_at")) or _now(),
    }


def _normalize_semantic(entry: Dict[str, Any]) -> Dict[str, Any]:
    """补齐语义条目字段并做类型归一 + 旧字段迁移（缺省值兜底）。"""
    now = _now()
    entry = dict(entry or {})

    # ---- 旧 schema -> 新 schema 迁移（仅当新字段缺失时补） ----
    if not entry.get("principle") and entry.get("insight"):
        entry["principle"] = entry["insight"]
    if not entry.get("source_domain") and entry.get("scope"):
        scope = _norm(entry["scope"])
        if scope == "global" or not scope:
            entry["source_domain"] = "*"
        elif scope.startswith("domain:"):
            entry["source_domain"] = scope.split(":", 1)[1]
        elif scope.startswith("task:"):
            entry["source_domain"] = "*"
        else:
            entry["source_domain"] = scope
    if not entry.get("applicability"):
        entry["applicability"] = _legacy_applicability(entry)
    if not entry.get("policy") and entry.get("action"):
        entry["policy"] = {"target": "prompt", "directive": entry["action"]}

    status = _norm(entry.get("status")) or "candidate"
    if status not in STATUSES:
        status = "candidate"

    return {
        "experience_id": _norm(entry.get("experience_id")) or new_experience_id(),
        "type": _norm(entry.get("type")) or "pattern",
        "source_domain": _norm(entry.get("source_domain")) or "*",
        "applicability": _normalize_applicability(entry.get("applicability")),
        "principle": _norm(entry.get("principle")),
        "policy": _normalize_policy(entry.get("policy")),
        "effect": _normalize_effect(entry.get("effect")),
        "confidence": _coerce_confidence(entry.get("confidence")),
        "support_count": int(entry.get("support_count") or 0),
        "status": status,
        "source_runs": _dedup(entry.get("source_runs")),
        "created_at": _norm(entry.get("created_at")) or now,
        "updated_at": _norm(entry.get("updated_at")) or now,
    }


def _dedup_key(entry: Dict[str, Any]) -> tuple:
    """去重键 = principle + applicability（§3.6；旧为 scope + insight）。"""
    app = entry.get("applicability") if isinstance(entry.get("applicability"), dict) else {}
    app_sig = json.dumps(
        {
            "domains": sorted(app.get("domains") or []),
            "task_types": sorted(app.get("task_types") or []),
            "preconditions": sorted(app.get("preconditions") or []),
        },
        ensure_ascii=False,
    )
    return (_norm(entry.get("principle")), app_sig)


# ---------------------------------------------------------------------------
# applicability 门控（防跨域污染，§3.7）
# ---------------------------------------------------------------------------

def _list_covers(entry_list: Any, query_list: Any) -> bool:
    """判断条目列表是否覆盖查询列表。

    - 条目列表为空或含 ``*`` -> 领域/任务无关，覆盖任何上下文；
    - 条目有限制但查询无上下文 -> 保守不注入（无法确认，防跨域污染）；
    - 否则要求二者有交集。
    """
    entry_list = _dedup(entry_list)
    if not entry_list or "*" in entry_list:
        return True
    query_list = _dedup(query_list)
    if not query_list:
        return False
    return bool(set(entry_list) & set(query_list))


def _precondition_overlap(precondition: str, signals: Any) -> bool:
    """判断条目的一条 precondition 是否被查询信号「指示」（双向子串包含）。

    只做确定性子串包含，不做模糊匹配：避免「项目包含任务：异常检测」与
    「项目包含任务：推荐」这类共享模板前缀的文本被误判为命中（防跨域污染）。
    """
    pn = _norm(precondition)
    if not pn:
        return True
    for s in signals or []:
        sn = _norm(s)
        if not sn:
            continue
        if pn in sn or sn in pn:
            return True
    return False


def _preconditions_match(entry_pre: Any, query_pre: Any) -> bool:
    """preconditions 门控：条目无 precondition -> 无条件命中；有则要求至少一条被当前信号指示。"""
    entry_pre = _dedup(entry_pre)
    if not entry_pre:
        return True
    query_pre = _dedup(query_pre)
    if not query_pre:
        return False  # 有条目前提但上下文未知 -> 保守不注入
    return any(_precondition_overlap(p, query_pre) for p in entry_pre)


def _applicability_matches(entry_app: Any, query: Any) -> bool:
    """判断条目 applicability 是否覆盖当前任务上下文（query）。

    query 形如 ``{"domains": [...], "task_types": [...], "preconditions": [...]}``；
    三者都覆盖才命中。``query is None`` 视为无上下文约束（返回 True，由调用方决定）。
    """
    entry_app = _normalize_applicability(entry_app)
    if query is None:
        return True
    if not isinstance(query, dict):
        return True
    if not _list_covers(entry_app.get("domains"), query.get("domains")):
        return False
    if not _list_covers(entry_app.get("task_types"), query.get("task_types")):
        return False
    if not _preconditions_match(entry_app.get("preconditions"), query.get("preconditions")):
        return False
    return True


# ---------------------------------------------------------------------------
# 生命周期（candidate -> active -> degraded -> retired，由 support_count + effect 驱动）
# ---------------------------------------------------------------------------

def _support(entry: Dict[str, Any]) -> int:
    return int(entry.get("support_count") or 0)


def _effect_outcome(entry: Dict[str, Any]) -> Optional[str]:
    eff = entry.get("effect")
    if isinstance(eff, dict):
        return eff.get("outcome")
    return None


def _apply_lifecycle(entry: Dict[str, Any]) -> None:
    """由 support_count + effect 重算 status（retired 终态不动）。

    - 降级：effect.outcome == negative 且 confidence <= 阈值 -> degraded；
    - 晋升：effect.outcome == positive 且 support_count >= 阈值 -> active；
    - 其余保持现状（不自动从 active/candidate 互跳）。
    """
    if entry.get("status") == "retired":
        return
    outcome = _effect_outcome(entry)
    if outcome == "negative" and _coerce_confidence(entry.get("confidence")) <= DEGRADE_CONFIDENCE_FLOOR:
        entry["status"] = "degraded"
        entry["updated_at"] = _now()
    elif outcome == "positive" and _support(entry) >= PROMOTE_THRESHOLD and entry.get("status") == "candidate":
        entry["status"] = "active"
        entry["updated_at"] = _now()


# ---------------------------------------------------------------------------
# 读写（JSONL）
# ---------------------------------------------------------------------------

def read_semantic() -> List[Dict[str, Any]]:
    """读取语义记忆全部条目（读时做旧字段迁移 + 归一）。"""
    return [_normalize_semantic(e) for e in storage.read_jsonl(_path(SEMANTIC_FILENAME))]


def _write_jsonl(path: Path, entries: List[Dict[str, Any]]) -> None:
    """整体重写一个 JSONL 文件（用于更新 support_count / status / effect）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        for obj in entries:
            fh.write(json.dumps(obj, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def append_semantic(entry: Dict[str, Any]) -> str:
    """写入一条语义记忆条目（去重：同 ``principle + applicability`` 累加 support_count）。

    - 命中已存在条目：support_count +1、合并 source_runs、confidence 取较大值；
      重复观察本身不晋升（§3.7 需人工确认或 effect positive），仅当新条目已 active
      （reflect 已人工确认）或 effect positive 时才晋升。
    - 新条目：按传入字段原样写入（reflect 已算好 status / effect）。
    返回该条目的 experience_id。
    """
    entry = _normalize_semantic(entry)
    if not entry["principle"]:
        return entry["experience_id"]  # 无原则的条目不落盘

    key = _dedup_key(entry)
    existing = read_semantic()
    for e in existing:
        if _dedup_key(e) != key:
            continue
        e["support_count"] = _support(e) + 1
        e["source_runs"] = _dedup(list(e.get("source_runs") or []) + entry["source_runs"])
        e["confidence"] = max(_coerce_confidence(e.get("confidence")), entry["confidence"])
        if entry.get("policy", {}).get("directive"):
            e["policy"] = entry["policy"]
        # 仅当已有 effect 为空、或新 effect 带非 neutral 信号时才覆盖（避免 neutral 冲掉正向信号）
        if entry.get("effect") and (not e.get("effect") or _effect_outcome(entry) != "neutral"):
            e["effect"] = entry["effect"]
        if entry.get("status") == "active":
            e["status"] = "active"
        _apply_lifecycle(e)
        _write_jsonl(_path(SEMANTIC_FILENAME), existing)
        return str(e.get("experience_id"))

    storage.append_jsonl(_path(SEMANTIC_FILENAME), entry)
    return str(entry["experience_id"])


def append_episodic(entry: Dict[str, Any]) -> str:
    """写入一条案例记忆（决策或运行摘要）。"""
    entry = dict(entry)
    entry.setdefault("experience_id", new_experience_id("epi"))
    entry.setdefault("created_at", _now())
    storage.append_jsonl(_path(EPISODIC_FILENAME), entry)
    return str(entry["experience_id"])


# ---------------------------------------------------------------------------
# 晋升 / 反馈 / 退役
# ---------------------------------------------------------------------------

def _promote_by_run(run_id: str) -> None:
    """F1 正反馈（accept/note）-> 提升同 run 的语义条目：support_count +1 并记 human_review 正信号。"""
    entries = read_semantic()
    changed = False
    for e in entries:
        if e.get("status") == "retired":
            continue
        if run_id not in [str(r) for r in (e.get("source_runs") or [])]:
            continue
        e["support_count"] = _support(e) + 1
        # F1 人工确认 = human_review 的 positive 信号（与 §3.7「人工确认或 effect positive」对齐）
        if _effect_outcome(e) in (None, "neutral"):
            e["effect"] = {
                "outcome": "positive",
                "measured_by": "human_review",
                "note": "检查点人工确认（F1）",
                "updated_at": _now(),
            }
        _apply_lifecycle(e)
        changed = True
    if changed:
        _write_jsonl(_path(SEMANTIC_FILENAME), entries)


def record_effect(experience_id: str, outcome: str,
                  measured_by: str = "human_review", note: str = "") -> Optional[str]:
    """记录一条经验的效果（F3 结果信号，落点为 ``effect``），并驱动生命周期。

    - positive：confidence +CONFIDENCE_STEP；candidate 且 support_count 达标 -> active。
    - negative：confidence -CONFIDENCE_STEP；跌破阈值 -> degraded。
    - neutral：仅更新 effect，不动 confidence / status。
    找不到 experience_id 时返回 None。
    """
    outcome = _norm(outcome)
    if outcome not in OUTCOMES:
        return None
    entries = read_semantic()
    for e in entries:
        if str(e.get("experience_id")) != str(experience_id):
            continue
        e["effect"] = {
            "outcome": outcome,
            "measured_by": _norm(measured_by) or "human_review",
            "note": _norm(note),
            "updated_at": _now(),
        }
        conf = _coerce_confidence(e.get("confidence"))
        if outcome == "positive":
            conf = round(min(1.0, conf + CONFIDENCE_STEP), 2)
        elif outcome == "negative":
            conf = round(max(0.0, conf - CONFIDENCE_STEP), 2)
        e["confidence"] = conf
        _apply_lifecycle(e)
        _write_jsonl(_path(SEMANTIC_FILENAME), entries)
        return str(e["experience_id"])
    return None


def retire(experience_id: str, note: str = "") -> Optional[str]:
    """退役一条经验：status -> retired（不物理删除，保留审计，不再参与 M1 注入）。"""
    entries = read_semantic()
    for e in entries:
        if str(e.get("experience_id")) != str(experience_id):
            continue
        e["status"] = "retired"
        e["updated_at"] = _now()
        if note:
            eff = dict(e.get("effect") or {})
            eff["note"] = "; ".join(x for x in (_norm(eff.get("note")), _norm(note)) if x)
            eff["updated_at"] = _now()
            e["effect"] = eff
        _write_jsonl(_path(SEMANTIC_FILENAME), entries)
        return str(e["experience_id"])
    return None


# ---------------------------------------------------------------------------
# 冻结接口
# ---------------------------------------------------------------------------

def record_decision(run_id: str, checkpoint: str, decision: str, note: str) -> None:
    """记录一次人类检查点决策（F1 反馈源），写案例记忆。

    冻结契约（docs/build-plan.md §4 M7）：
        def record_decision(run_id, checkpoint, decision, note) -> None

    - 决策落 ``episodic.jsonl``（type=decision），供审计与追溯；
    - ``decision == "accept" / "note"`` 时对同 run 的语义条目做人工确认晋升（F1）。
    """
    append_episodic({
        "type": "decision",
        "run_id": str(run_id),
        "checkpoint": str(checkpoint),
        "decision": str(decision),
        "note": str(note or ""),
    })
    # accept / note 都是正反馈（note = 接受并附注），触发同 run 条目的人工确认晋升
    if str(decision) in ("accept", "note"):
        _promote_by_run(str(run_id))
    return None


def retrieve(applicability: Optional[Dict[str, Any]] = None, k: int = 3) -> List[dict]:
    """按 applicability 门控检索 top-k 条 active 语义记忆，供 M1 检索注入。

    M8 契约（docs/build-plan.md §4 M8，由 M7 的 ``retrieve(scope, k)`` 升级）：
        def retrieve(applicability: dict, k: int = 3) -> list[dict]

    - 只返回 ``status == active`` 的条目（candidate / degraded / retired 均不注入，防污染）；
    - ``applicability`` 为当前任务上下文 ``{"domains"/"task_types"/"preconditions"}``；
      不覆盖的条目不命中；``None`` 视为无上下文约束（命中全部 active）。
    - 排序：confidence 降序、support_count 降序。
    """
    k = int(k) if k is not None else 3
    if k <= 0:
        return []
    entries = [
        e for e in read_semantic()
        if e.get("status") == "active" and _applicability_matches(e.get("applicability"), applicability)
    ]
    entries.sort(key=lambda e: (
        -_coerce_confidence(e.get("confidence")),
        -int(e.get("support_count") or 0),
    ))
    return entries[:k]
