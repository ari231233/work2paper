"""流水线编排：扫描 -> 抽取 -> 论文点生成 -> 报告。"""
from __future__ import annotations

import os

from .knowledge import extract_elements
from .mining import generate_points
from .models import Project, ProjectReport
from .scanner import scan


def run(root: str) -> ProjectReport:
    """对单个项目目录跑完整链路。"""
    root = os.path.abspath(root)
    name = os.path.basename(root.rstrip(os.sep)) or root
    assets = scan(root)
    project = Project(name=name, root=root, assets=assets)
    element, evidence = extract_elements(project)
    project.element = element
    points = generate_points(project, evidence)
    return ProjectReport(project=project, points=points)
