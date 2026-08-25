"""③ 知识检索 + ④ 创新点生成 Agent：problems + literature -> dossier.ideas。

对应 docs/build-plan.md §4 M5 / M5 v2 与 docs/architecture.md §5 ③④：

- **③ 检索编排**：从 ``dossier.problems`` 派生查询，调 ``retrieval.search_literature``
  写 ``dossier.literature``（含查询改写循环 + 缓存 + 降级）。
- **③½ 文献理解/矛盾挖掘/假设生成（M5 v2）**：调 ``literature.analyze_literature``，
  给每篇论文附结构化理解、每条文献条目附 ``contradiction_graph``（gap/矛盾）与
  ``hypotheses``（if-then 可证伪假设）。
- **④ 创新点生成**：problems + literature（含 gap/假设，+ facts 兜底）→ ``dossier.ideas``。
- **关键约束**（architecture §5 ④）：每个 idea 必须引用 ``literature_refs`` 并写
  ``novelty_hypothesis``；``literature_refs`` 只允许引用真实检索到的论文标题，
  禁止编造引用（academic integrity）。M5 v2 追加：每个 idea 必须追溯其来源 gap/矛盾
  （``gap_refs`` + ``hypothesis_refs`` + ``evidence`` 里挂 ``literature.contradiction_graph``）。

降级路径（architecture §7 / §8）：
无 key（NullProvider 返回空）、网络失败（LLMError）、schema 失败（SchemaError）时，
降级为确定性规则生成 ideas；离线时 literature 无论文，ideas 仍按规则产出。

冻结接口（docs/build-plan.md §3.3 / §4 M5）：

    def run(dossier: Dossier, llm: LLMProvider) -> None
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List

from .. import storage
from ..dossier import Dossier
from ..literature import _entry_gaps, _entry_hypotheses, analyze_literature
from ..llm import LLMError, LLMProvider, SchemaError
from ..retrieval import search_literature

__all__ = [
    "run",
    "IDEA_SCHEMA",
    "_derive_queries",
    "_deterministic_ideas",
    "_generate_with_llm",
    "_finalize_ideas",
]

# 本 Agent prompt 版本：单一事实源 = prompts/ideate.md 的 `<!-- version: N -->` 头
_PROMPT_VERSION = "v2"
_PROMPT_FILE = Path(__file__).resolve().parent.parent / "prompts" / "ideate.md"

# 派生查询的数量上限（控制 API 调用预算）
_MAX_QUERIES = 5

# 本 Agent 的 LLM 输出契约（schema 校验走 papermine/llm.py 的极简子集）
IDEA_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["ideas"],
    "properties": {
        "ideas": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["claim", "novelty_hypothesis", "problem_ref", "literature_refs"],
                "properties": {
                    "claim": {"type": "string"},
                    "novelty_hypothesis": {"type": "string"},
                    "problem_ref": {"type": "string"},
                    "literature_refs": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
    },
}

_FALLBACK_SYSTEM = (
    "你是 papermine 的「创新点生成 Agent」。输入是研究问题、检索到的文献（含结构化理解、"
    "矛盾/gap、可证伪假设）与项目事实，输出候选创新点。每个创新点应优先从给定的 gap/矛盾"
    "里生长出来，引用 literature_refs（只许用给定文献中真实存在的论文标题，禁止编造）并写 "
    "novelty_hypothesis，且挂 problem_ref。只输出 JSON，严格满足给定 schema。"
)


def _load_prompt() -> tuple:
    """从 prompts/ideate.md 读取版本号与 system 段；文件缺失时回退内置默认。"""
    if _PROMPT_FILE.exists():
        text = _PROMPT_FILE.read_text(encoding="utf-8")
        m = re.search(r"<!--\s*version:\s*(\d+)\s*-->", text)
        version = "v{}".format(m.group(1)) if m else _PROMPT_VERSION
        system = re.sub(r"<!--.*?-->\s*", "", text).strip()
        return version, system
    return _PROMPT_VERSION, _FALLBACK_SYSTEM


# ---------------------------------------------------------------------------
# 查询派生
# ---------------------------------------------------------------------------

def _dedup_strings(items: Any) -> List[str]:
    out: List[str] = []
    seen: set = set()
    for it in items or []:
        s = " ".join(str(it).split())
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _derive_queries(problems: List[dict], facts: Dict[str, Any]) -> List[str]:
    """从 problems 派生检索查询（title + formulation）；无问题时用 facts 兜底。"""
    queries: List[str] = []
    for p in problems or []:
        title = (p.get("title") or "").strip()
        formulation = (p.get("formulation") or "").strip()
        q = " ".join(x for x in (title, formulation) if x).strip()
        if q:
            queries.append(q)
    if not queries:
        scenario = " ".join(facts.get("scenarios", [])[:2])
        tasks = " ".join(facts.get("tasks", [])[:3])
        methods = " ".join(facts.get("methods", [])[:3])
        q = " ".join(x for x in (scenario, tasks, methods) if x).strip()
        if q:
            queries.append(q)
    return _dedup_strings(queries)[:_MAX_QUERIES]


# ---------------------------------------------------------------------------
# 创新点生成
# ---------------------------------------------------------------------------

def _entry_titles(entry: Dict[str, Any]) -> List[str]:
    return [
        (p.get("title") or "").strip()
        for p in (entry.get("papers") or [])
        if isinstance(p, dict) and (p.get("title") or "").strip()
    ]


def _build_user_prompt(problems: List[dict], literature: List[dict],
                       facts: Dict[str, Any]) -> str:
    """构造脱敏输入：problems + literature 摘要（标题/年份/来源，不含全文）+ facts。"""
    lit: List[dict] = []
    for e in literature or []:
        papers = [
            {
                "title": p.get("title", ""),
                "year": p.get("year"),
                "venue": p.get("venue", ""),
                "source": p.get("source", ""),
            }
            for p in (e.get("papers") or [])
            if isinstance(p, dict)
        ]
        lit.append({
            "query": e.get("query", ""),
            "gap_note": e.get("gap_note", ""),
            "papers": papers,
            "gaps": _entry_gaps(e),
            "hypotheses": _entry_hypotheses(e),
        })
    payload = {"problems": problems, "literature": lit, "facts": facts}
    return (
        "以下是研究问题、检索到的文献与项目事实，请据此生成候选创新点：\n"
        + json.dumps(payload, ensure_ascii=False)
    )


def _generate_with_llm(llm: LLMProvider, system: str, problems: List[dict],
                       literature: List[dict], facts: Dict[str, Any]) -> List[dict]:
    """调用 LLM 生成候选 idea 列表；失败/空结果由上层降级到确定性规则。"""
    result = llm.complete(system, _build_user_prompt(problems, literature, facts),
                          IDEA_SCHEMA, temperature=0.5)
    if not isinstance(result, dict):
        return []
    raw = result.get("ideas")
    if not isinstance(raw, list):
        return []
    return [x for x in raw if isinstance(x, dict)]


def _fmt(items: Any, n: int = 3, fallback: str = "通用") -> str:
    vals = [str(x) for x in (items or []) if str(x).strip()]
    return "、".join(vals[:n]) if vals else fallback


def _problem_idea(p: Dict[str, Any], refs: List[str], facts: Dict[str, Any]) -> dict:
    """按单个问题生成一条「方法改进」确定性 idea。"""
    task = _fmt(facts.get("tasks"))
    method = _fmt(facts.get("methods"))
    scenario = _fmt(facts.get("scenarios"))
    data = _fmt(facts.get("data"))
    metric = _fmt(facts.get("metrics"), fallback="关键指标")
    focus = (p.get("title") or "").strip() or task
    return {
        "claim": "针对「{}」问题，提出面向{}场景、结合{}的改进方法，提升{}表现。".format(
            focus, scenario, method, metric
        ),
        "novelty_hypothesis": "现有工作多在通用基准上评估{}，本项目面向{}的{}数据，"
                              "假设在{}约束下存在未被充分研究的方法设计空间（需对照文献核验）。".format(
                                  method, scenario, data, metric
                              ),
        "problem_ref": p.get("problem_id") or "",
        "literature_refs": list(refs[:2]),
    }


def _tool_idea(refs: List[str], facts: Dict[str, Any], problems: List[dict]) -> dict:
    task = _fmt(facts.get("tasks"))
    scenario = _fmt(facts.get("scenarios"))
    modules = _fmt(facts.get("modules"), fallback="可复用")
    return {
        "claim": "将项目中沉淀的{}组件抽象为面向{}任务的通用工具/框架，降低同类横向工程重复开发成本。".format(
            modules, task
        ),
        "novelty_hypothesis": "相较一次性工程实现，假设以可复用组件+统一接口构成的工具框架，"
                              "在{}场景的{}任务上具备可复现性与交付效率方面的研究价值（需对照文献核验）。".format(
                                  scenario, task
                              ),
        "problem_ref": problems[0].get("problem_id", "") if problems else "",
        "literature_refs": list(refs[:2]),
    }


def _empirical_idea(refs: List[str], facts: Dict[str, Any], problems: List[dict]) -> dict:
    task = _fmt(facts.get("tasks"))
    scenario = _fmt(facts.get("scenarios"))
    return {
        "claim": "在{}场景下，系统报告{}任务在真实数据上的方法对比与工程经验，形成可复现的实证研究。".format(
            scenario, task
        ),
        "novelty_hypothesis": "真实{}场景下{}任务的方法对比结论稀缺，假设系统化的实证证据"
                              "能填补工程经验与学术方法之间的空白（需对照文献核验）。".format(
                                  scenario, task
                              ),
        "problem_ref": problems[0].get("problem_id", "") if problems else "",
        "literature_refs": list(refs[:2]),
    }


def _deterministic_ideas(problems: List[dict], literature: List[dict],
                         facts: Dict[str, Any]) -> List[dict]:
    """无 LLM 时的确定性 idea 生成：每个问题一条「方法改进」，不足 2 条时补工具/实证视角。

    保证始终产出 ≥2 条、每条带非空 claim + novelty_hypothesis + literature_refs（可为空）。
    """
    titles_by_entry = [_entry_titles(e) for e in (literature or [])]
    all_titles = [t for ts in titles_by_entry for t in ts]

    ideas: List[dict] = []
    for i, p in enumerate(problems or []):
        refs = titles_by_entry[i] if i < len(titles_by_entry) else []
        ideas.append(_problem_idea(p, refs, facts))

    if len(ideas) < 2:
        ideas.append(_tool_idea(all_titles, facts, problems))
    if len(ideas) < 2:
        ideas.append(_empirical_idea(all_titles, facts, problems))
    return ideas


def _finalize_ideas(raw: List[dict], problems: List[dict], literature: List[dict]) -> List[dict]:
    """把 LLM / 确定性产出的原始 idea 规范化：赋 id、清洗字段、过滤幻觉引用、补空字段。"""
    problem_ids = [p.get("problem_id") for p in problems or [] if p.get("problem_id")]
    titles_by_entry = [_entry_titles(e) for e in (literature or [])]
    real_titles = {t for ts in titles_by_entry for t in ts}
    all_titles = [t for ts in titles_by_entry for t in ts]

    # M5 v2：gap/矛盾与假设的追溯映射（gap_id/hypothesis_id 全局唯一，按文献条目归组）
    gaps_by_entry = [[g["gap_id"] for g in _entry_gaps(e)] for e in (literature or [])]
    hyps_by_entry = [[h["hypothesis_id"] for h in _entry_hypotheses(e)] for e in (literature or [])]
    all_gap_ids = [gid for gids in gaps_by_entry for gid in gids]
    all_hyp_ids = [hid for hids in hyps_by_entry for hid in hids]
    gap_by_id = {g["gap_id"]: g for e in (literature or []) for g in _entry_gaps(e)}

    out: List[dict] = []
    seen_claims: set = set()
    for r in raw or []:
        if not isinstance(r, dict):
            continue
        claim = " ".join(str(r.get("claim") or "").split())
        if not claim or claim in seen_claims:
            continue
        seen_claims.add(claim)

        hypothesis = " ".join(str(r.get("novelty_hypothesis") or "").split())
        if not hypothesis:
            hypothesis = "相较现有工作，假设「{}」具备可检验的新颖性（离线降级，需人工核验）。".format(claim)

        # 只允许引用真实检索到的论文标题（杜绝编造引用）
        refs = [x for x in _dedup_strings(r.get("literature_refs")) if x in real_titles]

        problem_ref = " ".join(str(r.get("problem_ref") or "").split())
        if problem_ref not in problem_ids:
            problem_ref = problem_ids[0] if problem_ids else ""

        # 有文献却没引用时，按问题对应的检索条目补真实标题，保证 idea 带引用
        if not refs and all_titles:
            idx = problem_ids.index(problem_ref) if problem_ref in problem_ids else -1
            candidate = titles_by_entry[idx] if 0 <= idx < len(titles_by_entry) else all_titles
            refs = list(candidate[:2])

        # M5 v2：确定该 idea 的来源 gap/假设（按 problem 对应的文献条目归组，缺失时兜底全量）
        idx = problem_ids.index(problem_ref) if problem_ref in problem_ids else -1
        gap_refs = list(gaps_by_entry[idx]) if 0 <= idx < len(gaps_by_entry) else []
        if not gap_refs:
            gap_refs = list(all_gap_ids[:2])
        hyp_refs = list(hyps_by_entry[idx]) if 0 <= idx < len(hyps_by_entry) else []
        if not hyp_refs:
            hyp_refs = list(all_hyp_ids[:2])

        # evidence 追溯来源 gap/矛盾（provenance 强制，验收点 3）
        evidence: List[dict] = []
        for gid in gap_refs:
            gap = gap_by_id.get(gid)
            if not gap:
                continue
            evidence.append({
                "source": "literature.contradiction_graph",
                "gap_id": gid,
                "type": gap.get("type"),
                "note": (gap.get("description") or "")[:200],
            })

        out.append({
            "idea_id": "i{}".format(len(out) + 1),
            "claim": claim,
            "novelty_hypothesis": hypothesis,
            "problem_ref": problem_ref,
            "literature_refs": refs,
            "gap_refs": gap_refs,
            "hypothesis_refs": hyp_refs,
            "evidence": evidence,
            "status": "pending_eval",
        })
    return out


def _generate_ideas(problems: List[dict], literature: List[dict], facts: Dict[str, Any],
                    llm: LLMProvider, system: str) -> List[dict]:
    """生成 ideas：先试 LLM，失败/为空则降级确定性规则，最后统一规范化。"""
    raw: List[dict] = []
    if llm is not None:
        try:
            raw = _generate_with_llm(llm, system, problems, literature, facts)
        except (LLMError, SchemaError):
            raw = []
    if not raw:
        raw = _deterministic_ideas(problems, literature, facts)
    return _finalize_ideas(raw, problems, literature)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def run(dossier: Dossier, llm: LLMProvider) -> None:
    """problems → 文献检索 → literature；problems + literature → ideas（原地写 Dossier）。

    冻结契约（docs/build-plan.md §3.3 / §4 M5）：

        def run(dossier: Dossier, llm: LLMProvider) -> None

    只写 ``dossier.literature`` 与 ``dossier.ideas``（落盘 / 快照由编排器负责）。
    """
    problems = list(dossier.problems or [])
    facts = dict((dossier.assets or {}).get("facts") or {})
    version, system = _load_prompt()

    # ---- ③ 知识检索 ----
    queries = _derive_queries(problems, facts)
    cache_dir = storage.layout()["literature_cache"]
    dossier.literature = search_literature(queries, cache_dir, llm)

    # ---- ③½ 文献理解 + 矛盾/gap 挖掘 + 假设生成（M5 v2）----
    analyze_literature(dossier.literature, llm)

    # ---- ④ 创新点生成（复用 gap/矛盾与假设）----
    dossier.ideas = _generate_ideas(problems, dossier.literature, facts, llm, system)
    dossier.meta.setdefault("prompt_versions", {})["ideate"] = version
