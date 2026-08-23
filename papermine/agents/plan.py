"""⑥ 论文路线规划 Agent：evaluations -> roadmap。

对应 docs/build-plan.md §3.3 / §4 M6 与 docs/architecture.md §5 ⑥：

- 从评估通过的 idea 中选出最优先者（proceed 优先，其次 novelty 高 / 工作量低），
  产出一份可执行论文路线图（selected_idea / paper_type / outline / experiment_plan /
  timeline / missing_items）。
- `missing_items` 非空即「缺口回填」信号：编排器据此回退到①（architecture §6 ⑥→①）。
- 学术诚信边界（architecture §10）：只产框架与计划，不代写正文、不生成虚构引用。

降级路径：无 key / LLMError / SchemaError 时，paper_type / outline / experiment_plan /
timeline / missing_items 全部降级为确定性规则生成。
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
    "PLAN_SCHEMA",
    "_select_idea",
    "_deterministic_paper_type",
    "_deterministic_outline",
    "_deterministic_experiment_plan",
    "_deterministic_timeline",
    "_deterministic_missing_items",
]

_PROMPT_VERSION = "v1"
_PROMPT_FILENAME = "plan.md"
_PROMPT_VERSION_RE = re.compile(r"<!--\s*version:\s*(\d+)\s*-->")

# 本 Agent 的 LLM 输出契约（schema 校验走 papermine/llm.py 的极简子集）
PLAN_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["paper_type", "outline", "experiment_plan", "timeline", "missing_items"],
    "properties": {
        "paper_type": {"type": "string"},
        "outline": {"type": "array", "items": {"type": "string"}},
        "experiment_plan": {"type": "array", "items": {"type": "string"}},
        "timeline": {"type": "object"},
        "missing_items": {"type": "array", "items": {"type": "string"}},
    },
}

_EMPTY_ROADMAP: Dict[str, Any] = {
    "selected_idea": None,
    "paper_type": "",
    "outline": [],
    "experiment_plan": [],
    "timeline": {},
    "missing_items": [],
}

_SYSTEM_PROMPT_FALLBACK = (
    "你是 papermine 的「论文路线规划 Agent」。为一个已通过评估的候选创新点制定可执行论文路线图："
    "输出 paper_type（方法论文/系统工具论文/实证应用论文）、outline、experiment_plan、"
    "timeline、missing_items。不代写正文、不虚构引用。只输出符合 schema 的 JSON 对象。"
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
# 确定性兜底生成
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


def _deterministic_experiment_plan(facts: Dict[str, Any], idea: dict) -> List[str]:
    metrics = facts.get("metrics") or []
    claim = str(idea.get("claim") or "").strip()[:40]
    return [
        "1. 明确评测指标：{}".format("、".join(metrics) if metrics else "补全统一评测指标（如 F1/MSE/AUC）"),
        "2. 数据切分与预处理：划分训练/验证/测试，固定随机种子保证可复现",
        "3. baseline 对比：复现 2~3 个代表性 baseline（依据文献）",
        "4. 主实验：{} 的端到端效果对比".format(claim or "核心方案"),
        "5. 消融实验：验证关键设计（新模块/新损失）的贡献",
        "6. 稳健性/敏感性分析：不同数据规模与超参数下的表现",
    ]


def _deterministic_timeline(facts: Dict[str, Any], idea: dict) -> Dict[str, str]:
    return {
        "第1-2周": "文献精读 + 确认 gap + 定稿选题",
        "第3-4周": "数据准备 + baseline 复现",
        "第5-8周": "核心方法实现 + 主实验",
        "第9-10周": "消融与稳健性实验 + 补缺数据",
        "第11-12周": "论文写作（含 related work 骨架）",
        "第13-14周": "投稿前自查 + 打磨",
    }


def _deterministic_missing_items(facts: Dict[str, Any], idea: dict,
                                 ev: Optional[dict]) -> List[str]:
    """缺口清单（确定性）：数据 / 指标缺失 + 评估未通过时的回炉提示。"""
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


def _clean_str(value: Any) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return ""


def _clean_strings(vals: Any) -> List[str]:
    out: List[str] = []
    seen: set = set()
    if not isinstance(vals, list):
        return out
    for v in vals:
        if isinstance(v, str) and v.strip() and v.strip() not in seen:
            seen.add(v.strip())
            out.append(v.strip())
    return out


def _merge_missing(base: List[str], extra: List[str]) -> List[str]:
    out = list(base)
    for item in extra or []:
        if item and item not in out:
            out.append(item)
    return out


# ---------------------------------------------------------------------------
# LLM 调用
# ---------------------------------------------------------------------------

def _build_user_prompt(idea: dict, ev: Optional[dict], facts: Dict[str, Any]) -> str:
    payload = {
        "idea": {
            "idea_id": idea.get("idea_id"),
            "claim": idea.get("claim"),
            "novelty_hypothesis": idea.get("novelty_hypothesis"),
            "problem_ref": idea.get("problem_ref"),
            "literature_refs": idea.get("literature_refs"),
        },
        "evaluation": ev,
        "facts": facts,
    }
    return "以下是一个候选创新点及其评估，请制定论文路线图：\n" + json.dumps(
        payload, ensure_ascii=False)


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
# 入口（冻结契约）
# ---------------------------------------------------------------------------

def run(dossier: Dossier, llm: LLMProvider) -> None:
    """evaluations -> roadmap，原地写 dossier.roadmap。

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

    paper_type = _clean_str(llm_out.get("paper_type")) or _deterministic_paper_type(facts, selected)
    outline = _clean_strings(llm_out.get("outline")) or _deterministic_outline(paper_type)
    experiment_plan = _clean_strings(llm_out.get("experiment_plan")) \
        or _deterministic_experiment_plan(facts, selected)

    timeline = llm_out.get("timeline")
    if not isinstance(timeline, dict) or not timeline:
        timeline = _deterministic_timeline(facts, selected)

    missing = _deterministic_missing_items(facts, selected, ev)
    missing = _merge_missing(missing, _clean_strings(llm_out.get("missing_items")))

    dossier.roadmap = {
        "selected_idea": selected.get("idea_id"),
        "paper_type": paper_type,
        "outline": outline,
        "experiment_plan": experiment_plan,
        "timeline": timeline,
        "missing_items": missing,
    }
    dossier.meta.setdefault("prompt_versions", {})["plan"] = version
