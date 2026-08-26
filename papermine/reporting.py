"""M23 — 报告重构：两层报告（Decision Report + Evidence Appendix）。

把报告从「Agent 内部推理的人类可读版」改为「导师给学生的研究建议书」：

- **Layer 1 — Decision Report（默认，精简，≈ 当前 25~35% 信息量）**：
  默认给结论（推荐哪个 idea、为什么、下一步做什么），细节全部藏附录；
- **Layer 2 — Evidence Appendix（完整证据，后置）**：
  完整文献检索 + 证据卡 / gap 挖掘 / 假设 / novelty 完整评分过程 / 攻击测试 / 人类决策。

对应 docs/build-plan.md §4 M23。渲染逻辑从 ``orchestrator.py`` 移入本模块（M23 的归属文件），
编排器通过 ``orchestrator._render_report_md`` 转发到 ``render_report_md``（保留旧入口名，
M9/M9v2/M11/M20/M21 报告渲染与既有单测继续兼容）。

本模块**不改 Dossier 顶层字段、不改冻结接口**（docs/build-plan.md §3.2/§3.3），
只消费各 Agent 已产出的 dossier 数据（literature / ideas / evaluations / roadmap /
human_decisions）并渲染 Markdown；无任何 LLM 或网络调用，离线同样可用。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .agents import evaluate, plan
from .agents.contribution import (
    MATRIX_DIMENSIONS,
    MATRIX_LABELS,
    STRENGTH_LABELS,
    STRENGTH_ORDER,
    render_contribution_lines,
)
from .dossier import Dossier

__all__ = [
    "render_report_md",
    "render_decision_report",
    "render_evidence_appendix",
]


# ---------------------------------------------------------------------------
# 基础小工具
# ---------------------------------------------------------------------------

def _md_text(s: Any) -> str:
    """把任意值折叠成单行文本（去首尾 / 合并空白），供报告渲染安全拼接。"""
    return " ".join(str(s or "").split())


def _clip(s: Any, n: int) -> str:
    """折叠 + 截断（超长加省略号），供 Decision Report 的精简展示。"""
    s = _md_text(s)
    return s[:n] + ("…" if len(s) > n else "")


def _cell(s: Any) -> str:
    """表格单元格：折叠空白 + 转义竖线，避免破坏 Markdown 表格。"""
    return _md_text(s).replace("|", "\\|")


# ---------------------------------------------------------------------------
# 与 literature.py 字段对齐的展示标签
# ---------------------------------------------------------------------------

# M5 v2 文献理解五元组的展示标签（与 literature.py 的 understanding 字段对齐）
_UNDERSTANDING_FIELDS = (
    ("claim", "核心主张"),
    ("method", "方法"),
    ("conclusion", "结论"),
    ("applicability", "适用条件"),
    ("limitations", "局限"),
)

# contradiction_graph.gaps[].type → 中文标签（与 literature.py 对齐）
_GAP_TYPE_LABELS = {"gap": "缺口", "contradiction": "矛盾"}

# M18：gap 假设证据级别 → 中文标签（weak/moderate/strong，与 literature.py 对齐）
_EVIDENCE_LEVEL_LABELS = {"weak": "弱", "moderate": "中", "strong": "强"}

# M19 论文级证据卡字段 → 中文标签（Appendix A 渲染，缺失一律标「—」，绝不编造）
_EVIDENCE_CARD_FIELDS = (
    ("dataset", "数据集"),
    ("baseline", "基线"),
    ("metric", "指标"),
    ("main_gain", "主要提升"),
    ("limitation", "局限"),
    ("claim_strength", "主张强度"),
    ("evidence_source", "证据来源"),
)


def _gap_evidence_level_label(g: Dict[str, Any]) -> str:
    """读取一条 gap/矛盾记录的 evidence_level 中文标签（gap 型在 gap_hypothesis 内，矛盾型在顶层）。"""
    if not isinstance(g, dict):
        return ""
    gh = g.get("gap_hypothesis")
    lv = (gh.get("evidence_level") if isinstance(gh, dict) else None) or g.get("evidence_level")
    lv = _md_text(lv)
    return _EVIDENCE_LEVEL_LABELS.get(lv, lv)


def _render_understanding_lines(paper: Dict[str, Any]) -> List[str]:
    """把单篇论文的结构化理解渲染成嵌套子行；无 understanding 时返回空列表（M9 v2）。"""
    u = paper.get("understanding") if isinstance(paper, dict) else None
    if not isinstance(u, dict):
        return []
    lines: List[str] = []
    for key, label in _UNDERSTANDING_FIELDS:
        text = _md_text(u.get(key))
        if text:
            lines.append("    - {}：{}".format(label, text))
    return lines


def _entry_gap_records(entry: Dict[str, Any]) -> List[Dict[str, Any]]:
    """从文献条目取出 contradiction_graph.gaps（含 gap_id 的记录）；缺失/旧格式返回空。"""
    graph = entry.get("contradiction_graph") if isinstance(entry, dict) else None
    gaps = (graph or {}).get("gaps") if isinstance(graph, dict) else None
    return [g for g in (gaps or []) if isinstance(g, dict) and _md_text(g.get("gap_id"))]


def _entry_hypothesis_records(entry: Dict[str, Any]) -> List[Dict[str, Any]]:
    """从文献条目取出 hypotheses（含 hypothesis_id 的记录）；缺失/旧格式返回空。"""
    hyps = entry.get("hypotheses") if isinstance(entry, dict) else None
    return [h for h in (hyps or []) if isinstance(h, dict) and _md_text(h.get("hypothesis_id"))]


# ---------------------------------------------------------------------------
# M23 专属：推荐程度 / 排名 / 进度条 / 汇总
# ---------------------------------------------------------------------------

# 贡献矩阵强度档 → 5 格进度条（正文只留进度条，完整评分/原因放 Appendix）
_STRENGTH_BLOCKS = {
    "none": "▫▫▫▫▫",
    "low": "█░░░░",
    "medium": "██░░░",
    "medium_high": "███░░",
    "high": "█████",
}

# 贡献类型 → 紧凑标签（候选排名表「类型」列用）
_TYPE_SHORT = {
    "A": "方法创新",
    "B": "框架集成",
    "C": "应用创新",
    "D": "问题建模",
    "E": "训练策略",
}

# verdict → 推荐措辞（候选排名表「推荐」列用）
_VERDICT_LABELS = {"proceed": "推荐", "rework": "可改进", "drop": "不建议"}


def _progress_bar(strength: Any) -> str:
    """贡献矩阵强度 → 进度条文本（如 ``框架创新 █████ 高``，M23 改动 4）。"""
    s = _md_text(strength)
    return "{} {}".format(
        _STRENGTH_BLOCKS.get(s, "·····"), STRENGTH_LABELS.get(s, s or "—")
    )


def _type_short(ev: Optional[Dict[str, Any]]) -> str:
    """候选排名表用的紧凑贡献类型（如 ``B·框架集成``）。"""
    c = ev.get("contribution") if isinstance(ev, dict) else None
    if not isinstance(c, dict) or c.get("type") not in _TYPE_SHORT:
        return "—"
    return "{}·{}".format(c["type"], _TYPE_SHORT[c["type"]])


def _star_rating(ev: Optional[Dict[str, Any]]) -> str:
    """推荐程度（★）：按 verdict + novelty 合成，供 Executive Summary（M23 改动 2）。"""
    if not isinstance(ev, dict):
        return "☆☆☆☆☆（无评估）"
    verdict = ev.get("verdict")
    novelty = ev.get("novelty_score")
    if not isinstance(novelty, (int, float)) or isinstance(novelty, bool):
        novelty = 0.0
    if verdict == "drop":
        return "★☆☆☆☆（不建议）"
    if verdict == "rework":
        return "★★☆☆☆（改进后再议）"
    if float(novelty) >= 80:
        return "★★★★★（强烈推荐）"
    if float(novelty) >= 70:
        return "★★★★☆（推荐）"
    if float(novelty) >= 60:
        return "★★★☆☆（可尝试）"
    return "★★☆☆☆（谨慎）"


def _verdict_label(verdict: Any) -> str:
    return _VERDICT_LABELS.get(_md_text(verdict), _md_text(verdict) or "—")


def _ranked_pairs(dossier: Dossier) -> List[Tuple[dict, Optional[dict]]]:
    """候选 idea 排名：proceed > rework > drop，同级 novelty 降序、workload 升序（与 plan 对齐）。"""
    ideas = [i for i in (dossier.ideas or [])
             if isinstance(i, dict) and _md_text(i.get("idea_id"))]
    ev_map = {_md_text(ev.get("idea_ref")): ev for ev in (dossier.evaluations or [])
              if isinstance(ev, dict) and _md_text(ev.get("idea_ref"))}

    def _key(pair):
        _idea, ev = pair
        verdict = ev.get("verdict") if isinstance(ev, dict) else None
        prio = {"proceed": 0, "rework": 1, "drop": 2}.get(verdict, 1)
        novelty = ev.get("novelty_score") if isinstance(ev, dict) else 0.0
        if not isinstance(novelty, (int, float)) or isinstance(novelty, bool):
            novelty = 0.0
        workload = ev.get("workload_hours") if isinstance(ev, dict) else 0.0
        if not isinstance(workload, (int, float)) or isinstance(workload, bool):
            workload = 0.0
        return (prio, -float(novelty), float(workload))

    pairs = [(i, ev_map.get(_md_text(i.get("idea_id")))) for i in ideas]
    pairs.sort(key=_key)
    return pairs


def _selected_pair(dossier: Dossier) -> Tuple[Optional[dict], Optional[dict]]:
    """(选中 idea, 对应 evaluation)；roadmap.selected_idea 缺失时退回排名第一。"""
    selected_id = _md_text((dossier.roadmap or {}).get("selected_idea"))
    idea_map = {_md_text(i.get("idea_id")): i for i in (dossier.ideas or [])
                if isinstance(i, dict) and _md_text(i.get("idea_id"))}
    ev_map = {_md_text(ev.get("idea_ref")): ev for ev in (dossier.evaluations or [])
              if isinstance(ev, dict) and _md_text(ev.get("idea_ref"))}
    idea = idea_map.get(selected_id)
    ev = ev_map.get(selected_id)
    if idea is None:
        pairs = _ranked_pairs(dossier)
        if pairs:
            idea, ev = pairs[0]
    return idea, ev


def _research_goal(dossier: Dossier) -> str:
    """Project Understanding 的「研究目标」：取第一个问题的 formulation/title。"""
    for p in dossier.problems or []:
        if not isinstance(p, dict):
            continue
        formulation = _md_text(p.get("formulation"))
        if formulation:
            return formulation
        title = _md_text(p.get("title"))
        if title:
            return title
    return "（未抽象出研究目标）"


def _research_questions(dossier: Dossier) -> List[dict]:
    """Decision Report §2 的 3 个核心问题：优先 M22 路线图 RQ，缺失回退 problems。"""
    r = dossier.roadmap or {}
    rqs = [q for q in (r.get("research_questions") or [])
           if isinstance(q, dict) and _md_text(q.get("question"))]
    if rqs:
        return rqs[:3]
    return [p for p in (dossier.problems or [])
            if isinstance(p, dict)
            and (_md_text(p.get("title")) or _md_text(p.get("formulation")))][:3]


def _main_directions(literature: List[dict]) -> List[str]:
    """Literature Landscape 的「主要方向」：gap claim_point 去重 + gap_note 兜底。"""
    dirs: List[str] = []
    for entry in literature or []:
        if not isinstance(entry, dict):
            continue
        for g in _entry_gap_records(entry):
            point = _md_text(g.get("claim_point"))
            if point and point not in dirs:
                dirs.append(point)
    if not dirs:
        for entry in literature or []:
            note = _md_text(entry.get("gap_note")) if isinstance(entry, dict) else ""
            if note and note not in dirs:
                dirs.append(note)
    return dirs[:5] or ["（未识别到明确研究方向）"]


def _evidence_coverage(literature: List[dict]) -> str:
    """Literature Landscape 的「证据覆盖度」：论文数 + 证据来源层级 + gap 证据级别。"""
    entries = [e for e in (literature or []) if isinstance(e, dict)]
    n_papers = 0
    src_count: Dict[str, int] = {}
    gap_levels: List[str] = []
    for entry in entries:
        for p in entry.get("papers") or []:
            if not isinstance(p, dict):
                continue
            n_papers += 1
            card = p.get("evidence_card")
            if isinstance(card, dict):
                src = _md_text(card.get("evidence_source")) or "abstract"
                src_count[src] = src_count.get(src, 0) + 1
        for g in _entry_gap_records(entry):
            lv = _gap_evidence_level_label(g)
            if lv:
                gap_levels.append(lv)
    parts: List[str] = []
    parts.append("共 {} 篇论文".format(n_papers) if n_papers else "无文献（离线/无结果）")
    if src_count:
        parts.append("证据来源：" + "，".join("{}×{}".format(c, s) for s, c in sorted(src_count.items())))
    if gap_levels:
        parts.append("gap 证据级别：" + "、".join(gap_levels))
    return "；".join(parts)


def _risk_items(roadmap: Dict[str, Any], ev: Optional[dict]) -> List[Dict[str, str]]:
    """主要风险来源：优先 M22 风险分支，缺失时回退 M21 攻击测试（attack 即风险）。"""
    r = roadmap or {}
    risks = r.get("risk_branches") or []
    if risks:
        return [rb for rb in risks if isinstance(rb, dict) and _md_text(rb.get("risk"))]
    c = ev.get("contribution") if isinstance(ev, dict) else None
    attacks = c.get("attacks") if isinstance(c, dict) else None
    if isinstance(attacks, dict):
        out: List[Dict[str, str]] = []
        for key in ("ablation", "concatenation", "reviewer"):
            item = attacks.get(key)
            if isinstance(item, dict) and _md_text(item.get("attack")):
                out.append({"risk": _md_text(item["attack"]),
                            "branch": _md_text(item.get("answer")) or "（待定）"})
        return out
    return []


def _recommend_reason(idea: Optional[dict], ev: Optional[dict]) -> str:
    """Executive Summary 的「为什么推荐」：贡献类型 + 强维度 + 证据强度，兜底 novelty 假设。"""
    parts: List[str] = []
    if isinstance(ev, dict):
        c = ev.get("contribution")
        if isinstance(c, dict):
            tl = _md_text(c.get("type_label"))
            if tl:
                parts.append(tl.split("（")[0])
            matrix = c.get("matrix") if isinstance(c.get("matrix"), dict) else {}
            strong = [MATRIX_LABELS.get(d, d) for d in MATRIX_DIMENSIONS
                      if isinstance(matrix.get(d), dict)
                      and STRENGTH_ORDER.get(matrix[d].get("strength"), 0) >= STRENGTH_ORDER["medium"]]
            if strong:
                parts.append("贡献集中在" + "、".join(strong))
        evv = ev.get("evidence_validation")
        if isinstance(evv, dict) and _md_text(evv.get("evidence")):
            parts.append("证据强度 " + _md_text(evv.get("evidence")))
    if not parts and isinstance(idea, dict):
        parts.append(_md_text(idea.get("novelty_hypothesis")) or _md_text(idea.get("claim")))
    return "；".join(p for p in parts if p) or "（需人工复核）"


def _innovation_boundary(ev: Optional[dict]) -> Tuple[List[str], List[str]]:
    """innovation boundary：贡献矩阵中 ≥ 中 的强项 vs < 中 的弱项（正文只给结论）。"""
    strong: List[str] = []
    weak: List[str] = []
    c = ev.get("contribution") if isinstance(ev, dict) else None
    matrix = c.get("matrix") if isinstance(c, dict) else {}
    for d in MATRIX_DIMENSIONS:
        item = matrix.get(d)
        label = MATRIX_LABELS.get(d, d)
        if isinstance(item, dict) and STRENGTH_ORDER.get(item.get("strength"), 0) >= STRENGTH_ORDER["medium"]:
            strong.append(label)
        else:
            weak.append(label)
    return strong, weak


def _next_actions(roadmap: Dict[str, Any]) -> List[str]:
    """Immediate Next Actions（3 条）：优先 MVP must_have，数据缺口时前置采集动作。"""
    r = roadmap or {}
    actions: List[str] = []
    mvp = r.get("minimum_viable_paper")
    if isinstance(mvp, dict):
        actions.extend(_md_text(m) for m in (mvp.get("must_have") or []))
    actions = [a for a in actions if a]
    if not actions:
        stages = r.get("stage_exits") or []
        if stages and isinstance(stages[0], dict):
            actions = [_md_text(t) for t in (stages[0].get("tasks") or []) if _md_text(t)]
    if not actions:
        actions = [
            "准备评测数据并确定评测协议（train/val/test 划分 + 固定随机种子）",
            "复现 2~3 个代表性 baseline（指标对齐文献）",
            "跑通主实验并起草论文大纲（引言/方法/实验/结论）",
        ]
    missing = r.get("missing_items") or []
    if any(k in " ".join(str(m) for m in missing) for k in ("数据", "采集", "标注")):
        actions.insert(0, "回填/采集评测数据（路线图 missing_items 提示数据缺口）")
    return actions[:3]


# ---------------------------------------------------------------------------
# Layer 1 — Decision Report（默认，精简）
# ---------------------------------------------------------------------------

def render_decision_report(dossier: Dossier) -> str:
    """M23 Layer 1 — Decision Report（默认，精简，≈ 当前 25~35% 信息量）。

    目标：导师给学生的研究建议书——默认给结论（推荐哪个、为什么、下一步做什么），
    细节全部藏 Evidence Appendix。
    """
    lines: List[str] = []
    lines.append("# Papermine Research Report")
    lines.append("")
    lines.append("> run_id: {}".format(dossier.meta.get("run_id") or dossier.meta.get("project_id")))
    lines.append("> llm_backend: {}".format(dossier.meta.get("llm_backend") or "（未知）"))
    lines.append("> 本文为**决策版（Decision Report）**；完整证据见文末 **Evidence Appendix**。")
    lines.append("")

    roadmap = dossier.roadmap or {}
    idea, ev = _selected_pair(dossier)

    # ---- 0. Executive Summary ----
    lines.append("## 0. Executive Summary")
    lines.append("")
    if idea is not None:
        lines.append("- **推荐方向**：{}".format(_clip(idea.get("claim"), 80)))
    else:
        lines.append("- **推荐方向**：（无候选创新点）")
    lines.append("- **推荐程度**：{}".format(_star_rating(ev)))
    lines.append("- **论文类型**：{}".format(_md_text(roadmap.get("paper_type")) or "（未定）"))
    if isinstance(ev, dict) and ev.get("workload_hours") is not None:
        lines.append("- **工作量**：约 {} h".format(ev.get("workload_hours")))
    else:
        lines.append("- **工作量**：（未评估）")
    evv = ev.get("evidence_validation") if isinstance(ev, dict) else None
    lines.append("- **证据强度**：{}".format(
        _md_text(evv.get("evidence")) if isinstance(evv, dict) else "（未评估）"))
    risks = _risk_items(roadmap, ev)
    lines.append("- **主要风险**：{}".format(
        _clip(risks[0].get("risk"), 60) if risks else "（见路线图风险分支）"))
    lines.append("- **为什么推荐**：{}".format(_recommend_reason(idea, ev)))
    lines.append("- **当前最重要的 3 个动作**：")
    for i, action in enumerate(_next_actions(roadmap), 1):
        lines.append("  {}. {}".format(i, action))
    lines.append("")

    # ---- 1. Project Understanding ----
    lines.append("## 1. Project Understanding")
    lines.append("")
    lines.append("- 项目叙事：{}".format(
        _clip((dossier.assets or {}).get("narrative"), 200) or "（无）"))
    lines.append("- 研究目标：{}".format(_research_goal(dossier)))
    lines.append("")

    # ---- 2. Research Questions ----
    lines.append("## 2. Research Questions")
    lines.append("")
    rqs = _research_questions(dossier)
    if rqs:
        for q in rqs:
            if "question" in q:  # roadmap RQ
                seg = "- {}：{}".format(_md_text(q.get("id")) or "RQ", _md_text(q.get("question")))
                targets = [t for t in (q.get("target_experiments") or []) if _md_text(t)]
                if targets:
                    seg += "（→ {}）".format("、".join(targets))
                lines.append(seg)
            else:  # problem 兜底
                lines.append("- {}：{}".format(
                    _md_text(q.get("problem_id")) or _md_text(q.get("title")),
                    _md_text(q.get("formulation")) or _md_text(q.get("title"))))
    else:
        lines.append("（无）")
    lines.append("")

    # ---- 3. Literature Landscape ----
    lines.append("## 3. Literature Landscape")
    lines.append("")
    papers: List[dict] = []
    for entry in dossier.literature or []:
        if isinstance(entry, dict):
            papers.extend(p for p in (entry.get("papers") or []) if isinstance(p, dict))
    if papers:
        lines.append("- 关键论文：")
        for p in papers[:8]:
            title = _md_text(p.get("title"))
            if not title:
                continue
            meta = []
            venue = _md_text(p.get("venue"))
            year = p.get("year")
            if venue:
                meta.append(venue)
            if year is not None and _md_text(year):
                meta.append(_md_text(year))
            lines.append("  - {}{}".format(title, "（{}）".format("，".join(meta)) if meta else ""))
    else:
        lines.append("- 关键论文：（离线/无结果）")
    lines.append("- 主要方向：{}".format("、".join(_main_directions(dossier.literature))))
    lines.append("- 证据覆盖度：{}".format(_evidence_coverage(dossier.literature)))
    lines.append("")

    # ---- 4. Candidate Ideas ----
    lines.append("## 4. Candidate Ideas")
    lines.append("")
    pairs = _ranked_pairs(dossier)
    if pairs:
        lines.append("| Idea | 类型 | Novelty | Evidence | Feasibility | 推荐 |")
        lines.append("|---|---|---|---|---|---|")
        for idea_i, ev_i in pairs:
            evv_i = ev_i.get("evidence_validation") if isinstance(ev_i, dict) else None
            novelty = ev_i.get("novelty_score") if isinstance(ev_i, dict) else "—"
            evidence = _md_text(evv_i.get("evidence")) if isinstance(evv_i, dict) else "—"
            feasibility = _md_text(ev_i.get("data_feasibility")) if isinstance(ev_i, dict) else "—"
            verdict = ev_i.get("verdict") if isinstance(ev_i, dict) else None
            lines.append("| {} {} | {} | {} | {} | {} | {} |".format(
                _cell(idea_i.get("idea_id")), _cell(_clip(idea_i.get("claim"), 40)),
                _cell(_type_short(ev_i)), _cell(novelty), _cell(evidence),
                _cell(feasibility), _cell(_verdict_label(verdict))))
        lines.append("")
        lines.append("> 继续阅读 `iN` 的详细证据（贡献矩阵 / 攻击测试 / novelty 评分过程）→ 见 Appendix D/E。")
    else:
        lines.append("（无）")
    lines.append("")

    # ---- 5. Recommended Idea ----
    lines.append("## 5. Recommended Idea")
    lines.append("")
    if idea is not None:
        lines.append("- 创新点：{}".format(_md_text(idea.get("claim"))))
        c = ev.get("contribution") if isinstance(ev, dict) else None
        lines.append("- 贡献类型：{}".format(
            _md_text(c.get("type_label")) if isinstance(c, dict) else "（未分类）"))
        strong, weak = _innovation_boundary(ev)
        lines.append("- innovation boundary：论文只主张强项（{}）；弱项（{}）留作 limitation，不主张".format(
            "、".join(strong) or "—", "、".join(weak) or "—"))
        matrix = c.get("matrix") if isinstance(c, dict) else None
        if isinstance(matrix, dict) and matrix:
            lines.append("- 贡献矩阵（进度条；完整评分过程见 Appendix D/E）：")
            for d in MATRIX_DIMENSIONS:
                item = matrix.get(d)
                if isinstance(item, dict):
                    lines.append("  - {} {}".format(MATRIX_LABELS.get(d, d), _progress_bar(item.get("strength"))))
        risks = _risk_items(roadmap, ev)
        if risks:
            lines.append("- 主要风险：")
            for rb in risks[:2]:
                lines.append("  - {} → {}".format(
                    _md_text(rb.get("risk")), _md_text(rb.get("branch")) or "（待定）"))
        evv = ev.get("evidence_validation") if isinstance(ev, dict) else None
        lines.append("- 证据强度：{}".format(
            _md_text(evv.get("evidence")) if isinstance(evv, dict) else "（未评估）"))
    else:
        lines.append("（无候选创新点）")
    lines.append("")

    # ---- 6. Paper Roadmap ----
    lines.append("## 6. Paper Roadmap")
    lines.append("")
    m22_lines = plan.render_roadmap_lines(roadmap)
    if m22_lines:
        lines.extend(m22_lines)
    else:
        # 旧格式 roadmap（无 core_story）兜底
        lines.append("- 选中创新点：{}".format(roadmap.get("selected_idea")))
        lines.append("- 论文类型：{}".format(_md_text(roadmap.get("paper_type")) or "（未定）"))
        outline = roadmap.get("outline") or []
        if outline:
            lines.append("- 大纲：")
            for o in outline:
                lines.append("  - {}".format(o))
    missing = roadmap.get("missing_items") or []
    if missing:
        lines.append("- 待回填缺口（数据/指标）：")
        for m in missing:
            lines.append("  - {}".format(m))
    lines.append("")

    # ---- 7. Immediate Next Actions ----
    lines.append("## 7. Immediate Next Actions")
    lines.append("")
    for i, action in enumerate(_next_actions(roadmap), 1):
        lines.append("{}. {}".format(i, action))
    lines.append("")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Layer 2 — Evidence Appendix（完整证据，后置）
# ---------------------------------------------------------------------------

def _render_idea_full(idea: Optional[dict], ev: Optional[dict]) -> List[str]:
    """Appendix D：单条候选 idea 的完整证据（来源追溯 + novelty 评分过程 + 校准链路 + 证据强度）。

    - ``idea`` 可为 None（仅评估、无 idea 条目时只渲染评估）；
    - ``ev`` 可为 None（idea 尚未评估时只渲染来源追溯 + 「未评估」）。
    """
    lines: List[str] = []
    iid = _md_text((ev or {}).get("idea_ref")) or _md_text((idea or {}).get("idea_id"))
    claim = _md_text((idea or {}).get("claim"))
    if claim:
        lines.append("### {}：{}".format(iid or "（未命名）", claim))
    else:
        lines.append("### {}".format(iid or "（未命名）"))
    if idea is not None:
        gap_refs = [g for g in (idea.get("gap_refs") or []) if _md_text(g)]
        if gap_refs:
            lines.append("- 来源缺口：{}".format("、".join(gap_refs)))
        hyp_refs = [h for h in (idea.get("hypothesis_refs") or []) if _md_text(h)]
        if hyp_refs:
            lines.append("- 来源假设：{}".format("、".join(hyp_refs)))
        lit_refs = [t for t in (idea.get("literature_refs") or []) if _md_text(t)]
        if lit_refs:
            lines.append("- 文献引用：{}".format("、".join(lit_refs)))
    if ev is None:
        lines.append("- （未评估）")
        return lines
    novelty_disp = str(ev.get("novelty_score"))
    band = ev.get("novelty_band")
    if band:
        novelty_disp += "（{}）".format(band)
    lines.append("- novelty（总分）={}，数据可得性={}，工作量≈{}h，verdict={}，档位={}".format(
        novelty_disp, _md_text(ev.get("data_feasibility")) or "—",
        ev.get("workload_hours"), _md_text(ev.get("verdict")) or "—",
        _md_text(ev.get("venue_guess")) or "—"))
    dims = ev.get("novelty_dimensions")
    cal_lines = evaluate.render_calibration_lines(ev)
    if cal_lines:
        # M20：有完整校准链路时只展示校准（已含各维度得分与推导），不再重复「分维度明细」块
        lines.append("- 评分校准（问题 → 答案 → 规则 → 得分，分数由规则算出）：")
        lines.extend(cal_lines)
    elif isinstance(dims, dict) and dims:
        # 旧格式评估（无 calibration）：仅展示分维度汇总
        lines.append("- 分维度明细（各 0~5，加权合成 novelty 总分）：")
        for key, label, weight in evaluate.NOVELTY_DIMENSIONS:
            item = dims.get(key)
            if isinstance(item, dict):
                reason = _md_text(item.get("reason"))
                seg = "  - {}（权重{}）：{}".format(label, weight, item.get("score"))
                if reason:
                    seg += " — " + reason
                lines.append(seg)
    evv = ev.get("evidence_validation")
    if isinstance(evv, dict) and _md_text(evv.get("evidence")):
        lines.append("- 证据强度：{}".format(_md_text(evv.get("evidence"))))
        reason = _md_text(evv.get("reason"))
        if reason:
            lines.append("  - 理由：{}".format(reason))
        checks = evv.get("checks")
        if isinstance(checks, dict) and checks:
            lines.append("  - 证据审查维度：")
            for key, label in evaluate.CHECK_DIMENSIONS:
                item = checks.get(key)
                if isinstance(item, dict):
                    status = _md_text(item.get("status"))
                    note = _md_text(item.get("note"))
                    if status and note:
                        lines.append("    - {}（{}）：{}".format(label, status, note))
    if ev.get("rework_reason"):
        lines.append("- 回炉原因：{}".format(_md_text(ev.get("rework_reason"))))
    return lines


def render_evidence_appendix(dossier: Dossier) -> str:
    """M23 Layer 2 — Evidence Appendix（完整证据，后置）。

    A. 文献证据 / B. Gap 挖掘 / C. 假设 / D. novelty 完整评分 / E. 攻击测试 / F. 人类决策。
    """
    lines: List[str] = []
    lines.append("---")
    lines.append("")
    lines.append("# Evidence Appendix（完整证据）")
    lines.append("")

    # ---- A. Literature Evidence ----
    lines.append("## A. Literature Evidence")
    lines.append("")
    if dossier.literature:
        for lit in dossier.literature:
            if not isinstance(lit, dict):
                continue
            lines.append("### query：{}".format(_md_text(lit.get("query")) or "（未命名查询）"))
            sources = lit.get("sources") or []
            lines.append("- 来源：{}".format("、".join(str(s) for s in sources) if sources else "未知"))
            papers = lit.get("papers") or []
            if papers:
                for p in papers:
                    if not isinstance(p, dict):
                        continue
                    title = _md_text(p.get("title"))
                    if not title:
                        continue
                    venue = _md_text(p.get("venue"))
                    year = p.get("year")
                    meta = []
                    if venue:
                        meta.append(venue)
                    if year is not None and _md_text(year):
                        meta.append(_md_text(year))
                    lines.append("- {}{}".format(title, "（{}）".format("，".join(meta)) if meta else ""))
                    lines.extend(_render_understanding_lines(p))
                    card = p.get("evidence_card")
                    if isinstance(card, dict):
                        lines.append("    - 证据卡：")
                        for key, label in _EVIDENCE_CARD_FIELDS:
                            val = _md_text(card.get(key))
                            lines.append("      - {}：{}".format(label, val or "—"))
            else:
                lines.append("- 离线/无结果")
            gap_note = _md_text(lit.get("gap_note"))
            if gap_note:
                lines.append("- gap_note：{}".format(gap_note))
            lines.append("")
    else:
        lines.append("（离线/无结果）")
        lines.append("")
    lines.append("")

    # ---- B. Gap Mining ----
    lines.append("## B. Gap Mining")
    lines.append("")
    gap_table: List[Tuple[dict, str, str, int]] = []
    for entry in dossier.literature or []:
        if not isinstance(entry, dict):
            continue
        n_papers = len([p for p in (entry.get("papers") or []) if isinstance(p, dict)])
        for g in _entry_gap_records(entry):
            gh = g.get("gap_hypothesis")
            claim = _md_text(gh.get("claim")) if isinstance(gh, dict) else _md_text(g.get("claim_point"))
            gap_table.append((g, claim or "—", _gap_evidence_level_label(g) or "—", n_papers))
    if gap_table:
        lines.append("| Gap | 研究空白假设 | Evidence | Coverage |")
        lines.append("|---|---|---|---|")
        for g, claim, lv, n_papers in gap_table:
            coverage = "{} papers".format(n_papers) if n_papers else "0 papers"
            lines.append("| {} | {} | {} | {} |".format(
                _cell(g.get("gap_id")) or "—", _cell(claim), _cell(lv), _cell(coverage)))
        lines.append("")
        lines.append("完整依据：")
        for g, _claim, _lv, _n in gap_table:
            gtype = _md_text(g.get("type"))
            label = _GAP_TYPE_LABELS.get(gtype, "缺口")
            point = _md_text(g.get("claim_point")) or _md_text(g.get("angle")) or "（未命名结论点）"
            lines.append("- {} {}：{}".format(label, _md_text(g.get("gap_id")) or "—", point))
            desc = _md_text(g.get("description"))
            if desc:
                lines.append("  - 描述：{}".format(desc))
            if gtype == "contradiction":
                refs = [t for t in (g.get("paper_refs") or []) if _md_text(t)]
                if refs:
                    lines.append("  - 冲突双方：{}".format(" ⇄ ".join(refs)))
            else:
                angle = _md_text(g.get("angle"))
                if angle and angle != point:
                    lines.append("  - 缺口角度：{}".format(angle))
                gh = g.get("gap_hypothesis")
                if isinstance(gh, dict):
                    lines.append("  - 假设：{}".format(_md_text(gh.get("claim")) or "—"))
                    lines.append("  - 依据：{}".format(_md_text(gh.get("basis")) or "—"))
                    scope = _md_text(gh.get("scope"))
                    if scope:
                        lines.append("  - {}".format(scope))
    else:
        lines.append("（无）")
    lines.append("")

    # ---- C. Hypotheses ----
    lines.append("## C. Hypotheses")
    lines.append("")
    _hyp_to_ideas: Dict[str, List[str]] = {}
    for idea in dossier.ideas or []:
        if not isinstance(idea, dict):
            continue
        iid = _md_text(idea.get("idea_id"))
        if not iid:
            continue
        for href in (idea.get("hypothesis_refs") or []):
            h = _md_text(href)
            if h:
                _hyp_to_ideas.setdefault(h, []).append(iid)
    _hyp_rendered = False
    for entry in dossier.literature or []:
        if not isinstance(entry, dict):
            continue
        hyps = _entry_hypothesis_records(entry)
        if not hyps:
            continue
        lines.append("- query：{}".format(_md_text(entry.get("query")) or "（未命名查询）"))
        for h in hyps:
            hid = _md_text(h.get("hypothesis_id"))
            lines.append("  - {}：{}".format(hid, _md_text(h.get("statement")) or "（无陈述）"))
            gap_ref = _md_text(h.get("gap_ref"))
            if gap_ref:
                lines.append("    - 来自 {}".format(gap_ref))
            fals = _md_text(h.get("falsification"))
            if fals:
                lines.append("    - 可证伪条件：{}".format(fals))
            ideas = _hyp_to_ideas.get(hid) or []
            if ideas:
                lines.append("    - 催生的 idea：{}".format("、".join(ideas)))
        _hyp_rendered = True
    if not _hyp_rendered:
        lines.append("（无）")
    lines.append("")

    # ---- D. Full Novelty Evaluation ----
    lines.append("## D. Full Novelty Evaluation")
    lines.append("")
    if dossier.evaluations or dossier.ideas:
        idea_map = {_md_text(i.get("idea_id")): i for i in (dossier.ideas or [])
                    if isinstance(i, dict) and _md_text(i.get("idea_id"))}
        seen: set = set()
        for ev in dossier.evaluations:
            if not isinstance(ev, dict):
                continue
            iid = _md_text(ev.get("idea_ref"))
            seen.add(iid)
            lines.extend(_render_idea_full(idea_map.get(iid), ev))
        # 未被评估的 idea 也给出来源追溯（M5 v2 关联可追溯，即便离线无评估）
        for idea in dossier.ideas or []:
            if not isinstance(idea, dict):
                continue
            iid = _md_text(idea.get("idea_id"))
            if iid and iid not in seen:
                lines.extend(_render_idea_full(idea, None))
    else:
        lines.append("（无）")
    lines.append("")

    # ---- E. Attack Tests ----
    lines.append("## E. Attack Tests")
    lines.append("")
    _attack_rendered = False
    if dossier.evaluations:
        for ev in dossier.evaluations:
            if not isinstance(ev, dict):
                continue
            contrib_lines = render_contribution_lines(ev)
            if contrib_lines:
                lines.extend(contrib_lines)
                _attack_rendered = True
    if not _attack_rendered:
        lines.append("（无）")
    lines.append("")

    # ---- F. Human Decisions ----
    lines.append("## F. Human Decisions")
    lines.append("")
    if dossier.human_decisions:
        for d in dossier.human_decisions:
            lines.append("- {}：{}（{}）".format(
                d.get("checkpoint"), d.get("decision"), d.get("note") or ""))
    else:
        lines.append("（无）")
    lines.append("")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# 总入口（两层报告 = Decision Report + Evidence Appendix）
# ---------------------------------------------------------------------------

def render_report_md(dossier: Dossier) -> str:
    """两层报告：Decision Report（默认，前）+ Evidence Appendix（完整证据，后）。

    M23 报告重构核心原则：**默认给结论，细节藏附录**。
    """
    return render_decision_report(dossier) + render_evidence_appendix(dossier)
