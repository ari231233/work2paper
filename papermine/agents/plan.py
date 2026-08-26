"""⑥ 论文路线规划 Agent：evaluations -> roadmap（M22 升级版）。

对应 docs/build-plan.md §3.3 / §4 M6、§4 M22 与 docs/architecture.md §5 ⑥：

M22 论文路线图重构：把路线图从「泛泛的时间线 + 缺口清单」升级为「可执行、可裁剪、有出口、
有成功标准、有风险预案」的学生友好路线图——学生读完能直接开始写代码，且知道「哪些不做也能发」。

新结构 7 部分（替代旧 timeline / missing_items 的主内容地位）：

1. 论文主线（Core Story）：现状 / 问题 / 方法 / 贡献 四段；
2. Research Questions：2~4 个，每个 RQ 对应后续实验（RQ1→主实验、RQ2→消融…）；
3. Experiment Matrix（实验表）：实验 / 目的 / 自变量 / 对比模型 / 指标 / 对应 RQ；
4. Minimum Viable Paper：必须完成 vs 可选扩展（哪些不做也能发）；
5. Success Criteria：成功/失败标准 + 未达标转向方案；
6. Risk Branches：具体风险 → 具体转向（而非泛泛「局限性」）；
7. 阶段出口时间线：阶段 + 任务 + 交付物（而非纯日期）。

与 M21 的关系：Core Story 的「贡献」段、Risk Branches 优先复用 ``evaluation.contribution``
（类型 + 贡献矩阵 + 攻击测试）——攻击测试即 reviewer 风险，提前回答即转向预案。

降级路径：无 key / LLMError / SchemaError 时，7 部分**逐段**降级为确定性规则生成，仍输出
完整 7 部分结构（低置信，报告标注 degraded）。

``missing_items`` 保留为**派生的确定性缺口清单**（数据/指标缺失 + 评估回炉），仅供编排器
「数据缺口回填」信号（architecture §6 ⑥→①）与 M14 测试复用；不再是路线图的主内容。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..dossier import Dossier
from ..llm import LLMError, LLMProvider, SchemaError
from .contribution import (
    ATTACK_KEYS,
    MATRIX_DIMENSIONS,
    MATRIX_LABELS,
    STRENGTH_ORDER,
)

__all__ = [
    "run",
    "PLAN_SCHEMA",
    "render_roadmap_lines",
    "_select_idea",
    "_deterministic_paper_type",
    "_deterministic_outline",
    "_deterministic_core_story",
    "_deterministic_research_questions",
    "_deterministic_experiment_matrix",
    "_deterministic_minimum_viable_paper",
    "_deterministic_success_criteria",
    "_deterministic_risk_branches",
    "_deterministic_stage_exits",
    "_deterministic_missing_items",
]

_PROMPT_VERSION = "v2"
_PROMPT_FILENAME = "plan.md"
_PROMPT_VERSION_RE = re.compile(r"<!--\s*version:\s*(\d+)\s*-->")

# 论文主线四段（字段键 -> 中文标签，报告展示顺序）
_CORE_STORY_FIELDS: Tuple[Tuple[str, str], ...] = (
    ("status_quo", "现状"),
    ("problem", "问题"),
    ("method", "方法"),
    ("contribution", "贡献"),
)

# 成功/失败标准三段（字段键 -> 中文标签）
_SUCCESS_FIELDS: Tuple[Tuple[str, str], ...] = (
    ("success", "成功（idea 成立）"),
    ("failure", "失败条件"),
    ("pivot", "转向方案"),
)

# 本 Agent 的 LLM 输出契约（schema 校验走 papermine/llm.py 的极简子集）。
# 注意：校验子集不支持 minItems/maxItems，RQ 数量 2~4 由 _normalize_research_questions 兜底。
PLAN_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "paper_type", "outline",
        "core_story", "research_questions", "experiment_matrix",
        "minimum_viable_paper", "success_criteria", "risk_branches", "stage_exits",
    ],
    "properties": {
        "paper_type": {"type": "string"},
        "outline": {"type": "array", "items": {"type": "string"}},
        "core_story": {
            "type": "object",
            "additionalProperties": False,
            "required": ["status_quo", "problem", "method", "contribution"],
            "properties": {
                "status_quo": {"type": "string"},
                "problem": {"type": "string"},
                "method": {"type": "string"},
                "contribution": {"type": "string"},
            },
        },
        "research_questions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "question", "target_experiments"],
                "properties": {
                    "id": {"type": "string"},
                    "question": {"type": "string"},
                    "target_experiments": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
        "experiment_matrix": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["experiment", "purpose", "independent_variable",
                             "baselines", "metrics", "rq"],
                "properties": {
                    "experiment": {"type": "string"},
                    "purpose": {"type": "string"},
                    "independent_variable": {"type": "string"},
                    "baselines": {"type": "array", "items": {"type": "string"}},
                    "metrics": {"type": "array", "items": {"type": "string"}},
                    "rq": {"type": "string"},
                },
            },
        },
        "minimum_viable_paper": {
            "type": "object",
            "additionalProperties": False,
            "required": ["must_have", "optional"],
            "properties": {
                "must_have": {"type": "array", "items": {"type": "string"}},
                "optional": {"type": "array", "items": {"type": "string"}},
            },
        },
        "success_criteria": {
            "type": "object",
            "additionalProperties": False,
            "required": ["success", "failure", "pivot"],
            "properties": {
                "success": {"type": "array", "items": {"type": "string"}},
                "failure": {"type": "array", "items": {"type": "string"}},
                "pivot": {"type": "string"},
            },
        },
        "risk_branches": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["risk", "branch"],
                "properties": {
                    "risk": {"type": "string"},
                    "branch": {"type": "string"},
                },
            },
        },
        "stage_exits": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["stage", "tasks", "exit_criteria"],
                "properties": {
                    "stage": {"type": "string"},
                    "tasks": {"type": "array", "items": {"type": "string"}},
                    "exit_criteria": {"type": "string"},
                },
            },
        },
    },
}

_EMPTY_ROADMAP: Dict[str, Any] = {
    "selected_idea": None,
    "paper_type": "",
    "outline": [],
    "core_story": {"status_quo": "", "problem": "", "method": "", "contribution": ""},
    "research_questions": [],
    "experiment_matrix": [],
    "minimum_viable_paper": {"must_have": [], "optional": []},
    "success_criteria": {"success": [], "failure": [], "pivot": ""},
    "risk_branches": [],
    "stage_exits": [],
    "missing_items": [],
}

_SYSTEM_PROMPT_FALLBACK = (
    "你是 papermine 的「论文路线规划 Agent」。为一个已通过可行性评估的候选创新点制定可执行的"
    "学生友好论文路线图，输出 7 部分：\n"
    "1. core_story（论文主线：现状/问题/方法/贡献四段）；\n"
    "2. research_questions（2~4 个 RQ，各含 id/question/target_experiments）；\n"
    "3. experiment_matrix（实验表：实验/目的/自变量/对比模型/指标/对应 RQ）；\n"
    "4. minimum_viable_paper（必须完成 vs 可选扩展）；\n"
    "5. success_criteria（成功/失败标准 + 转向方案）；\n"
    "6. risk_branches（具体风险 → 具体转向）；\n"
    "7. stage_exits（阶段 + 任务 + 交付物）。\n"
    "外加 paper_type 与 outline。不代写正文、不虚构引用。只输出符合 schema 的 JSON 对象。"
)

# 建模方法 / 建模任务集合（与 v0.1 mining.py 对齐，用于推断 paper_type）
_MODELING_METHODS = {"深度学习", "孤立森林", "随机森林", "SVM", "XGBoost",
                     "时间序列模型", "集成学习"}
_MODELING_TASKS = {"分类", "回归预测", "时序预测", "异常检测", "剩余寿命预测",
                   "聚类", "推荐", "目标检测"}


def _prompt_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "prompts"


def _load_prompt() -> tuple:
    """读取 prompts/plan.md，返回 (system_prompt_text, version)。文件缺失时用内联兜底。"""
    path = _prompt_dir() / _PROMPT_FILENAME
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return _SYSTEM_PROMPT_FALLBACK, _PROMPT_VERSION
    m = _PROMPT_VERSION_RE.search(text)
    version = "v{}".format(m.group(1)) if m else _PROMPT_VERSION
    return text, version


# ---------------------------------------------------------------------------
# idea 选择
# ---------------------------------------------------------------------------

def _select_idea(ideas: List[dict], evaluations: List[dict]) -> Tuple[Optional[dict], Optional[dict]]:
    """选择最优先的 idea：proceed > rework > drop，同级按 novelty 降序、workload 升序。

    返回 (idea, evaluation)；无 idea 或全无评估时返回 (None, None)。
    """
    if not ideas:
        return None, None
    ev_map: Dict[str, dict] = {}
    for ev in evaluations or []:
        if isinstance(ev, dict) and ev.get("idea_ref"):
            ev_map[ev["idea_ref"]] = ev

    pairs: List[Tuple[dict, Optional[dict]]] = []
    for idea in ideas:
        if isinstance(idea, dict) and idea.get("idea_id"):
            pairs.append((idea, ev_map.get(idea.get("idea_id"))))
    if not pairs:
        return None, None

    def _sort_key(pair: Tuple[dict, Optional[dict]]) -> tuple:
        _idea, ev = pair
        if not ev:
            return (1, 0.0, 0.0)
        verdict = ev.get("verdict")
        prio = 0 if verdict == "proceed" else (1 if verdict == "rework" else 2)
        novelty = ev.get("novelty_score")
        if not isinstance(novelty, (int, float)) or isinstance(novelty, bool):
            novelty = 0.0
        workload = ev.get("workload_hours")
        if not isinstance(workload, (int, float)) or isinstance(workload, bool):
            workload = 0.0
        return (prio, -float(novelty), float(workload))

    pairs.sort(key=_sort_key)
    return pairs[0]


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def _clean(s: Any) -> str:
    """把任意值折叠成单行文本（去首尾 / 合并空白）。"""
    return " ".join(str(s or "").split())


def _clean_strings(vals: Any) -> List[str]:
    """把值规范化为去重、非空的字符串列表。"""
    out: List[str] = []
    seen: set = set()
    if not isinstance(vals, list):
        return out
    for v in vals:
        if isinstance(v, str) and v.strip() and v.strip() not in seen:
            seen.add(v.strip())
            out.append(v.strip())
    return out


def _join(vals: Any, limit: int = 3) -> str:
    """把列表拼成「、」分隔的短串（空 → ""）。"""
    items = [str(v).strip() for v in (vals or []) if str(v).strip()]
    if not items:
        return ""
    return "、".join(items[:limit])


def _merge_missing(base: List[str], extra: List[str]) -> List[str]:
    out = list(base)
    for item in extra or []:
        if item and item not in out:
            out.append(item)
    return out


# ---------------------------------------------------------------------------
# M21 复用：从 evaluation.contribution 提炼「贡献」与「风险分支」
# ---------------------------------------------------------------------------

def _contribution_summary(ev: Optional[dict]) -> str:
    """从 M21 贡献分析提炼「贡献」一句话：类型 + 达到「中」及以上的贡献维度。"""
    if not isinstance(ev, dict):
        return ""
    c = ev.get("contribution")
    if not isinstance(c, dict):
        return ""
    parts: List[str] = []
    type_label = _clean(c.get("type_label"))
    if type_label:
        parts.append(type_label)
    matrix = c.get("matrix") if isinstance(c.get("matrix"), dict) else {}
    strong: List[str] = []
    for dim in MATRIX_DIMENSIONS:
        item = matrix.get(dim)
        if isinstance(item, dict) and STRENGTH_ORDER.get(item.get("strength"), 0) >= STRENGTH_ORDER["medium"]:
            strong.append(MATRIX_LABELS.get(dim, dim))
    if strong:
        parts.append("贡献集中在：" + "、".join(strong))
    return "；".join(parts)


def _attack_risk_branches(ev: Optional[dict]) -> List[Dict[str, str]]:
    """从 M21 攻击测试提取风险分支（attack 即风险，answer 即转向/预案）。"""
    if not isinstance(ev, dict):
        return []
    c = ev.get("contribution")
    attacks = c.get("attacks") if isinstance(c, dict) else None
    if not isinstance(attacks, dict):
        return []
    out: List[Dict[str, str]] = []
    for key in ATTACK_KEYS:
        item = attacks.get(key)
        if isinstance(item, dict) and _clean(item.get("attack")):
            out.append({
                "risk": _clean(item["attack"]),
                "branch": _clean(item.get("answer")) or "需在实验中验证并提前准备反驳",
            })
    return out


# ---------------------------------------------------------------------------
# 确定性兜底生成（7 部分）
# ---------------------------------------------------------------------------

def _is_tool_claim(claim: str) -> bool:
    """判定 idea 是否在主张「做工具/框架」，用强信号避免把方法标签「流水线/框架」误判为工具。"""
    if any(k in claim for k in ("工具", "平台", "通用框架", "可复用组件")):
        return True
    return ("组件" in claim) and ("抽象" in claim or "复用" in claim)


def _deterministic_paper_type(facts: Dict[str, Any], idea: dict) -> str:
    """推断 paper_type：工具主张 → 系统/工具；实证主张 → 实证/应用；建模任务×方法 → 方法。"""
    claim = " ".join(str(idea.get(k) or "") for k in ("claim", "novelty_hypothesis"))
    methods = facts.get("methods") or []
    tasks = facts.get("tasks") or []
    modules = facts.get("modules") or []

    if _is_tool_claim(claim):
        return "系统/工具论文"
    if "实证" in claim:
        return "实证/应用论文"
    if any(m in _MODELING_METHODS for m in methods) and any(t in _MODELING_TASKS for t in tasks):
        return "方法论文"
    if modules:
        return "系统/工具论文"
    return "实证/应用论文"


def _deterministic_outline(paper_type: str) -> List[str]:
    if paper_type == "方法论文":
        return [
            "1. 引言：横向场景痛点 + 研究动机",
            "2. 相关工作：任务现有方法 + 文献 gap",
            "3. 问题定义与符号",
            "4. 方法：核心思路 + 算法流程",
            "5. 实验设置：数据 / baseline / 评测指标",
            "6. 结果与分析：与 baseline 对比 + 消融",
            "7. 讨论与局限",
            "8. 结论",
        ]
    if paper_type == "系统/工具论文":
        return [
            "1. 引言：同类任务重复开发痛点",
            "2. 相关工作：同类工具 / 框架对比",
            "3. 系统设计：架构 + 可复用组件",
            "4. 关键实现与工程取舍",
            "5. 案例研究：在真实横向项目上的应用",
            "6. 讨论与局限",
            "7. 结论",
        ]
    return [
        "1. 引言：应用背景与问题",
        "2. 相关工作",
        "3. 数据集与实验设计",
        "4. 方法对比与结果",
        "5. 工程经验与教训（可迁移）",
        "6. 讨论与局限",
        "7. 结论",
    ]


def _deterministic_core_story(facts: Dict[str, Any], idea: dict,
                              ev: Optional[dict]) -> Dict[str, str]:
    """论文主线四段（确定性）：现状 = 场景×任务×已有方法；问题/方法 = claim/hypothesis；
    贡献 = M21 贡献分析（类型 + 最强维度），缺失时回退 claim。"""
    scenario = _join(facts.get("scenarios")) or "目标场景"
    tasks = _join(facts.get("tasks")) or "目标任务"
    methods = _join(facts.get("methods")) or "已有方法"
    claim = _clean(idea.get("claim")) or _clean(idea.get("novelty_hypothesis")) or "候选方案"
    hypothesis = _clean(idea.get("novelty_hypothesis")) or claim
    contribution = _contribution_summary(ev) or claim
    return {
        "status_quo": "{}场景下，{}任务目前主要依赖{}等手段，仍存在（文献 gap 指出的）改进空间".format(
            scenario, tasks, methods),
        "problem": claim,
        "method": hypothesis,
        "contribution": contribution,
    }


def _deterministic_research_questions(facts: Dict[str, Any], idea: dict,
                                      ev: Optional[dict]) -> List[Dict[str, Any]]:
    """2~4 个 RQ（确定性）：RQ1 主实验、RQ2 消融、RQ3 稳健性，映射到实验 E1/E2/E3。"""
    claim = _clean(idea.get("claim")) or "核心方案"
    brief = claim[:40]
    return [
        {
            "id": "RQ1",
            "question": "{}能否在目标场景上显著优于现有 baseline？".format(brief),
            "target_experiments": ["E1"],
        },
        {
            "id": "RQ2",
            "question": "核心机制/交互是否真正贡献了性能（消融去除后是否退化）？",
            "target_experiments": ["E2"],
        },
        {
            "id": "RQ3",
            "question": "方法在不同数据规模/条件下的稳健性如何？",
            "target_experiments": ["E3"],
        },
    ]


def _deterministic_experiment_matrix(facts: Dict[str, Any], idea: dict,
                                     ev: Optional[dict]) -> List[Dict[str, Any]]:
    """实验表（确定性）：E1 主实验 / E2 消融 / E3 稳健性。"""
    metric = _join(facts.get("metrics")) or "统一评测指标（如 F1/MSE/AUC）"
    return [
        {
            "experiment": "E1 主实验",
            "purpose": "验证核心方案端到端效果",
            "independent_variable": "是否启用核心方案",
            "baselines": ["2~3 个代表性 baseline（依据文献）"],
            "metrics": [metric],
            "rq": "RQ1",
        },
        {
            "experiment": "E2 消融实验",
            "purpose": "验证核心模块/交互的贡献",
            "independent_variable": "删除/替换核心模块",
            "baselines": ["完整方案 vs 去除核心模块（或 A+B concat）"],
            "metrics": [metric],
            "rq": "RQ2",
        },
        {
            "experiment": "E3 数据量/稳健性实验",
            "purpose": "验证方法在数据规模/条件变化下的稳健性",
            "independent_variable": "训练数据比例 / 超参数",
            "baselines": ["各 baseline 在相同子集上的表现"],
            "metrics": [metric],
            "rq": "RQ3",
        },
    ]


def _deterministic_minimum_viable_paper(facts: Dict[str, Any], idea: dict,
                                        ev: Optional[dict]) -> Dict[str, Any]:
    """MVP（确定性）：必须完成 = 数据 + baseline + 主实验 + 消融 + 正文；可选 = 可延后扩展。"""
    return {
        "must_have": [
            "评测数据准备：确定数据集与评测协议（train/val/test 划分 + 固定随机种子）",
            "复现 2~3 个代表性 baseline（指标对齐文献报告）",
            "主实验：核心方案 vs baseline 端到端对比（至少一张主结果表）",
            "消融实验：证明核心模块/交互的贡献（去除后是否退化）",
            "论文正文：引言 / 方法 / 实验 / 结论",
        ],
        "optional": [
            "额外数据集 / 更大规模的稳健性实验（可延后）",
            "理论分析（收敛性 / 复杂度）（可延后）",
            "系统演示 / 可复用组件开源（可延后）",
            "训练策略细粒度消融（如损失权重扫描）（可延后）",
        ],
    }


def _deterministic_success_criteria(facts: Dict[str, Any], idea: dict,
                                    ev: Optional[dict]) -> Dict[str, Any]:
    """成功/失败标准（确定性）：主实验显著 + 消融证明贡献；失败转向「失效条件分析」。"""
    metric = _join(facts.get("metrics")) or "统一评测指标（如 F1/MSE/AUC）"
    return {
        "success": [
            "主实验在{}上显著优于全部 baseline（或与 SOTA 持平但更简单/更省资源）".format(metric),
            "消融证明核心机制/交互不可或缺（去除后显著退化，而非与简单拼接等效）",
        ],
        "failure": [
            "主实验与 baseline 差异在噪声范围内（无显著提升）",
            "消融显示核心机制去除后性能无变化（核心贡献不成立）",
        ],
        "pivot": "若失败：转向「失效条件分析」——定位方法在哪些数据/条件下失效、为何失效，"
                 "把论文改写为针对该任务的经验/分析性研究（仍可发表）",
    }


def _deterministic_risk_branches(facts: Dict[str, Any], idea: dict,
                                 ev: Optional[dict]) -> List[Dict[str, str]]:
    """风险分支（确定性）：优先复用 M21 攻击测试（风险→预案），再补通用风险，去重。"""
    branches = _attack_risk_branches(ev)
    seen = {b["risk"] for b in branches}
    generic = (
        ("强 baseline（如 XGBoost）始终占优",
         "转「方法失效条件分析」：定位哪些条件下深度方法失效、为何失效，改写为经验/分析性论文"),
        ("消融显示核心机制与简单拼接（A+B concat）等效",
         "转「交互机制生效的必要条件」：分析何种条件下交互才带来增益，把贡献重新定位为条件性发现"),
        ("评测数据不足或缺失",
         "转小样本/迁移学习方向，或明确把结论限定在现有数据规模内"),
    )
    for risk, branch in generic:
        if risk not in seen:
            branches.append({"risk": risk, "branch": branch})
            seen.add(risk)
    return branches


def _deterministic_stage_exits(facts: Dict[str, Any], idea: dict,
                               ev: Optional[dict]) -> List[Dict[str, Any]]:
    """阶段出口时间线（确定性）：阶段 + 任务 + 交付物（出口）。"""
    return [
        {"stage": "Week 1", "tasks": ["数据集跑通：加载/清洗/划分 train/val/test"],
         "exit_criteria": "baseline 可复现"},
        {"stage": "Week 2-3", "tasks": ["复现 2~3 个代表性 baseline"],
         "exit_criteria": "baseline 指标与文献对齐"},
        {"stage": "Week 4-6", "tasks": ["实现核心方法", "跑通主实验"],
         "exit_criteria": "主实验结果表可生成"},
        {"stage": "Week 7-8", "tasks": ["消融实验", "稳健性/数据量实验"],
         "exit_criteria": "消融证明核心贡献、明确失效条件"},
        {"stage": "Week 9-10", "tasks": ["论文写作（引言/方法/实验/结论）"],
         "exit_criteria": "完整初稿"},
        {"stage": "Week 11+", "tasks": ["投稿前自查与打磨"],
         "exit_criteria": "可投稿"},
    ]


def _deterministic_missing_items(facts: Dict[str, Any], idea: dict,
                                 ev: Optional[dict]) -> List[str]:
    """缺口清单（确定性，派生字段）：数据 / 指标缺失 + 评估未通过时的回炉提示。

    不再是路线图主内容，仅供编排器「数据缺口回填」信号（architecture §6 ⑥→①）。
    """
    missing: List[str] = []
    if not (facts.get("data") or []):
        missing.append("评测数据集缺失：需采集/标注数据，或明确数据来源与规模")
    if not (facts.get("metrics") or []):
        missing.append("统一评测指标缺失：需确定主指标与 baseline 对比协议")
    if ev:
        if ev.get("data_feasibility") == "low":
            missing.append("数据可得性低：需回填项目事实（回退①项目理解补采集）")
        verdict = ev.get("verdict")
        if verdict and verdict != "proceed":
            missing.append("评估结论为「{}」：需先完成可行性回炉再进入写作".format(verdict))
    return missing


# ---------------------------------------------------------------------------
# LLM 输出规范化：逐段校验，非法 → 确定性兜底（绝不部分信任单个字段，但逐段可独立回退）
# ---------------------------------------------------------------------------

def _normalize_core_story(raw: Any, idea: dict, ev: Optional[dict],
                          facts: Dict[str, Any]) -> Dict[str, str]:
    if isinstance(raw, dict) and all(_clean(raw.get(k)) for k, _ in _CORE_STORY_FIELDS):
        return {k: _clean(raw.get(k)) for k, _ in _CORE_STORY_FIELDS}
    return _deterministic_core_story(facts, idea, ev)


def _normalize_research_questions(raw: Any, idea: dict, ev: Optional[dict],
                                  facts: Dict[str, Any]) -> List[Dict[str, Any]]:
    """RQ：需 2~4 个、每个含非空 id/question；否则整体确定性兜底。"""
    if isinstance(raw, list):
        out: List[Dict[str, Any]] = []
        for q in raw:
            if isinstance(q, dict) and _clean(q.get("id")) and _clean(q.get("question")):
                out.append({
                    "id": _clean(q["id"]),
                    "question": _clean(q["question"]),
                    "target_experiments": _clean_strings(q.get("target_experiments")),
                })
        if 2 <= len(out) <= 4:
            return out
    return _deterministic_research_questions(facts, idea, ev)


def _normalize_experiment_matrix(raw: Any, idea: dict, ev: Optional[dict],
                                 facts: Dict[str, Any]) -> List[Dict[str, Any]]:
    if isinstance(raw, list):
        out: List[Dict[str, Any]] = []
        for e in raw:
            if not isinstance(e, dict) or not _clean(e.get("experiment")):
                continue
            out.append({
                "experiment": _clean(e.get("experiment")),
                "purpose": _clean(e.get("purpose")),
                "independent_variable": _clean(e.get("independent_variable")),
                "baselines": _clean_strings(e.get("baselines")),
                "metrics": _clean_strings(e.get("metrics")),
                "rq": _clean(e.get("rq")),
            })
        if out:
            return out
    return _deterministic_experiment_matrix(facts, idea, ev)


def _normalize_minimum_viable_paper(raw: Any, idea: dict, ev: Optional[dict],
                                    facts: Dict[str, Any]) -> Dict[str, Any]:
    if isinstance(raw, dict):
        must = _clean_strings(raw.get("must_have"))
        if must:
            return {"must_have": must, "optional": _clean_strings(raw.get("optional"))}
    return _deterministic_minimum_viable_paper(facts, idea, ev)


def _normalize_success_criteria(raw: Any, idea: dict, ev: Optional[dict],
                                facts: Dict[str, Any]) -> Dict[str, Any]:
    if isinstance(raw, dict):
        success = _clean_strings(raw.get("success"))
        if success and _clean(raw.get("pivot")):
            return {
                "success": success,
                "failure": _clean_strings(raw.get("failure")),
                "pivot": _clean(raw.get("pivot")),
            }
    return _deterministic_success_criteria(facts, idea, ev)


def _normalize_risk_branches(raw: Any, idea: dict, ev: Optional[dict],
                             facts: Dict[str, Any]) -> List[Dict[str, str]]:
    if isinstance(raw, list):
        out: List[Dict[str, str]] = []
        for rb in raw:
            if isinstance(rb, dict) and _clean(rb.get("risk")) and _clean(rb.get("branch")):
                out.append({"risk": _clean(rb["risk"]), "branch": _clean(rb["branch"])})
        if out:
            return out
    return _deterministic_risk_branches(facts, idea, ev)


def _normalize_stage_exits(raw: Any, idea: dict, ev: Optional[dict],
                           facts: Dict[str, Any]) -> List[Dict[str, Any]]:
    if isinstance(raw, list):
        out: List[Dict[str, Any]] = []
        for s in raw:
            if isinstance(s, dict) and _clean(s.get("stage")) and _clean(s.get("exit_criteria")):
                out.append({
                    "stage": _clean(s.get("stage")),
                    "tasks": _clean_strings(s.get("tasks")),
                    "exit_criteria": _clean(s.get("exit_criteria")),
                })
        if out:
            return out
    return _deterministic_stage_exits(facts, idea, ev)


# ---------------------------------------------------------------------------
# LLM 调用
# ---------------------------------------------------------------------------

def _eval_summary(ev: Optional[dict]) -> Optional[dict]:
    """裁剪评估为路线规划所需字段（含 M21 contribution / M12 evidence，剪掉 calibration 等长尾）。"""
    if not isinstance(ev, dict):
        return None
    return {
        "idea_ref": ev.get("idea_ref"),
        "verdict": ev.get("verdict"),
        "novelty_score": ev.get("novelty_score"),
        "data_feasibility": ev.get("data_feasibility"),
        "workload_hours": ev.get("workload_hours"),
        "venue_guess": ev.get("venue_guess"),
        "contribution": ev.get("contribution"),
        "evidence_validation": ev.get("evidence_validation"),
    }


def _build_user_prompt(idea: dict, ev: Optional[dict], facts: Dict[str, Any]) -> str:
    payload = {
        "idea": {
            "idea_id": idea.get("idea_id"),
            "claim": idea.get("claim"),
            "novelty_hypothesis": idea.get("novelty_hypothesis"),
            "problem_ref": idea.get("problem_ref"),
            "literature_refs": idea.get("literature_refs"),
        },
        "evaluation": _eval_summary(ev),
        "facts": facts,
    }
    return (
        "以下是一个候选创新点及其可行性评估，请制定学生友好论文路线图（7 部分结构）：\n"
        + json.dumps(payload, ensure_ascii=False)
    )


def _call_llm(llm: LLMProvider, system_prompt: str, idea: dict,
              ev: Optional[dict], facts: Dict[str, Any]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    try:
        result = llm.complete(
            system_prompt,
            _build_user_prompt(idea, ev, facts),
            PLAN_SCHEMA, temperature=0.3,
        )
    except (LLMError, SchemaError):
        result = {}
    if not isinstance(result, dict):
        result = {}
    return result


# ---------------------------------------------------------------------------
# 报告渲染（供 orchestrator._render_report_md 复用）
# ---------------------------------------------------------------------------

def render_roadmap_lines(roadmap: Dict[str, Any]) -> List[str]:
    """把 roadmap 的 7 部分渲染成 Markdown 行。

    - 供 orchestrator 在「论文路线图」段复用（M22 验收：学生读完能直接开写、知道哪些可不做）；
    - 旧格式 roadmap（无 ``core_story``）返回空列表，由 orchestrator 走旧渲染兜底。
    """
    r = roadmap or {}
    if not isinstance(r.get("core_story"), dict):
        return []
    lines: List[str] = []

    cs = r["core_story"]
    lines.append("- 论文主线（Core Story）：")
    for key, label in _CORE_STORY_FIELDS:
        text = _clean(cs.get(key))
        if text:
            lines.append("  - {}：{}".format(label, text))

    rqs = r.get("research_questions") or []
    if rqs:
        lines.append("- Research Questions：")
        for q in rqs:
            if not isinstance(q, dict):
                continue
            qid = _clean(q.get("id"))
            if not qid:
                continue
            seg = "  - {}：{}".format(qid, _clean(q.get("question")) or "（无）")
            targets = _clean_strings(q.get("target_experiments"))
            if targets:
                seg += "（→ {}）".format("、".join(targets))
            lines.append(seg)

    matrix = r.get("experiment_matrix") or []
    if matrix:
        lines.append("- 实验矩阵（Experiment Matrix）：")
        lines.append("  | 实验 | 目的 | 自变量 | 对比模型 | 指标 | 对应 RQ |")
        lines.append("  |---|---|---|---|---|---|")
        for e in matrix:
            if not isinstance(e, dict):
                continue
            lines.append("  | {} | {} | {} | {} | {} | {} |".format(
                _clean(e.get("experiment")) or "—",
                _clean(e.get("purpose")) or "—",
                _clean(e.get("independent_variable")) or "—",
                "、".join(_clean_strings(e.get("baselines"))) or "—",
                "、".join(_clean_strings(e.get("metrics"))) or "—",
                _clean(e.get("rq")) or "—",
            ))

    mvp = r.get("minimum_viable_paper")
    if isinstance(mvp, dict):
        lines.append("- 最小可发表版本（MVP）：")
        must = _clean_strings(mvp.get("must_have"))
        if must:
            lines.append("  - 必须完成：")
            for m in must:
                lines.append("    - {}".format(m))
        optional = _clean_strings(mvp.get("optional"))
        if optional:
            lines.append("  - 可选扩展（不做也能发）：")
            for m in optional:
                lines.append("    - {}".format(m))

    sc = r.get("success_criteria")
    if isinstance(sc, dict):
        lines.append("- 成功/失败标准：")
        for key, label in _SUCCESS_FIELDS:
            val = sc.get(key)
            if isinstance(val, list):
                val = _clean_strings(val)
                if not val:
                    continue
                lines.append("  - {}：".format(label))
                for v in val:
                    lines.append("    - {}".format(v))
            else:
                text = _clean(val)
                if text:
                    lines.append("  - {}：{}".format(label, text))

    risks = r.get("risk_branches") or []
    if risks:
        lines.append("- 风险分支（Risk Branches）：")
        for rb in risks:
            if isinstance(rb, dict) and _clean(rb.get("risk")):
                lines.append("  - {} → {}".format(_clean(rb["risk"]), _clean(rb.get("branch")) or "（待定）"))

    stages = r.get("stage_exits") or []
    if stages:
        lines.append("- 阶段出口时间线：")
        for s in stages:
            if not isinstance(s, dict):
                continue
            stage = _clean(s.get("stage"))
            if not stage:
                continue
            tasks = "；".join(_clean_strings(s.get("tasks")))
            seg = "  - {}：{}".format(stage, tasks or "（任务待定）")
            exit_c = _clean(s.get("exit_criteria"))
            if exit_c:
                seg += "；出口：{}".format(exit_c)
            lines.append(seg)
    return lines


# ---------------------------------------------------------------------------
# 入口（冻结契约）
# ---------------------------------------------------------------------------

def run(dossier: Dossier, llm: LLMProvider) -> None:
    """evaluations -> roadmap（M22 七部分结构），原地写 dossier.roadmap。

    冻结契约（docs/build-plan.md §3.3）：
        def run(dossier: Dossier, llm: LLMProvider) -> None
    """
    assets = dossier.assets if isinstance(dossier.assets, dict) else {}
    facts = assets.get("facts") if isinstance(assets.get("facts"), dict) else {}
    ideas = list(dossier.ideas or [])
    evaluations = list(dossier.evaluations or [])

    system_prompt, version = _load_prompt()
    selected, ev = _select_idea(ideas, evaluations)

    if selected is None:
        dossier.roadmap = dict(_EMPTY_ROADMAP)
        dossier.meta.setdefault("prompt_versions", {})["plan"] = version
        return

    llm_out = _call_llm(llm, system_prompt, selected, ev, facts)

    paper_type = _clean(llm_out.get("paper_type")) or _deterministic_paper_type(facts, selected)
    outline = _clean_strings(llm_out.get("outline")) or _deterministic_outline(paper_type)

    core_story = _normalize_core_story(llm_out.get("core_story"), selected, ev, facts)
    research_questions = _normalize_research_questions(
        llm_out.get("research_questions"), selected, ev, facts)
    experiment_matrix = _normalize_experiment_matrix(
        llm_out.get("experiment_matrix"), selected, ev, facts)
    mvp = _normalize_minimum_viable_paper(
        llm_out.get("minimum_viable_paper"), selected, ev, facts)
    success_criteria = _normalize_success_criteria(
        llm_out.get("success_criteria"), selected, ev, facts)
    risk_branches = _normalize_risk_branches(
        llm_out.get("risk_branches"), selected, ev, facts)
    stage_exits = _normalize_stage_exits(
        llm_out.get("stage_exits"), selected, ev, facts)

    missing = _deterministic_missing_items(facts, selected, ev)

    dossier.roadmap = {
        "selected_idea": selected.get("idea_id"),
        "paper_type": paper_type,
        "outline": outline,
        "core_story": core_story,
        "research_questions": research_questions,
        "experiment_matrix": experiment_matrix,
        "minimum_viable_paper": mvp,
        "success_criteria": success_criteria,
        "risk_branches": risk_branches,
        "stage_exits": stage_exits,
        "missing_items": missing,
    }
    dossier.meta.setdefault("prompt_versions", {})["plan"] = version
