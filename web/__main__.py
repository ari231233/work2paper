"""``python -m web`` 启动 FastAPI 服务（uvicorn）。

依赖可选 extra ``web``（fastapi + uvicorn）：``pip install -e ".[web]"``。
"""
from __future__ import annotations

import uvicorn

from .app import create_app


def main() -> None:
    uvicorn.run(create_app(), host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
