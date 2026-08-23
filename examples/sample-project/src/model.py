"""异常检测与寿命预测模型。"""
from sklearn.ensemble import IsolationForest, RandomForestRegressor
import xgboost as xgb


class AnomalyDetector:
    """基于孤立森林的异常检测。"""

    def __init__(self):
        self.model = IsolationForest()

    def fit(self, X):
        self.model.fit(X)

    def predict(self, X):
        return self.model.predict(X)
