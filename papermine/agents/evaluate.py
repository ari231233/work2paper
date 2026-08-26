"""⑤ 可行性评估 Agent：ideas -> evaluations（证据驱动）。

对应 docs/build-plan.md §3.3 / §4 M6 / §4 M11 与 docs/architecture.md §5 ⑤：

评估是**证据驱动**的，不是 LLM 自评（architecture §8「LLM 自评不可靠」）：

+-------------+--------------------------------+---------------------------+
| 维度        | 证据来源                       | 谁算                      |
+=============+================================+===========================+
| novelty     | literature.gap_note 对拍       | LLM 解释 + 检索事实        |
| 数据可得性  | assets.facts.data / metrics    | 确定性规则                |
| 工作量      | idea 复杂度 + 证据量           | LLM 估计（带确定性兜底）  |
| 档位        | 检索论文的 venue 分布          | 规则 + 静态档位库         |
| 风险        | metrics / baseline 缺失        | 规则                      |
+-------------+--------------------------------+---------------------------+

M11 升级：novelty 从单一 0~5 分改为**多维加权评分**（docs/build-plan.md §4 M11）：

- 5 个维度各 0~5，加权归一后总分 0~100：``总分 = Σ(权重 × 维度分) / 5``（权重合计 100）；
- 分数段映射旧 verdict：Reject / Weak Reject → drop、Revise → rework、Accept / Priority → proceed。

M18 升级：gap 假设证据级别（docs/build-plan.md §4 M18）——``gap_evidence_levels`` 整体 weak 时，
对「与已有工作的差异程度（Gap 维度）」硬打折（``_apply_gap_evidence_discount``，0.6×），LLM 与
确定性两条路径统一生效，杜绝「没搜到 ≠ 不存在」的伪创新。

M20 升级（Score Calibration，评分校准）：novelty 各维度从「LLM 自由打分」改为
**「规则 + LLM 解释」**（docs/build-plan.md §4 M20）：

- **数字来源可追溯**：每个维度用一组校准问题（rubric，见 ``RUBRIC``），LLM 只负责
  「答题（yes/no）+ 给证据」（引用 gap_note / M19 证据卡 / 矛盾图），**分数由规则算出**，
  而不是 LLM 直接给分。
- 规则引擎 ``score_rubric(answers)`` 是**纯确定性函数**：相同答案 → 相同分数（可复现）。
- 每条 evaluation 新增 ``calibration`` 字段，记录「问题 → 答案 → 规则 → 得分」完整链路。
- 无 LLM 时同样走规则引擎：由确定性信号对同一组问题作答（``_deterministic_answers``），
  再交 ``score_rubric`` 算分，保证离线路径同样可追溯、可复现。

M21 升级（面向硕士的创新点理解）：评估从「novelty 打分 → accept/reject」改为
**「创新类型分类 + 贡献矩阵 + 攻击测试」前置重构**（docs/build-plan.md §4 M21）：

- 每个 idea 先调 ``agents/contribution.classify_contribution`` 得到 ``contribution``
  （类型 A–E + 贡献矩阵 + 攻击测试），**先于** novelty 评分；
- novelty 分数降级为「参考维度」，不再作为直接 reject 依据；
- verdict 按贡献类型差异化（``_decide_verdict`` / ``_decide_verdict_contribution``）：
  贡献矩阵有任一维度 ≥ 中 → 可行贡献，不因 novelty 低而直接 drop（模块组合类不再被误 reject）；
  贡献矩阵无可行的贡献维度 → drop。

M12 升级：与 novelty 评分**并列**加入「证据强度」子审查（docs/build-plan.md §4 M12）：

- 调 ``agents/evidence.validate_evidence`` 对每个 idea 做 4 维证据审查
  （文献对拍 / 理论支撑 / 实验设计支持 / claim 强度校准），输出 weak / medium / strong + 理由；
- ``evidence=weak`` 时把 verdict 下调为 ``rework``，随 verdict 一起回炉到 ④ 细化 claim；
- 结果写入每条 evaluation 的 ``evidence_validation`` 子对象，并挂进 evidence 证据链。

verdict ∈ {proceed, rework, drop}；每条评估必须挂 `evidence`（provenance 强制）。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..dossier import Dossier
from ..literature import _literature_gap_evidence_levels
from ..llm import LLMError, LLMProvider, SchemaError
from ..parallel import map_parallel
from .contribution import (
    classify_contribution,
    classify_contribution_batch,
    matrix_viable,
)
from .evidence import CHECK_DIMENSIONS, validate_evidence, validate_evidence_batch

__all__ = [
    "run",
    "EVALUATE_SCHEMA",
    "EVALUATE_BATCH_SCHEMA",
    "NOVELTY_DIMENSIONS",
    "RUBRIC",
    "score_rubric",
    "render_calibration_lines",
    "_data_feasibility",
    "_deterministic_dimensions",
    "_deterministic_answers",
    "_deterministic_workload",
    "_weighted_total",
    "_score_band",
    "_decide_verdict",
    "_decide_verdict_contribution",
    "_rework_reason",
    "_append_evidence_validation",
    "_guess_venue",
    "_tier_of",
    "_venue_distribution",
    "_call_llm_batch",
    "_build_batch_user_prompt",
    "_extract_rubric_answers",
    "_collect_evidence_cards",
    "_apply_gap_evidence_discount",
]

# 本 Agent prompt 版本：优先读 prompts/evaluate.md 头的 version，缺失时用此兜底
_PROMPT_VERSION = "v3"
_PROMPT_FILENAME = "evaluate.md"
_PROMPT_VERSION_RE = re.compile(r"<!--\s*version:\s*(\d+)\s*-->")

# ---------------------------------------------------------------------------
# M20 评分校准 rubric：每个维度一组「问题 → 规则」。
#
# 结构：RUBRIC = ( (维度键, 中文标签, 权重, 起点分, (问题, ...)), ... )
# 问题 = ("问题ID", "问题文本", "规则种类", "规则数值")
# 规则种类：
#   - "add"：答 yes → 加分（规则数值为加分数）；
#   - "cap"：答 yes → 封顶（最终分不超过该数值）。
# 最终分 = clamp(起点分 + Σ add，0, 5)，再取 min(封顶)。分数由规则算出，LLM 不产分。
#
# 方法新颖性（method_novelty）严格沿用 M20 任务卡模板三问；其余 4 维照「问题 → 规则」模式。
# ---------------------------------------------------------------------------
RUBRIC: Tuple[Tuple[Any, ...], ...] = (
    ("problem_novelty", "问题新颖性", 20, 2.0, (
        ("Q1", "检索到的文献是否已明确聚焦并解决了同一问题？", "cap", 2),
        ("Q2", "gap_note/矛盾图是否指出该问题存在未被充分覆盖的角度？", "add", 1),
        ("Q3", "该问题是否由项目真实痛点/数据约束驱动？", "add", 1),
        ("Q4", "是否提出新的问题表述/重新建模？", "add", 1),
    )),
    ("method_novelty", "方法新颖性", 35, 2.0, (
        ("Q1", "是否只是已有模块组合？", "cap", 3),
        ("Q2", "是否改变核心 optimization objective？", "add", 1),
        ("Q3", "是否提出新的学习机制？", "add", 1),
    )),
    ("technical_depth", "技术突破性", 20, 1.0, (
        ("Q1", "是否解决了文献明确指出的技术瓶颈/挑战？", "add", 1),
        ("Q2", "方法是否涉及深度机制/理论（而非仅工程调参）？", "add", 1),
        ("Q3", "是否处理了数据/模型层面的难点（稀缺/漂移/缺失/可扩展）？", "add", 1),
        ("Q4", "项目事实是否具备重型方法/数据证据支撑该瓶颈？", "add", 1),
    )),
    ("gap", "与已有工作的差异程度", 15, 1.0, (
        ("Q1", "gap_note/矛盾图是否明确指出了与本 idea 对应的差异点？", "add", 1),
        ("Q2", "是否给出了与具体基线（证据卡 baseline）的明确对比？", "add", 1),
        ("Q3", "文献中是否已存在与本 idea 高度重合的工作且未给出区别？", "cap", 2),
        ("Q4", "差异是否体现在机制/目标层面（而非仅数据/场景不同）？", "add", 1),
    )),
    ("generalization", "可推广价值", 10, 1.0, (
        ("Q1", "是否提出通用方法/框架（而非绑定单一数据集的一次性方案）？", "add", 1),
        ("Q2", "是否给出跨任务/跨场景的迁移路径或适用条件？", "add", 1),
        ("Q3", "是否依赖项目私有数据/特定场景而难以复现到其他任务？", "cap", 2),
        ("Q4", "是否有可复用组件/接口设计支撑迁移？", "add", 1),
    )),
)

# M11 多维加权 novelty 评分体系：维度名 -> (中文标签, 权重)。由 RUBRIC 派生，保证单一事实源。
# 权重合计 100；各维度分 0~5；总分 = Σ(权重 × 维度分) / 5 ∈ [0, 100]。
NOVELTY_DIMENSIONS: Tuple[Tuple[str, str, int], ...] = tuple(
    (k, label, w) for k, label, w, _base, _qs in RUBRIC
)
_DIM_KEYS: Tuple[str, ...] = tuple(k for k, _l, _w in NOVELTY_DIMENSIONS)
_DIM_LABELS: Dict[str, str] = {k: label for k, label, _w in NOVELTY_DIMENSIONS}
_DIM_WEIGHTS: Dict[str, int] = {k: w for k, _l, w in NOVELTY_DIMENSIONS}
# 维度键 -> (起点分, 问题元组)；维度键 -> 问题 ID 元组（供 schema / 规则引擎 / 提取复用）
_RUBRIC_INDEX: Dict[str, Tuple[float, Tuple[Tuple[str, str, str, int], ...]]] = {
    k: (base, qs) for k, _label, _w, base, qs in RUBRIC
}
_RUBRIC_QUESTIONS: Dict[str, Tuple[str, ...]] = {
    k: tuple(q[0] for q in qs) for k, _label, _w, _base, qs in RUBRIC
}

# M12 证据审查的 4 个维度（来自 agents/evidence.py，供报告/证据链复用）
_EVIDENCE_CHECK_LABELS: Tuple[Tuple[str, str], ...] = tuple(CHECK_DIMENSIONS)

# M20 确定性作答用的词面信号词典（无 LLM 时对同一组问题作答）
_COMBINATION_MARKERS: Tuple[str, ...] = (
    "结合", "组合", "集成", "混合", "融合", "拼接", "串接", "组合而成",
)
_NEW_MECHANISM_MARKERS: Tuple[str, ...] = (
    "新机制", "自适应", "端到端", "可微", "联合优化", "自监督", "对比学习",
    "可学习", "注意力", "记忆", "动态权重", "可训练",
)
_OBJECTIVE_MARKERS: Tuple[str, ...] = (
    "目标", "objective", "多目标", "损失", "优化目标", "联合优化", "代价函数",
)
_REFORM_MARKERS: Tuple[str, ...] = (
    "重新建模", "重新定义", "重新形式化", "新问题", "联合建模", "统一框架", "重构",
)
_BOTTLENECK_MARKERS: Tuple[str, ...] = (
    "瓶颈", "挑战", "难点", "困难", "限制", "boundary", "challenge",
)
_THEORY_MARKERS: Tuple[str, ...] = (
    "机制", "原理", "理论", "定理", "收敛", "可证明", "可证伪", "归纳偏置", "最优性",
)
_DATA_DIFFICULTY_MARKERS: Tuple[str, ...] = (
    "稀缺", "漂移", "缺失", "长尾", "小样本", "冷启动", "可扩展", "分布外", "不平衡",
)
_HEAVY_METHODS: Tuple[str, ...] = (
    "深度学习", "集成学习", "随机森林", "XGBoost", "LSTM", "Transformer", "图神经网络",
)
_DIFF_MARKERS: Tuple[str, ...] = (
    "不同于", "区别于", "而非", "而不是", "未覆盖", "尚未", "缺口", "gap",
    "现有方法", "现有工作", "与现有", "baseline", "sota", "state-of-the-art",
)
_GENERAL_MARKERS: Tuple[str, ...] = (
    "通用", "框架", "平台", "可复用", "工具", "组件化", "跨任务", "跨场景", "跨领域",
)
_MIGRATION_MARKERS: Tuple[str, ...] = (
    "迁移", "适用", "泛化", "跨任务", "跨场景", "跨领域", "可推广",
)

# 静态档位库：检索到的 venue 名称 -> 档位（architecture §5 ⑤「规则 + 静态档位库」的 MVP 子集）
_VENUE_TIERS = {
    # CCF-A 顶会 / 顶刊
    "neurips": "CCF-A", "nips": "CCF-A", "icml": "CCF-A", "iclr": "CCF-A",
    "cvpr": "CCF-A", "iccv": "CCF-A", "eccv": "CCF-A", "acl": "CCF-A",
    "aaai": "CCF-A", "ijcai": "CCF-A", "kdd": "CCF-A", "sigmod": "CCF-A",
    "vldb": "CCF-A", "icde": "CCF-A", "sigir": "CCF-A", "www": "CCF-A",
    "tkde": "CCF-A", "tpami": "CCF-A",
    # CCF-B
    "icdm": "CCF-B", "sdm": "CCF-B", "cikm": "CCF-B", "ecml": "CCF-B",
    "pkdd": "CCF-B", "icassp": "CCF-B", "emnlp": "CCF-B", "coling": "CCF-B",
    "naacl": "CCF-B", "icpr": "CCF-B", "dasfaa": "CCF-B", "ecai": "CCF-B",
    "tkdd": "CCF-B", "tist": "CCF-B", "kais": "CCF-B",
    # CCF-C
    "pakdd": "CCF-C", "apweb": "CCF-C", "waim": "CCF-C", "adma": "CCF-C",
    "dexa": "CCF-C",
    # 预印本（未分级）
    "arxiv": "预印本（arXiv）", "semantic scholar": "预印本",
    # 中文核心
    "计算机学报": "中文核心（A类）", "软件学报": "中文核心（A类）",
    "自动化学报": "中文核心（A类）", "电子学报": "中文核心（A类）",
    "计算机研究与发展": "中文核心（A类）", "中文信息学报": "中文核心",
}


def _clean(s: Any) -> str:
    return " ".join(str(s or "").split())


def _num(x: float) -> str:
    """把浮点/整数格式化成干净字符串（2.0 → '2'，1.5 → '1.5'）。"""
    if isinstance(x, float) and x.is_integer():
        return str(int(x))
    return str(x)


def _rule_text(kind: str, value: int) -> str:
    """把规则种类+数值渲染成人类可读的规则文本（用于报告可追溯展示）。"""
    if kind == "cap":
        return "yes → 封顶 ≤ {}".format(value)
    if kind == "add":
        return "yes → +{}".format(value)
    return "yes → 不计分"


def _normalize_answer(value: Any) -> str:
    """把任意答案归一到 yes/no（非 yes 一律 no）。"""
    s = str(value or "").strip().lower()
    if s in ("yes", "y", "true", "1", "是"):
        return "yes"
    return "no"


# ---------------------------------------------------------------------------
# M20 规则引擎：答案 -> 维度分（纯确定性，相同答案 → 相同分数）
# ---------------------------------------------------------------------------

def _score_dimension(key: str, answers: Dict[str, Any]) -> Tuple[float, List[dict], str]:
    """对单个维度按规则算分，返回 (分数, 逐问题链路, 推导文本)。"""
    base, questions = _RUBRIC_INDEX[key]
    score = float(base)
    cap: Optional[int] = None
    trace: List[dict] = []
    dim_answers = answers.get(key) if isinstance(answers, dict) else None
    dim_answers = dim_answers if isinstance(dim_answers, dict) else {}

    for (qid, text, kind, value) in questions:
        item = dim_answers.get(qid)
        item = item if isinstance(item, dict) else {}
        answer = _normalize_answer(item.get("answer"))
        evidence = _clean(item.get("evidence"))
        effect = "—"
        if answer == "yes":
            if kind == "add":
                score += value
                effect = "+{}".format(value)
            elif kind == "cap":
                cap = value if cap is None else min(cap, value)
                effect = "封顶≤{}".format(value)
        trace.append({
            "id": qid,
            "text": text,
            "answer": answer,
            "evidence": evidence,
            "rule": _rule_text(kind, value),
            "effect": effect,
        })

    score = max(0.0, min(5.0, score))
    cap_hit: Optional[int] = None
    if cap is not None and score > cap:
        cap_hit = cap
        score = float(cap)

    parts = ["起点 {}".format(_num(base))]
    for q in trace:
        if q["effect"].startswith("+"):
            parts.append("{}:{}".format(q["id"], q["effect"]))
    derivation = " + ".join(parts)
    caps = [q for q in trace if q["effect"].startswith("封顶")]
    if caps:
        derivation += "；{}".format("、".join("{}:{}".format(q["id"], q["effect"]) for q in caps))
    if cap_hit is not None:
        derivation += " → 命中封顶，最终 {}".format(_num(score))
    else:
        derivation += " = {}".format(_num(score))

    return round(score, 1), trace, derivation


def score_rubric(answers: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """把 rubric 答案算成 (novelty_dimensions, calibration)。

    - ``novelty_dimensions``：``{维度键: {score, reason}}``（reason 为规则推导文本）；
    - ``calibration``：``{维度键: {label, weight, score, base, derivation, questions}}``，
      questions 为「问题 → 答案 → 规则 → 效果」逐条链路。

    纯确定性函数：相同 answers → 相同输出（可复现）。
    """
    dims: Dict[str, Any] = {}
    calibration: Dict[str, Any] = {}
    for key, label, weight in NOVELTY_DIMENSIONS:
        score, trace, derivation = _score_dimension(key, answers)
        dims[key] = {"score": score, "reason": "规则计算：{}".format(derivation)}
        calibration[key] = {
            "label": label,
            "weight": weight,
            "score": score,
            "base": _RUBRIC_INDEX[key][0],
            "derivation": derivation,
            "questions": trace,
        }
    return dims, calibration


# ---------------------------------------------------------------------------
# 结构化输出契约（schema 校验走 papermine/llm.py 的极简子集）
# ---------------------------------------------------------------------------

def _question_answer_object() -> Dict[str, Any]:
    """单个校准问题的输出契约：answer（yes/no）+ evidence（引用 gap_note/证据卡）。"""
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["answer", "evidence"],
        "properties": {
            "answer": {"type": "string", "enum": ["yes", "no"]},
            "evidence": {"type": "string"},
        },
    }


def _rubric_schema() -> Dict[str, Any]:
    """rubric 输出契约：每个维度 -> 每个问题 -> {answer, evidence}。"""
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(_DIM_KEYS),
        "properties": {
            key: {
                "type": "object",
                "additionalProperties": False,
                "required": list(_RUBRIC_QUESTIONS[key]),
                "properties": {qid: _question_answer_object() for qid in _RUBRIC_QUESTIONS[key]},
            }
            for key in _DIM_KEYS
        },
    }


# 本 Agent 的 LLM 输出契约：LLM 只答题（rubric）+ 估工作量 + 给 verdict 建议，
# novelty 分数由规则算出，不出现在输出契约里（M20）。
EVALUATE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "rubric", "workload_hours",
        "verdict_suggestion", "rework_reason",
    ],
    "properties": {
        "rubric": _rubric_schema(),
        "workload_hours": {"type": "number"},
        "verdict_suggestion": {
            "type": "string", "enum": ["proceed", "rework", "drop"],
        },
        "rework_reason": {"type": ["string", "null"]},
    },
}

# M15 方向④：批量评估——一次 LLM 调用返回多个 idea 的评估（每条 = 单个 EVALUATE_SCHEMA + idea_id）。
EVALUATE_BATCH_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["evaluations"],
    "properties": {
        "evaluations": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["idea_id"] + list(EVALUATE_SCHEMA["required"]),
                "properties": dict(
                    {"idea_id": {"type": "string"}},
                    **EVALUATE_SCHEMA["properties"],
                ),
            },
        },
    },
}

_SYSTEM_PROMPT_FALLBACK = (
    "你是 papermine 的「可行性评估 Agent」。对候选创新点做证据驱动的可行性评估。"
    "**你不打 novelty 分**：novelty 分数由系统按规则从你的答题中算出。你只做三件事：\n"
    "1. 对 rubric 里的每个校准问题回答 yes/no，并给出**证据**（必须引用 gap_note / "
    "论文级证据卡 evidence_card / 矛盾图，禁止空泛或编造）；\n"
    "2. 估计 workload_hours；\n"
    "3. 给出 verdict_suggestion∈{proceed,rework,drop}，非 proceed 时给 rework_reason。\n"
    "只输出符合 schema 的 JSON 对象。"
)


def _prompt_dir() -> Path:
    """返回包内 prompts 目录（papermine/prompts）。"""
    return Path(__file__).resolve().parent.parent / "prompts"


def _load_prompt() -> tuple:
    """读取 prompts/evaluate.md，返回 (system_prompt_text, version)。文件缺失时用内联兜底。"""
    path = _prompt_dir() / _PROMPT_FILENAME
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return _SYSTEM_PROMPT_FALLBACK, _PROMPT_VERSION
    m = _PROMPT_VERSION_RE.search(text)
    version = "v{}".format(m.group(1)) if m else _PROMPT_VERSION
    return text, version


# ---------------------------------------------------------------------------
# 确定性信号：数据可得性 / 检索 venue 分布 / gap 笔记 / M19 证据卡
# ---------------------------------------------------------------------------

def _data_feasibility(facts: Dict[str, Any]) -> str:
    """数据可得性（确定性规则）：high = 有数据且有指标；medium = 有数据缺指标；low = 无数据。"""
    data = facts.get("data") or []
    metrics = facts.get("metrics") or []
    if data and metrics:
        return "high"
    if data:
        return "medium"
    return "low"


def _venue_distribution(literature: List[dict]) -> Dict[str, int]:
    """统计检索论文的 venue 分布（venue -> 出现次数）。"""
    dist: Dict[str, int] = {}
    for lit in literature or []:
        if not isinstance(lit, dict):
            continue
        for paper in lit.get("papers") or []:
            if not isinstance(paper, dict):
                continue
            venue = (
                paper.get("venue") or paper.get("journal")
                or paper.get("conference") or paper.get("venue_name")
                or paper.get("source")
            )
            if venue and str(venue).strip():
                v = str(venue).strip()
                dist[v] = dist.get(v, 0) + 1
    return dist


def _all_gap_notes(literature: List[dict]) -> List[str]:
    """收集全部 gap_note（去重保序），作为 novelty 对拍的证据。"""
    notes: List[str] = []
    for lit in literature or []:
        if not isinstance(lit, dict):
            continue
        gap = lit.get("gap_note")
        if gap and str(gap).strip() and gap not in notes:
            notes.append(gap)
    return notes


def _collect_evidence_cards(literature: List[dict]) -> List[Dict[str, Any]]:
    """收集全部论文级证据卡（M19），作为 novelty 答题的对拍依据。"""
    cards: List[Dict[str, Any]] = []
    for entry in literature or []:
        if not isinstance(entry, dict):
            continue
        for p in entry.get("papers") or []:
            if isinstance(p, dict) and isinstance(p.get("evidence_card"), dict):
                cards.append(p["evidence_card"])
    return cards


def _collect_gap_records(literature: List[dict]) -> List[Dict[str, Any]]:
    """收集 contradiction_graph 里的 gap/矛盾记录（含 gap_id），供答题引用。"""
    out: List[Dict[str, Any]] = []
    for entry in literature or []:
        if not isinstance(entry, dict):
            continue
        graph = entry.get("contradiction_graph") or {}
        for g in (graph.get("gaps") or []):
            if isinstance(g, dict):
                out.append({
                    "gap_id": g.get("gap_id"),
                    "type": g.get("type"),
                    "claim_point": g.get("claim_point"),
                    "description": g.get("description"),
                })
    return out


def _has_papers(literature: List[dict]) -> bool:
    """是否检索到了真实论文。"""
    for entry in literature or []:
        if isinstance(entry, dict) and (entry.get("papers") or []):
            return True
    return False


def _format_venue_distribution(dist: Dict[str, int]) -> str:
    if not dist:
        return "（检索论文未提供 venue 信息）"
    items = sorted(dist.items(), key=lambda kv: (-kv[1], kv[0]))
    return "，".join("{}×{}".format(v, k) for k, v in items)


# ---------------------------------------------------------------------------
# M18：gap 假设证据级别（Gap 维度消费；weak 时 novelty 打折）
# ---------------------------------------------------------------------------

def _gap_evidence_levels(literature: List[dict]) -> List[str]:
    """收集全部 gap/矛盾的 evidence_level（M18，供 Gap 维度与证据链消费）。"""
    return _literature_gap_evidence_levels(literature)


def _gap_evidence_weak(levels: List[str]) -> Optional[bool]:
    """gap 假设证据是否「整体偏弱」：有 gap 假设且全为 weak → True；无 gap 假设 → None（不打折）。"""
    if not levels:
        return None
    return all(lv == "weak" for lv in levels)


def _gap_evidence_summary(levels: List[str]) -> str:
    """把 evidence_level 列表聚合成单档（无 → unknown；有 strong → strong；全 weak → weak；否则 moderate）。"""
    if not levels:
        return "unknown"
    if any(lv == "strong" for lv in levels):
        return "strong"
    if all(lv == "weak" for lv in levels):
        return "weak"
    return "moderate"


# gap 假设证据级别为 weak 时，Gap 维度分的折扣系数（docs/build-plan.md §4 M18：weak 打折）
_GAP_WEAK_DISCOUNT = 0.6


def _tier_of(venue: str) -> str:
    """把单个 venue 名称映射到档位（静态档位库 + 兜底）。"""
    v = (venue or "").strip().lower()
    if not v:
        return "未知档位"
    for key, tier in _VENUE_TIERS.items():
        if key in v:
            return tier
    return "未分级（{}）".format((venue or "").strip()[:30] or "未知")


# ---------------------------------------------------------------------------
# 确定性兜底估算：novelty（规则引擎 + 确定性作答）/ workload / 档位
# ---------------------------------------------------------------------------

def _gap_signal_strength(gap_notes: List[str]) -> int:
    """gap 信号强度：0 = 无 gap（无法对拍）；1 = 弱信号；2 = 强信号（明确缺口）。"""
    if not gap_notes:
        return 0
    low = " ".join(str(g) for g in gap_notes).lower()
    strong = ("尚未", "没有系统", "缺乏系统", "no systematic", "rarely",
              "open problem", "未解决", "缺口", "未有", "少有")
    weak = ("gap", "不足", "limited", "missing", "challenge", "缺乏")
    if any(m in low for m in strong):
        return 2
    if any(m in low for m in weak):
        return 1
    return 0


def _deterministic_answers(idea: dict, gap_notes: List[str],
                           facts: Dict[str, Any],
                           literature: Optional[List[dict]] = None) -> Dict[str, Any]:
    """无 LLM 时由确定性信号对 rubric 同一组问题作答，返回 ``{维度: {问题: {answer, evidence}}}``。

    与 LLM 路径共享 ``score_rubric`` 规则引擎，故离线路径同样可追溯、可复现。
    证据文本注明「确定性规则信号」，诚实标注其为词面启发式（低置信，报告会标注 degraded）。
    """
    facts = facts or {}
    claim = _clean(" ".join(str(idea.get(k) or "") for k in ("claim", "novelty_hypothesis")))
    methods = [str(m) for m in (facts.get("methods") or []) if str(m).strip()]
    strength = _gap_signal_strength(gap_notes)
    has_gap = strength >= 1
    strong_gap = strength >= 2
    gap_ref = str(gap_notes[0])[:100] if gap_notes else "（空）"
    cards = _collect_evidence_cards(literature)
    has_baseline = any(_clean(c.get("baseline")) for c in cards)
    papers_exist = bool(cards) or _has_papers(literature)
    has_task_scenario = bool((facts.get("tasks") or []) or (facts.get("scenarios") or []))
    has_modules = bool(facts.get("modules") or [])
    has_data = bool(facts.get("data") or [])

    def _has(markers: Tuple[str, ...]) -> bool:
        return any(m in claim for m in markers)

    def _a(cond: bool, evidence: str) -> Dict[str, str]:
        return {"answer": "yes" if cond else "no", "evidence": evidence}

    has_combination = _has(_COMBINATION_MARKERS)
    has_new_mechanism = _has(_NEW_MECHANISM_MARKERS)
    has_objective = _has(_OBJECTIVE_MARKERS)
    has_reform = _has(_REFORM_MARKERS)
    has_bottleneck = _has(_BOTTLENECK_MARKERS)
    has_theory = _has(_THEORY_MARKERS)
    has_data_difficulty = _has(_DATA_DIFFICULTY_MARKERS)
    has_diff = _has(_DIFF_MARKERS)
    has_general = _has(_GENERAL_MARKERS)
    has_migration = _has(_MIGRATION_MARKERS)
    heavy = any(m in _HEAVY_METHODS for m in methods)

    answers: Dict[str, Any] = {}

    # 问题新颖性
    answers["problem_novelty"] = {
        "Q1": _a(False, "确定性规则无法仅凭关键词判断文献已充分解决同一问题，保守取 no（不封顶）"),
        "Q2": _a(has_gap,
                 "gap_note 指出未覆盖角度：{}".format(gap_ref) if has_gap
                 else "无 gap_note，无法确认未覆盖角度"),
        "Q3": _a(has_task_scenario,
                 "项目有任务/场景事实，问题由真实痛点驱动" if has_task_scenario
                 else "无任务/场景事实，问题来源存疑"),
        "Q4": _a(has_reform,
                 "claim 含重新建模/新问题表述信号" if has_reform else "claim 无重新建模信号"),
    }

    # 方法新颖性（M20 模板三问）
    answers["method_novelty"] = {
        "Q1": _a(has_combination and not has_new_mechanism,
                 "claim 含模块组合信号且无新机制信号" if has_combination and not has_new_mechanism
                 else "非单纯模块组合（或有新机制信号）"),
        "Q2": _a(has_objective,
                 "claim 含 optimization objective 改动信号" if has_objective
                 else "claim 无 optimization objective 改动信号"),
        "Q3": _a(has_new_mechanism,
                 "claim 含新学习机制信号" if has_new_mechanism else "claim 无新学习机制信号"),
    }

    # 技术突破性
    answers["technical_depth"] = {
        "Q1": _a(strong_gap or has_bottleneck,
                 "gap 信号强（文献明确缺口）/claim 含瓶颈信号" if strong_gap or has_bottleneck
                 else "无明确技术瓶颈信号"),
        "Q2": _a(has_theory,
                 "claim 含深度机制/理论信号" if has_theory else "claim 无深度机制/理论信号"),
        "Q3": _a(has_data_difficulty,
                 "claim 含数据/模型难点信号" if has_data_difficulty else "claim 无数据/模型难点信号"),
        "Q4": _a(heavy,
                 "facts.methods 含重型方法：{}".format("、".join(methods)[:60]) if heavy
                 else "facts.methods 无重型方法支撑"),
    }

    # 与已有工作的差异程度
    answers["gap"] = {
        "Q1": _a(has_gap,
                 "gap_note 指出差异点：{}".format(gap_ref) if has_gap
                 else "无 gap_note，无法确认差异点"),
        "Q2": _a(has_baseline or has_diff,
                 "证据卡含 baseline / claim 含 SOTA 对比信号" if has_baseline or has_diff
                 else "无 baseline / SOTA 对比证据"),
        "Q3": _a((not has_gap) and papers_exist,
                 "有文献但无 gap 信号，可能与已有工作高度重合" if (not has_gap) and papers_exist
                 else "有 gap 信号或无论文，不判高度重合"),
        "Q4": _a(has_diff,
                 "claim 含差异化定位信号" if has_diff else "claim 无差异化定位信号"),
    }

    # 可推广价值
    bound_to_specific = (bool(has_data) or bool((facts.get("scenarios") or []))) \
        and not has_general and not has_migration
    answers["generalization"] = {
        "Q1": _a(has_general,
                 "claim 含通用方法/框架信号" if has_general else "claim 无通用方法/框架信号"),
        "Q2": _a(has_migration,
                 "claim 含迁移/适用条件信号" if has_migration else "claim 无迁移/适用条件信号"),
        "Q3": _a(bound_to_specific,
                 "依赖项目特定数据/场景且无通用/迁移主张" if bound_to_specific
                 else "有通用/迁移主张，或不绑定特定数据"),
        "Q4": _a(has_modules or _has(("组件", "复用", "接口")),
                 "facts.modules 非空 / claim 含可复用组件信号" if has_modules or _has(("组件", "复用", "接口"))
                 else "无可复用组件/接口信号"),
    }

    return answers


def _deterministic_dimensions(idea: dict, gap_notes: List[str],
                              facts: Dict[str, Any]) -> Dict[str, Any]:
    """无 LLM 时的多维粗估（低置信，报告会标注）：确定性作答 + 规则引擎算分。

    返回 ``{维度键: {score, reason}}``，分数与 LLM 路径同一规则引擎算出（可追溯、可复现）。
    """
    answers = _deterministic_answers(idea, gap_notes, facts)
    dims, _calibration = score_rubric(answers)
    return dims


def _weighted_total(dims: Dict[str, Any]) -> float:
    """加权合成 0~100 总分：``总分 = Σ(权重 × 维度分) / 5``（权重合计 100、维度分 0~5）。"""
    total = 0.0
    for key, _label, weight in NOVELTY_DIMENSIONS:
        item = (dims or {}).get(key)
        score = _coerce_number(item.get("score"), 0.0, 0.0, 5.0) \
            if isinstance(item, dict) else 0.0
        total += weight * float(score)
    return round(total / 5.0, 1)


def _score_band(score: float) -> Tuple[str, str]:
    """总分 → (Agent 建议动作标签, 旧 verdict)。

    分数段（docs/build-plan.md §4 M11，区间约定为左闭右开）：
        (-∞, 40)   Reject      → drop
        [40, 60)   Weak Reject → drop
        [60, 70)   Revise      → rework
        [70, 80]   Accept      → proceed
        (80, +∞)   Priority    → proceed
    """
    if score < 40:
        return ("Reject", "drop")
    if score < 60:
        return ("Weak Reject", "drop")
    if score < 70:
        return ("Revise", "rework")
    if score <= 80:
        return ("Accept", "proceed")
    return ("Priority", "proceed")


def _deterministic_workload(idea: dict, facts: Dict[str, Any]) -> int:
    """确定性工作量兜底：依据 facts 丰富度估算（有指标减负、缺指标加负、复杂方法加负）。"""
    methods = facts.get("methods") or []
    metrics = facts.get("metrics") or []
    base = 60
    if metrics:
        base -= 10
    else:
        base += 20
    base += min(len(methods), 4) * 10
    heavy = {"深度学习", "集成学习", "随机森林", "XGBoost"}
    if any(m in heavy for m in methods):
        base += 20
    return max(20, min(400, base))


def _is_tool_claim(claim: str) -> bool:
    """判定 idea 是否在主张「做工具/框架」，用强信号避免把方法标签「流水线/框架」误判为工具。"""
    if any(k in claim for k in ("工具", "平台", "通用框架", "可复用组件")):
        return True
    return ("组件" in claim) and ("抽象" in claim or "复用" in claim)


def _rule_venue_guess(facts: Dict[str, Any], idea: dict, novelty: float) -> str:
    """无检索 venue 时的规则档位猜测（复用 v0.1 mining 思路，依据 idea 类型 + novelty 总分）。"""
    claim = str(idea.get("claim") or "")
    if _is_tool_claim(claim):
        return "中文核心 / EI 会议（系统/工具类）"
    if "实证" in claim:
        return "中文核心 / 应用类期刊"
    if novelty >= 80:
        return "CCF-B / 中文核心或 EI 会议"
    if novelty >= 60:
        return "CCF-C / 中文核心"
    return "EI 会议 / 中文核心（创新度有限）"


def _guess_venue(facts: Dict[str, Any], idea: dict,
                 venue_dist: Dict[str, int], novelty: float) -> str:
    """档位匹配（确定性规则）：有检索 venue 分布则对照分布，否则退规则档位库。"""
    if venue_dist:
        tier_counts: Dict[str, int] = {}
        for venue, cnt in venue_dist.items():
            tier = _tier_of(venue)
            tier_counts[tier] = tier_counts.get(tier, 0) + cnt
        dominant = max(tier_counts.items(), key=lambda kv: (kv[1], kv[0]))[0]
        if "预印本" in dominant or "arXiv" in dominant:
            return "检索论文多为预印本，方向尚新；建议从 EI 会议 / 中文核心起步"
        return "检索论文档位集中（{}），建议对照 {}".format(
            _format_venue_distribution(venue_dist), dominant)
    return _rule_venue_guess(facts, idea, novelty)


# ---------------------------------------------------------------------------
# verdict 决策（M21：按贡献类型差异化；novelty 降级为参考维度 + 证据驱动硬护栏）
# ---------------------------------------------------------------------------

def _decide_verdict(novelty: float, data_feasibility: str, workload: float,
                    suggestion: Optional[str], evidence: str = "medium",
                    contribution: Optional[dict] = None) -> str:
    """综合 verdict。

    M21：novelty 分数降级为「参考维度」，不再作为直接 reject 依据；verdict 按贡献类型差异化。

    - 硬约束（所有类型一致）：``data_feasibility=low`` → rework；``workload>400`` → rework；
    - ``contribution`` 非 None（M21 主路径）：由贡献矩阵是否「有任一维度 ≥ 中」决定——
      可行贡献 → 不因 novelty 低而直接 drop（``_decide_verdict_contribution``）；
      无可行贡献 → drop；
    - ``contribution`` 为 None（旧调用 / 无贡献分析）：退回 novelty 分数段映射旧 verdict，
      保证本纯函数可独立复用（向后兼容）。

    M12：``evidence=weak``（证据不足以支撑 claim）时下调为 ``rework``（回炉到 ④ 细化 claim）。
    """
    _band, base = _score_band(novelty)
    if data_feasibility == "low":
        return "rework"        # 无数据支撑，回炉补数据
    if workload > 400:
        return "rework"        # 工作量过大，需拆分或回炉

    if contribution is None:
        # 旧路径（无贡献分析）：novelty 分数段作为参考映射
        if data_feasibility == "medium" and base == "drop":
            return "rework"    # 中低新颖性 + 数据不完整
        # M12：证据强度弱 → 回炉细化 claim（仅下调，不把 drop 上调）
        if evidence == "weak" and base != "drop":
            return "rework"
        # LLM 建议仅可下调（proceed -> rework/drop），不可把 drop 上调
        if suggestion in ("drop", "rework") and base == "proceed":
            return suggestion
        return base

    # M21：按贡献类型差异化 verdict（novelty 分数不再作为直接 reject 依据）
    return _decide_verdict_contribution(evidence, suggestion, contribution)


def _decide_verdict_contribution(evidence: str, suggestion: Optional[str],
                                 contribution: dict) -> str:
    """M21：按贡献矩阵是否可行决定 verdict（novelty 分数在此仅作参考）。

    - 贡献矩阵无任一维度 ≥ 中 → 无可行贡献 → ``drop``；
    - 可行贡献 + evidence=weak → ``rework``（回炉细化 claim，不 drop）；
    - 可行贡献 + suggestion=rework → ``rework``（仅下调）；
    - 其余（可行贡献 + 证据不弱 + 无硬阻塞）→ ``proceed``。

    这样，模块组合类 idea（框架创新 / 应用创新 / 工程价值 ≥ 中）不再因 method novelty 低而被
    直接 reject——这正是 M21 对硕士生评估的核心修正。
    """
    viable = matrix_viable((contribution or {}).get("matrix"))
    if not viable:
        return "drop"
    if evidence == "weak":
        return "rework"
    if suggestion == "rework":
        return "rework"
    return "proceed"


def _rework_reason(verdict: str, novelty: float, data_feasibility: str,
                   workload: float, llm_reason: Optional[str] = None,
                   evidence_strength: Optional[str] = None,
                   evidence_reason: Optional[str] = None,
                   contribution: Optional[dict] = None) -> Optional[str]:
    """生成 rework_reason（proceed 时为 None）。M12：evidence=weak 时给出证据不足的专属理由。

    M21：drop 的措辞改为「无可行贡献」，novelty 仅作参考（不再说「新颖性不足 → 放弃」）。
    """
    if verdict == "proceed":
        return None
    band_label, _ = _score_band(novelty)
    if verdict == "drop":
        if contribution is not None:
            return "无可行贡献：贡献矩阵各维度均未达「中」（novelty_score={} 仅作参考），无明确的论文贡献点，建议放弃".format(
                novelty)
        return "新颖性不足：novelty_score={}（{}），与文献 gap 对拍无明显差异，建议放弃该创新点".format(
            novelty, band_label)
    if data_feasibility == "low":
        return "数据可得性低：assets.facts 未识别到数据/指标，需回炉补充评测数据（回退①项目理解补采集）"
    if evidence_strength == "weak":
        return "证据不足：evidence=weak；{}（建议回炉到④细化 claim）".format(
            evidence_reason or "需补文献对拍/理论依据/实验设计")
    if novelty < 70:
        return "新颖性偏低：novelty_score={}（{}），建议回炉到②问题抽象/④创新点生成以强化 novelty".format(
            novelty, band_label)
    if workload > 400:
        return "工作量过大：workload={}h，建议拆分范围或回炉缩小目标".format(workload)
    if llm_reason:
        return str(llm_reason)
    return "需回炉打磨（详见 evidence）"


def _coerce_number(value: Any, default: Optional[float],
                   lo: Optional[float] = None, hi: Optional[float] = None) -> Optional[float]:
    """把值安全转为 float 并夹取区间；非数字返回 default。"""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        num = float(value)
        if lo is not None:
            num = max(lo, num)
        if hi is not None:
            num = min(hi, num)
        return num
    return default


def _apply_gap_evidence_discount(dims: Dict[str, Any],
                                 gap_evidence_levels: Optional[List[str]],
                                 calibration: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """M18：gap 假设证据级别整体 weak 时，对「Gap 维度」分硬打折（0.6×）并标注理由。

    证据弱 → 「与 SOTA 的差异主张」也应弱。打折确定性执行（可复现、数字可追溯），
    对 LLM 与确定性两条评分路径**一致生效**，不依赖 LLM 自觉保守。

    M20：可选 ``calibration`` 一并同步更新 gap 维度的 score / derivation（原地更新），
    使报告里「问题 → 答案 → 规则 → 得分」链路与最终 novelty_dimensions 保持一致。
    """
    if _gap_evidence_weak(gap_evidence_levels or []) is not True:
        return dims
    out = dict(dims)
    item = out.get("gap")
    if isinstance(item, dict):
        score = _coerce_number(item.get("score"), None, 0.0, 5.0)
        if score is not None:
            new_score = round(score * _GAP_WEAK_DISCOUNT, 1)
            reason = str(item.get("reason") or "").strip()
            note = "；gap 假设证据级别=weak → 差异度打折 0.6×（{} → {}）".format(
                _num(score), _num(new_score))
            out["gap"] = {
                "score": new_score,
                "reason": reason + note if reason else note.lstrip("；"),
            }
            if isinstance(calibration, dict) and isinstance(calibration.get("gap"), dict):
                cal_gap = dict(calibration["gap"])
                cal_gap["score"] = new_score
                cal_gap["derivation"] = str(cal_gap.get("derivation") or "") + note
                calibration["gap"] = cal_gap
    return out


# ---------------------------------------------------------------------------
# LLM 调用与证据装配
# ---------------------------------------------------------------------------

def _rubric_questions_payload() -> Dict[str, List[Dict[str, str]]]:
    """把 rubric 的问题（id + 文本）打包给 LLM；规则不外传，LLM 只答题不给分。"""
    return {
        key: [{"id": qid, "question": text} for (qid, text, _kind, _value) in questions]
        for key, _label, _w, _base, questions in RUBRIC
    }


def _build_user_prompt(idea: dict, gap_notes: List[str],
                       venue_summary: str, facts: Dict[str, Any],
                       literature: List[dict],
                       gap_evidence_summary: str = "unknown") -> str:
    payload = {
        "idea": {
            "idea_id": idea.get("idea_id"),
            "claim": idea.get("claim"),
            "novelty_hypothesis": idea.get("novelty_hypothesis"),
            "problem_ref": idea.get("problem_ref"),
            "literature_refs": idea.get("literature_refs"),
        },
        "rubric": _rubric_questions_payload(),
        "gap_notes": gap_notes,
        "evidence_cards": _collect_evidence_cards(literature),
        "gaps": _collect_gap_records(literature),
        # M18：gap 假设证据级别（weak/moderate/strong/unknown），Gap 维度答题时参考
        "gap_evidence": gap_evidence_summary,
        "venue_distribution": venue_summary,
        "facts": facts,
    }
    return ("以下是一个候选创新点及其证据，请按 rubric 逐题作答（yes/no + 证据，引用 gap_note / "
            "证据卡 / 矛盾图），并估计工作量、给出 verdict 建议：\n" + json.dumps(
        payload, ensure_ascii=False))


def _call_llm(llm: LLMProvider, system_prompt: str, idea: dict,
              gap_notes: List[str], venue_summary: str,
              facts: Dict[str, Any], literature: List[dict],
              gap_evidence_summary: str = "unknown") -> Dict[str, Any]:
    """调用 LLM；任何失败/空结果都返回空 dict，由上层降级。"""
    result: Dict[str, Any] = {}
    try:
        result = llm.complete(
            system_prompt,
            _build_user_prompt(idea, gap_notes, venue_summary, facts, literature, gap_evidence_summary),
            EVALUATE_SCHEMA, temperature=0.2,
        )
    except (LLMError, SchemaError):
        result = {}
    if not isinstance(result, dict):
        result = {}
    return result


def _build_batch_user_prompt(ideas: List[dict], gap_notes: List[str],
                             venue_summary: str, facts: Dict[str, Any],
                             literature: List[dict],
                             gap_evidence_summary: str = "unknown") -> str:
    """构造批量评估的脱敏输入：一组 idea + 共享证据（rubric / gap_notes / 证据卡 / 矛盾图 / venue 分布 / facts）。"""
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
        "rubric": _rubric_questions_payload(),
        "gap_notes": gap_notes,
        "evidence_cards": _collect_evidence_cards(literature),
        "gaps": _collect_gap_records(literature),
        # M18：gap 假设证据级别（weak/moderate/strong/unknown），Gap 维度答题时参考
        "gap_evidence": gap_evidence_summary,
        "venue_distribution": venue_summary,
        "facts": facts,
    }
    return (
        "以下是一组候选创新点及其共享证据，请对每个 idea 按 rubric 逐题作答"
        "（yes/no + 证据，引用 gap_note / 证据卡 / 矛盾图），并估计工作量、给出 verdict 建议：\n"
        + json.dumps(payload, ensure_ascii=False)
    )


def _call_llm_batch(llm: LLMProvider, system_prompt: str, ideas: List[dict],
                    gap_notes: List[str], venue_summary: str,
                    facts: Dict[str, Any], literature: List[dict],
                    gap_evidence_summary: str = "unknown") -> Optional[Dict[str, dict]]:
    """M15 方向④：批量评估多个 idea（一次 LLM 调用），返回 ``{idea_id: 单条评估 dict}``。

    失败 / 空结果 / 结构非法 → 返回 None（由 run 回退到单条 ``_call_llm`` + 确定性兜底）。
    """
    if llm is None or not ideas:
        return None
    result: Dict[str, Any] = {}
    try:
        result = llm.complete(
            system_prompt,
            _build_batch_user_prompt(ideas, gap_notes, venue_summary, facts, literature, gap_evidence_summary),
            EVALUATE_BATCH_SCHEMA, temperature=0.2,
        )
    except (LLMError, SchemaError):
        return None
    if not isinstance(result, dict):
        return None
    raw = result.get("evaluations")
    if not isinstance(raw, list):
        return None
    out: Dict[str, dict] = {}
    for item in raw:
        if isinstance(item, dict) and str(item.get("idea_id") or "").strip():
            out[str(item["idea_id"]).strip()] = item
    return out or None


def _extract_rubric_answers(raw: Any) -> Optional[Dict[str, Any]]:
    """从 LLM 输出提取并规范化 rubric 答案；任一问题缺失/非法/证据为空 → 返回 None（触发确定性兜底）。

    M20 铁律：每个 yes/no 都必须有证据（引用 gap_note / 证据卡），证据为空视为不可追溯 → 兜底。
    """
    if not isinstance(raw, dict):
        return None
    answers: Dict[str, Any] = {}
    for key in _DIM_KEYS:
        dim_raw = raw.get(key)
        if not isinstance(dim_raw, dict):
            return None
        q_answers: Dict[str, Dict[str, str]] = {}
        for qid in _RUBRIC_QUESTIONS[key]:
            item = dim_raw.get(qid)
            if not isinstance(item, dict):
                return None
            answer = str(item.get("answer") or "").strip().lower()
            if answer not in ("yes", "no"):
                return None
            evidence = _clean(item.get("evidence"))
            if not evidence:
                return None
            q_answers[qid] = {"answer": answer, "evidence": evidence}
        answers[key] = q_answers
    return answers


def _dims_converged(dims: Dict[str, Any]) -> bool:
    """判断 5 个维度分是否完全相等（趋同信号，用于报告可见性标注）。"""
    scores: List[float] = []
    for key in _DIM_KEYS:
        item = (dims or {}).get(key)
        if isinstance(item, dict) and isinstance(item.get("score"), (int, float)) \
                and not isinstance(item.get("score"), bool):
            scores.append(round(float(item["score"]), 1))
    return len(scores) == len(_DIM_KEYS) and len(set(scores)) == 1


def _assemble_evidence(gap_notes: List[str], facts: Dict[str, Any],
                       venue_dist: Dict[str, int], dims: Dict[str, Any],
                       degraded: bool, converged: bool,
                       gap_evidence_levels: Optional[List[str]] = None) -> List[dict]:
    """装配评估证据链（provenance 强制：每条结论挂证据源，含 M11/M20 分维度明细）。"""
    evidence: List[dict] = []
    if gap_notes:
        for g in gap_notes:
            evidence.append({"source": "literature.gap_note", "note": str(g)[:200]})
    else:
        evidence.append({"source": "literature.gap_note",
                         "note": "文献为空，novelty 无法对照，按规则保守估计"})
    # M18：gap 假设证据级别挂进证据链（weak 时 Gap 维度打折的依据可见）
    if gap_evidence_levels:
        evidence.append({
            "source": "literature.gap_hypothesis",
            "note": "gap 假设证据级别：{}（{} 条 gap/矛盾；weak 时 novelty 的 Gap 维度打折）".format(
                _gap_evidence_summary(gap_evidence_levels), len(gap_evidence_levels)),
        })
    data = facts.get("data") or []
    if data:
        evidence.append({"source": "assets.facts.data", "note": "数据标签：" + "、".join(data)})
    metrics = facts.get("metrics") or []
    if metrics:
        evidence.append({"source": "assets.facts.metrics", "note": "指标标签：" + "、".join(metrics)})
    if venue_dist:
        evidence.append({"source": "literature.venues",
                         "note": "检索论文档位分布：" + _format_venue_distribution(venue_dist)})
    # 分维度明细：每维分数 + 规则推导挂进证据链（M11/M20）
    for key, label, _weight in NOVELTY_DIMENSIONS:
        item = (dims or {}).get(key)
        if isinstance(item, dict):
            reason = str(item.get("reason") or "").strip()
            if reason:
                evidence.append({
                    "source": "novelty_dimensions.{}".format(key),
                    "note": "{}={}，{}".format(label, item.get("score"), reason[:160]),
                })
    if degraded:
        evidence.append({
            "source": "degradation",
            "note": "novelty 校准问题答案为确定性规则信号（无 LLM 或 LLM 输出非法），低置信",
        })
    if converged:
        evidence.append({
            "source": "degradation",
            "note": "各维度分数趋同（区分度不足），建议人工复核",
        })
    return evidence


def _append_evidence_validation(evidence: List[dict],
                                ev_validation: Dict[str, Any]) -> None:
    """把 M12 证据强度 + 4 维检查挂进评估证据链（provenance 强制）。"""
    strength = ev_validation.get("evidence", "medium")
    reason = str(ev_validation.get("reason") or "").strip()
    evidence.append({
        "source": "evidence_validation",
        "note": "证据强度={}；{}".format(strength, reason[:200] or "（无理由）"),
    })
    for key, label in _EVIDENCE_CHECK_LABELS:
        item = (ev_validation.get("checks") or {}).get(key)
        if isinstance(item, dict):
            status = str(item.get("status") or "").strip()
            note = str(item.get("note") or "").strip()
            if status and note:
                evidence.append({
                    "source": "evidence_validation.{}".format(key),
                    "note": "{}={}，{}".format(label, status, note[:160]),
                })
    if ev_validation.get("degraded"):
        evidence.append({
            "source": "degradation",
            "note": "证据强度为确定性规则粗估（无 LLM 或 LLM 输出非法），低置信",
        })


def render_calibration_lines(ev: Dict[str, Any]) -> List[str]:
    """把一条 evaluation 的 calibration 渲染成「问题 → 答案 → 规则 → 得分」Markdown 行。

    供 orchestrator 的报告渲染复用（M20 要点 3：报告展示完整链路，分数可追溯）。
    无 calibration（旧格式评估）时返回空列表。
    """
    cal = ev.get("calibration") if isinstance(ev, dict) else None
    if not isinstance(cal, dict) or not cal:
        return []
    lines: List[str] = []
    for key, label, weight in NOVELTY_DIMENSIONS:
        item = cal.get(key)
        if not isinstance(item, dict):
            continue
        lines.append("    - {}（权重{}）：得分 {} = {}".format(
            label, weight, item.get("score"), item.get("derivation") or "—"))
        for q in (item.get("questions") or []):
            if not isinstance(q, dict):
                continue
            seg = "      - {} [{}] {} — 规则：{}".format(
                q.get("id"), q.get("answer"), q.get("text"), q.get("rule") or "")
            evidence = (q.get("evidence") or "").strip()
            if evidence:
                seg += "；证据：{}".format(evidence)
            lines.append(seg)
    return lines


def _evaluate_idea(idea: dict, facts: Dict[str, Any], gap_notes: List[str],
                   venue_dist: Dict[str, int], venue_summary: str,
                   data_feasibility: str, literature: List[dict],
                   llm: LLMProvider, system_prompt: str,
                   llm_out: Optional[Dict[str, Any]] = None,
                   ev_validation: Optional[Dict[str, Any]] = None,
                   gap_evidence_levels: Optional[List[str]] = None,
                   contribution_out: Optional[Dict[str, Any]] = None) -> dict:
    """对单个 idea 做证据驱动评估，返回一条 evaluation dict（M21 贡献分析 + M11 多维 + M20 校准 + M18 打折 + M12 证据强度）。

    ``llm_out`` / ``ev_validation`` / ``contribution_out`` 为 M15 批量路径注入的结果；
    为 None 时回退到单条调用，保证批量失败时逐条降级，绝不改变确定性兜底语义。
    ``gap_evidence_levels``（M18）为 gap 假设证据级别列表，供 Gap 维度弱证据打折与证据链消费。
    """
    idea_id = str(idea.get("idea_id") or "").strip()
    gap_evidence_summary = _gap_evidence_summary(gap_evidence_levels or [])
    out = llm_out if llm_out is not None else _call_llm(
        llm, system_prompt, idea, gap_notes, venue_summary, facts, literature, gap_evidence_summary)

    # M20：LLM 只答题（rubric），分数由规则算出；答题非法/缺失 → 确定性作答兜底。
    answers = _extract_rubric_answers(out.get("rubric"))
    degraded = answers is None
    if degraded:
        answers = _deterministic_answers(idea, gap_notes, facts, literature)
    dims, calibration = score_rubric(answers)
    # M18：gap 假设证据级别 weak → Gap 维度硬打折（LLM 与确定性路径统一生效；同步校准链路）。
    dims = _apply_gap_evidence_discount(dims, gap_evidence_levels, calibration)
    novelty = _weighted_total(dims)
    band_label, _ = _score_band(novelty)
    converged = _dims_converged(dims)

    workload = _coerce_number(out.get("workload_hours"), None, 10.0, 1000.0)
    if workload is None:
        workload = float(_deterministic_workload(idea, facts))
    workload = int(round(workload))

    suggestion = out.get("verdict_suggestion")
    if suggestion not in ("proceed", "rework", "drop"):
        suggestion = None

    llm_rework_reason = out.get("rework_reason")
    if not isinstance(llm_rework_reason, str) or not llm_rework_reason.strip():
        llm_rework_reason = None

    # M12：证据强度子审查（与 novelty 并列），不跑实验，只审证据；批量路径传入结果则复用
    if ev_validation is None:
        ev_validation = validate_evidence(idea, literature, llm, facts)
    evidence_strength = ev_validation.get("evidence", "medium")
    evidence_reason = str(ev_validation.get("reason") or "").strip() or None

    # M21：创新贡献分析（类型分类 + 贡献矩阵 + 攻击测试），先于 novelty 评分；
    # 批量路径传入结果则复用，否则单条调用（失败则确定性兜底）。
    contribution = contribution_out if contribution_out is not None \
        else classify_contribution(idea, facts, literature, llm)

    venue_guess = _guess_venue(facts, idea, venue_dist, novelty)
    verdict = _decide_verdict(novelty, data_feasibility, float(workload),
                              suggestion, evidence=evidence_strength,
                              contribution=contribution)
    rework_reason = _rework_reason(verdict, novelty, data_feasibility,
                                   float(workload), llm_rework_reason,
                                   evidence_strength=evidence_strength,
                                   evidence_reason=evidence_reason,
                                   contribution=contribution)
    evidence = _assemble_evidence(gap_notes, facts, venue_dist, dims, degraded, converged,
                                  gap_evidence_levels)
    _append_evidence_validation(evidence, ev_validation)

    return {
        "idea_ref": idea_id,
        "contribution": contribution,
        "novelty_score": novelty,
        "novelty_band": band_label,
        "novelty_dimensions": dims,
        "calibration": calibration,
        "evidence_validation": {
            "evidence": evidence_strength,
            "reason": evidence_reason or "",
            "checks": ev_validation.get("checks", {}),
            "degraded": bool(ev_validation.get("degraded")),
        },
        "data_feasibility": data_feasibility,
        "workload_hours": workload,
        "venue_guess": venue_guess,
        "verdict": verdict,
        "rework_reason": rework_reason,
        "evidence": evidence,
    }


# ---------------------------------------------------------------------------
# 入口（冻结契约）
# ---------------------------------------------------------------------------

def run(dossier: Dossier, llm: LLMProvider) -> None:
    """ideas -> evaluations（M21 贡献分析前置 + M11 多维 + M20 校准 + M18 打折 + M12 证据强度），原地写 dossier.evaluations。

    冻结契约（docs/build-plan.md §3.3）：
        def run(dossier: Dossier, llm: LLMProvider) -> None
    """
    assets = dossier.assets if isinstance(dossier.assets, dict) else {}
    facts = assets.get("facts") if isinstance(assets.get("facts"), dict) else {}
    literature = list(dossier.literature or [])
    ideas = [i for i in (dossier.ideas or [])
             if isinstance(i, dict) and (i.get("idea_id") or "").strip()]

    gap_notes = _all_gap_notes(literature)
    venue_dist = _venue_distribution(literature)
    venue_summary = _format_venue_distribution(venue_dist)
    data_feasibility = _data_feasibility(facts)
    gap_evidence_levels = _gap_evidence_levels(literature)
    gap_evidence_summary = _gap_evidence_summary(gap_evidence_levels)
    system_prompt, version = _load_prompt()

    # M15 方向④：批量推理——贡献分析 / 评估 / 证据审查各合并成一次 LLM 调用。
    # M21：贡献分析（类型 + 矩阵 + 攻击测试）**先于** novelty 评分，故先批量调用。
    # 批量失败 / 某 idea 缺失时，逐条回退（保证确定性兜底语义不变）。
    contribution_batch = classify_contribution_batch(ideas, facts, literature, llm)
    eval_batch = _call_llm_batch(llm, system_prompt, ideas, gap_notes, venue_summary,
                                 facts, literature, gap_evidence_summary)
    evidence_batch = validate_evidence_batch(ideas, literature, llm, facts)

    def _eval_one(idea: dict) -> dict:
        idea_id = str(idea.get("idea_id") or "").strip()
        return _evaluate_idea(
            idea, facts, gap_notes, venue_dist, venue_summary,
            data_feasibility, literature, llm, system_prompt,
            llm_out=eval_batch.get(idea_id) if eval_batch else None,
            ev_validation=evidence_batch.get(idea_id) if evidence_batch else None,
            gap_evidence_levels=gap_evidence_levels,
            contribution_out=contribution_batch.get(idea_id) if contribution_batch else None,
        )

    # M16 方向⑥：多个 idea 的评估并行执行（结果保持 idea 顺序）。
    # 批量命中时各 idea 仅做确定性装配（无 LLM 调用）；批量缺失回退逐条调用时并行提速。
    evaluations = map_parallel(_eval_one, ideas)

    dossier.evaluations = evaluations
    dossier.meta.setdefault("prompt_versions", {})["evaluate"] = version
