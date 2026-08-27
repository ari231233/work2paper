"""① 项目理解 Agent：项目目录 -> dossier.assets（facts + narrative + evidence）。

对应 docs/build-plan.md §3.3 / §4 M3 与 docs/architecture.md §5 ①：

- **L0 确定性层**：复用 ``scanner.scan`` + ``knowledge.extract_elements`` 扫出六元组 facts
  与证据（零成本、可复现）。
- **L1 理解层**：把**脱敏后**的 facts + evidence 交给 LLM，产出 ``narrative``（项目叙事）
  与「事实语义纠偏」（合并同义标签 / 修正误标 / 去噪 / 补漏）。
- **证据沿用确定性层**：不在 LLM 阶段新造 evidence，保证 provenance 强制。

隐私边界（architecture §9）：发给 LLM 的只有结构化 facts 与 evidence 摘要，绝不回传完整源码。

降级路径（architecture §7 / §8）：
无 key（NullProvider 返回空 dict）、网络失败（LLMError）、schema 失败（SchemaError）时，
降级为确定性 narrative，facts 保持确定性层结果。
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List

from ..dossier import Dossier
from ..knowledge import extract_elements
from ..llm import LLMError, LLMProvider, SchemaError
from ..models import Project
from ..scanner import scan

__all__ = [
    "run",
    "UNDERSTAND_SCHEMA",
    "_deterministic_narrative",
    "_apply_corrections",
]

# 本 Agent 系统 prompt 版本：任何 prompt 改动必须 bump 并写入 meta.prompt_versions
_PROMPT_VERSION = "v2"

# 六元组中可由 LLM 语义纠偏的类别（关键词词典命中 → 可能误标 / 可合并 / 可去噪）
_SEMANTIC_KEYS = ("tasks", "methods", "data", "scenarios", "metrics")
# 结构类：AST / 目录客观提取，不做语义纠偏，保持确定性结果
_STRUCTURAL_KEYS = ("libraries", "modules")
_FACTS_KEYS = _SEMANTIC_KEYS + _STRUCTURAL_KEYS

# 本 Agent 的 LLM 输出契约（schema 校验走 papermine/llm.py 的极简子集）
UNDERSTAND_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["narrative", "corrections"],
    "properties": {
        "narrative": {"type": "string"},
        "corrections": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "tasks": {"type": "array", "items": {"type": "string"}},
                "methods": {"type": "array", "items": {"type": "string"}},
                "data": {"type": "array", "items": {"type": "string"}},
                "scenarios": {"type": "array", "items": {"type": "string"}},
                "metrics": {"type": "array", "items": {"type": "string"}},
            },
        },
    },
}

_SYSTEM_PROMPT = (
    "你是 papermine 的「项目理解 Agent」。输入是确定性扫描得到的项目事实（facts，六元组标签）"
    "与脱敏后的证据摘要；其中可能包含文档正文短摘录，但不含完整源码或完整文档。\n\n"
    "你的任务有两项：\n"
    "1. narrative：用 2~4 句学术中文，概括这个横向项目做什么、用什么方法、面向什么场景、"
    "关注什么指标。只基于给定 facts 与 evidence 归纳，不得编造 facts 之外的信息。\n"
    "2. corrections：对 facts 中由关键词词典命中的语义标签做「事实语义纠偏」：\n"
    "   - 合并同义 / 近义标签为一个规范标签；\n"
    "   - 修正明显误标（关键词误命中导致的错误标签）；\n"
    "   - 去除与项目无关的偶发噪声标签；\n"
    "   - 仅当给定 evidence 中明确出现、但词典漏掉时才补充；\n"
    "   - 文档标题、摘要或正文摘录的直接陈述优先于孤立关键词计数；两者冲突时删除误标；\n"
    "   - 每个类别返回「纠偏后的完整标签列表」（不是增量）；若无需修改，原样返回该类别。\n"
    "只纠偏 tasks / methods / data / scenarios / metrics 五类；"
    "libraries / modules 为客观提取，无需返回。"
)


def _facts_from_element(element: Any) -> Dict[str, List[str]]:
    """把确定性层的 Element 转成 dossier.assets.facts（七类列表，保序）。"""
    return {
        "tasks": list(element.tasks),
        "methods": list(element.methods),
        "data": list(element.data),
        "scenarios": list(element.scenarios),
        "metrics": list(element.metrics),
        "libraries": list(element.libraries),
        "modules": list(element.modules),
    }


def _build_user_prompt(facts: Dict[str, List[str]], evidence: List[dict]) -> str:
    """构造发给 LLM 的脱敏输入：结构化 facts + evidence 摘要（不回传源码）。"""
    excerpts = [item for item in evidence if str(item.get("snippet", "")).startswith("文档正文摘录")]
    diagnostics = [item for item in evidence if str(item.get("snippet", "")).startswith("文档解析提示")]
    keyword_hits = [
        item for item in evidence
        if item not in excerpts and item not in diagnostics
    ]
    selected_evidence = excerpts[:12] + diagnostics[:8] + keyword_hits[:40]
    payload = {"facts": facts, "evidence": selected_evidence}
    return (
        "以下是确定性扫描得到的项目事实与证据摘要，请据此生成 narrative 与事实纠偏：\n"
        + json.dumps(payload, ensure_ascii=False)
    )


def _deterministic_narrative(facts: Dict[str, List[str]]) -> str:
    """无 LLM 时的确定性降级叙事：按六元组拼出一段非空中文概括。"""
    def _first(items: List[str], n: int = 3) -> str:
        return "、".join([i for i in items if i][:n])

    parts: List[str] = []
    scenario = _first(facts.get("scenarios", []))
    tasks = _first(facts.get("tasks", []))
    methods = _first(facts.get("methods", []))
    data = _first(facts.get("data", []))
    metrics = _first(facts.get("metrics", []))

    if scenario:
        parts.append("面向{}场景".format(scenario))
    if tasks:
        parts.append("围绕{}等任务".format(tasks))
    if methods:
        parts.append("采用{}等方法".format(methods))
    if data:
        parts.append("处理{}数据".format(data))
    if metrics:
        parts.append("以{}为主要评估指标".format(metrics))

    if not parts:
        return "（确定性层未识别到足够项目信号，建议补充项目文档或代码注释。）"
    return "本项目" + "，".join(parts) + "。"


def _clean_strings(vals: Any) -> List[str]:
    """把 LLM 返回的列表清洗成去重保序的非空字符串列表。"""
    out: List[str] = []
    seen: set = set()
    if not isinstance(vals, list):
        return out
    for v in vals:
        if isinstance(v, str) and v.strip() and v.strip() not in seen:
            seen.add(v.strip())
            out.append(v.strip())
    return out


def _apply_corrections(
    facts: Dict[str, List[str]], corrections: Any
) -> Dict[str, List[str]]:
    """把 LLM 纠偏结果合并进 facts。

    - 语义类（tasks/methods/data/scenarios/metrics）：LLM 返回**非空**列表时替换，否则保留确定性值；
    - 结构类（libraries/modules）：客观提取，忽略纠偏，始终保持确定性值。
    """
    corrected = {k: list(v) for k, v in facts.items()}
    if not isinstance(corrections, dict):
        return corrected
    for key in _SEMANTIC_KEYS:
        clean = _clean_strings(corrections.get(key))
        if clean:
            corrected[key] = clean
    return corrected


def _generate_with_llm(
    llm: LLMProvider,
    facts: Dict[str, List[str]],
    evidence: List[dict],
) -> tuple:
    """调用 LLM 生成 narrative + 纠偏；任何失败/空结果都安全降级。"""
    result: Dict[str, Any] = {}
    try:
        result = llm.complete(
            _SYSTEM_PROMPT, _build_user_prompt(facts, evidence),
            UNDERSTAND_SCHEMA, temperature=0.2,
        )
    except (LLMError, SchemaError):
        result = {}
    if not isinstance(result, dict):
        result = {}

    narrative = str(result.get("narrative") or "").strip()
    corrections = result.get("corrections")
    if not narrative:
        narrative = _deterministic_narrative(facts)
    return narrative, corrections


def run(project_dir: str, dossier: Dossier, llm: LLMProvider) -> None:
    """项目目录 -> dossier.assets（facts + narrative + evidence）。

    冻结契约（docs/build-plan.md §3.3）：
        def run(project_dir: str, dossier: Dossier, llm: LLMProvider) -> None

    步骤：确定性扫描出 facts/evidence -> LLM 生成 narrative + 事实语义纠偏 -> 原地写 dossier.assets。
    """
    project_dir = os.path.abspath(project_dir)
    name = os.path.basename(project_dir.rstrip(os.sep)) or project_dir

    # ---- L0 确定性层：扫描 + 六元组抽取（复用 scanner / knowledge）----
    assets = scan(project_dir)
    project = Project(name=name, root=project_dir, assets=assets)
    element, evidence = extract_elements(project)

    facts = _facts_from_element(element)
    evidence_list = [{"source": ev.source, "snippet": ev.snippet} for ev in evidence]

    # ---- L1 理解层：LLM narrative + 语义纠偏（可降级）----
    narrative, corrections = _generate_with_llm(llm, facts, evidence_list)
    facts = _apply_corrections(facts, corrections)

    # ---- 原地写 Dossier ----
    dossier.assets["facts"] = facts
    dossier.assets["narrative"] = narrative
    dossier.assets["evidence"] = evidence_list
    dossier.meta.setdefault("prompt_versions", {})["understand"] = _PROMPT_VERSION
