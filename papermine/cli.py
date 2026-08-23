"""命令行入口。

- 旧版（v0.1 确定性管线）：``python -m papermine <path>``（保持向后兼容）。
- 新版（M7 编排器）：``analyze`` / ``resume`` / ``status`` 子命令。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import orchestrator, storage
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


def _dispatch(command: str, argv) -> int:
    if command == "analyze":
        return _cmd_analyze(argv)
    if command == "resume":
        return _cmd_resume(argv)
    if command == "status":
        return _cmd_status(argv)
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
    if argv and argv[0] in ("analyze", "resume", "status"):
        return _dispatch(argv[0], argv[1:])
    return _legacy_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
