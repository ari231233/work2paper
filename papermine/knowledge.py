"""六元组要素抽取：关键词词典 + 代码/文档信号 -> Element + 证据列表。

关键词词典是 MVP 的"知识源"：把工程信号（库名、函数名、文档用语）映射为学术语言。
后续版本可替换为本地 LLM 的语义抽取，接口保持不变。
"""
from __future__ import annotations

import os
import re
from typing import Dict, List, Tuple

from .extractor.code_extractor import analyze_code, is_reusable_name
from .extractor.doc_extractor import read_asset_text
from .models import Asset, Element, Evidence, Project

# ---- 关键词词典：标签 -> [关键词...] ----
TASK_DICT = {
    "分类": ["分类", "classify", "classification", "classifier"],
    "回归预测": ["回归", "regression", "regressor"],
    "时序预测": ["时序预测", "时间序列预测", "forecast", "forecasting"],
    "异常检测": ["异常检测", "异常点", "anomaly", "outlier", "anomaly detection"],
    "剩余寿命预测": ["剩余寿命", "剩余使用寿命", "rul", "remaining useful life", "寿命预测"],
    "数据清洗": ["数据清洗", "清洗", "预处理", "clean", "cleaning", "preprocess", "preprocessing"],
    "特征工程": ["特征工程", "特征提取", "feature engineering", "feature extraction"],
    "聚类": ["聚类", "cluster", "clustering"],
    "推荐": ["推荐", "recommend", "recommendation"],
    "目标检测": ["目标检测", "object detection", "yolo", "检测框"],
    "文本处理": ["文本", "自然语言", "nlp", "natural language", "分词"],
}

METHOD_DICT = {
    "深度学习": ["深度学习", "deep learning", "神经网络", "neural", "lstm", "transformer", "cnn", "rnn", "gru", "attention"],
    "孤立森林": ["孤立森林", "isolation forest", "isolationforest"],
    "随机森林": ["随机森林", "random forest", "randomforest"],
    "SVM": ["svm", "support vector"],
    "XGBoost": ["xgboost", "梯度提升", "gradient boosting", "gbdt", "lightgbm"],
    "时间序列模型": ["arima", "sarima", "holt-winters", "prophet"],
    "统计方法": ["统计", "statistic", "假设检验", "显著性"],
    "集成学习": ["集成", "ensemble", "stacking", "bagging", "boosting"],
    "流水线/框架": ["pipeline", "流水线", "framework", "框架", "workflow"],
}

DATA_DICT = {
    "时序数据": ["时序", "时间序列", "time series", "timeseries", "temporal", "传感器", "sensor", "信号", "signal"],
    "表格数据": ["表格", "tabular", "csv", "excel", "结构化数据"],
    "文本数据": ["文本", "text", "语料", "corpus"],
    "图像数据": ["图像", "image", "图片", "png", "jpg", "jpeg"],
    "数据库": ["数据库", "database", "sql", "mysql", "postgres", "mongodb"],
    "工业设备数据": ["设备", "机器", "machine", "equipment", "产线", "生产线", "工况"],
}

SCENARIO_DICT = {
    "工业制造": ["工业", "制造", "industrial", "manufacturing", "工厂", "生产线", "设备"],
    "预测性维护": ["预测性维护", "predictive maintenance", "故障诊断", "故障预测", "fault", "剩余寿命"],
    "医疗健康": ["医疗", "临床", "医学", "medical", "clinical", "医院", "患者"],
    "金融风控": ["金融", "风控", "finance", "risk control", "信贷"],
    "物联网": ["物联网", "iot", "设备联网", "边缘"],
    "能源电力": ["能源", "电力", "energy", "power", "电网", "风电", "光伏"],
}

METRIC_DICT = {
    "准确率": ["准确率", "accuracy", "acc"],
    "F1": ["f1", "f1-score", "f1score"],
    "精确率": ["精确率", "precision"],
    "召回率": ["召回率", "recall"],
    "AUC": ["auc", "roc", "roc-auc"],
    "MSE/MAE": ["mse", "mae", "rmse", "均方误差", "平均绝对误差"],
}

ALL_DICTS = {
    "task": TASK_DICT,
    "method": METHOD_DICT,
    "data": DATA_DICT,
    "scenario": SCENARIO_DICT,
    "metric": METRIC_DICT,
}

CATEGORY_CN = {"task": "任务", "method": "方法", "data": "数据", "scenario": "场景", "metric": "指标"}


def _kw_count(kw: str, text_lower: str) -> int:
    """统计关键词命中次数。短英文词用词边界，其余用子串匹配以覆盖驼峰/下划线命名。"""
    if re.search(r"[a-z]", kw):
        if len(kw) < 4:
            return len(re.findall(r"\b" + re.escape(kw) + r"\b", text_lower))
        return text_lower.count(kw)
    return text_lower.count(kw)


def _match_labels(text_lower: str) -> Dict[str, Dict[str, int]]:
    """返回 {category: {label: count}}。"""
    result: Dict[str, Dict[str, int]] = {}
    for cat, mapping in ALL_DICTS.items():
        counts: Dict[str, int] = {}
        for label, kws in mapping.items():
            c = sum(_kw_count(kw, text_lower) for kw in kws)
            if c > 0:
                counts[label] = c
        result[cat] = counts
    return result


def _ranked(counts: Dict[str, int]) -> List[str]:
    return [k for k, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]


def extract_elements(project: Project) -> Tuple[Element, List[Evidence]]:
    """扫描项目所有资产，抽取六元组要素与证据。"""
    element = Element()
    evidence: List[Evidence] = []
    acc: Dict[str, Dict[str, int]] = {cat: {} for cat in ALL_DICTS}
    lib_counts: Dict[str, int] = {}
    reusable: List[Tuple[str, str]] = []

    for asset in project.assets:
        full = os.path.join(project.root, asset.path)
        text_lower = ""

        if asset.kind == "code" and asset.language == "py":
            try:
                with open(full, "r", encoding="utf-8", errors="ignore") as fh:
                    source = fh.read()
            except OSError:
                continue
            sig = analyze_code(source)
            if sig["error"]:
                continue
            text_lower = sig["text"].lower()
            for lib in sig["imports"]:
                lib_counts[lib] = lib_counts.get(lib, 0) + 1
            for name in sig["classes"] + sig["functions"]:
                if is_reusable_name(name):
                    reusable.append((name, asset.path))
        elif asset.kind in ("readme", "doc"):
            text_lower = read_asset_text(project.root, asset).lower()
        else:
            continue

        # 目录名本身也可能是可复用组件线索（utils/common/lib 等）
        dir_segments = set(asset.path.split("/")[:-1])
        if dir_segments & {"utils", "common", "lib", "core", "framework", "toolkit"}:
            reusable.append((f"[目录]{asset.path.split('/')[-2] if len(asset.path.split('/')) > 1 else 'utils'}", asset.path))

        labels = _match_labels(text_lower)
        for cat, counts in labels.items():
            for label, cnt in counts.items():
                acc[cat][label] = acc[cat].get(label, 0) + cnt
                evidence.append(Evidence(
                    source=asset.path,
                    snippet="命中{}「{}」（{} 次）".format(CATEGORY_CN[cat], label, cnt),
                ))

    element.tasks = _ranked(acc["task"])
    element.methods = _ranked(acc["method"])
    element.data = _ranked(acc["data"])
    element.scenarios = _ranked(acc["scenario"])
    element.metrics = _ranked(acc["metric"])
    element.libraries = [k for k, _ in sorted(lib_counts.items(), key=lambda kv: -kv[1])]

    seen: set = set()
    for name, src in reusable:
        if name not in seen:
            seen.add(name)
            element.modules.append(name)
            evidence.append(Evidence(source=src, snippet="可复用组件「{}」".format(name)))

    return element, evidence
