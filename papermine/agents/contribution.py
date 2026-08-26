"""M21 — 面向硕士的创新点理解：创新类型分类 + 贡献矩阵 + 攻击测试。

对应 docs/build-plan.md §4 M21：

把评估从「novelty 打分 → accept/reject」改为「创新类型分类 + 贡献矩阵 + 攻击测试」，
避免「模块组合 → 误 reject」（硕士生创新点要求较博士宽松，框架集成 / 应用创新同样有论文价值）。

- **M21.1 Contribution Type Classifier**：先分类、不评分——A 新模块创新 / B 框架集成创新 /
  C 应用创新 / D 问题重新建模 / E 训练策略创新；
- **M21.2 Contribution Matrix**：输出贡献矩阵（贡献类型 × 强度 × 原因），不输出单一 novelty 分；
- **M21.3 Attack Test**：对每个 idea 生成三类攻击并提前回答——消融 / 简单拼接 / reviewer 视角。

与 M11/M12/M20 的关系：本模块是评估的「前置重构」——分类 / 矩阵 / 攻击测试**先于** novelty 评分，
由 evaluate.py 在 EVALUATE 内部**先**调用本模块，再走 novelty rubric；novelty 分数降级为
「参考维度」，不再作为直接 reject 依据（verdict 按贡献类型差异化，见 evaluate._decide_verdict）。

本模块**不改 Dossier 顶层字段、不改冻结接口**（docs/build-plan.md §3.2/§3.3）：只提供
``classify_contribution`` / ``classify_contribution_batch`` 纯函数，由 evaluate.py 在
EVALUATE 内部调用并把结果写入每条 evaluation 的 ``contribution`` 子对象。

降级路径（architecture §7 / §8）：无 LLM（NullProvider 返回空）、LLMError、SchemaError、
或 LLM 返回结构非法时，退化为**确定性规则**（词面信号分类 + 规则强度 + 模板攻击），
并标注 ``degraded=True``（低置信）。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..llm import LLMError, LLMProvider, SchemaError

__all__ = [
    "classify_contribution",
    "classify_contribution_batch",
    "CONTRIBUTION_TYPES",
    "CONTRIBUTION_TYPE_LABELS",
    "MATRIX_DIMENSIONS",
    "MATRIX_LABELS",
    "STRENGTH_LEVELS",
    "STRENGTH_ORDER",
    "STRENGTH_LABELS",
    "ATTACK_KEYS",
    "ATTACK_LABELS",
    "CONTRIBUTION_SCHEMA",
    "CONTRIBUTION_BATCH_SCHEMA",
    "matrix_viable",
    "render_contribution_lines",
    "_deterministic_contribution",
    "_finalize_contribution",
    "_call_llm",
    "_build_user_prompt",
    "_build_batch_user_prompt",
]

# 本 Agent prompt 版本：优先读 prompts/contribution.md 头的 version，缺失时用此兜底
_PROMPT_VERSION = "v1"
_PROMPT_FILENAME = "contribution.md"
_PROMPT_VERSION_RE = re.compile(r"<!--\s*version:\s*(\d+)\s*-->")

# ---------------------------------------------------------------------------
# M21.1：贡献类型（A-E）
# ---------------------------------------------------------------------------

CONTRIBUTION_TYPES: Tuple[str, ...] = ("A", "B", "C", "D", "E")

# 类型 -> 中文标签（含英文原词，便于人读）
CONTRIBUTION_TYPE_LABELS: Dict[str, str] = {
    "A": "新模块创新（Method Innovation）",
    "B": "框架集成创新（Framework Integration）",
    "C": "应用创新（Application Innovation）",
    "D": "问题重新建模（Problem Formulation）",
    "E": "训练策略创新（Training Strategy Innovation）",
}

# 确定性分类时每种类型的默认理由
_TYPE_REASONS: Dict[str, str] = {
    "A": "主张提出新模块 / 新机制，属于方法层面的创新",
    "B": "主张已有方法的新组合 / 框架集成，属于框架集成创新",
    "C": "主张把已有方法迁移到新场景，属于应用创新",
    "D": "主张对问题重新建模 / 重新定义，属于问题重新建模",
    "E": "主张训练策略层面的改进，属于训练策略创新",
}

# ---------------------------------------------------------------------------
# M21.2：贡献矩阵（6 个固定维度，覆盖 A-E + 工程价值）
# ---------------------------------------------------------------------------

# 矩阵维度键（顺序即报告展示顺序）
MATRIX_DIMENSIONS: Tuple[str, ...] = (
    "method", "framework", "application", "problem", "training", "engineering",
)

MATRIX_LABELS: Dict[str, str] = {
    "method": "方法创新",
    "framework": "框架创新",
    "application": "应用创新",
    "problem": "问题创新",
    "training": "训练策略创新",
    "engineering": "工程价值",
}

# 强度档（0~4），中文标签对齐 M21 任务卡示例（低 / 中高 / 高）
STRENGTH_LEVELS: Tuple[str, ...] = ("none", "low", "medium", "medium_high", "high")
STRENGTH_ORDER: Dict[str, int] = {s: i for i, s in enumerate(STRENGTH_LEVELS)}
STRENGTH_LABELS: Dict[str, str] = {
    "none": "无", "low": "低", "medium": "中", "medium_high": "中高", "high": "高",
}

# ---------------------------------------------------------------------------
# M21.3：攻击测试（三类）
# ---------------------------------------------------------------------------

ATTACK_KEYS: Tuple[str, ...] = ("ablation", "concatenation", "reviewer")

ATTACK_LABELS: Dict[str, str] = {
    "ablation": "Attack 1（消融）",
    "concatenation": "Attack 2（简单拼接）",
    "reviewer": "Attack 3（reviewer 视角）",
}

# ---------------------------------------------------------------------------
# 确定性分类 / 强度 / 攻击用的词面信号词典（无 LLM 时降级）
# ---------------------------------------------------------------------------

_COMBINATION_MARKERS: Tuple[str, ...] = (
    "组合", "集成", "融合", "联合建模", "联合优化", "联合", "结合", "拼接", "串接",
    "协同", "多任务", "辅助", "耦合", "多阶段", "级联", "framework integration",
)
_APPLICATION_MARKERS: Tuple[str, ...] = (
    "迁移", "应用到", "应用", "新场景", "场景", "跨域", "跨任务", "落地", "部署",
    "适配", "泛化", "跨领域", "application",
)
_REFORM_MARKERS: Tuple[str, ...] = (
    "重新建模", "重新定义", "重新形式化", "重新表述", "重构", "形式化", "新问题",
    "统一框架", "重新刻画", "problem formulation",
)
_TRAINING_MARKERS: Tuple[str, ...] = (
    "训练策略", "课程学习", "自监督", "对比学习", "知识蒸馏", "蒸馏", "预训练", "微调",
    "损失函数", "优化目标", "动态权重", "学习率", "正则化", "curriculum", "pretrain",
)
# 方法创新 / 新模块强信号（与「提出/改进」这类泛词区分开）
_NEW_MECHANISM_MARKERS: Tuple[str, ...] = (
    "新模块", "新机制", "新结构", "新算子", "新网络", "新损失", "端到端", "可微",
    "自适应", "可学习", "novel module", "novel mechanism",
)

_SYSTEM_PROMPT_FALLBACK = (
    "你是 papermine 的「创新贡献分析 Agent」。对候选创新点做**先分类、不评分**的贡献分析，"
    "为硕士论文评估提供比单一 novelty 分数更细的判断。\n"
    "你只做三件事：\n"
    "1. contribution_type：把创新点归入 A/B/C/D/E 之一（A 新模块创新、B 框架集成创新、"
    "C 应用创新、D 问题重新建模、E 训练策略创新），并给 reason；\n"
    "2. matrix：对 6 个贡献维度（method/framework/application/problem/training/engineering）"
    "各给 strength∈{none,low,medium,medium_high,high} + reason；\n"
    "3. attacks：生成三类攻击测试并提前回答——ablation（消融：删掉核心模块剩下什么）、"
    "concatenation（简单拼接：A→B 换成 A+B concat 是否等效）、reviewer（reviewer 会说"
    "「merely a combination」，提前准备反驳）。\n"
    "只输出符合 schema 的 JSON 对象。"
)


def _prompt_dir() -> Path:
    """返回包内 prompts 目录（papermine/prompts）。"""
    return Path(__file__).resolve().parent.parent / "prompts"


def _load_prompt() -> Tuple[str, str]:
    """读取 prompts/contribution.md，返回 (system_prompt_text, version)。文件缺失时用内联兜底。"""
    path = _prompt_dir() / _PROMPT_FILENAME
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return _SYSTEM_PROMPT_FALLBACK, _PROMPT_VERSION
    m = _PROMPT_VERSION_RE.search(text)
    version = "v{}".format(m.group(1)) if m else _PROMPT_VERSION
    return text, version


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def _clean(s: Any) -> str:
    return " ".join(str(s or "").split())


def _text_of(idea: Dict[str, Any], facts: Optional[Dict[str, Any]]) -> str:
    """把 idea 与项目事实拼成用于词面信号匹配的文本（小写，供词典命中）。"""
    facts = facts or {}
    parts = [
        _clean(idea.get("claim")),
        _clean(idea.get("novelty_hypothesis")),
        " ".join(str(x) for x in (facts.get("tasks") or [])),
        " ".join(str(x) for x in (facts.get("methods") or [])),
        " ".join(str(x) for x in (facts.get("scenarios") or [])),
    ]
    return " ".join(p for p in parts if p).lower()


def _hits(markers: Tuple[str, ...], text: str) -> bool:
    return any(m in text for m in markers)


# ---------------------------------------------------------------------------
# 结构化输出契约（schema 校验走 papermine/llm.py 的极简子集）
# ---------------------------------------------------------------------------

def _strength_object() -> Dict[str, Any]:
    """单个贡献维度的输出契约：strength（强度档）+ reason（原因）。"""
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["strength", "reason"],
        "properties": {
            "strength": {"type": "string", "enum": list(STRENGTH_LEVELS)},
            "reason": {"type": "string"},
        },
    }


def _attack_object() -> Dict[str, Any]:
    """单条攻击测试的输出契约：attack（攻击话术）+ answer（提前准备的回答）。"""
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["attack", "answer"],
        "properties": {
            "attack": {"type": "string"},
            "answer": {"type": "string"},
        },
    }


CONTRIBUTION_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["contribution_type", "matrix", "attacks"],
    "properties": {
        "contribution_type": {
            "type": "object",
            "additionalProperties": False,
            "required": ["type", "reason"],
            "properties": {
                "type": {"type": "string", "enum": list(CONTRIBUTION_TYPES)},
                "reason": {"type": "string"},
            },
        },
        "matrix": {
            "type": "object",
            "additionalProperties": False,
            "required": list(MATRIX_DIMENSIONS),
            "properties": {d: _strength_object() for d in MATRIX_DIMENSIONS},
        },
        "attacks": {
            "type": "object",
            "additionalProperties": False,
            "required": list(ATTACK_KEYS),
            "properties": {k: _attack_object() for k in ATTACK_KEYS},
        },
    },
}

# M15 方向④：批量贡献分析——一次 LLM 调用返回多个 idea 的贡献分析（每条 = 单个 schema + idea_id）。
CONTRIBUTION_BATCH_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["results"],
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["idea_id"] + list(CONTRIBUTION_SCHEMA["required"]),
                "properties": dict(
                    {"idea_id": {"type": "string"}},
                    **CONTRIBUTION_SCHEMA["properties"],
                ),
            },
        },
    },
}


# ---------------------------------------------------------------------------
# 输入装配
# ---------------------------------------------------------------------------

def _literature_summary(literature: List[dict]) -> List[dict]:
    """构造供攻击测试对拍的文献摘要（标题 + gap_note，不含全文）。"""
    out: List[dict] = []
    for entry in literature or []:
        if not isinstance(entry, dict):
            continue
        out.append({
            "query": _clean(entry.get("query")),
            "gap_note": _clean(entry.get("gap_note")),
            "titles": [
                _clean(p.get("title"))
                for p in (entry.get("papers") or [])
                if isinstance(p, dict) and _clean(p.get("title"))
            ],
        })
    return out


def _build_user_prompt(idea: Dict[str, Any], facts: Dict[str, Any],
                       literature: List[dict]) -> str:
    """构造脱敏输入：idea + 项目事实 + 文献摘要，供贡献分析。"""
    facts = facts or {}
    payload = {
        "idea": {
            "idea_id": idea.get("idea_id"),
            "claim": idea.get("claim"),
            "novelty_hypothesis": idea.get("novelty_hypothesis"),
            "problem_ref": idea.get("problem_ref"),
            "literature_refs": idea.get("literature_refs"),
        },
        "facts": {
            "tasks": facts.get("tasks"),
            "methods": facts.get("methods"),
            "scenarios": facts.get("scenarios"),
            "data": facts.get("data"),
            "metrics": facts.get("metrics"),
            "modules": facts.get("modules"),
        },
        "literature": _literature_summary(literature),
    }
    return (
        "以下是一个候选创新点及其证据材料，请做创新贡献分析"
        "（先分类不评分：类型 + 贡献矩阵 + 攻击测试）：\n"
        + json.dumps(payload, ensure_ascii=False)
    )


def _build_batch_user_prompt(ideas: List[dict], facts: Dict[str, Any],
                             literature: List[dict]) -> str:
    """构造批量贡献分析的脱敏输入：一组 idea + 共享项目事实 + 文献摘要。"""
    facts = facts or {}
    payload = {
        "ideas": [
            {
                "idea_id": idea.get("idea_id"),
                "claim": idea.get("claim"),
                "novelty_hypothesis": idea.get("novelty_hypothesis"),
                "problem_ref": idea.get("problem_ref"),
                "literature_refs": idea.get("literature_refs"),
            }
            for idea in ideas
            if isinstance(idea, dict)
        ],
        "facts": {
            "tasks": facts.get("tasks"),
            "methods": facts.get("methods"),
            "scenarios": facts.get("scenarios"),
            "data": facts.get("data"),
            "metrics": facts.get("metrics"),
            "modules": facts.get("modules"),
        },
        "literature": _literature_summary(literature),
    }
    return (
        "以下是一组候选创新点及其共享证据材料，请对每个 idea 分别做创新贡献分析"
        "（先分类不评分：类型 + 贡献矩阵 + 攻击测试）：\n"
        + json.dumps(payload, ensure_ascii=False)
    )


# ---------------------------------------------------------------------------
# LLM 调用
# ---------------------------------------------------------------------------

def _call_llm(llm: Optional[LLMProvider], system: str, idea: Dict[str, Any],
              facts: Dict[str, Any], literature: List[dict]) -> Dict[str, Any]:
    """调用 LLM（核心推理，走 complete）；失败/空结果返回空 dict，由上层降级。"""
    if llm is None:
        return {}
    result: Dict[str, Any] = {}
    try:
        result = llm.complete(
            system, _build_user_prompt(idea, facts, literature),
            CONTRIBUTION_SCHEMA, temperature=0.3,
        )
    except (LLMError, SchemaError):
        result = {}
    return result if isinstance(result, dict) else {}


def _call_llm_batch(llm: Optional[LLMProvider], system: str, ideas: List[dict],
                    facts: Dict[str, Any], literature: List[dict]) -> Optional[Dict[str, dict]]:
    """M15 方向④：批量贡献分析（一次 LLM 调用），返回 ``{idea_id: 单条原始 dict}``。

    失败 / 空结果 / 结构非法 → 返回 None（由 evaluate.py 回退到单条 ``classify_contribution``）。
    """
    if llm is None or not ideas:
        return None
    result: Dict[str, Any] = {}
    try:
        result = llm.complete(
            system, _build_batch_user_prompt(ideas, facts, literature),
            CONTRIBUTION_BATCH_SCHEMA, temperature=0.3,
        )
    except (LLMError, SchemaError):
        return None
    if not isinstance(result, dict):
        return None
    raw = result.get("results")
    if not isinstance(raw, list):
        return None
    out: Dict[str, dict] = {}
    for item in raw:
        if isinstance(item, dict) and _clean(item.get("idea_id")):
            out[_clean(item["idea_id"])] = item
    return out or None


# ---------------------------------------------------------------------------
# 输出规范化（非法 → 确定性降级，全有或全无）
# ---------------------------------------------------------------------------

def _normalize_matrix(matrix: Any) -> Optional[Dict[str, Dict[str, str]]]:
    """把 LLM 的 matrix 输出规范化为 ``{维度: {strength, label, reason}}``；任一维度非法 → None。"""
    if not isinstance(matrix, dict):
        return None
    out: Dict[str, Dict[str, str]] = {}
    for dim in MATRIX_DIMENSIONS:
        item = matrix.get(dim)
        if not isinstance(item, dict):
            return None
        strength = item.get("strength")
        if strength not in STRENGTH_LEVELS:
            return None
        reason = _clean(item.get("reason")) or "（未说明）"
        out[dim] = {"strength": strength, "label": STRENGTH_LABELS[strength], "reason": reason}
    return out


def _normalize_attacks(attacks: Any) -> Optional[Dict[str, Dict[str, str]]]:
    """把 LLM 的 attacks 输出规范化为 ``{ablation/concatenation/reviewer: {attack, answer}}``。

    attack（攻击话术）必须非空；answer（回答）可空（兜底「（未预回答）」）。任一维度非法 → None。
    """
    if not isinstance(attacks, dict):
        return None
    out: Dict[str, Dict[str, str]] = {}
    for key in ATTACK_KEYS:
        item = attacks.get(key)
        if not isinstance(item, dict):
            return None
        attack = _clean(item.get("attack"))
        if not attack:
            return None
        out[key] = {"attack": attack, "answer": _clean(item.get("answer")) or "（未预回答）"}
    return out


def _finalize_contribution(raw: Any, idea: Dict[str, Any],
                           facts: Dict[str, Any],
                           literature: List[dict]) -> Dict[str, Any]:
    """把一条 LLM 原始输出（或空）规范化为最终 ``contribution`` 子对象。

    任一关键字段非法 / 缺失 → 整体退化为确定性规则（degraded=True），绝不部分信任。
    """
    facts = facts or {}
    raw = raw if isinstance(raw, dict) else {}
    ctype_raw = raw.get("contribution_type")
    ctype = ctype_raw.get("type") if isinstance(ctype_raw, dict) else None
    if ctype not in CONTRIBUTION_TYPES:
        return _deterministic_contribution(idea, facts, literature)

    matrix = _normalize_matrix(raw.get("matrix"))
    attacks = _normalize_attacks(raw.get("attacks"))
    if matrix is None or attacks is None:
        return _deterministic_contribution(idea, facts, literature)

    return {
        "type": ctype,
        "type_label": CONTRIBUTION_TYPE_LABELS[ctype],
        "reason": _clean(ctype_raw.get("reason")) or _TYPE_REASONS[ctype],
        "matrix": matrix,
        "attacks": attacks,
        "degraded": False,
    }


# ---------------------------------------------------------------------------
# 确定性降级（词面信号分类 + 规则强度 + 模板攻击）
# ---------------------------------------------------------------------------

def _classify_type(text: str) -> str:
    """按词面信号做确定性分类（优先级：D 问题 > E 训练 > B 框架 > C 应用 > A 方法）。"""
    if _hits(_REFORM_MARKERS, text):
        return "D"
    if _hits(_TRAINING_MARKERS, text):
        return "E"
    if _hits(_COMBINATION_MARKERS, text):
        return "B"
    if _hits(_APPLICATION_MARKERS, text):
        return "C"
    return "A"


def _row(strength: str, reason: str) -> Dict[str, str]:
    return {"strength": strength, "label": STRENGTH_LABELS[strength], "reason": reason}


def _deterministic_matrix(text: str, facts: Dict[str, Any], ctype: str) -> Dict[str, Any]:
    """无 LLM 时按词面信号 + 项目事实给 6 个贡献维度定强度（低置信，报告会标注 degraded）。"""
    new_mech = _hits(_NEW_MECHANISM_MARKERS, text)
    combo = _hits(_COMBINATION_MARKERS, text)
    reform = _hits(_REFORM_MARKERS, text)
    training = _hits(_TRAINING_MARKERS, text)
    has_scenario = bool(facts.get("scenarios") or [])
    migrated = _hits(("迁移", "跨域", "跨任务", "新场景", "落地", "部署", "适配", "跨领域"), text)
    modules = bool(facts.get("modules") or [])
    data = bool(facts.get("data") or [])
    metrics = bool(facts.get("metrics") or [])

    matrix: Dict[str, Any] = {}

    # 方法创新（类型 A 落点）
    if new_mech:
        matrix["method"] = _row("high", "含新模块/新机制信号，方法层面有独立贡献（需消融验证）")
    elif ctype == "A":
        matrix["method"] = _row("medium", "归为方法创新，但未见明确新模块/新机制信号，方法贡献需消融证明")
    elif combo:
        matrix["method"] = _row("low", "以已有模块组合为主，没有提出新模块/新机制")
    else:
        matrix["method"] = _row("none", "无方法层面的贡献信号")

    # 框架创新（类型 B 落点）
    if combo:
        matrix["framework"] = _row("medium_high", "多个已有方法/任务产生交互，存在框架集成价值")
    elif ctype == "B":
        matrix["framework"] = _row("medium", "归为框架集成创新，但交互机制需进一步明确")
    else:
        matrix["framework"] = _row("none", "无框架集成信号")

    # 应用创新（类型 C 落点）
    if migrated:
        matrix["application"] = _row("high", "把已有方法迁移到新场景，应用创新明确")
    elif has_scenario:
        matrix["application"] = _row("medium_high", "面向具体应用场景，具备落地价值")
    else:
        matrix["application"] = _row("low", "未明确面向具体场景")

    # 问题创新（类型 D 落点）
    if reform:
        matrix["problem"] = _row("high", "重新定义/建模了问题，问题层面有创新")
    elif ctype == "D":
        matrix["problem"] = _row("medium_high", "归为问题重新建模，但重定义表述需进一步明确")
    else:
        matrix["problem"] = _row("low", "未重新建模问题")

    # 训练策略创新（类型 E 落点）
    if training:
        matrix["training"] = _row("high", "提出训练策略层面的创新（损失/优化/课程等）")
    elif ctype == "E":
        matrix["training"] = _row("medium", "归为训练策略创新，但具体策略需明确")
    else:
        matrix["training"] = _row("none", "无训练策略层面的贡献信号")

    # 工程价值（横向项目特有：可落地 / 可复现 / 可评测）
    if modules and data and metrics:
        matrix["engineering"] = _row("high", "有可复用组件 + 数据 + 指标，容易落地且可复现")
    elif data and metrics:
        matrix["engineering"] = _row("medium_high", "有数据 + 指标，具备可落地/可评测基础")
    elif modules or data or metrics:
        matrix["engineering"] = _row("medium", "具备部分落地要素（组件/数据/指标其一）")
    else:
        matrix["engineering"] = _row("low", "缺少落地与评测要素")

    return matrix


def _core_descriptor(ctype: str) -> str:
    """按类型给消融攻击用「核心模块」一个可读描述。"""
    return {
        "B": "框架/组合机制",
        "E": "训练策略",
        "D": "重新建模的问题定义",
        "C": "迁移到新场景的适配",
        "A": "核心模块/机制",
    }.get(ctype, "核心模块/机制")


def _deterministic_attacks(text: str, facts: Dict[str, Any], ctype: str) -> Dict[str, Any]:
    """无 LLM 时的模板攻击测试（Attack 1/2/3 + 提前回答），诚实标注需实验验证。"""
    core = _core_descriptor(ctype)
    scenario = "、".join(str(s) for s in (facts.get("scenarios") or [])[:2]) or "目标场景"
    task = "、".join(str(t) for t in (facts.get("tasks") or [])[:2]) or "原任务"
    return {
        "ablation": {
            "attack": "删除{}后，方案还剩下什么？".format(core),
            "answer": "删除{}后方案退化为普通的「{}」，说明{}是主要贡献"
                       "（需消融实验证明该模块不可或缺）。".format(core, task, core),
        },
        "concatenation": {
            "attack": "把 A→B 的交互换成 A+B 的简单拼接（concat/级联）是否等效？",
            "answer": "若等效则机制创新弱；若 dynamic weighting / 共享表示 / 联合优化有效，"
                       "则交互本身是贡献（需以 A+B concat 作消融基线对比）。",
        },
        "reviewer": {
            "attack": "reviewer 可能质疑「merely a combination（只是模块组合/工程实现）」。",
            "answer": "预反驳：存在共享表示 / 联合优化 / 交互机制，且消融实验可证明交互有效；"
                       "即便方法创新有限，在{}下的框架集成/应用价值对硕士论文仍成立。".format(scenario),
        },
    }


def _deterministic_contribution(idea: Dict[str, Any], facts: Dict[str, Any],
                                literature: List[dict]) -> Dict[str, Any]:
    """无 LLM / LLM 输出非法时的确定性贡献分析（degraded=True，低置信）。"""
    text = _text_of(idea, facts)
    ctype = _classify_type(text)
    return {
        "type": ctype,
        "type_label": CONTRIBUTION_TYPE_LABELS[ctype],
        "reason": "（确定性降级）{}".format(_TYPE_REASONS[ctype]),
        "matrix": _deterministic_matrix(text, facts, ctype),
        "attacks": _deterministic_attacks(text, facts, ctype),
        "degraded": True,
    }


# ---------------------------------------------------------------------------
# 冻结入口（纯函数，供 evaluate.py 在 EVALUATE 内部先于 novelty 评分调用）
# ---------------------------------------------------------------------------

def classify_contribution(idea: Dict[str, Any], facts: Dict[str, Any],
                          literature: List[dict],
                          llm: Optional[LLMProvider]) -> Dict[str, Any]:
    """对单个 idea 做创新贡献分析，返回 ``contribution`` 子对象。

    - ``type`` ∈ {A,B,C,D,E}，``type_label`` / ``reason``；
    - ``matrix``：6 个贡献维度的 ``{strength, label, reason}``；
    - ``attacks``：``{ablation, concatenation, reviewer}`` 各 ``{attack, answer}``；
    - ``degraded``：True 表示走了确定性兜底（无 LLM / LLM 输出非法），低置信。

    本函数不抛异常、不改 Dossier，是 evaluate.py 在 EVALUATE 内部先于 novelty 评分调用的前置单元。
    """
    system, _version = _load_prompt()
    raw = _call_llm(llm, system, idea, facts, literature)
    return _finalize_contribution(raw, idea, facts, literature)


def classify_contribution_batch(ideas: List[dict], facts: Dict[str, Any],
                                literature: List[dict],
                                llm: Optional[LLMProvider]) -> Dict[str, Dict[str, Any]]:
    """M15 方向④：批量贡献分析——一次 LLM 调用分析多个 idea，返回 ``{idea_id: contribution}``。

    - 返回每个 idea 的最终 ``contribution`` 子对象；
    - 失败 / 空结果 / 某 idea 缺失 → 该 idea 不在返回 dict 中（由 evaluate.py 回退单条路径）；
    - 与 ``classify_contribution`` 语义一致（共用 ``_finalize_contribution``），绝不抛异常。
    """
    if llm is None or not ideas:
        return {}
    facts = facts or {}
    idea_by_id: Dict[str, dict] = {}
    for idea in ideas:
        if isinstance(idea, dict) and _clean(idea.get("idea_id")):
            idea_by_id[_clean(idea.get("idea_id"))] = idea

    system, _version = _load_prompt()
    batch = _call_llm_batch(llm, system, ideas, facts, literature)
    out: Dict[str, Dict[str, Any]] = {}
    for idea_id, raw in (batch or {}).items():
        idea = idea_by_id.get(idea_id)
        if idea is None:
            continue
        out[idea_id] = _finalize_contribution(raw, idea, facts, literature)
    return out


# ---------------------------------------------------------------------------
# 供 evaluate.py 的 verdict 差异化复用
# ---------------------------------------------------------------------------

def matrix_viable(matrix: Any) -> bool:
    """贡献矩阵是否有任一维度 ≥ 中（medium）。

    - 无矩阵 / 空矩阵 → True（保守，避免误 reject）；
    - 用于 evaluate._decide_verdict：可行贡献（≥ 中）时 novelty 分数不再作为直接 reject 依据。
    """
    if not isinstance(matrix, dict):
        return True
    strengths: List[int] = []
    for dim in MATRIX_DIMENSIONS:
        item = matrix.get(dim)
        if isinstance(item, dict):
            strengths.append(STRENGTH_ORDER.get(item.get("strength"), 0))
    if not strengths:
        return True
    return any(s >= STRENGTH_ORDER["medium"] for s in strengths)


# ---------------------------------------------------------------------------
# 报告渲染（供 orchestrator._render_report_md 复用，先于 novelty 评分展示）
# ---------------------------------------------------------------------------

def render_contribution_lines(ev: Dict[str, Any]) -> List[str]:
    """把一条 evaluation 的 ``contribution`` 渲染成「类型 + 贡献矩阵 + 攻击测试」Markdown 行。

    - 供 orchestrator 在「可行性评估」段里**先于 novelty 评分**渲染（M21 验收）；
    - 无 ``contribution``（旧格式评估）返回空列表，由 orchestrator 走旧渲染。
    """
    c = ev.get("contribution") if isinstance(ev, dict) else None
    if not isinstance(c, dict) or c.get("type") not in CONTRIBUTION_TYPES:
        return []
    lines: List[str] = []
    lines.append("- **{}**".format(ev.get("idea_ref")))
    lines.append("  - 创新类型：{}（{}）".format(c.get("type"), c.get("type_label") or ""))
    reason = _clean(c.get("reason"))
    if reason:
        lines.append("    - 分类理由：{}".format(reason))
    matrix = c.get("matrix")
    if isinstance(matrix, dict) and matrix:
        lines.append("    - 贡献矩阵：")
        lines.append("      | 贡献类型 | 强度 | 原因 |")
        lines.append("      |---|---|---|")
        for dim in MATRIX_DIMENSIONS:
            item = matrix.get(dim)
            if not isinstance(item, dict):
                continue
            lines.append("      | {} | {} | {} |".format(
                MATRIX_LABELS.get(dim, dim),
                item.get("label") or item.get("strength") or "—",
                _clean(item.get("reason")) or "—",
            ))
    attacks = c.get("attacks")
    if isinstance(attacks, dict) and attacks:
        lines.append("    - 攻击测试：")
        for key in ATTACK_KEYS:
            item = attacks.get(key)
            if not isinstance(item, dict):
                continue
            lines.append("      - {}：{}".format(
                ATTACK_LABELS.get(key, key), _clean(item.get("attack")) or "—"))
            answer = _clean(item.get("answer"))
            if answer:
                lines.append("        - 回答：{}".format(answer))
    if c.get("degraded"):
        lines.append("    - （创新贡献分析为确定性降级产物，低置信，需人工复核）")
    return lines
