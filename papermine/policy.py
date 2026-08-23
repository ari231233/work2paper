"""policy 注入辅助模块：把经验库的 policy 渲染成对应 Agent 的行为约束注入。

对应 docs/architecture.md §3.4 M1 / §3.5（混合注入）与 docs/build-plan.md §4 M8 要点 4：

- **结构化层（确定性）**：``policy.target`` 决定改哪个 Agent 的哪个环节
  （``prompt | planning | search | evaluation`` -> 状态机里的具体状态）。
- **LLM 层（灵活性）**：运行时把 ``policy.directive`` 渲染成该 Agent 的行为准则，
  追加到其 system prompt 的末尾，由 LLM 在科研的开放空间里灵活执行。

即：**结构决定「在哪生效 + 约束是什么」，LLM 负责「在开放空间里执行这条约束」**。

注入不修改任何 Agent 的 ``run(...)`` 签名，也不改动其它模块的实现：编排器按状态
把匹配的 directive 包一层 ``LLMProvider`` 包装器，Agent 内部照常 ``llm.complete(system, ...)``，
包装器在调用底层 provider 前把 directive 追加到 system 文本。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from . import experience
from .dossier import Dossier

__all__ = [
    "TARGETS_FOR_STATE",
    "build_context",
    "targets_for_state",
    "retrieve_for_state",
    "group_by_target",
    "render_block",
    "inject",
    "InjectingProvider",
]

# policy.target -> 状态机里的状态（architecture §3.1 四个行为环节）
# ``prompt`` 落在 L1 理解抽象层（① 项目理解 + ② 问题抽象）。
TARGETS_FOR_STATE: Dict[str, tuple] = {
    "prompt": ("UNDERSTAND", "ABSTRACT"),
    "search": ("IDEATE",),
    "evaluation": ("EVALUATE",),
    "planning": ("PLAN",),
}


def build_context(dossier: Dossier) -> Dict[str, List[str]]:
    """从 dossier 构建当前任务的 applicability 上下文（domains / task_types / preconditions）。

    领域 = facts.scenarios，任务类型 = facts.tasks；preconditions 用 facts 的具象信号拼出，
    与 experience 里的 precondition 文本做子串/双字组重叠匹配（确定性门控）。
    """
    facts = (dossier.assets or {}).get("facts") if isinstance(dossier.assets, dict) else {}
    if not isinstance(facts, dict):
        facts = {}
    domains = [str(x) for x in (facts.get("scenarios") or []) if str(x).strip()]
    task_types = [str(x) for x in (facts.get("tasks") or []) if str(x).strip()]

    preconditions: List[str] = []
    if task_types:
        preconditions.append("项目包含任务：{}".format("、".join(task_types[:3])))
    if domains:
        preconditions.append("项目场景：{}".format("、".join(domains[:3])))
    metrics = [str(x) for x in (facts.get("metrics") or []) if str(x).strip()]
    if metrics:
        preconditions.append("关注指标：{}".format("、".join(metrics[:3])))

    return {"domains": domains, "task_types": task_types, "preconditions": preconditions}


def targets_for_state(state: str) -> List[str]:
    """返回某状态应注入的 policy.target 列表。"""
    state = (state or "").strip()
    return [t for t, states in TARGETS_FOR_STATE.items() if state in states]


def group_by_target(entries: List[dict]) -> Dict[str, List[str]]:
    """把检索到的经验条目按 policy.target 分组，返回 target -> [directive, ...]。"""
    out: Dict[str, List[str]] = {}
    for e in entries or []:
        if not isinstance(e, dict):
            continue
        policy = e.get("policy")
        if not isinstance(policy, dict):
            continue
        target = str(policy.get("target") or "").strip()
        directive = str(policy.get("directive") or "").strip()
        if target and directive and directive not in out.setdefault(target, []):
            out[target].append(directive)
    return out


def render_block(directives: List[str]) -> str:
    """把若干 directive 渲染成追加到 system prompt 的行为准则块。"""
    directives = [d for d in (directives or []) if str(d).strip()]
    if not directives:
        return ""
    lines = ["", "【历史经验注入的行为准则】",
             "以下为系统从历史任务中沉淀的可复用策略，请在开放空间里尽量遵循（不违背给定事实与输出 schema 优先）："]
    for i, d in enumerate(directives, 1):
        lines.append("{}. {}".format(i, d))
    return "\n".join(lines)


class InjectingProvider:
    """把若干 directive 追加到 system prompt 的 LLMProvider 包装器。

    只影响 system 文本；user / schema / temperature 原样透传，底层仍做 schema 校验与重试。
    """

    def __init__(self, inner: Any, directives: List[str]) -> None:
        self._inner = inner
        self._block = render_block(directives)

    def complete(self, system: str, user: str,
                 schema: dict, temperature: float = 0.2) -> dict:
        return self._inner.complete(system + self._block, user, schema, temperature)


def inject(llm: Any, directives: List[str]) -> Any:
    """无 directive 时原样返回 llm；否则返回包装后的 InjectingProvider。"""
    block = render_block(directives)
    if not block:
        return llm
    return InjectingProvider(llm, directives)


def retrieve_for_state(dossier: Dossier, state: str, k: int = 8) -> List[dict]:
    """编排器入口：按当前任务上下文检索 active 经验，过滤出本状态 target 对应的条目。"""
    context = build_context(dossier)
    entries = experience.retrieve(context, k=k)
    targets = set(targets_for_state(state))
    return [e for e in entries if e.get("policy", {}).get("target") in targets]
