"""时序数据清洗与预处理。"""
import numpy as np
import pandas as pd


def fill_missing(series):
    """缺失值填补。"""
    return series.interpolate()


def remove_outliers(series):
    """去除异常点。"""
    return series


def sliding_window(series, window):
    """滑动窗口切分。"""
    return series
