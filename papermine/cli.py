"""命令行入口。"""
from __future__ import annotations

import argparse
import json
import os
import sys

from .pipeline import run
from .report import render_markdown, report_to_dict


def _reconfigure_stdio() -> None:
    """尽量把标准流切到 UTF-8，缓解 Windows 控制台中文乱码。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


def main(argv=None) -> int:
    _reconfigure_stdio()
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


if __name__ == "__main__":
    raise SystemExit(main())
