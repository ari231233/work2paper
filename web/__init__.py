"""papermine Web 外壳：FastAPI REST API（围绕 Dossier）。

对应 docs/build-plan.md §4 M24 与 docs/web-demo.md：

- 把 Python 核心暴露成 REST API，前端只经 API 访问（不直接碰 Dossier / Agent）。
- **模块化重跑**是核心：暴露单 Agent / 单环节的重跑端点（refine / evaluate / retrieve-more），
  而非只暴露全量 analyze。
- 核心引擎不改接口契约（docs/build-plan.md §3），本包是薄封装
  （docs/architecture.md §15「核心引擎 + 多外壳」）。

FastAPI / uvicorn 属于可选依赖（``pip install -e ".[web]"``），核心依赖仍只有 httpx。
"""
from __future__ import annotations

from .app import create_app

__all__ = ["create_app"]
__version__ = "0.1.0"
