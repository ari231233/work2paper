"""论文点生成：基于六元组要素，用规则组合生成候选论文点并打分排序。

MVP 用规则 + 启发式打分；后续可替换为 LLM 归纳 + 文献 gap 对照。
"""
from __future__ import annotations

from typing import List

from .models import Element, Evidence, PaperPoint, Project

DL_LIBS = {"pytorch", "tensorflow", "keras", "transformers"}

# 这类"方法"其实是系统/工具信号，不应生成方法论文点
SYSTEM_METHODS = {"流水线/框架"}

# 只有"建模类任务"才适合生成方法论文点；预处理类任务（数据清洗/特征工程）不参与方法组合
MODELING_TASKS = {"分类", "回归预测", "时序预测", "异常检测", "剩余寿命预测", "聚类", "推荐", "目标检测"}

# 常见方法的饱和度高，新颖性加成低；相对冷门/新颖的方法加成高
METHOD_NOVELTY_BONUS = {
    "深度学习": 1,
    "孤立森林": 1,
    "时间序列模型": 1,
}


def _stars(n: int) -> str:
    return "★" * n + "☆" * (5 - n)


def _novelty(element: Element, has_scenario: bool, method: str) -> int:
    score = 2
    if has_scenario:
        score += 1
    if element.metrics:
        score += 1
    score += METHOD_NOVELTY_BONUS.get(method, 0)
    return min(5, score)


def _workload(n_evidence: int, is_dl: bool) -> int:
    base = 40 if is_dl else 25
    return min(90, base + min(n_evidence, 10) * 4)


def _target(paper_type: str, novelty: int) -> str:
    if paper_type == "方法论文":
        return "CCF-B / 中文核心或 EI 会议" if novelty >= 4 else "CCF-C / 中文核心"
    if paper_type == "系统/工具论文":
        return "中文核心 / EI 会议（系统类）"
    return "中文核心 / 应用类期刊"


def _data_availability(element: Element) -> str:
    if element.data and element.metrics:
        return "高（有数据来源且有指标）"
    if element.data:
        return "中（有数据来源，需补统一评测指标）"
    return "需补充评测数据"


def _risk(element: Element) -> str:
    if not element.metrics:
        return "缺少量化指标，需补统一评测与 baseline 对比"
    return "数据可得性较好，主要风险在与已有方法的对比实验"


def _attach(point: PaperPoint, evidence: List[Evidence], terms: List[str], limit: int = 6) -> None:
    """按关键词把相关证据挂到论文点上。"""
    picked = [ev for ev in evidence if any(t and t in ev.snippet for t in terms)]
    for ev in picked[:limit]:
        if all(ev.source != p.source or ev.snippet != p.snippet for p in point.evidence):
            point.evidence.append(ev)


def generate_points(project: Project, evidence: List[Evidence]) -> List[PaperPoint]:
    e = project.element
    points: List[PaperPoint] = []
    scenario = e.scenarios[0] if e.scenarios else ""
    scenario_text = scenario if scenario else "跨项目"

    top_task = e.tasks[0] if e.tasks else ""
    methods = [m for m in e.methods if m not in SYSTEM_METHODS]
    if any(lib in DL_LIBS for lib in e.libraries) and "深度学习" not in methods:
        methods.append("深度学习")

    # 1. 方法论文点：任务 × 方法 组合（仅建模类任务）
    modeling_tasks = [t for t in e.tasks if t in MODELING_TASKS]
    for task in modeling_tasks[:3]:
        for method in methods[:2]:
            if not method or task == method:
                continue
            is_dl = method == "深度学习" or any(lib in DL_LIBS for lib in e.libraries)
            novelty = _novelty(e, bool(scenario), method)
            title = "面向{}场景的{}方法：基于{}的方案".format(scenario_text, task, method)
            point = PaperPoint(
                title=title,
                one_line="针对横向项目中反复出现的{}问题，引入{}形成一套可复用的{}方案。".format(task, method, task),
                paper_type="方法论文",
                target=_target("方法论文", novelty),
                novelty=novelty,
                workload_hours=_workload(len(evidence), is_dl),
                data_availability=_data_availability(e),
                risk=_risk(e),
            )
            _attach(point, evidence, [task, method])
            points.append(point)

    # 2. 系统/工具论文点：存在可复用组件
    if e.modules:
        comps = "、".join(e.modules[:4])
        focus = top_task if top_task else "数据处理"
        title = "面向{}任务的通用{}工具/框架".format(scenario_text, focus)
        novelty = _novelty(e, bool(scenario), "")
        point = PaperPoint(
            title=title,
            one_line="将项目中沉淀的可复用组件（{}）抽象为通用工具/框架，降低同类横向任务的重复开发成本。".format(comps),
            paper_type="系统/工具论文",
            target=_target("系统/工具论文", novelty),
            novelty=novelty,
            workload_hours=_workload(len(evidence), False),
            data_availability=_data_availability(e),
            risk=_risk(e),
        )
        _attach(point, evidence, [c for c in e.modules[:4]])
        points.append(point)

    # 3. 实证/应用论文点：场景 × 任务
    if scenario and e.tasks:
        tasks_text = "、".join(e.tasks[:2])
        title = "{}场景下{}的实证研究".format(scenario, tasks_text)
        novelty = _novelty(e, True, "")
        point = PaperPoint(
            title=title,
            one_line="以{}为背景，系统报告{}任务在真实数据上的方法对比与工程经验。".format(scenario, tasks_text),
            paper_type="实证/应用论文",
            target=_target("实证/应用论文", novelty),
            novelty=novelty,
            workload_hours=_workload(len(evidence), False),
            data_availability=_data_availability(e),
            risk=_risk(e),
        )
        _attach(point, evidence, [scenario] + e.tasks[:2])
        points.append(point)

    # 去重 + 按新颖性/工作量排序
    seen_titles: set = set()
    deduped: List[PaperPoint] = []
    for p in points:
        if p.title not in seen_titles:
            seen_titles.add(p.title)
            deduped.append(p)
    deduped.sort(key=lambda p: (-p.novelty, p.workload_hours))
    return deduped
