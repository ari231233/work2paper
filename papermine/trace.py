"""M13 — Agent Trace：轻量执行轨迹记录器（append-only JSONL）+ 汇总分析。

对应 docs/build-plan.md §4 M13 与 docs/architecture.md §6（编排器状态机）：

- **存储**：``~/.papermine/runs/<run_id>/trace.jsonl``，每行一条轨迹事件，append-only。
- **记录内容**：
  1. 每个 Agent 的 start/end 时间戳 + 耗时（``agent_start`` / ``agent_end``）；
  2. 每个 LLM 调用的耗时 + 模型 + token 数（``llm``，token 当前不可获取记为 null）；
  3. 每个 HTTP 检索调用的耗时（``http``）；
  4. 回炉 / 降级 / 超时等异常信号（``signal``）。
- **要点**：
  1. **轻量零侵入**：用 context manager 包裹各 Agent 的 ``run()``（``TraceRecorder.agent_span``），
     不改 Agent 的 ``run()`` 签名、不改冻结接口契约；LLM 用 ``wrap_llm`` 包一层 provider，
     HTTP 用 ``enable_http_tracing`` 给 ``retrieval._http`` 的共享 client 挂一个 tracing transport
     （运行时打补丁，不改 retrieval 源码）。
  2. 提供 ``summarize`` / ``render_summary``：按耗时排序汇总各环节，供
     ``papermine trace <run_id>`` 子命令定位瓶颈。

设计说明：事件通过 ``contextvars.ContextVar`` 在同步单线程管线里传递「当前 recorder / 当前 span」，
使 HTTP transport 这种无法显式拿到 recorder 的底层钩子也能把事件归到正确的 span（``stage``）。
"""
from __future__ import annotations

import contextvars
import datetime
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from . import storage

__all__ = [
    "TRACE_FILENAME",
    "STATE_LABELS",
    "TraceRecorder",
    "wrap_llm",
    "enable_http_tracing",
    "current_recorder",
    "current_stage",
    "summarize",
    "render_summary",
    "_TracingTransport",
    "_model_of",
]

# trace 文件名（相对 run_dir）
TRACE_FILENAME = "trace.jsonl"

# 状态名 -> 中文环节标签（与 orchestrator._STATE_LABELS 对齐；此处独立维护，避免反向依赖编排器）
STATE_LABELS: Dict[str, str] = {
    "UNDERSTAND": "① 项目理解",
    "ABSTRACT": "② 问题抽象",
    "IDEATE": "③ 知识检索 ⇄ ④ 创新点生成",
    "EVALUATE": "⑤ 可行性评估",
    "PLAN": "⑥ 路线规划",
    "REFLECT": "⑦ 经验沉淀",
    "DONE": "完成",
}

# ---------------------------------------------------------------------------
# 上下文（contextvars）：让底层钩子（HTTP transport）拿到「当前 recorder / 当前 span」
# ---------------------------------------------------------------------------

_current_recorder: contextvars.ContextVar[Optional["TraceRecorder"]] = \
    contextvars.ContextVar("papermine_trace_recorder", default=None)
_span_stack: contextvars.ContextVar[tuple] = \
    contextvars.ContextVar("papermine_trace_span_stack", default=())


def current_recorder() -> Optional["TraceRecorder"]:
    """返回当前上下文里的 recorder（无则 None）。"""
    return _current_recorder.get()


def current_stage() -> Optional[str]:
    """返回当前最内层 agent span 的状态名（无则 None）。"""
    stack = _span_stack.get()
    return stack[-1] if stack else None


def _now_ts() -> str:
    """毫秒精度的 ISO 时间戳，用于事件排序与人工审计。"""
    return datetime.datetime.now().isoformat(timespec="milliseconds")


def _duration_ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000.0, 3)


# ---------------------------------------------------------------------------
# 记录器
# ---------------------------------------------------------------------------

class TraceRecorder:
    """绑定某个 run_dir 的 append-only 轨迹记录器。

    - ``event(kind, **fields)``：追加一条事件（带 ``ts`` 与 ``kind``）；
    - ``agent_span(name, label=None)``：上下文管理器，进出各写一条 ``agent_start`` / ``agent_end``；
    - ``signal(signal, detail)``：写一条异常信号（回炉/降级/超时）；
    - ``with recorder:``：把 recorder 设为当前上下文（供 HTTP transport 等底层钩子读取）。
    写盘失败静默（trace 绝不影响主流程）。
    """

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = Path(run_dir)
        self.path = self.run_dir / TRACE_FILENAME
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._token: Optional[contextvars.Token] = None

    # -- 底层追加 --
    def event(self, kind: str, **fields: Any) -> None:
        payload: Dict[str, Any] = {"ts": _now_ts(), "kind": kind}
        payload.update(fields)
        try:
            storage.append_jsonl(self.path, payload)
        except OSError:
            pass  # 记录失败不影响管线

    # -- 异常信号 --
    def signal(self, signal: str, detail: str = "", stage: Optional[str] = None) -> None:
        fields: Dict[str, Any] = {"signal": signal, "detail": detail}
        if stage is not None:
            fields["stage"] = stage
        self.event("signal", **fields)

    # -- agent span --
    def agent_span(self, name: str, label: Optional[str] = None):
        return _AgentSpan(self, name, label or STATE_LABELS.get(name, name))

    # -- context manager：激活当前 recorder --
    def __enter__(self) -> "TraceRecorder":
        self._token = _current_recorder.set(self)
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if self._token is not None:
            _current_recorder.reset(self._token)
            self._token = None
        return False


class _AgentSpan:
    """包裹单个 Agent 调用的上下文管理器，写 ``agent_start`` / ``agent_end``。"""

    def __init__(self, recorder: TraceRecorder, name: str, label: str) -> None:
        self._recorder = recorder
        self._name = name
        self._label = label
        self._id = uuid.uuid4().hex[:12]
        self._start = 0.0
        self._token: Optional[contextvars.Token] = None

    def __enter__(self) -> "_AgentSpan":
        self._start = time.perf_counter()
        self._token = _span_stack.set(_span_stack.get() + (self._name,))
        self._recorder.event(
            "agent_start", name=self._name, label=self._label, id=self._id)
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if self._token is not None:
            _span_stack.reset(self._token)
            self._token = None
        self._recorder.event(
            "agent_end",
            name=self._name,
            label=self._label,
            id=self._id,
            duration_ms=_duration_ms(self._start),
            status="error" if exc_type is not None else "ok",
        )
        return False  # 不吞异常


# ---------------------------------------------------------------------------
# LLM 调用包装（零侵入：包一层 provider，不改 complete 签名）
# ---------------------------------------------------------------------------

def _model_of(provider: Any) -> Optional[str]:
    """沿 ``_inner`` 链找到底层 provider 的 ``model``；找不到则退回类名（如 NullProvider）。"""
    obj = provider
    while obj is not None:
        model = getattr(obj, "model", None)
        if model:
            return str(model)
        obj = getattr(obj, "_inner", None)
    return type(provider).__name__ if provider is not None else None


class _TracedLLM:
    """记录每次 ``complete()`` 耗时的 provider 包装器（结果/异常原样透传）。"""

    def __init__(self, inner: Any, recorder: Optional[TraceRecorder]) -> None:
        self._inner = inner
        self._recorder = recorder

    def complete(self, system: str, user: str,
                 schema: dict, temperature: float = 0.2) -> dict:
        start = time.perf_counter()
        status = "ok"
        try:
            result = self._inner.complete(system, user, schema, temperature)
        except Exception as exc:  # noqa: BLE001 —— trace 只观察，不改变异常传播
            status = type(exc).__name__
            self._record_llm(start, status)
            low = str(exc).lower()
            if isinstance(exc, httpx.TimeoutException) or \
                    any(m in low for m in ("timeout", "timed out", "timedout")):
                self._record_timeout_signal(str(exc))
            raise
        self._record_llm(start, status)
        return result

    def _record_llm(self, start: float, status: str) -> None:
        if self._recorder is None:
            return
        self._recorder.event(
            "llm",
            duration_ms=_duration_ms(start),
            model=_model_of(self._inner),
            tokens=None,          # 当前 provider 不回传 usage，token 数不可获取
            status=status,
            stage=current_stage(),
        )

    def _record_timeout_signal(self, detail: str) -> None:
        if self._recorder is not None:
            self._recorder.signal("timeout", detail[:200], stage=current_stage())


def wrap_llm(llm: Any, recorder: Optional[TraceRecorder] = None) -> Any:
    """把 llm 包成带轨迹记录的 provider；已包装 / 无 recorder / None 时原样返回。"""
    if llm is None or recorder is None or isinstance(llm, _TracedLLM):
        return llm
    return _TracedLLM(llm, recorder)


# ---------------------------------------------------------------------------
# HTTP 检索调用追踪（运行时给 retrieval 共享 client 挂 transport，不改 retrieval 源码）
# ---------------------------------------------------------------------------

class _TracingTransport(httpx.BaseTransport):
    """包裹底层 transport，记录每个 HTTP 请求的耗时；无 recorder 时近零开销透传。"""

    def __init__(self, inner: httpx.BaseTransport) -> None:
        self._inner = inner

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        recorder = _current_recorder.get()
        start = time.perf_counter()
        try:
            response = self._inner.handle_request(request)
        except Exception as exc:  # noqa: BLE001
            if recorder is not None:
                recorder.event(
                    "http",
                    duration_ms=_duration_ms(start),
                    method=request.method,
                    url=str(request.url),
                    status=None,
                    error=type(exc).__name__,
                    stage=current_stage(),
                )
                if isinstance(exc, httpx.TimeoutException):
                    recorder.signal("timeout", "HTTP 检索超时：{}".format(request.url),
                                    stage=current_stage())
            raise
        if recorder is not None:
            recorder.event(
                "http",
                duration_ms=_duration_ms(start),
                method=request.method,
                url=str(request.url),
                status=response.status_code,
                stage=current_stage(),
            )
        return response

    def close(self) -> None:
        self._inner.close()


_http_patched = False


def _wrap_client_transport(client: Any) -> None:
    """给 httpx.Client 的底层 transport 挂一次 tracing transport（幂等）。"""
    transport = getattr(client, "_transport", None)
    if transport is None or isinstance(transport, _TracingTransport):
        return
    client._transport = _TracingTransport(transport)


def enable_http_tracing() -> None:
    """给 ``retrieval._http`` 的共享 client 挂 tracing transport（幂等，运行时打补丁）。

    不改 retrieval 源码；无 recorder 激活时 transport 只透传，几乎零开销。
    导入失败 / 属性缺失时静默降级（HTTP 轨迹缺失不影响主流程）。
    """
    global _http_patched
    if _http_patched:
        return
    try:
        from . import retrieval  # 惰性导入，避免模块加载期的环
    except Exception:
        return
    orig = retrieval._http

    def traced_http() -> Any:
        client = orig()
        _wrap_client_transport(client)
        return client

    retrieval._http = traced_http
    _http_patched = True


# ---------------------------------------------------------------------------
# 汇总与渲染（供 `papermine trace <run_id>` 定位瓶颈）
# ---------------------------------------------------------------------------

def _fmt_ms(ms: Any) -> str:
    try:
        v = float(ms)
    except (TypeError, ValueError):
        v = 0.0
    if v >= 1000.0:
        return "{:.2f} s".format(v / 1000.0)
    return "{:.1f} ms".format(v)


def _sort_events(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(events, key=lambda e: -float(e.get("duration_ms") or 0.0))


def summarize(run_dir: Path) -> Dict[str, Any]:
    """读取 trace.jsonl，汇总出「各环节耗时 + LLM/HTTP 调用 + 异常信号」，用于排序定位瓶颈。

    返回结构：
        {
          "run_id": str,
          "stages": [{"name","label","count","total_ms","max_ms","errors"}, ...],   # 按 total_ms 降序
          "llm": {"calls": n, "total_ms": t, "items": [...]},                       # items 按耗时降序
          "http": {"calls": n, "total_ms": t, "items": [...]},
          "signals": [...],
          "slowest": {"name","label","total_ms"} | None,
        }
    """
    path = Path(run_dir) / TRACE_FILENAME
    events = storage.read_jsonl(path) if path.exists() else []

    spans: Dict[str, Dict[str, Any]] = {}
    stage_agg: Dict[str, Dict[str, Any]] = {}
    llm_items: List[Dict[str, Any]] = []
    http_items: List[Dict[str, Any]] = []
    signals: List[Dict[str, Any]] = []

    for ev in events:
        if not isinstance(ev, dict):
            continue
        kind = ev.get("kind")
        if kind == "agent_start":
            spans[str(ev.get("id"))] = ev
        elif kind == "agent_end":
            start = spans.pop(str(ev.get("id")), None)
            name = str(ev.get("name") or (start or {}).get("name") or "")
            if not name:
                continue
            label = str(ev.get("label") or (start or {}).get("label") or STATE_LABELS.get(name, name))
            dur = float(ev.get("duration_ms") or 0.0)
            agg = stage_agg.setdefault(
                name, {"name": name, "label": label, "count": 0,
                       "total_ms": 0.0, "max_ms": 0.0, "errors": 0})
            agg["count"] += 1
            agg["total_ms"] += dur
            agg["max_ms"] = max(float(agg["max_ms"]), dur)
            if ev.get("status") == "error":
                agg["errors"] += 1
        elif kind == "llm":
            llm_items.append(ev)
        elif kind == "http":
            http_items.append(ev)
        elif kind == "signal":
            signals.append(ev)

    stages = sorted(stage_agg.values(), key=lambda a: -float(a["total_ms"]))
    llm_items = _sort_events(llm_items)
    http_items = _sort_events(http_items)

    slowest = None
    if stages:
        s = stages[0]
        slowest = {"name": s["name"], "label": s["label"], "total_ms": s["total_ms"]}

    return {
        "run_id": Path(run_dir).name,
        "stages": stages,
        "llm": {
            "calls": len(llm_items),
            "total_ms": round(sum(float(e.get("duration_ms") or 0.0) for e in llm_items), 3),
            "items": llm_items,
        },
        "http": {
            "calls": len(http_items),
            "total_ms": round(sum(float(e.get("duration_ms") or 0.0) for e in http_items), 3),
            "items": http_items,
        },
        "signals": signals,
        "slowest": slowest,
    }


def render_summary(run_dir: Path) -> str:
    """把 ``summarize`` 渲染成可读文本，按耗时排序展示各环节，末尾给出「最慢环节」。"""
    s = summarize(run_dir)
    lines = ["== papermine 执行轨迹汇总 ==", "", "run_id: {}".format(s["run_id"]), ""]

    # 1) Agent 环节耗时（降序）
    lines.append("【Agent 环节耗时（降序）】")
    if not s["stages"]:
        lines.append("  （无 agent 轨迹）")
    for i, st in enumerate(s["stages"], 1):
        extra = "，异常 {} 次".format(st["errors"]) if st["errors"] else ""
        lines.append("  {:>2}. {:<10}  {:>10}  (共 {} 次，最慢 {}{})".format(
            i, st["name"], _fmt_ms(st["total_ms"]), st["count"],
            _fmt_ms(st["max_ms"]), extra))
        lines.append("        └ {}".format(st["label"]))
    lines.append("")

    # 2) LLM 调用（降序，最多展示前 20 条）
    llm = s["llm"]
    lines.append("【LLM 调用（降序，共 {} 次，合计 {}）】".format(
        llm["calls"], _fmt_ms(llm["total_ms"])))
    if not llm["items"]:
        lines.append("  （无 LLM 调用）")
    for i, e in enumerate(llm["items"][:20], 1):
        stage = " [{}]".format(e.get("stage")) if e.get("stage") else ""
        status = "" if e.get("status") == "ok" else "  status={}".format(e.get("status"))
        lines.append("  {:>2}. {:<16} {:>10}{}{}".format(
            i, e.get("model") or "（离线/无模型）", _fmt_ms(e.get("duration_ms")), stage, status))
    if llm["calls"] > 20:
        lines.append("  （其余 {} 次省略）".format(llm["calls"] - 20))
    lines.append("")

    # 3) HTTP 检索调用（降序，最多展示前 20 条）
    http = s["http"]
    lines.append("【HTTP 检索调用（降序，共 {} 次，合计 {}）】".format(
        http["calls"], _fmt_ms(http["total_ms"])))
    if not http["items"]:
        lines.append("  （无 HTTP 检索调用）")
    for i, e in enumerate(http["items"][:20], 1):
        stage = " [{}]".format(e.get("stage")) if e.get("stage") else ""
        status = e.get("status")
        status_s = "  status={}".format(status) if status is not None else ""
        err = "  error={}".format(e.get("error")) if e.get("error") else ""
        url = str(e.get("url") or "")
        if len(url) > 72:
            url = url[:72] + "…"
        lines.append("  {:>2}. {} {} {:>10}{}{}{}".format(
            i, e.get("method") or "GET", url, _fmt_ms(e.get("duration_ms")),
            stage, status_s, err))
    if http["calls"] > 20:
        lines.append("  （其余 {} 次省略）".format(http["calls"] - 20))
    lines.append("")

    # 4) 异常信号
    lines.append("【异常信号（回炉 / 降级 / 超时）】")
    if not s["signals"]:
        lines.append("  （无）")
    for sig in s["signals"]:
        stage = " [{}]".format(sig.get("stage")) if sig.get("stage") else ""
        detail = sig.get("detail") or ""
        lines.append("  - {}{}：{}".format(sig.get("signal"), stage, detail or "（无详情）"))
    lines.append("")

    # 5) 最慢环节
    if s["slowest"]:
        st = s["slowest"]
        lines.append("最慢环节：{}（{}）  {}".format(
            st["name"], st["label"], _fmt_ms(st["total_ms"])))
    else:
        lines.append("最慢环节：（无法判定，缺少 agent 轨迹）")
    return "\n".join(lines) + "\n"
