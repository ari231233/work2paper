"""模型训练入口。"""
import torch
import torch.nn as nn
from sklearn.metrics import f1_score, mean_squared_error


class LSTMRUL(nn.Module):
    """基于 LSTM 的剩余寿命预测。"""

    def __init__(self, input_size, hidden_size):
        super().__init__()
        self.lstm = nn.LSTM(input_size, hidden_size)

    def forward(self, x):
        return self.lstm(x)


def train_lstm(model, data):
    return model


def evaluate(y_true, y_pred):
    f1 = f1_score(y_true, y_pred, average="macro")
    mse = mean_squared_error(y_true, y_pred)
    return f1, mse
