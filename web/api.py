"""M24 — FastAPI 路由：把 Python 核心暴露成 REST API（围绕 Dossier）。

对应 docs/build-plan.md §4 M24 与 docs/web-demo.md「REST API（围绕 Dossier）」：

查询：
    GET /projects/{id}                 —— Dossier 的 Decision Report 数据
    GET /projects/{id}/ideas           —— 候选创新点（含对应评估）
    GET /projects/{id}/literature      —— 文献（query / papers / gap / 理解 / 证据卡）
    GET /projects/{id}/gaps            —— 展平的 gap/矛盾列表（含证据级别 / coverage）
    GET /projects/{id}/roadmap         —— 论文路线图（M22 七部分）
    GET /projects/{id}/history         —— 人类决策 + dossier 历史快照 + 状态

操作（**模块化重跑**，只跑受影响环节，不整个 pipeline 重跑）：
    POST /projects                     —— 新建项目并端到端分析（body: {project_dir}）
    POST /projects/{id}/analyze        —— 续跑/重跑一个项目
    POST /projects/{id}/ideas/{iid}/refine       —— 单 idea 细化（带 idea/literature_refs/gap/evaluation 上下文）
    POST /projects/{id}/ideas/{iid}/evaluate     —— 单 idea 评估（M11/M12/M18/M20/M21）
    POST /projects/{id}/gaps/{gid}/retrieve-more —— 只为该 gap 检索更多文献 → gap 证据级别更新 → 评估更新

为兼容任务卡里 ``/ideas``、``/literature`` … 与 ``POST /ideas/{id}/refine`` 的简写，
同时提供「当前项目」别名路由：项目由 ``X-Project-Id`` 请求头或 ``project_id`` 查询参数指定，
缺省回退到最近一次 run（本地单项目 demo 的便捷约定）。

设计约束：
- **薄封装**：不改 Python 核心的任何接口契约（docs/build-plan.md §3）；只组合核心既有
  primitives（``dossier`` / ``orchestrator`` / ``evaluate`` / ``literature`` / ``retrieval``）。
- 操作端点无 key / 网络失败时走确定性降级，绝不抛 500（对应 architecture §7/§8 与
  「无 key 就崩」不可接受的交付判据）。
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, HTTPException, Request
from papermine import literature, orchestrator, reporting, storage
from papermine.agents import evaluate
from papermine.dossier import Dossier
from papermine.llm import LLMError, SchemaError, get_provider
from papermine.retrieval import search_literature

router = APIRouter()

__all__ = ["router", "REFINE_SCHEMA"]


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _load_dossier(project_id: str) -> Dossier:
    """按 project_id（= run_id）加载 Dossier；缺失抛 404。"""
    run_dir = storage.run_dir(project_id)
    if not (run_dir / "dossier.json").exists():
        raise HTTPException(status_code=404, detail="项目不存在：{}".format(project_id))
    return Dossier.load(run_dir)


def _commit(dossier: Dossier) -> None:
    """与编排器一致的落盘：递增版本 -> 写 dossier.json -> 写历史快照（engineering.md §3.1）。"""
    dossier.bump_version()
    dossier.save(dossier._run_dir)
    dossier.snapshot()


def _status_or_none(project_id: str) -> Optional[dict]:
    try:
        return orchestrator.status(project_id)
    except FileNotFoundError:
        return None


def _latest_run_id() -> Optional[str]:
    """最近一次 run（按 dossier.json 修改时间倒序）；无任何 run 返回 None。"""
    runs_dir = storage.layout()["runs"]
    try:
        dirs = [d for d in runs_dir.iterdir()
                if d.is_dir() and (d / "dossier.json").exists()]
    except OSError:
        return None
    if not dirs:
        return None
    dirs.sort(key=lambda d: (d / "dossier.json").stat().st_mtime, reverse=True)
    return dirs[0].name


def _resolve_project(request: Request) -> str:
    """别名路由的「当前项目」解析：X-Project-Id 头 > project_id 查询参数 > 最近一次 run。"""
    pid = request.headers.get("X-Project-Id") or request.query_params.get("project_id")
    if pid and str(pid).strip():
        return str(pid).strip()
    latest = _latest_run_id()
    if not latest:
        raise HTTPException(status_code=404, detail="未指定项目且本地无可用 run")
    return latest


# ---------------------------------------------------------------------------
# 读取工具：idea / gap / 上下文
# ---------------------------------------------------------------------------

def _idea_index(dossier: Dossier, idea_id: str) -> Optional[int]:
    for idx, i in enumerate(dossier.ideas or []):
        if isinstance(i, dict) and str(i.get("idea_id")) == str(idea_id):
            return idx
    return None


def _find_gap(dossier: Dossier, gap_id: str):
    """返回 (文献条目下标, gap 下标, 条目, gap 记录)；找不到返回 None。"""
    for e_idx, entry in enumerate(dossier.literature or []):
        if not isinstance(entry, dict):
            continue
        graph = entry.get("contradiction_graph") or {}
        gaps = graph.get("gaps") if isinstance(graph, dict) else None
        for g_idx, g in enumerate(gaps or []):
            if isinstance(g, dict) and str(g.get("gap_id")) == str(gap_id):
                return e_idx, g_idx, entry, g
    return None


def _evaluation_for(dossier: Dossier, idea_id: str) -> Optional[dict]:
    for ev in dossier.evaluations or []:
        if isinstance(ev, dict) and str(ev.get("idea_ref")) == str(idea_id):
            return ev
    return None


def _cited_papers(dossier: Dossier, idea: dict) -> List[dict]:
    """把 idea.literature_refs（论文标题）解析为真实论文对象（provenance 强制，不编造）。"""
    refs = {str(r).strip() for r in (idea.get("literature_refs") or []) if str(r).strip()}
    out: List[dict] = []
    for entry in dossier.literature or []:
        if not isinstance(entry, dict):
            continue
        for p in entry.get("papers") or []:
            if isinstance(p, dict) and str(p.get("title") or "").strip() in refs:
                out.append(p)
    return out


def _referenced_gaps(dossier: Dossier, idea: dict) -> List[dict]:
    """把 idea.gap_refs 解析为 gap 记录。"""
    ids = {str(r).strip() for r in (idea.get("gap_refs") or []) if str(r).strip()}
    out: List[dict] = []
    for entry in dossier.literature or []:
        if not isinstance(entry, dict):
            continue
        graph = entry.get("contradiction_graph") or {}
        for g in (graph.get("gaps") or []):
            if isinstance(g, dict) and str(g.get("gap_id")) in ids:
                out.append(g)
    return out


# ---------------------------------------------------------------------------
# 项目 payload
# ---------------------------------------------------------------------------

def _project_payload(project_id: str) -> dict:
    dossier = _load_dossier(project_id)
    return {
        "project_id": project_id,
        "run_id": dossier.meta.get("run_id") or project_id,
        "status": _status_or_none(project_id),
        # Decision Report（默认精简版）；完整证据见 dossier + Appendix 渲染
        "decision_report": reporting.render_decision_report(dossier),
        "dossier": dossier.to_dict(),
    }


def _ideas_payload(dossier: Dossier) -> dict:
    ev_map = {str(ev.get("idea_ref")): ev for ev in (dossier.evaluations or [])
              if isinstance(ev, dict) and ev.get("idea_ref")}
    ideas = []
    for i in (dossier.ideas or []):
        if not isinstance(i, dict) or not i.get("idea_id"):
            continue
        ideas.append({"idea": i, "evaluation": ev_map.get(str(i.get("idea_id")))})
    return {"ideas": ideas}


def _gaps_payload(dossier: Dossier) -> dict:
    out: List[dict] = []
    for entry in dossier.literature or []:
        if not isinstance(entry, dict):
            continue
        graph = entry.get("contradiction_graph") or {}
        gaps = graph.get("gaps") if isinstance(graph, dict) else None
        n_papers = len([p for p in (entry.get("papers") or []) if isinstance(p, dict)])
        for g in (gaps or []):
            if not isinstance(g, dict) or not g.get("gap_id"):
                continue
            out.append({
                "gap_id": g.get("gap_id"),
                "type": g.get("type"),
                "claim_point": g.get("claim_point"),
                "description": g.get("description"),
                "angle": g.get("angle"),
                "paper_refs": g.get("paper_refs"),
                "evidence_level": literature._gap_evidence_level(g),
                "gap_hypothesis": g.get("gap_hypothesis"),
                "query": entry.get("query"),
                "coverage": n_papers,
                "sources": entry.get("sources"),
            })
    return {"gaps": out}


def _history_payload(project_id: str, dossier: Dossier) -> dict:
    run_dir = storage.run_dir(project_id)
    snapshots: List[dict] = []
    hist_dir = run_dir / "dossier.history"
    if hist_dir.is_dir():
        for f in sorted(hist_dir.iterdir()):
            if not f.name.endswith(".json"):
                continue
            try:
                data = storage.load_json(f, "dossier")
            except Exception:
                continue
            data.pop("_schema", None)
            data.pop("_schema_version", None)
            evs = data.get("evaluations") or []
            snapshots.append({
                "file": f.name,
                "version": (data.get("meta") or {}).get("version"),
                "evaluations": [
                    {"idea_ref": ev.get("idea_ref"), "novelty_score": ev.get("novelty_score"),
                     "verdict": ev.get("verdict")}
                    for ev in evs if isinstance(ev, dict)
                ],
            })
    return {
        "project_id": project_id,
        "status": _status_or_none(project_id),
        "human_decisions": dossier.human_decisions or [],
        "snapshots": snapshots,
    }


# ---------------------------------------------------------------------------
# 查询路由（canonical，project 范围）
# ---------------------------------------------------------------------------

@router.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "papermine-web"}


@router.get("/projects")
def list_projects() -> dict:
    runs_dir = storage.layout()["runs"]
    try:
        dirs = sorted([d for d in runs_dir.iterdir() if (d / "dossier.json").exists()],
                      key=lambda d: (d / "dossier.json").stat().st_mtime, reverse=True)
    except OSError:
        dirs = []
    items = []
    for d in dirs:
        st = _status_or_none(d.name) or {}
        items.append({
            "project_id": d.name,
            "state": st.get("state"),
            "updated_at": st.get("updated_at"),
        })
    return {"projects": items}


@router.get("/projects/{project_id}")
def get_project(project_id: str) -> dict:
    return _project_payload(project_id)


@router.get("/projects/{project_id}/ideas")
def get_ideas(project_id: str) -> dict:
    return _ideas_payload(_load_dossier(project_id))


@router.get("/projects/{project_id}/ideas/{idea_id}")
def get_idea(project_id: str, idea_id: str) -> dict:
    dossier = _load_dossier(project_id)
    idx = _idea_index(dossier, idea_id)
    if idx is None:
        raise HTTPException(status_code=404, detail="idea 不存在：{}".format(idea_id))
    return {"idea": dossier.ideas[idx], "evaluation": _evaluation_for(dossier, idea_id)}


@router.get("/projects/{project_id}/literature")
def get_literature(project_id: str) -> dict:
    return {"literature": _load_dossier(project_id).literature or []}


@router.get("/projects/{project_id}/gaps")
def get_gaps(project_id: str) -> dict:
    return _gaps_payload(_load_dossier(project_id))


@router.get("/projects/{project_id}/roadmap")
def get_roadmap(project_id: str) -> dict:
    return {"roadmap": _load_dossier(project_id).roadmap or {}}


@router.get("/projects/{project_id}/history")
def get_history(project_id: str) -> dict:
    dossier = _load_dossier(project_id)
    return _history_payload(project_id, dossier)


# ---------------------------------------------------------------------------
# 操作路由
# ---------------------------------------------------------------------------

@router.post("/projects")
def create_project(payload: dict = Body(...)) -> dict:
    project_dir = str(payload.get("project_dir") or "").strip()
    if not project_dir:
        raise HTTPException(status_code=400, detail="缺少 project_dir")
    if not os.path.isdir(project_dir):
        raise HTTPException(status_code=400, detail="项目目录不存在：{}".format(project_dir))
    run_id = orchestrator.run_pipeline(project_dir, auto=True)
    return _project_payload(run_id)


@router.post("/projects/{project_id}/analyze")
def analyze_project(project_id: str) -> dict:
    _load_dossier(project_id)  # 404 若项目不存在
    try:
        orchestrator.resume(project_id, auto=True)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="项目缺少运行状态，无法续跑：{}".format(project_id))
    return _project_payload(project_id)


# ---------------------------------------------------------------------------
# 单 idea 细化（refine）——带 idea / literature_refs / gap / evaluation 上下文
# ---------------------------------------------------------------------------

REFINE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["claim", "novelty_hypothesis"],
    "properties": {
        "claim": {"type": "string"},
        "novelty_hypothesis": {"type": "string"},
    },
}

_REFINE_SYSTEM = (
    "你是 papermine 的「创新点细化 Agent」。给定一个候选创新点（idea）及其当前上下文"
    "（引用的文献 / 来源 gap / 已有评估结论），对它做**单点细化**：\n"
    "1. 把 claim 收紧为可检验、范围明确的主张（明确适用场景、对比对象、机制差异）；\n"
    "2. 把 novelty_hypothesis 重写为可证伪的差异假设（说明相对现有工作多了什么机制/角度，"
    "而非仅换场景）；\n"
    "3. 只基于给定文献与 gap 信息，不编造新的引用、不虚构文献；literature_refs 保持原样"
    "（不得新增未给出的论文）。\n"
    "只输出 JSON，严格满足给定 schema。"
)


def _build_refine_prompt(idea: dict, papers: List[dict],
                         gaps: List[dict], ev: Optional[dict]) -> str:
    payload = {
        "idea": {k: idea.get(k) for k in (
            "idea_id", "claim", "novelty_hypothesis", "problem_ref",
            "literature_refs", "gap_refs", "hypothesis_refs")},
        "literature": [
            {
                "title": p.get("title"),
                "abstract": (p.get("abstract") or "")[:500],
                "understanding": p.get("understanding"),
                "evidence_card": p.get("evidence_card"),
            }
            for p in papers
        ],
        "gaps": [
            {k: g.get(k) for k in ("gap_id", "type", "claim_point", "description", "angle")}
            for g in gaps
        ],
        "evaluation": {
            "verdict": ev.get("verdict"),
            "rework_reason": ev.get("rework_reason"),
            "evidence_validation": ev.get("evidence_validation"),
        } if ev else None,
    }
    return (
        "以下是一个候选创新点及其当前上下文，请做单点细化（收紧 claim + 重写可证伪的 "
        "novelty_hypothesis）：\n" + json.dumps(payload, ensure_ascii=False)
    )


def _refine_with_llm(llm: Any, idea: dict, papers: List[dict],
                     gaps: List[dict], ev: Optional[dict]) -> dict:
    if llm is None:
        return {}
    try:
        result = llm.complete(
            _REFINE_SYSTEM, _build_refine_prompt(idea, papers, gaps, ev),
            REFINE_SCHEMA, temperature=0.4,
        )
    except (LLMError, SchemaError):
        return {}
    return result if isinstance(result, dict) else {}


def _deterministic_refine(idea: dict, ev: Optional[dict]) -> dict:
    """无 LLM 时的确定性细化：沿用评估结论里的「如何强化」提示补进 novelty_hypothesis。"""
    claim = str(idea.get("claim") or "").strip()
    hyp = str(idea.get("novelty_hypothesis") or "").strip()
    hint = ""
    if isinstance(ev, dict):
        hint = str(ev.get("rework_reason") or "").strip()
        evv = ev.get("evidence_validation")
        if not hint and isinstance(evv, dict):
            hint = str(evv.get("reason") or "").strip()
    if hint:
        hyp = hyp.rstrip("。") + "。" if hyp else "（待定）"
        hyp += "细化方向：{}（离线确定性细化）。".format(hint)
    else:
        suffix = "（离线确定性细化：需人工进一步明确与已有工作的区别与可验证条件）"
        hyp = (hyp + suffix) if hyp else "（离线确定性细化，需人工补充）"
    return {"claim": claim or "（待细化）", "novelty_hypothesis": hyp}


@router.post("/projects/{project_id}/ideas/{idea_id}/refine")
def refine_idea(project_id: str, idea_id: str) -> dict:
    dossier = _load_dossier(project_id)
    idx = _idea_index(dossier, idea_id)
    if idx is None:
        raise HTTPException(status_code=404, detail="idea 不存在：{}".format(idea_id))
    idea = dossier.ideas[idx]

    papers = _cited_papers(dossier, idea)
    gaps = _referenced_gaps(dossier, idea)
    ev = _evaluation_for(dossier, idea_id)

    raw = _refine_with_llm(get_provider(), idea, papers, gaps, ev)
    claim = str(raw.get("claim") or "").strip()
    hyp = str(raw.get("novelty_hypothesis") or "").strip()
    degraded = False
    if not (claim and hyp):
        det = _deterministic_refine(idea, ev)
        claim = claim or det["claim"]
        hyp = hyp or det["novelty_hypothesis"]
        degraded = True

    before = {"claim": idea.get("claim"), "novelty_hypothesis": idea.get("novelty_hypothesis")}
    idea["claim"] = claim
    idea["novelty_hypothesis"] = hyp
    idea["status"] = "refined"
    history = idea.setdefault("history", [])
    history.append({
        "ts": _now(), "action": "refine",
        "before": before, "after": {"claim": claim, "novelty_hypothesis": hyp},
        "degraded": degraded,
    })
    _commit(dossier)
    return {"idea": idea, "evaluation": _evaluation_for(dossier, idea_id), "degraded": degraded}


# ---------------------------------------------------------------------------
# 单 idea 评估（evaluate）——复用核心 evaluate 的单条装配，不改接口契约
# ---------------------------------------------------------------------------

def _evaluate_single_idea(dossier: Dossier, idea: dict,
                          llm: Optional[Any] = None) -> dict:
    """对单个 idea 复用核心 M11/M12/M18/M20/M21 的评估装配（薄封装，不重写评估逻辑）。"""
    llm = get_provider() if llm is None else llm
    assets = dossier.assets or {}
    facts = assets.get("facts") if isinstance(assets.get("facts"), dict) else {}
    literature_list = list(dossier.literature or [])

    gap_notes = evaluate._all_gap_notes(literature_list)
    venue_dist = evaluate._venue_distribution(literature_list)
    venue_summary = evaluate._format_venue_distribution(venue_dist)
    data_feasibility = evaluate._data_feasibility(facts)
    gap_evidence_levels = evaluate._gap_evidence_levels(literature_list)
    system_prompt, version = evaluate._load_prompt()

    ev = evaluate._evaluate_idea(
        idea, facts, gap_notes, venue_dist, venue_summary,
        data_feasibility, literature_list, llm, system_prompt,
        gap_evidence_levels=gap_evidence_levels,
    )
    dossier.meta.setdefault("prompt_versions", {})["evaluate"] = version
    return ev


def _replace_evaluation(dossier: Dossier, ev: dict) -> None:
    idea_id = str(ev.get("idea_ref"))
    dossier.evaluations = [
        e for e in (dossier.evaluations or [])
        if not (isinstance(e, dict) and str(e.get("idea_ref")) == idea_id)
    ]
    dossier.evaluations.append(ev)


@router.post("/projects/{project_id}/ideas/{idea_id}/evaluate")
def evaluate_idea(project_id: str, idea_id: str) -> dict:
    dossier = _load_dossier(project_id)
    idx = _idea_index(dossier, idea_id)
    if idx is None:
        raise HTTPException(status_code=404, detail="idea 不存在：{}".format(idea_id))
    ev = _evaluate_single_idea(dossier, dossier.ideas[idx])
    _replace_evaluation(dossier, ev)
    _commit(dossier)
    return {"evaluation": ev}


# ---------------------------------------------------------------------------
# gap 检索补充（retrieve-more）——只跑 检索 → gap 证据级别更新 → 评估更新
# ---------------------------------------------------------------------------

@router.post("/projects/{project_id}/gaps/{gap_id}/retrieve-more")
def retrieve_more(project_id: str, gap_id: str) -> dict:
    dossier = _load_dossier(project_id)
    loc = _find_gap(dossier, gap_id)
    if loc is None:
        raise HTTPException(status_code=404, detail="gap 不存在：{}".format(gap_id))
    e_idx, g_idx, entry, gap = loc

    # ① 检索：从 gap 的 angle/claim_point 派生更聚焦的查询（不复用整个 pipeline 的检索）
    focused = str(gap.get("angle") or gap.get("claim_point")
                  or entry.get("query") or "").strip()
    if not focused:
        raise HTTPException(status_code=400, detail="gap 缺少可用于检索的角度")
    cache_dir = storage.layout()["literature_cache"]
    llm = get_provider()
    new_entries = search_literature([focused], cache_dir, llm)

    # ② gap 更新：把新论文并入父条目（按标题去重），再重算 gap 的证据级别（M18）
    existing = {str(p.get("title") or "").strip()
                for p in (entry.get("papers") or [])
                if isinstance(p, dict) and str(p.get("title") or "").strip()}
    sources = set(str(s) for s in (entry.get("sources") or []))
    added_titles: List[str] = []
    for ne in new_entries or []:
        if not isinstance(ne, dict):
            continue
        for p in ne.get("papers") or []:
            if not isinstance(p, dict):
                continue
            title = str(p.get("title") or "").strip()
            if not title or title in existing:
                continue
            existing.add(title)
            entry.setdefault("papers", []).append(p)
            added_titles.append(title)
        for s in (ne.get("sources") or []):
            if str(s).strip():
                sources.add(str(s).strip())
    entry["sources"] = sorted(sources)
    gap["gap_hypothesis"] = literature._build_gap_hypothesis(
        entry, entry["papers"], gap)

    # ③ 评估更新：只重评估引用该 gap 的 idea（受 gap 证据级别变化影响）
    affected = [
        i for i in (dossier.ideas or [])
        if isinstance(i, dict) and i.get("idea_id")
        and str(gap_id) in [str(r) for r in (i.get("gap_refs") or [])]
    ]
    updated: List[dict] = []
    for idea in affected:
        ev = _evaluate_single_idea(dossier, idea, llm)
        _replace_evaluation(dossier, ev)
        updated.append(ev)

    _commit(dossier)
    return {
        "gap": gap,
        "added_papers": added_titles,
        "updated_evaluations": updated,
    }


# ---------------------------------------------------------------------------
# 「当前项目」别名路由（兼容任务卡简写：/ideas、POST /ideas/{id}/refine …）
# ---------------------------------------------------------------------------

@router.get("/ideas")
def get_ideas_alias(request: Request) -> dict:
    return get_ideas(_resolve_project(request))


@router.get("/literature")
def get_literature_alias(request: Request) -> dict:
    return get_literature(_resolve_project(request))


@router.get("/gaps")
def get_gaps_alias(request: Request) -> dict:
    return get_gaps(_resolve_project(request))


@router.get("/roadmap")
def get_roadmap_alias(request: Request) -> dict:
    return get_roadmap(_resolve_project(request))


@router.get("/history")
def get_history_alias(request: Request) -> dict:
    project_id = _resolve_project(request)
    return get_history(project_id)


@router.post("/ideas/{idea_id}/refine")
def refine_idea_alias(idea_id: str, request: Request) -> dict:
    return refine_idea(_resolve_project(request), idea_id)


@router.post("/ideas/{idea_id}/evaluate")
def evaluate_idea_alias(idea_id: str, request: Request) -> dict:
    return evaluate_idea(_resolve_project(request), idea_id)


@router.post("/gaps/{gap_id}/retrieve-more")
def retrieve_more_alias(gap_id: str, request: Request) -> dict:
    return retrieve_more(_resolve_project(request), gap_id)
