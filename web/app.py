"""FastAPI 应用工厂（Web 外壳入口）。

组装 FastAPI app、挂 CORS、注册 ``web.api`` 路由。供 ``python -m web``（uvicorn）与
测试（``fastapi.testclient.TestClient``）共用，保证同一路由契约只定义一份。
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api import router


def create_app() -> FastAPI:
    """创建 FastAPI 应用实例（薄封装：不承载业务逻辑，只组装路由与中间件）。"""
    app = FastAPI(
        title="PaperMine Web",
        version="0.1.0",
        description="科研决策工作台后端：把 PaperMine Python 核心暴露成 REST API（围绕 Dossier）。",
    )
    # 本地 demo：允许任意来源（M25 Next.js 前端通常在 127.0.0.1:3000）。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(router)
    return app
