"""文档要素抽取：读取 README / docs 文本内容。"""
from __future__ import annotations

import os

from ..models import Asset


def read_asset_text(root: str, asset: Asset, max_chars: int = 200_000) -> str:
    """按资产路径读取文本（仅文本类；二进制格式读取失败时返回空串）。"""
    full = os.path.join(root, asset.path)
    try:
        with open(full, "r", encoding="utf-8", errors="ignore") as fh:
            return fh.read(max_chars)
    except (OSError, UnicodeDecodeError):
        return ""
