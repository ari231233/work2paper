"""编排器：把 M3–M6 串成显式状态机 + 检查点 + 回退 + ⑦ 经验沉淀。

对应 docs/build-plan.md §4 M7 与 docs/architecture.md §6：

状态机（每个状态 = 一次 Agent 调用 + 一次 Dossier 落盘）：

    UNDERSTAND ─☑1─> ABSTRACT ─☑2─> RETRIEVE⇄GENERATE(IDEATE) ─☑3─> EVALUATE
    ─☑4─> PLAN ─☑5─> REFLECT ─> DONE

- **检查点默认暂停等输入**（accept / rework / note；``auto=True`` 跳过并默认 accept）；
- **回退有最大轮数**（``MAX_ROLLBACK_ROUNDS``），超限降级为前进；
- **每状态迁移后 ``dossier.snapshot()``**（append-only 历史，可回滚）；
- ⑦ 在 DONE 前执行一次，把本次运行蒸馏成经验条目写入经验库；
- **M8 混合注入**：每个状态执行前按 applicability 门控检索 active 经验，把命中条目里的
  ``policy.directive`` 渲染成该状态对应 Agent 的行为准则注入其 system prompt（结构决定位置，LLM 执行约束）。

冻结接口（docs/build-plan.md §4 M7，M8 增量见同节 M8）：

    def run_pipeline(project_dir: str, auto: bool = False) -> str   # 返回 run_id

额外提供 ``resume`` / ``status``（「检查点暂停/续跑」能力）。
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import experience, policy, storage
from .agents import abstract, evaluate, ideate, plan, reflect, understand
from .dossier import Dossier
from .llm import NullProvider, get_provider

__all__ = [
    "run_pipeline",
    "resume",
    "status",
    "MAX_ROLLBACK_ROUNDS",
    "STATE_FILE",
]

# 状态机：状态名 + 检查点名按序排列（DONE 为终态）
_SEQUENCE = [
    "UNDERSTAND", "cp1",
    "ABSTRACT", "cp2",
    "IDEATE", "cp3",       # RETRIEVE ⇄ GENERATE（由 M5 ideate.run 一次完成）
    "EVALUATE", "cp4",
    "PLAN", "cp5",
    "REFLECT", "DONE",
]

_STATE_LABELS = {
    "UNDERSTAND": "① 项目理解",
    "ABSTRACT": "② 问题抽象",
    "IDEATE": "③ 知识检索 ⇄ ④ 创新点生成",
    "EVALUATE": "⑤ 可行性评估",
    "PLAN": "⑥ 路线规划",
    "REFLECT": "⑦ 经验沉淀",
    "DONE": "完成",
}

_CHECKPOINT_LABELS = {
    "cp1": "项目理解是否准确",
    "cp2": "抽象出的研究问题是否认可",
    "cp3": "候选创新点是否认可",
    "cp4": "评估结论是否接受",
    "cp5": "最终路线图是否采纳",
}

# 检查点 rework → 回退目标状态（architecture §2.2 / §6）
_ROLLBACK_TARGET = {
    "cp1": "UNDERSTAND",
    "cp2": "ABSTRACT",
    "cp3": "IDEATE",
    "cp4": "IDEATE",     # 评估不接受 → 回创新点生成（⑤→④）
    "cp5": "PLAN",
}

# 每个检查点回退的最大轮数（超限降级为前进，防死循环）
MAX_ROLLBACK_ROUNDS = 3

# 运行状态文件（供 resume / status）
STATE_FILE = "run_state.json"
STATE_SCHEMA = "run_state"
STATE_SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _log(msg: str) -> None:
    sys.stderr.write("[papermine] {}\n".format(msg))
    sys.stderr.flush()


def _advance(name: str) -> str:
    """返回序列中 name 之后的下一个节点名。"""
    i = _SEQUENCE.index(name)
    return _SEQUENCE[i + 1] if i + 1 < len(_SEQUENCE) else "DONE"


def _llm_backend(llm: Any) -> str:
    return "null" if isinstance(llm, NullProvider) else "deepseek"


# ---------------------------------------------------------------------------
# 落盘 / 快照 / 状态
# ---------------------------------------------------------------------------

def _commit(dossier: Dossier, run_dir: Path) -> None:
    """每次状态迁移后：递增版本 -> 落盘 -> 写历史快照（engineering.md §3.1）。"""
    dossier.bump_version()
    dossier.save(run_dir)
    dossier.snapshot()


def _save_state(run_dir: Path, state: Dict[str, Any]) -> None:
    payload = dict(state)
    payload.pop("_schema", None)
    payload.pop("_schema_version", None)
    payload["updated_at"] = _now_iso()
    storage.save_json(run_dir / STATE_FILE, payload, STATE_SCHEMA, STATE_SCHEMA_VERSION)


def _load_state(run_dir: Path) -> Dict[str, Any]:
    data = storage.load_json(run_dir / STATE_FILE, STATE_SCHEMA)
    data.pop("_schema", None)
    data.pop("_schema_version", None)
    data.setdefault("rollback_rounds", {})
    data.setdefault("degradations", 0)
    return data


def _new_state(run_id: str, project_dir: str, auto: bool) -> Dict[str, Any]:
    return {
        "run_id": run_id,
        "project_dir": project_dir,
        "state": "UNDERSTAND",
        "auto": bool(auto),
        "rollback_rounds": {},
        "degradations": 0,
        "updated_at": _now_iso(),
    }


# ---------------------------------------------------------------------------
# 检查点交互
# ---------------------------------------------------------------------------

def _parse_decision(raw: str) -> tuple:
    """把用户输入解析成 (decision, note)，decision ∈ {accept, rework, note}。"""
    s = (raw or "").strip()
    low = s.lower()
    if low in ("", "accept", "a", "y", "yes", "ok", "接受", "通过", "继续"):
        return ("accept", "")
    if low in ("rework", "r", "no", "回退", "重做", "返工"):
        return ("rework", "")
    for prefix in ("note ", "n ", "备注 ", "附注 "):
        if low.startswith(prefix):
            return ("note", s.split(None, 1)[1] if " " in s else "")
    if low in ("note", "n", "备注", "附注"):
        return ("note", "")
    # 未识别 → 视为接受（附注原始输入，方便追溯）
    return ("accept", s)


def _prompt(checkpoint: str, label: str) -> tuple:
    """在检查点暂停，读取人工决策；非交互 stdin（EOF）默认 accept。"""
    sys.stderr.write("\n=== 检查点 {}：{} ===\n".format(checkpoint, label))
    sys.stderr.write("  accept / a / y 或直接回车 → 接受并继续\n")
    sys.stderr.write("  rework / r                    → 回退重做\n")
    sys.stderr.write("  note <内容>                    → 接受并附注\n")
    sys.stderr.write("请输入：")
    sys.stderr.flush()
    try:
        raw = input()
    except EOFError:
        sys.stderr.write("\n（非交互输入，默认接受）\n")
        return ("accept", "非交互环境，默认接受")
    decision, note = _parse_decision(raw)
    return (decision, note)


# ---------------------------------------------------------------------------
# 回退 / 降级
# ---------------------------------------------------------------------------

def _rollback(key: str, state: Dict[str, Any]) -> bool:
    """尝试回退；在最大轮数内返回 True（并记轮数），超限返回 False（降级前进）。"""
    rounds = state.setdefault("rollback_rounds", {})
    if int(rounds.get(key, 0)) >= MAX_ROLLBACK_ROUNDS:
        return False
    rounds[key] = int(rounds.get(key, 0)) + 1
    return True


def _has_data_gap(roadmap: Dict[str, Any]) -> bool:
    """路线图 missing_items 是否提示「数据/指标缺口」（architecture §6：PLAN--missing-->回UNDERSTAND）。"""
    missing = (roadmap or {}).get("missing_items") or []
    text = " ".join(str(m) for m in missing)
    return any(k in text for k in ("数据", "指标", "回填", "采集", "标注"))


def _process_signals(state: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "rollback_rounds": int(sum((state.get("rollback_rounds") or {}).values())),
        "degradations": int(state.get("degradations") or 0),
    }


# ---------------------------------------------------------------------------
# 状态机执行
# ---------------------------------------------------------------------------

def _injected_llm(llm: Any, dossier: Dossier, state: str) -> Any:
    """按当前状态检索并注入 policy directive（M8 混合注入）。

    - ``policy.retrieve_for_state``：applicability 门控 + target 过滤；
    - ``policy.inject``：把 directive 包一层 LLM 包装器，追加到 system prompt。
    无命中时原样返回 llm（零成本透传）。
    """
    entries = policy.retrieve_for_state(dossier, state)
    grouped = policy.group_by_target(entries)
    directives: List[str] = []
    for target in policy.targets_for_state(state):
        directives.extend(grouped.get(target, []))
    return policy.inject(llm, directives)


def _run_state(name: str, project_dir: str, dossier: Dossier, llm: Any) -> None:
    llm = _injected_llm(llm, dossier, name)
    if name == "UNDERSTAND":
        understand.run(project_dir, dossier, llm)
    elif name == "ABSTRACT":
        abstract.run(dossier, llm)
    elif name == "IDEATE":
        ideate.run(dossier, llm)
    elif name == "EVALUATE":
        evaluate.run(dossier, llm)
    elif name == "PLAN":
        plan.run(dossier, llm)
    elif name == "REFLECT":
        reflect.run(dossier, llm)


def _execute(run_dir: Path, project_dir: str, dossier: Dossier, llm: Any,
             state: Dict[str, Any]) -> None:
    """跑状态机直到 DONE（或从 resume 处的状态继续）。"""
    while True:
        cur = state.get("state", "UNDERSTAND")
        if cur == "DONE":
            return

        if cur in _STATE_LABELS:
            _log("执行状态：{}".format(_STATE_LABELS[cur]))
            if cur == "REFLECT":
                dossier.meta["process_signals"] = _process_signals(state)
            _run_state(cur, project_dir, dossier, llm)
            _commit(dossier, run_dir)

            if cur == "PLAN" and _has_data_gap(dossier.roadmap):
                if _rollback("plan_missing", state):
                    _log("路线图存在数据缺口 → 自动回退到 UNDERSTAND（回填项目事实）")
                    state["state"] = "UNDERSTAND"
                else:
                    _log("缺口回退轮数超限，降级前进")
                    state["degradations"] = int(state.get("degradations") or 0) + 1
                    state["state"] = _advance(cur)
            else:
                state["state"] = _advance(cur)
            _save_state(run_dir, state)
            continue

        if cur in _CHECKPOINT_LABELS:
            if state.get("auto"):
                decision, note = "accept", "auto=True 跳过检查点"
            else:
                decision, note = _prompt(cur, _CHECKPOINT_LABELS[cur])

            dossier.human_decisions.append({
                "checkpoint": cur,
                "decision": decision,
                "note": note,
                "ts": _now_iso(),
            })
            experience.record_decision(state["run_id"], cur, decision, note)
            _commit(dossier, run_dir)

            if decision == "rework":
                target = _ROLLBACK_TARGET[cur]
                if _rollback(cur, state):
                    _log("检查点 {} 决策 rework → 回退到 {}".format(cur, target))
                    state["state"] = target
                else:
                    _log("检查点 {} 回退轮数超限，降级前进".format(cur))
                    state["degradations"] = int(state.get("degradations") or 0) + 1
                    state["state"] = _advance(cur)
            else:
                state["state"] = _advance(cur)
            _save_state(run_dir, state)
            continue

        # 防御：未知状态前进
        state["state"] = _advance(cur)
        _save_state(run_dir, state)


# ---------------------------------------------------------------------------
# 报告（run_dir/report.md + report.json）
# ---------------------------------------------------------------------------

def _render_report_md(dossier: Dossier) -> str:
    lines = ["# papermine 分析报告", ""]
    lines.append("> run_id: {}".format(dossier.meta.get("run_id") or dossier.meta.get("project_id")))
    lines.append("> llm_backend: {}".format(dossier.meta.get("llm_backend") or "（未知）"))
    lines.append("")

    lines.append("## 项目叙事")
    lines.append("")
    lines.append((dossier.assets or {}).get("narrative") or "（无）")
    lines.append("")

    lines.append("## 研究问题")
    lines.append("")
    if dossier.problems:
        for p in dossier.problems:
            lines.append("- **{}**：{}".format(p.get("title") or p.get("problem_id"), p.get("formulation") or ""))
    else:
        lines.append("（无）")
    lines.append("")

    lines.append("## 候选创新点")
    lines.append("")
    if dossier.ideas:
        for idea in dossier.ideas:
            lines.append("- **{}**：{}".format(idea.get("idea_id"), idea.get("claim") or ""))
            lines.append("  - novelty 假设：{}".format(idea.get("novelty_hypothesis") or ""))
    else:
        lines.append("（无）")
    lines.append("")

    lines.append("## 可行性评估")
    lines.append("")
    if dossier.evaluations:
        for ev in dossier.evaluations:
            lines.append("- **{}**：novelty={}，数据可得性={}，工作量≈{}h，verdict={}".format(
                ev.get("idea_ref"), ev.get("novelty_score"), ev.get("data_feasibility"),
                ev.get("workload_hours"), ev.get("verdict")))
            if ev.get("rework_reason"):
                lines.append("  - 回炉原因：{}".format(ev["rework_reason"]))
    else:
        lines.append("（无）")
    lines.append("")

    lines.append("## 论文路线图")
    lines.append("")
    r = dossier.roadmap or {}
    lines.append("- 选中创新点：{}".format(r.get("selected_idea")))
    lines.append("- 论文类型：{}".format(r.get("paper_type") or "（未定）"))
    outline = r.get("outline") or []
    if outline:
        lines.append("- 大纲：")
        for o in outline:
            lines.append("  - {}".format(o))
    missing = r.get("missing_items") or []
    if missing:
        lines.append("- 缺口：")
        for m in missing:
            lines.append("  - {}".format(m))
    lines.append("")

    lines.append("## 人类决策记录")
    lines.append("")
    if dossier.human_decisions:
        for d in dossier.human_decisions:
            lines.append("- {}：{}（{}）".format(d.get("checkpoint"), d.get("decision"), d.get("note") or ""))
    else:
        lines.append("（无）")
    lines.append("")
    return "\n".join(lines) + "\n"


def _write_report(run_dir: Path, dossier: Dossier) -> None:
    storage.save_json(run_dir / "report.json", dossier.to_dict(), "report", 1)
    (run_dir / "report.md").write_text(_render_report_md(dossier), encoding="utf-8")


# ---------------------------------------------------------------------------
# 冻结接口
# ---------------------------------------------------------------------------

def run_pipeline(project_dir: str, auto: bool = False) -> str:
    """端到端跑一次分析，返回 run_id。

    冻结契约（docs/build-plan.md §4 M7）：
        def run_pipeline(project_dir: str, auto: bool = False) -> str

    - 检查点默认暂停等输入；``auto=True`` 跳过并默认 accept；
    - 结束执行 ⑦ 经验沉淀，写出一条经验条目；
    - 产出自 ``~/.papermine/runs/<run_id>/``（dossier.json + 历史快照 + report.*）。
    """
    project_dir = os.path.abspath(project_dir)
    if not os.path.isdir(project_dir):
        raise FileNotFoundError("项目目录不存在：{}".format(project_dir))

    storage.ensure_layout()
    run_id = storage.new_run_id()
    run_dir = storage.run_dir(run_id)
    run_dir.mkdir(parents=True, exist_ok=True)

    llm = get_provider()
    dossier = Dossier(llm_backend=_llm_backend(llm))
    dossier.meta["run_id"] = run_id
    dossier.save(run_dir)

    state = _new_state(run_id, project_dir, auto)
    _save_state(run_dir, state)

    try:
        _execute(run_dir, project_dir, dossier, llm, state)
    finally:
        # 兜底落盘：异常 / 中断也能续跑
        dossier.save(run_dir)
        _save_state(run_dir, state)

    _write_report(run_dir, dossier)
    _log("分析完成：run_id={}，数据目录={}".format(run_id, run_dir))
    return run_id


def resume(run_id: str, auto: bool = False) -> str:
    """从上次检查点续跑一个 run（返回 run_id）。

    - 重新读取 dossier.json + run_state.json，从 state 记录的当前状态继续；
    - ``auto=True`` 覆盖为跳过剩余检查点。
    """
    run_dir = storage.run_dir(run_id)
    if not (run_dir / "dossier.json").exists():
        raise FileNotFoundError("run 不存在或 dossier 缺失：{}".format(run_id))

    state = _load_state(run_dir)
    state["auto"] = bool(auto)
    project_dir = state.get("project_dir") or ""
    dossier = Dossier.load(run_dir)
    dossier.meta["run_id"] = run_id
    llm = get_provider()

    try:
        _execute(run_dir, project_dir, dossier, llm, state)
    finally:
        dossier.save(run_dir)
        _save_state(run_dir, state)

    _write_report(run_dir, dossier)
    _log("续跑完成：run_id={}，当前状态={}".format(run_id, state.get("state")))
    return run_id


def status(run_id: str) -> Dict[str, Any]:
    """查看一个 run 的进度。"""
    run_dir = storage.run_dir(run_id)
    if not (run_dir / STATE_FILE).exists():
        raise FileNotFoundError("run 不存在：{}".format(run_id))

    state = _load_state(run_dir)
    dossier_version: Optional[int] = None
    try:
        dossier_version = Dossier.load(run_dir).meta.get("version")
    except Exception:
        dossier_version = None

    return {
        "run_id": run_id,
        "state": state.get("state"),
        "auto": state.get("auto"),
        "rollback_rounds": state.get("rollback_rounds") or {},
        "degradations": state.get("degradations") or 0,
        "dossier_version": dossier_version,
        "updated_at": state.get("updated_at"),
    }
