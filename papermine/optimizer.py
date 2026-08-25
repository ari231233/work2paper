"""Policy Optimizer（M8 v2）：根据 policy 的 usage + effect（含 M12 evidence 强度信号）
自动优化 confidence / 生命周期 / 检索注入优先级。

对应 docs/build-plan.md §4 M8 v2 与 docs/architecture.md §3.7（生命周期护栏）：

- **usage**：注入次数 + 关联 run/idea，是「被使用」的弱信号（不单独改 confidence，防漂移）。
- **effect**：人工 review / F3 结果信号（复用 M8 的 ``effect``，落点不变）。
- **evidence**：M12 的 idea 证据强度（weak / medium / strong）作为 idea 质量信号，折入 effect 通道。
- **自动更新**：按 usage + effect 调 confidence（升/降）、推进生命周期
  （candidate -> active -> degraded -> retired）、调整检索注入优先级（排序）。
- **防漂移**：更新设阈值门槛——置信度单步封顶（``CONFIDENCE_STEP``）、晋升/降级/退役
  均需累计信号达标，单次信号不会剧烈波动（沿用 §3.7 生命周期护栏）。

纯函数模块：只做确定性计算，不读写磁盘（磁盘读写由 ``papermine.experience`` 负责）。
本模块是阈值与评分规则的**唯一事实源**，``experience.py`` 从此导入（单向依赖，无环）。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

__all__ = [
    "CONFIDENCE_STEP",
    "DEGRADE_CONFIDENCE_THRESHOLD",
    "PROMOTE_SUPPORT_THRESHOLD",
    "PROMOTE_USAGE_THRESHOLD",
    "PROMOTE_USAGE_CONFIDENCE",
    "RETIRE_NEGATIVE_THRESHOLD",
    "EVIDENCE_SIGNAL",
    "PRIORITY_WEIGHTS",
    "priority_score",
    "evidence_signal",
    "aggregate_evidence",
    "evidence_levels_from_evaluations",
    "recompute_lifecycle",
    "optimize",
]

# 置信度单步幅度（升/降都封顶一步，防单次信号剧烈波动）
CONFIDENCE_STEP = 0.25

# 置信度跌破该值 -> 进入 degraded（与 experience.DEGRADE_CONFIDENCE_FLOOR 语义一致）
DEGRADE_CONFIDENCE_THRESHOLD = 0.3

# 晋升门槛：support_count 达到该值即具备晋升资格（还需 effect positive 或人工确认）
PROMOTE_SUPPORT_THRESHOLD = 2

# usage 驱动的自动晋升门槛：注入次数达到该值 + 置信度达标 -> candidate 晋升 active
PROMOTE_USAGE_THRESHOLD = 2
PROMOTE_USAGE_CONFIDENCE = 0.6

# 退役门槛：连续负信号（effect negative / evidence weak）达到该值且置信度跌破阈值 -> retired
RETIRE_NEGATIVE_THRESHOLD = 3

# M12 evidence 强度 -> 质量信号（weak 折为负、strong 折为正、medium 中性）
EVIDENCE_SIGNAL: Dict[str, float] = {"weak": -1.0, "medium": 0.0, "strong": 1.0}

# 检索注入优先级合成权重：priority = confidence + Σ(权重 × 信号)。
# confidence 主导排序；support / usage / effect 作为可解释的次要信号打破平局并体现
# 「被使用且有效」的策略应更靠前（M8 v2 要点 3）。
PRIORITY_WEIGHTS: Dict[str, float] = {
    "support": 0.05,
    "usage": 0.02,
    "effect": 0.1,
}

# retired 条目在排序里的惩罚分（防御性：即使被误读入检索池也排到最后）
_RETIRED_PENALTY = 1000.0


def _conf(value: Any) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return round(max(0.0, min(1.0, float(value))), 2)
    return 0.5


def _outcome(entry: Dict[str, Any]) -> Optional[str]:
    eff = entry.get("effect")
    if isinstance(eff, dict):
        return eff.get("outcome")
    return None


def _injections(entry: Dict[str, Any]) -> int:
    usage = entry.get("usage")
    if isinstance(usage, dict):
        try:
            return max(0, int(usage.get("injections") or 0))
        except (TypeError, ValueError):
            return 0
    return 0


# ---------------------------------------------------------------------------
# 优先级（检索注入排序）
# ---------------------------------------------------------------------------

def priority_score(entry: Dict[str, Any]) -> float:
    """计算一条经验的检索注入优先级（分数越高越靠前）。

    - 主体 = confidence（延续 M8 既有排序，保证旧行为不回归）；
    - 次要信号 = support_count / usage.injections / effect.outcome（M8 v2：被使用且有效 → 更靠前）；
    - ``retired`` 施加 -1000 惩罚（防御性兜底，正常情况下不会被读入检索池）。
    """
    outcome = _outcome(entry)
    effect_signal = 1.0 if outcome == "positive" else (-1.0 if outcome == "negative" else 0.0)
    support = 0
    try:
        support = max(0, int(entry.get("support_count") or 0))
    except (TypeError, ValueError):
        support = 0

    score = (
        _conf(entry.get("confidence"))
        + PRIORITY_WEIGHTS["support"] * support
        + PRIORITY_WEIGHTS["usage"] * _injections(entry)
        + PRIORITY_WEIGHTS["effect"] * effect_signal
    )
    if entry.get("status") == "retired":
        score -= _RETIRED_PENALTY
    return round(score, 4)


# ---------------------------------------------------------------------------
# M12 evidence 强度 -> idea 质量信号
# ---------------------------------------------------------------------------

def evidence_signal(level: Any) -> float:
    """把 M12 证据强度（weak/medium/strong）映射为 -1/0/+1 的质量信号。"""
    return EVIDENCE_SIGNAL.get(level, 0.0)


def aggregate_evidence(levels: Optional[List[str]]) -> float:
    """把一组 idea 的证据强度聚合成净信号（-1 ~ +1）。

    净信号 = 有效信号求和 / 有效信号数（比例，抗漂移）：
    - 全是 strong -> +1；全是 weak -> -1；
    - strong/weak 相抵 -> 0（中性，不动 confidence）。
    """
    valid = [evidence_signal(l) for l in (levels or []) if l in EVIDENCE_SIGNAL]
    if not valid:
        return 0.0
    return round(sum(valid) / len(valid), 4)


def evidence_levels_from_evaluations(evaluations: Optional[List[dict]]) -> List[str]:
    """从 dossier.evaluations 抽取 M12 证据强度（``evaluation.evidence_validation.evidence``）。

    返回各 idea 的证据强度列表，供 ``experience.optimize(evidence_by_run=...)`` 折入 policy。
    """
    out: List[str] = []
    for ev in evaluations or []:
        if not isinstance(ev, dict):
            continue
        evv = ev.get("evidence_validation")
        if isinstance(evv, dict) and evv.get("evidence") in EVIDENCE_SIGNAL:
            out.append(evv["evidence"])
    return out


# ---------------------------------------------------------------------------
# 生命周期（candidate -> active -> degraded -> retired，§3.7 护栏）
# ---------------------------------------------------------------------------

def recompute_lifecycle(entry: Dict[str, Any]) -> str:
    """由 usage + effect + confidence 重算 status（retired 为终态，不自动复活）。

    规则（护栏：每级迁移都需累计信号达标，单次信号不触发）：
    - 退役：连续负信号 ``negative_count >= RETIRE_NEGATIVE_THRESHOLD`` 且 ``confidence`` 跌破阈值；
    - 降级：至少一次负信号且 ``confidence <= DEGRADE_CONFIDENCE_THRESHOLD``；
    - 恢复：degraded + effect positive + confidence 回升 -> active（滞后带，防抖动）；
    - 晋升：candidate 在（effect positive 且 support 达标）或（usage 达标且 confidence 达标）时 -> active；
    - 其余保持现状（不自动在 active/candidate 间互跳）。
    """
    status = entry.get("status")
    if status == "retired":
        return "retired"

    conf = _conf(entry.get("confidence"))
    outcome = _outcome(entry)
    support = 0
    try:
        support = max(0, int(entry.get("support_count") or 0))
    except (TypeError, ValueError):
        support = 0
    negative_count = 0
    try:
        negative_count = max(0, int(entry.get("negative_count") or 0))
    except (TypeError, ValueError):
        negative_count = 0

    if negative_count >= RETIRE_NEGATIVE_THRESHOLD and conf <= DEGRADE_CONFIDENCE_THRESHOLD:
        return "retired"
    if (outcome == "negative" or negative_count >= 1) and conf <= DEGRADE_CONFIDENCE_THRESHOLD:
        return "degraded"
    if status == "degraded" and outcome == "positive" and conf > DEGRADE_CONFIDENCE_THRESHOLD:
        return "active"
    if status == "candidate":
        if outcome == "positive" and support >= PROMOTE_SUPPORT_THRESHOLD:
            return "active"
        if _injections(entry) >= PROMOTE_USAGE_THRESHOLD and conf >= PROMOTE_USAGE_CONFIDENCE:
            return "active"
    return status


# ---------------------------------------------------------------------------
# 自动优化入口（纯函数）
# ---------------------------------------------------------------------------

def optimize(entry: Dict[str, Any],
             evidence_levels: Optional[List[str]] = None) -> Tuple[Dict[str, Any], List[dict]]:
    """对一条经验做一次自动优化，返回 ``(新条目, 变更列表)``。

    - ``evidence_levels``：该条目关联 idea 的 M12 证据强度列表（可空）；
      - 净信号 > 0（strong 共识）-> confidence +CONFIDENCE_STEP，并清零连负计数；
      - 净信号 < 0（weak 共识）-> confidence -CONFIDENCE_STEP，连负计数 +1；
      - 净信号 = 0 -> 不动 confidence（抗漂移）。
    - 随后重算生命周期与优先级（effect 已由 ``record_effect`` 折入 confidence，此处不再重复折入）。
    """
    entry = dict(entry)
    changes: List[dict] = []

    net = aggregate_evidence(evidence_levels) if evidence_levels else 0.0
    conf = _conf(entry.get("confidence"))
    if net > 0:
        new_conf = _conf(conf + CONFIDENCE_STEP)
        if new_conf != conf:
            entry["confidence"] = new_conf
            changes.append({"field": "confidence", "delta": round(new_conf - conf, 2),
                            "signal": "evidence_positive"})
        entry["negative_count"] = 0
    elif net < 0:
        new_conf = _conf(conf - CONFIDENCE_STEP)
        if new_conf != conf:
            entry["confidence"] = new_conf
            changes.append({"field": "confidence", "delta": round(new_conf - conf, 2),
                            "signal": "evidence_negative"})
        negative_count = 0
        try:
            negative_count = max(0, int(entry.get("negative_count") or 0))
        except (TypeError, ValueError):
            negative_count = 0
        entry["negative_count"] = negative_count + 1

    new_status = recompute_lifecycle(entry)
    if new_status != entry.get("status"):
        changes.append({"field": "status", "from": entry.get("status"), "to": new_status})
        entry["status"] = new_status

    entry["priority"] = priority_score(entry)
    return entry, changes
