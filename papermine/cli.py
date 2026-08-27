"""命令行入口。

- 旧版（v0.1 确定性管线）：``python -m papermine <path>``（保持向后兼容）。
- 新版（M7 编排器）：``analyze`` / ``resume`` / ``status`` 子命令。
- M13：``trace`` 子命令（按耗时排序汇总执行轨迹，定位瓶颈）。
- M26：``web`` 子命令（统一启动本地 FastAPI + Next.js）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import orchestrator, storage, trace
from .pipeline import run
from .report import render_markdown, report_to_dict


def _reconfigure_stdio() -> None:
    """尽量把标准流切到 UTF-8，缓解 Windows 控制台中文乱码。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


# ---------------------------------------------------------------------------
# 新版子命令（M7）
# ---------------------------------------------------------------------------

def _print_status(run_id: str) -> None:
    st = orchestrator.status(run_id)
    print("run_id: {}".format(st["run_id"]))
    print("状态: {}".format(st["state"]))
    print("dossier 版本: {}".format(st["dossier_version"]))
    print("数据目录: {}".format(storage.run_dir(st["run_id"])))


def _cmd_analyze(argv) -> int:
    parser = argparse.ArgumentParser(
        prog="papermine analyze",
        description="端到端分析：扫描→理解→抽象→检索创新→评估→路线→经验沉淀。",
    )
    parser.add_argument("path", help="项目根目录")
    parser.add_argument("--auto", action="store_true",
                        help="跳过检查点暂停，自动接受（默认在每个检查点暂停等输入）")
    ns = parser.parse_args(argv)

    root = os.path.abspath(ns.path)
    if not os.path.isdir(root):
        print("错误：目录不存在：{}".format(root), file=sys.stderr)
        return 2

    run_id = orchestrator.run_pipeline(root, auto=ns.auto)
    _print_status(run_id)
    return 0


def _cmd_resume(argv) -> int:
    parser = argparse.ArgumentParser(
        prog="papermine resume",
        description="从上次检查点续跑一个分析。",
    )
    parser.add_argument("run_id", help="要续跑的 run_id")
    parser.add_argument("--auto", action="store_true", help="跳过剩余检查点暂停")
    ns = parser.parse_args(argv)

    run_id = orchestrator.resume(ns.run_id, auto=ns.auto)
    _print_status(run_id)
    return 0


def _cmd_status(argv) -> int:
    parser = argparse.ArgumentParser(
        prog="papermine status",
        description="查看一个 run 的进度。",
    )
    parser.add_argument("run_id", help="run_id")
    ns = parser.parse_args(argv)

    st = orchestrator.status(ns.run_id)
    print(json.dumps(st, ensure_ascii=False, indent=2))
    return 0


def _cmd_trace(argv) -> int:
    parser = argparse.ArgumentParser(
        prog="papermine trace",
        description="按耗时排序汇总一次分析的执行轨迹，定位哪个环节最慢。",
    )
    parser.add_argument("run_id", help="run_id")
    ns = parser.parse_args(argv)

    run_dir = storage.run_dir(ns.run_id)
    if not (run_dir / trace.TRACE_FILENAME).exists():
        print("错误：run 不存在或没有轨迹文件：{}".format(ns.run_id), file=sys.stderr)
        return 2
    sys.stdout.write(trace.render_summary(run_dir))
    return 0


def _cmd_web(argv) -> int:
    parser = argparse.ArgumentParser(
        prog="papermine web",
        description="启动本地科研决策工作台（FastAPI + Next.js）。",
    )
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认仅本机）")
    parser.add_argument("--api-port", type=int, default=8000, help="后端端口，默认 8000")
    parser.add_argument("--web-port", type=int, default=3000, help="前端端口，默认 3000")
    parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    parser.add_argument("--dev", action="store_true", help="使用 Next.js 开发服务器")
    ns = parser.parse_args(argv)
    if not (1 <= ns.api_port <= 65535 and 1 <= ns.web_port <= 65535):
        print("错误：端口必须在 1~65535 之间", file=sys.stderr)
        return 2
    if ns.api_port == ns.web_port:
        print("错误：前后端端口不能相同", file=sys.stderr)
        return 2
    try:
        from .web_launcher import run_web
        return run_web(
            host=ns.host, api_port=ns.api_port, web_port=ns.web_port,
            open_browser=not ns.no_browser, dev=ns.dev,
        )
    except (ImportError, RuntimeError, OSError) as exc:
        print("错误：{}".format(exc), file=sys.stderr)
        return 2


def _dispatch(command: str, argv) -> int:
    if command == "analyze":
        return _cmd_analyze(argv)
    if command == "resume":
        return _cmd_resume(argv)
    if command == "status":
        return _cmd_status(argv)
    if command == "trace":
        return _cmd_trace(argv)
    if command == "web":
        return _cmd_web(argv)
    return 2


# ---------------------------------------------------------------------------
# 旧版（v0.1 确定性管线），保持向后兼容
# ---------------------------------------------------------------------------

def _legacy_main(argv) -> int:
    parser = argparse.ArgumentParser(
        prog="papermine",
        description="从横向项目工作（代码 + 文档）中挖掘候选论文点，输出评估报告。",
    )
    parser.add_argument("path", help="项目根目录")
    parser.add_argument("-o", "--output", help="将 Markdown 报告写入该文件（默认打印到终端）")
    parser.add_argument("--json", metavar="FILE", help="将结构化结果另存为 JSON 文件")
    args = parser.parse_args(argv)

    root = os.path.abspath(args.path)
    if not os.path.isdir(root):
        print("错误：目录不存在：{}".format(root), file=sys.stderr)
        return 2

    report = run(root)
    md = render_markdown(report)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(md)
        print("报告已写入：{}".format(args.output))
    else:
        sys.stdout.write(md)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(report_to_dict(report), fh, ensure_ascii=False, indent=2)
        print("JSON 已写入：{}".format(args.json))

    return 0


def main(argv=None) -> int:
    _reconfigure_stdio()
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("analyze", "resume", "status", "trace", "web"):
        return _dispatch(argv[0], argv[1:])
    return _legacy_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
