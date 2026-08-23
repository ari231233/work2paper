"""经验库 v1：跨项目记忆（Evolution Layer 的数据层）。

对应 docs/architecture.md §3 与 docs/engineering.md §2/§3：

- 三种记忆文件（JSONL，append-only，位于 ``~/.papermine/experience/``）：
  - ``episodic.jsonl``     案例记忆（人类检查点决策 + 运行摘要，F1 反馈源）。
  - ``semantic.jsonl``     语义记忆（⑦ 蒸馏的经验条目，type=pattern）。
  - ``calibration.jsonl``  校准记忆（评估预测 vs 实际结果，预留）。
- 经验条目 schema 与 architecture §3.6 对齐，带 ``confidence / support_count``。
- 晋升门槛：``candidate`` -> ``active``（人工确认 或 ``support_count >= PROMOTE_THRESHOLD``），
  ``active`` 才参与 ``retrieve`` 的 M1 检索注入；``retired`` 不物理删除、可审计。

冻结接口（docs/build-plan.md §4 M7）：

    def record_decision(run_id, checkpoint, decision, note) -> None
    def retrieve(scope: str, k: int = 3) -> list[dict]
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
    "EPISODIC_FILENAME",
    "SEMANTIC_FILENAME",
    "CALIBRATION_FILENAME",
    "record_decision",
    "retrieve",
    "append_semantic",
    "read_semantic",
    "append_episodic",
    "new_experience_id",
    "_scope_matches",
]

# 三种记忆的文件名（相对 experience/ 目录）
EPISODIC_FILENAME = "episodic.jsonl"
SEMANTIC_FILENAME = "semantic.jsonl"
CALIBRATION_FILENAME = "calibration.jsonl"

# 晋升门槛：support_count 达到该值即从 candidate 转 active（进入检索池）
PROMOTE_THRESHOLD = 2

# 语义条目必需键（与 architecture §3.6 对齐；status 为本实现额外加的晋升状态位）
_SEMANTIC_KEYS = (
    "experience_id", "type", "scope", "trigger", "insight", "action",
    "confidence", "support_count", "source_runs", "status",
    "created_at", "updated_at",
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
    """折叠空白，用于 scope+insight 去重比较。"""
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


def _scope_matches(entry_scope: Any, query_scope: Any) -> bool:
    """判断条目 scope 是否命中查询 scope。

    - ``global`` 或空查询匹配所有；
    - 其余按前缀匹配（``task`` 命中 ``task:异常检测``，``task:异常检测`` 精确命中）。
    """
    q = _norm(query_scope)
    if not q or q == "global":
        return True
    e = _norm(entry_scope)
    if not e or e == "global":
        return True
    return e == q or e.startswith(q)


# ---------------------------------------------------------------------------
# 读写（JSONL）
# ---------------------------------------------------------------------------

def read_semantic() -> List[Dict[str, Any]]:
    """读取语义记忆全部条目。"""
    return storage.read_jsonl(_path(SEMANTIC_FILENAME))


def _write_jsonl(path: Path, entries: List[Dict[str, Any]]) -> None:
    """整体重写一个 JSONL 文件（用于更新 support_count / status）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        for obj in entries:
            fh.write(json.dumps(obj, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def _normalize_semantic(entry: Dict[str, Any]) -> Dict[str, Any]:
    """补齐语义条目字段并做类型归一（缺省值兜底）。"""
    now = _now()
    out: Dict[str, Any] = {
        "experience_id": _norm(entry.get("experience_id")) or new_experience_id(),
        "type": _norm(entry.get("type")) or "pattern",
        "scope": _norm(entry.get("scope")) or "global",
        "trigger": _norm(entry.get("trigger")),
        "insight": _norm(entry.get("insight")),
        "action": _norm(entry.get("action")),
        "confidence": _coerce_confidence(entry.get("confidence")),
        "support_count": int(entry.get("support_count") or 0),
        "source_runs": _dedup(entry.get("source_runs")),
        "status": _norm(entry.get("status")) or "candidate",
        "created_at": _norm(entry.get("created_at")) or now,
        "updated_at": _norm(entry.get("updated_at")) or now,
    }
    return out


def _coerce_confidence(value: Any) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return round(max(0.0, min(1.0, float(value))), 2)
    return 0.5


def append_semantic(entry: Dict[str, Any]) -> str:
    """写入一条语义记忆条目（去重：同 ``scope + insight`` 累加 support_count）。

    - 命中已存在条目：support_count +1、合并 source_runs、confidence 取较大值；
      达到 ``PROMOTE_THRESHOLD`` 或新条目已 active 时晋升为 active。
    - 新条目：按传入的 support_count / status 原样写入（reflect 已算好）。
    返回该条目的 experience_id。
    """
    entry = _normalize_semantic(entry)
    if not entry["insight"]:
        return entry["experience_id"]  # 无洞察的条目不落盘

    key = (entry["scope"], _norm(entry["insight"]))
    existing = read_semantic()
    for e in existing:
        if (e.get("scope"), _norm(e.get("insight"))) != key:
            continue
        e["support_count"] = int(e.get("support_count") or 0) + 1
        e["source_runs"] = _dedup(list(e.get("source_runs") or []) + entry["source_runs"])
        e["confidence"] = max(_coerce_confidence(e.get("confidence")),
                              entry["confidence"])
        if entry.get("trigger"):
            e["trigger"] = entry["trigger"]
        if entry.get("action"):
            e["action"] = entry["action"]
        if int(e.get("support_count") or 0) >= PROMOTE_THRESHOLD or entry.get("status") == "active":
            e["status"] = "active"
        e["updated_at"] = _now()
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
# 晋升 / 反馈
# ---------------------------------------------------------------------------

def _promote_by_run(run_id: str) -> None:
    """F1 正反馈（accept）→ 提升同 run 的 candidate 语义条目支持数（人工确认晋升路径）。"""
    entries = read_semantic()
    changed = False
    for e in entries:
        if e.get("status") != "candidate":
            continue
        if run_id not in [str(r) for r in (e.get("source_runs") or [])]:
            continue
        e["support_count"] = int(e.get("support_count") or 0) + 1
        if int(e["support_count"]) >= PROMOTE_THRESHOLD:
            e["status"] = "active"
        e["updated_at"] = _now()
        changed = True
    if changed:
        _write_jsonl(_path(SEMANTIC_FILENAME), entries)


# ---------------------------------------------------------------------------
# 冻结接口
# ---------------------------------------------------------------------------

def record_decision(run_id: str, checkpoint: str, decision: str, note: str) -> None:
    """记录一次人类检查点决策（F1 反馈源），写案例记忆。

    冻结契约（docs/build-plan.md §4 M7）：
        def record_decision(run_id, checkpoint, decision, note) -> None

    - 决策落 ``episodic.jsonl``（type=decision），供审计与追溯；
    - ``decision == "accept"`` 时对同 run 的 candidate 语义条目做人工确认晋升。
    """
    append_episodic({
        "type": "decision",
        "run_id": str(run_id),
        "checkpoint": str(checkpoint),
        "decision": str(decision),
        "note": str(note or ""),
    })
    # accept / note 都是正反馈（note = 接受并附注），触发同 run candidate 的人工确认晋升
    if str(decision) in ("accept", "note"):
        _promote_by_run(str(run_id))
    return None


def retrieve(scope: str, k: int = 3) -> List[dict]:
    """按 scope 检索 top-k 条已晋升（active）的语义记忆，供 M1 检索注入。

    冻结契约（docs/build-plan.md §4 M7）：
        def retrieve(scope: str, k: int = 3) -> list[dict]

    - 只返回 ``status == active`` 的条目（candidate 不参与注入，防污染）；
    - 排序：confidence 降序、support_count 降序；
    - ``scope == "global"`` 匹配全部。
    """
    k = int(k) if k is not None else 3
    if k <= 0:
        return []
    entries = [
        e for e in read_semantic()
        if e.get("status") == "active" and _scope_matches(e.get("scope"), scope)
    ]
    entries.sort(key=lambda e: (
        -_coerce_confidence(e.get("confidence")),
        -int(e.get("support_count") or 0),
    ))
    return entries[:k]
