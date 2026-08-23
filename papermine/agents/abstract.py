"""② 问题抽象 Agent：dossier.assets -> dossier.problems。

对应 docs/build-plan.md §3.3 / §4 M4 与 docs/architecture.md §5 ②：

- 输入：``dossier.assets``（facts + narrative + evidence，由 ① 项目理解 Agent 产出）。
- 输出：``dossier.problems``（候选研究问题列表）。
- 每个问题强制包含 ``formulation / motivation / why_not_engineering / evidence_refs``，
  其中 ``why_not_engineering`` 用反例思维论证「为何不是纯工程」，用于过滤伪问题。
- 无 LLM（NullProvider 空结果 / LLMError / SchemaError）时降级为确定性规则：
  按 facts.tasks 生成问题骨架，并显式标记 ``provenance=deterministic``、``confidence=low``。

prompt 模板位于 ``papermine/prompts/abstract.md``（带 version 头），版本写入
``meta.prompt_versions["abstract"]``，用于可重放（engineering.md §1.2）。

注意：Agent 只原地写 ``dossier.problems``，不负责 save / bump_version / snapshot——
落盘与版本推进由编排器（M7）统一处理，保证契约单一职责。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..dossier import Dossier
from ..llm import LLMError, LLMProvider, SchemaError

__all__ = [
    "run",
    "PROBLEMS_SCHEMA",
    "_load_prompt",
    "_fallback_problems",
]

# prompt 文件（包相对路径），带 version 头（engineering.md §1.2）
_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "abstract.md"
_VERSION_RE = re.compile(r"<!--\s*version\s*:\s*([A-Za-z0-9._-]+)\s*-->")
_PROMPT_VERSION_FALLBACK = "v1"

# 建模类任务：只有这类任务才适合抽象成"方法问题"（与 mining.py 语义一致，本模块独立维护）
_MODELING_TASKS = frozenset({
    "分类", "回归预测", "时序预测", "异常检测", "剩余寿命预测",
    "聚类", "推荐", "目标检测",
})

# 本 Agent 的 LLM 输出契约：与 schemas/dossier.schema.json 的 problems 项对齐。
# 仅强制「任务卡要求」的四个内容字段；problem_id / title 由 normalize 阶段补全，
# additionalProperties 留宽，避免模型多带字段导致不必要重试。
PROBLEMS_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "required": ["problems"],
    "properties": {
        "problems": {
            "type": "array",
            "items": {
                "type": "object",
                "required": [
                    "formulation",
                    "motivation",
                    "why_not_engineering",
                    "evidence_refs",
                ],
                "properties": {
                    "problem_id": {"type": "string"},
                    "title": {"type": "string"},
                    "formulation": {"type": "string"},
                    "motivation": {"type": "string"},
                    "why_not_engineering": {"type": "string"},
                    "evidence_refs": {"type": "array", "items": {"type": "string"}},
                },
            },
        }
    },
}


# ---------------------------------------------------------------------------
# prompt 加载与版本化
# ---------------------------------------------------------------------------

def _load_prompt() -> Tuple[str, str]:
    """加载 prompts/abstract.md，返回 (系统 prompt 文本, 版本字符串如 "v1")。

    版本来自文件头的 ``<!-- version: N -->``；文件缺失或头缺失时降级为
    ``_PROMPT_VERSION_FALLBACK``（此时 LLM 路径大概率校验失败，自然落到确定性降级）。
    """
    try:
        text = _PROMPT_PATH.read_text(encoding="utf-8")
    except OSError:
        text = ""
    m = _VERSION_RE.search(text)
    if m:
        version = "v" + m.group(1).lstrip("vV")
    else:
        version = _PROMPT_VERSION_FALLBACK
    return text, version


# ---------------------------------------------------------------------------
# 输入 / 证据
# ---------------------------------------------------------------------------

def _evidence_sources(dossier: Dossier) -> List[str]:
    """收集 dossier.assets.evidence 中的全部 source（去重保序）。"""
    evidence = (dossier.assets or {}).get("evidence") or []
    out: List[str] = []
    for ev in evidence:
        if isinstance(ev, dict) and ev.get("source"):
            src = str(ev["source"])
            if src not in out:
                out.append(src)
    return out


def _build_user_prompt(dossier: Dossier) -> str:
    """构造发给 LLM 的脱敏输入：narrative + facts + evidence（不回传源码）。"""
    assets = dossier.assets or {}
    payload = {
        "narrative": assets.get("narrative") or "",
        "facts": assets.get("facts") or {},
        "evidence": assets.get("evidence") or [],
    }
    return (
        "以下是项目理解阶段的产出（已脱敏），请据此抽象出可研究问题：\n"
        + json.dumps(payload, ensure_ascii=False)
    )


# ---------------------------------------------------------------------------
# LLM 路径：调用 + 规范化
# ---------------------------------------------------------------------------

def _sanitize_refs(value: Any, known_sources: List[str]) -> List[str]:
    """清洗 evidence_refs：仅保留出现在证据源列表中的非空字符串（provenance 强制）。

    模型编造 / 引用不到的 source 一律丢弃；空数组是合法的（schema 允许）。
    """
    if not isinstance(value, list):
        return []
    known = set(known_sources)
    out: List[str] = []
    for v in value:
        if isinstance(v, str) and v.strip() and v.strip() in known and v.strip() not in out:
            out.append(v.strip())
    return out


def _title_from_text(text: str) -> str:
    """从 formulation 截取一个可读标题（供 LLM 漏给 title 时兜底）。"""
    text = (text or "").strip()
    if not text:
        return "候选研究问题"
    if len(text) <= 40:
        return text
    return text[:40] + "…"


def _normalize_problems(items: Any, dossier: Dossier) -> List[dict]:
    """把 LLM 返回的 problems 清洗成规范问题列表。

    - 跳过非 dict 项与缺关键论证字段（formulation / motivation / why_not_engineering）的项；
    - 重新编号 problem_id 为 p1..pn，保证唯一、可被后续 idea.problem_ref 稳定引用；
    - 补 title（缺失时从 formulation 截取）；
    - evidence_refs 只保留真实证据源；标注 provenance=llm。
    """
    sources = _evidence_sources(dossier)
    out: List[dict] = []
    if not isinstance(items, list):
        return out
    for item in items:
        if not isinstance(item, dict):
            continue
        formulation = str(item.get("formulation") or "").strip()
        motivation = str(item.get("motivation") or "").strip()
        why_not = str(item.get("why_not_engineering") or "").strip()
        if not (formulation and motivation and why_not):
            continue
        title = str(item.get("title") or "").strip() or _title_from_text(formulation)
        out.append({
            "problem_id": "p{}".format(len(out) + 1),
            "title": title,
            "formulation": formulation,
            "motivation": motivation,
            "why_not_engineering": why_not,
            "evidence_refs": _sanitize_refs(item.get("evidence_refs"), sources),
            "provenance": "llm",
        })
    return out


def _try_llm(dossier: Dossier, llm: LLMProvider, system: str) -> Optional[List[dict]]:
    """尝试用 LLM 抽象问题；成功返回规范化问题列表，失败 / 空结果返回 None。"""
    result: Dict[str, Any] = {}
    try:
        result = llm.complete(
            system, _build_user_prompt(dossier), PROBLEMS_SCHEMA, temperature=0.2,
        )
    except (LLMError, SchemaError):
        return None
    if not isinstance(result, dict):
        return None
    problems = _normalize_problems(result.get("problems"), dossier)
    return problems or None


# ---------------------------------------------------------------------------
# 确定性降级：按任务生成问题骨架
# ---------------------------------------------------------------------------

def _task_problem(
    idx: int, task: str, scenario: str,
    data: List[str], methods: List[str], metrics: List[str], sources: List[str],
) -> dict:
    """为单个建模任务生成问题骨架（含强制 why_not_engineering）。"""
    data_text = data[0] if data else "给定数据"
    method_text = "、".join([m for m in methods if m][:2]) or "现有方法"
    metric_text = metrics[0] if metrics else "统一评测指标"
    return {
        "problem_id": "p{}".format(idx),
        "title": "{}场景下的{}问题".format(scenario, task),
        "formulation": (
            "在{}场景中，给定{}，如何对「{}」构建可泛化、可复现的建模与评测方案"
            "（当前仅有{}等实现/基线，缺少方法层面的系统比较与结论）？".format(
                scenario, data_text, task, method_text
            )
        ),
        "motivation": (
            "横向项目中反复出现{}需求；若能超越一次性交付，提炼出可迁移、可比较的方法，"
            "将直接复用于同类项目。".format(task)
        ),
        "why_not_engineering": (
            "纯工程交付只需在单次项目中达到甲方验收指标即可，而本问题要求回答一个可泛化的研究问题："
            "「{}」在{}场景下是否存在优于{}、且能稳定复现的建模方案；"
            "这需要系统性对比与可量化结论（如{}），而非一次性脚本。".format(
                task, scenario, method_text, metric_text
            )
        ),
        "evidence_refs": list(sources),
        "provenance": "deterministic",
        "confidence": "low",
    }


def _tool_problem(idx: int, scenario: str, modules: List[str], sources: List[str]) -> dict:
    """当存在可复用组件时，补一个系统/工具方向的骨架问题。"""
    comps = "、".join([m for m in modules if m][:4])
    return {
        "problem_id": "p{}".format(idx),
        "title": "面向{}任务的通用工具/框架抽象".format(scenario),
        "formulation": (
            "如何将项目沉淀的可复用组件（{}）抽象为一套通用、可扩展、可复现的工具/框架，"
            "以降低同类任务重复开发成本？".format(comps)
        ),
        "motivation": (
            "项目中已沉淀可复用组件（{}），但目前停留在单仓库级别，未抽象为通用系统，"
            "同类横向项目仍在重复造轮子。".format(comps)
        ),
        "why_not_engineering": (
            "一次性脚本/组件的工程复用不是研究；本问题需回答该工具/框架的抽象边界、可扩展性与可复现性是否成立，"
            "以及相比手工实现是否带来可量化的效率提升——这需要系统设计与评测，而非单纯重构。"
        ),
        "evidence_refs": list(sources),
        "provenance": "deterministic",
        "confidence": "low",
    }


def _generic_problem(idx: int, scenario: str, sources: List[str]) -> dict:
    """facts 完全缺失时的兜底骨架，避免 problems 为空。"""
    return {
        "problem_id": "p{}".format(idx),
        "title": "{}场景下的可泛化方法与评测问题".format(scenario),
        "formulation": "给定该项目涉及的任务与数据，如何提炼出一个可泛化、可复现、可比较的方法问题？",
        "motivation": "确定性层未识别到足够明确的任务信号，但仍可沿项目场景做基础的问题抽象。",
        "why_not_engineering": (
            "本问题要求回答一个可泛化、可复现、可比较的研究问题，而非一次性工程交付；"
            "但当前证据不足，需人工补充项目事实后核验其研究价值。"
        ),
        "evidence_refs": list(sources),
        "provenance": "deterministic",
        "confidence": "low",
    }


def _fallback_problems(dossier: Dossier) -> List[dict]:
    """无 LLM 时的确定性降级：按 facts.tasks 生成问题骨架（低置信）。"""
    facts = (dossier.assets or {}).get("facts") or {}
    tasks = [t for t in (facts.get("tasks") or []) if isinstance(t, str) and t.strip()]
    methods = facts.get("methods") or []
    data = facts.get("data") or []
    scenarios = facts.get("scenarios") or []
    metrics = facts.get("metrics") or []
    modules = facts.get("modules") or []
    sources = _evidence_sources(dossier)

    scenario = scenarios[0] if scenarios else "跨项目"
    modeling = [t for t in tasks if t in _MODELING_TASKS]
    chosen = (modeling or tasks)[:3]

    problems: List[dict] = []
    for task in chosen:
        problems.append(
            _task_problem(len(problems) + 1, task, scenario, data, methods, metrics, sources)
        )

    if modules and len(problems) < 3:
        problems.append(_tool_problem(len(problems) + 1, scenario, modules, sources))

    if not problems:
        problems.append(_generic_problem(1, scenario, sources))
    return problems


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def run(dossier: Dossier, llm: LLMProvider) -> None:
    """dossier.assets -> dossier.problems（原地写）。

    冻结契约（docs/build-plan.md §3.3）：
        def run(dossier: Dossier, llm: LLMProvider) -> None

    步骤：加载 prompt 并记录版本 -> 优先 LLM 抽象 -> 失败 / 空结果降级为确定性规则
    -> 原地**替换** dossier.problems（不追加，保证重跑幂等）。
    """
    system, version = _load_prompt()
    dossier.meta.setdefault("prompt_versions", {})["abstract"] = version

    problems = _try_llm(dossier, llm, system)
    if problems is None:
        problems = _fallback_problems(dossier)
    dossier.problems = problems
