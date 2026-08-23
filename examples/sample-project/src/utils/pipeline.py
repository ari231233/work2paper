"""通用数据处理流水线（可复用组件）。"""
import pandas as pd


class DataPipeline:
    """可复用的数据处理流水线。"""

    def __init__(self, steps):
        self.steps = steps

    def run(self, data):
        for step in self.steps:
            data = step(data)
        return data


class FeaturePipeline:
    """特征工程流水线。"""

    def build(self, df):
        return df
