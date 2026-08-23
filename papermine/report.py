"""报告渲染：把项目报告渲染为 Markdown，并提供 JSON 序列化。"""
from __future__ import annotations

from typing import Any, Dict, List

from .models import ProjectReport


def _stars(n: int) -> str:
    return "★" * n + "☆" * (5 - n)


def render_markdown(report: ProjectReport) -> str:
    p = report.project
    e = p.element
    lines: List[str] = []

    lines.append("# 论文点挖掘报告：{}".format(p.name))
    lines.append("")
    lines.append("> 由 papermine 自动生成 —— 把横向工作转成候选论文点。")
    lines.append("> 本工具只做「看见 + 评估」，不代写正文。")
    lines.append("")
    lines.append("## 一、项目画像（六元组）")
    lines.append("")

    rows = [
        ("任务", e.tasks),
        ("方法", e.methods),
        ("数据", e.data),
        ("场景", e.scenarios),
        ("指标", e.metrics),
        ("依赖库", e.libraries),
        ("可复用组件", e.modules),
    ]
    for label, items in rows:
        val = "、".join(items) if items else "（未识别到）"
        lines.append("- **{}**：{}".format(label, val))
    lines.append("")
    lines.append("- **资产数**：{}（含代码/文档/配置）".format(len(p.assets)))
    lines.append("")

    lines.append("## 二、候选论文点")
    lines.append("")
    if not report.points:
        lines.append("暂未生成候选点。建议补充项目文档或代码注释以增强信号。")
    for i, pt in enumerate(report.points, 1):
        lines.append("### 候选点 #{}：{}".format(i, pt.title))
        lines.append("")
        lines.append("- **一句话贡献**：{}".format(pt.one_line))
        lines.append("- **论文类型**：{}".format(pt.paper_type))
        lines.append("- **建议档位**：{}".format(pt.target))
        lines.append("- **新颖性**：{}（{}/5）".format(_stars(pt.novelty), pt.novelty))
        lines.append("- **预计工作量**：约 {} 小时".format(pt.workload_hours))
        lines.append("- **数据可得性**：{}".format(pt.data_availability))
        lines.append("- **主要风险**：{}".format(pt.risk))
        lines.append("- **支撑证据**：")
        if pt.evidence:
            for ev in pt.evidence[:6]:
                lines.append("  - `{}` — {}".format(ev.source, ev.snippet))
        else:
            lines.append("  - （无）")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("*MVP 版本：关键词 + 规则驱动。后续可接入本地 LLM 做语义级共性挖掘与文献 gap 对照。*")
    return "\n".join(lines) + "\n"


def report_to_dict(report: ProjectReport) -> Dict[str, Any]:
    e = report.project.element
    return {
        "project": report.project.name,
        "root": report.project.root,
        "element": {
            "tasks": e.tasks,
            "methods": e.methods,
            "data": e.data,
            "scenarios": e.scenarios,
            "metrics": e.metrics,
            "libraries": e.libraries,
            "modules": e.modules,
        },
        "points": [
            {
                "title": pt.title,
                "one_line": pt.one_line,
                "paper_type": pt.paper_type,
                "target": pt.target,
                "novelty": pt.novelty,
                "workload_hours": pt.workload_hours,
                "data_availability": pt.data_availability,
                "risk": pt.risk,
                "evidence": [{"source": ev.source, "snippet": ev.snippet} for ev in pt.evidence],
            }
            for pt in report.points
        ],
    }
