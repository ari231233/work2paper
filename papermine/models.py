"""核心数据模型：资产、六元组要素、候选论文点、项目报告。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class Asset:
    """扫描到的一个文件/资产。"""
    path: str                 # 相对项目根的路径
    kind: str                 # code | readme | doc | config | other
    language: Optional[str] = None


@dataclass
class Element:
    """单个项目抽取出的六元组要素 + 附加信号。

    六元组：任务 - 方法 - 数据 - 场景 - 指标（+ 依赖库 / 可复用组件）。
    """
    tasks: List[str] = field(default_factory=list)
    methods: List[str] = field(default_factory=list)
    data: List[str] = field(default_factory=list)
    scenarios: List[str] = field(default_factory=list)
    metrics: List[str] = field(default_factory=list)
    libraries: List[str] = field(default_factory=list)
    modules: List[str] = field(default_factory=list)   # 可复用组件名（类/函数/目录）


@dataclass
class Project:
    name: str
    root: str
    assets: List[Asset] = field(default_factory=list)
    element: Element = field(default_factory=Element)


@dataclass
class Evidence:
    """一条支撑证据：来源文件 + 命中的片段描述。"""
    source: str
    snippet: str


@dataclass
class PaperPoint:
    """一个候选论文点。"""
    title: str
    one_line: str
    paper_type: str           # 方法论文 | 系统/工具论文 | 实证/应用论文
    target: str               # 建议投稿档位
    novelty: int              # 1-5 星
    workload_hours: int
    data_availability: str
    risk: str
    evidence: List[Evidence] = field(default_factory=list)


@dataclass
class ProjectReport:
    project: Project
    points: List[PaperPoint] = field(default_factory=list)
