"""M13 Agent Trace 单测：记录器、LLM/HTTP 包装、汇总排序、编排器集成。

用标准库 unittest 编写（与 tests/test_orchestrator.py 一致），
`python -m unittest discover -s tests -v` 即可运行（也兼容 pytest 收集）。
"""
from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import httpx

from papermine import cli, storage, trace
from papermine.llm import NullProvider

SAMPLE_PROJECT = Path(__file__).resolve().parent.parent / "examples" / "sample-project"


class _RecordingProvider:
    """记录 complete 调用次数并返回固定结果，带 model 属性（模拟 DeepSeekProvider）。"""

    def __init__(self, result=None, model="deepseek-chat"):
        self.model = model
        self.result = {} if result is None else result
        self.calls = 0

    def complete(self, system, user, schema, temperature=0.2):
        self.calls += 1
        return self.result


class TraceRecorderTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = os.environ.get(storage.ENV_HOME)
        os.environ[storage.ENV_HOME] = self._tmp.name
        self.run_dir = Path(self._tmp.name) / "runs" / "run_test"
        self.run_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        if self._orig is None:
            os.environ.pop(storage.ENV_HOME, None)
        else:
            os.environ[storage.ENV_HOME] = self._orig
        self._tmp.cleanup()

    def _events(self):
        return storage.read_jsonl(self.run_dir / trace.TRACE_FILENAME)

    def test_event_and_agent_span_write_jsonl(self):
        rec = trace.TraceRecorder(self.run_dir)
        rec.event("signal", signal="test")
        with rec.agent_span("IDEATE"):
            pass

        events = self._events()
        kinds = [e["kind"] for e in events]
        self.assertIn("signal", kinds)
        self.assertIn("agent_start", kinds)
        self.assertIn("agent_end", kinds)

        end = next(e for e in events if e["kind"] == "agent_end")
        self.assertEqual(end["name"], "IDEATE")
        self.assertEqual(end["status"], "ok")
        self.assertGreaterEqual(float(end["duration_ms"]), 0.0)

    def test_agent_span_records_error_status(self):
        rec = trace.TraceRecorder(self.run_dir)
        with self.assertRaises(ValueError):
            with rec.agent_span("PLAN"):
                raise ValueError("boom")
        end = [e for e in self._events() if e["kind"] == "agent_end"][0]
        self.assertEqual(end["status"], "error")

    def test_context_manager_activates_recorder(self):
        rec = trace.TraceRecorder(self.run_dir)
        self.assertIsNone(trace.current_recorder())
        with rec:
            self.assertIs(trace.current_recorder(), rec)
        self.assertIsNone(trace.current_recorder())

    def test_signal_event(self):
        rec = trace.TraceRecorder(self.run_dir)
        rec.signal("rollback", "cp3 → IDEATE", stage="EVALUATE")
        sig = [e for e in self._events() if e["kind"] == "signal"][0]
        self.assertEqual(sig["signal"], "rollback")
        self.assertEqual(sig["detail"], "cp3 → IDEATE")
        self.assertEqual(sig["stage"], "EVALUATE")


class WrapLLMTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = os.environ.get(storage.ENV_HOME)
        os.environ[storage.ENV_HOME] = self._tmp.name
        self.run_dir = Path(self._tmp.name) / "runs" / "run_test"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.rec = trace.TraceRecorder(self.run_dir)

    def tearDown(self):
        if self._orig is None:
            os.environ.pop(storage.ENV_HOME, None)
        else:
            os.environ[storage.ENV_HOME] = self._orig
        self._tmp.cleanup()

    def _llm_events(self):
        events = storage.read_jsonl(self.run_dir / trace.TRACE_FILENAME)
        return [e for e in events if e["kind"] == "llm"]

    def test_wrap_llm_records_model_and_duration(self):
        inner = _RecordingProvider(result={"ok": 1})
        wrapped = trace.wrap_llm(inner, self.rec)
        with self.rec:
            result = wrapped.complete("sys", "user", {}, 0.2)
        self.assertEqual(result, {"ok": 1})
        self.assertEqual(inner.calls, 1)

        events = self._llm_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["model"], "deepseek-chat")
        self.assertEqual(events[0]["status"], "ok")
        self.assertIsNone(events[0]["tokens"])  # token 当前不可获取
        self.assertGreaterEqual(float(events[0]["duration_ms"]), 0.0)

    def test_wrap_llm_passthrough_exception_and_records_status(self):
        inner = _RecordingProvider(result={})

        def boom(system, user, schema, temperature=0.2):
            raise RuntimeError("网络失败")

        inner.complete = boom
        wrapped = trace.wrap_llm(inner, self.rec)
        with self.rec:
            with self.assertRaises(RuntimeError):
                wrapped.complete("sys", "user", {}, 0.2)
        self.assertEqual(self._llm_events()[0]["status"], "RuntimeError")

    def test_wrap_llm_records_timeout_signal(self):
        inner = _RecordingProvider(result={})

        def timeout(system, user, schema, temperature=0.2):
            raise httpx.ConnectTimeout("read timeout")

        inner.complete = timeout
        wrapped = trace.wrap_llm(inner, self.rec)
        with self.rec:
            with self.assertRaises(httpx.ConnectTimeout):
                wrapped.complete("sys", "user", {}, 0.2)
        signals = [e for e in storage.read_jsonl(self.run_dir / trace.TRACE_FILENAME)
                   if e["kind"] == "signal"]
        self.assertTrue(any(s["signal"] == "timeout" for s in signals))

    def test_wrap_llm_noop(self):
        inner = _RecordingProvider()
        self.assertIs(trace.wrap_llm(inner, None), inner)
        self.assertIs(trace.wrap_llm(None, self.rec), None)
        already = trace.wrap_llm(inner, self.rec)
        self.assertIs(trace.wrap_llm(already, self.rec), already)


class _FakeTransport(httpx.BaseTransport):
    def __init__(self, status=200, exc=None):
        self.status = status
        self.exc = exc

    def handle_request(self, request):
        if self.exc is not None:
            raise self.exc
        return httpx.Response(self.status, request=request)


class HttpTracingTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = os.environ.get(storage.ENV_HOME)
        os.environ[storage.ENV_HOME] = self._tmp.name
        self.run_dir = Path(self._tmp.name) / "runs" / "run_test"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.rec = trace.TraceRecorder(self.run_dir)

    def tearDown(self):
        if self._orig is None:
            os.environ.pop(storage.ENV_HOME, None)
        else:
            os.environ[storage.ENV_HOME] = self._orig
        self._tmp.cleanup()

    def _http_events(self):
        events = storage.read_jsonl(self.run_dir / trace.TRACE_FILENAME)
        return [e for e in events if e["kind"] == "http"]

    def test_transport_records_http_duration(self):
        transport = trace._TracingTransport(_FakeTransport(status=200))
        req = httpx.Request("GET", "http://example.com/search")
        with self.rec:
            with self.rec.agent_span("IDEATE"):
                resp = transport.handle_request(req)
        self.assertEqual(resp.status_code, 200)
        events = self._http_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["method"], "GET")
        self.assertEqual(events[0]["url"], "http://example.com/search")
        self.assertEqual(events[0]["status"], 200)
        self.assertEqual(events[0]["stage"], "IDEATE")
        self.assertGreaterEqual(float(events[0]["duration_ms"]), 0.0)

    def test_transport_records_timeout_signal(self):
        transport = trace._TracingTransport(
            _FakeTransport(exc=httpx.ConnectTimeout("timeout")))
        req = httpx.Request("GET", "http://example.com/slow")
        with self.rec:
            with self.assertRaises(httpx.ConnectTimeout):
                transport.handle_request(req)
        events = self._http_events()
        self.assertEqual(events[0]["error"], "ConnectTimeout")
        signals = [e for e in storage.read_jsonl(self.run_dir / trace.TRACE_FILENAME)
                   if e["kind"] == "signal"]
        self.assertTrue(any(s["signal"] == "timeout" for s in signals))

    def test_transport_noop_without_recorder(self):
        transport = trace._TracingTransport(_FakeTransport(status=200))
        req = httpx.Request("GET", "http://example.com/search")
        resp = transport.handle_request(req)  # 无 recorder：透传、不写事件
        self.assertEqual(resp.status_code, 200)
        self.assertFalse((self.run_dir / trace.TRACE_FILENAME).exists())

    def test_enable_http_tracing_is_idempotent(self):
        import papermine.retrieval as retrieval
        old_flag = trace._http_patched
        original = retrieval._http
        try:
            trace._http_patched = False
            trace.enable_http_tracing()
            replaced = retrieval._http
            self.assertIsNot(replaced, original)
            trace.enable_http_tracing()   # 第二次调用：guard 命中，不重复替换
            self.assertIs(retrieval._http, replaced)
        finally:
            retrieval._http = original
            trace._http_patched = old_flag


class SummarizeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = os.environ.get(storage.ENV_HOME)
        os.environ[storage.ENV_HOME] = self._tmp.name
        self.run_dir = Path(self._tmp.name) / "runs" / "run_abc"
        self.run_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        if self._orig is None:
            os.environ.pop(storage.ENV_HOME, None)
        else:
            os.environ[storage.ENV_HOME] = self._orig
        self._tmp.cleanup()

    def _build(self):
        rec = trace.TraceRecorder(self.run_dir)
        # IDEATE 最慢（含 LLM + HTTP 子调用），EVALUATE 次之
        with rec.agent_span("UNDERSTAND"):
            pass
        with rec.agent_span("EVALUATE"):
            pass
        with rec.agent_span("IDEATE"):
            rec.event("llm", duration_ms=400.0, model="deepseek-chat", status="ok")
            rec.event("http", duration_ms=120.0, method="GET",
                      url="https://export.arxiv.org/api/query", status=200)
        rec.signal("rollback", "cp3 → IDEATE")
        return rec

    def test_summarize_ranks_stages_and_finds_slowest(self):
        self._build()
        s = trace.summarize(self.run_dir)
        self.assertEqual(s["run_id"], "run_abc")
        names = [st["name"] for st in s["stages"]]
        # IDEATE 应排第一（其内部还写了 llm/http 事件，但阶段耗时按 span 计时）
        self.assertEqual(names[0], "IDEATE")
        self.assertIn("UNDERSTAND", names)
        self.assertIn("EVALUATE", names)
        self.assertEqual(s["slowest"]["name"], "IDEATE")
        self.assertEqual(s["llm"]["calls"], 1)
        self.assertEqual(s["http"]["calls"], 1)
        self.assertEqual(len(s["signals"]), 1)

    def test_render_summary_contains_slowest_and_sorted(self):
        self._build()
        out = trace.render_summary(self.run_dir)
        self.assertIn("最慢环节：IDEATE", out)
        self.assertIn("【Agent 环节耗时（降序）】", out)
        self.assertIn("【LLM 调用（降序", out)
        self.assertIn("【HTTP 检索调用（降序", out)
        self.assertIn("【异常信号（回炉 / 降级 / 超时）】", out)
        # IDEATE 排在 EVALUATE 之前
        self.assertLess(out.index("IDEATE"), out.index("EVALUATE"))

    def test_summarize_missing_file_is_empty(self):
        s = trace.summarize(self.run_dir / "nonexistent")
        self.assertEqual(s["stages"], [])
        self.assertEqual(s["slowest"], None)
        self.assertIn("（无 agent 轨迹）", trace.render_summary(self.run_dir / "nonexistent"))


class OrchestratorTraceIntegrationTest(unittest.TestCase):
    """验收 #2：跑一次 analyze，trace.jsonl 能看到各 Agent 耗时。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = os.environ.get(storage.ENV_HOME)
        os.environ[storage.ENV_HOME] = self._tmp.name
        storage.ensure_layout()
        self._llm_patch = mock.patch.object(
            cli.orchestrator, "get_provider", return_value=NullProvider())
        self._retrieval_patch = mock.patch.object(
            cli.orchestrator.ideate, "search_literature", return_value=[])
        self._llm_patch.start()
        self._retrieval_patch.start()

    def tearDown(self):
        self._retrieval_patch.stop()
        self._llm_patch.stop()
        if self._orig is None:
            os.environ.pop(storage.ENV_HOME, None)
        else:
            os.environ[storage.ENV_HOME] = self._orig
        self._tmp.cleanup()

    def test_analyze_writes_agent_trace(self):
        run_id = cli.orchestrator.run_pipeline(str(SAMPLE_PROJECT), auto=True)
        run_dir = storage.run_dir(run_id)
        trace_path = run_dir / trace.TRACE_FILENAME
        self.assertTrue(trace_path.exists())

        events = storage.read_jsonl(trace_path)
        ends = {e["name"] for e in events if e["kind"] == "agent_end"}
        starts = {e["name"] for e in events if e["kind"] == "agent_start"}
        expected = {"UNDERSTAND", "ABSTRACT", "IDEATE", "EVALUATE", "PLAN", "REFLECT"}
        self.assertEqual(ends, expected)
        self.assertEqual(starts, expected)
        # 每个 agent_end 都带耗时
        for e in events:
            if e["kind"] == "agent_end":
                self.assertIn("duration_ms", e)

    def test_trace_subcommand_renders_slowest(self):
        run_id = cli.orchestrator.run_pipeline(str(SAMPLE_PROJECT), auto=True)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(["trace", run_id])
        self.assertEqual(rc, 0)
        self.assertIn("最慢环节", buf.getvalue())
        self.assertIn("IDEATE", buf.getvalue())

    def test_trace_subcommand_missing_run_returns_2(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(["trace", "no_such_run_12345"])
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
