"""资产扫描：遍历项目目录，识别代码 / 文档 / 配置文件，忽略噪声目录。"""
from __future__ import annotations

import os
from typing import List

from .models import Asset

IGNORE_DIRS = {
    ".git", ".svn", ".hg", "__pycache__", ".venv", "venv", "env",
    "node_modules", "dist", "build", ".idea", ".vscode", ".mypy_cache",
    ".pytest_cache", ".tox",
}

CODE_EXTS = {".py", ".pyx", ".ipynb", ".js", ".ts", ".cpp", ".c", ".java", ".go", ".r", ".m"}
DOC_EXTS = {".md", ".markdown", ".txt", ".rst", ".tex", ".docx", ".pptx", ".pdf"}
CONFIG_EXTS = {".json", ".yaml", ".yml", ".toml", ".cfg", ".ini", ".xml"}

_KIND_ORDER = {"readme": 0, "doc": 1, "code": 2, "config": 3, "other": 4}


def _relative(root: str, path: str) -> str:
    return os.path.relpath(path, root).replace("\\", "/")


def scan(root: str) -> List[Asset]:
    """递归扫描目录，返回资产列表（文档/README 优先，代码其次）。"""
    assets: List[Asset] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS and not d.startswith(".")]
        for fname in filenames:
            full = os.path.join(dirpath, fname)
            rel = _relative(root, full)
            ext = os.path.splitext(fname)[1].lower()
            base = fname.lower()

            if base in {"readme.md", "readme.rst", "readme.txt", "readme"}:
                kind = "readme"
            elif ext in CODE_EXTS:
                kind = "code"
            elif ext in DOC_EXTS:
                kind = "doc"
            elif ext in CONFIG_EXTS:
                kind = "config"
            else:
                kind = "other"

            language = ext.lstrip(".") if kind == "code" else None
            assets.append(Asset(path=rel, kind=kind, language=language))

    assets.sort(key=lambda a: (_KIND_ORDER.get(a.kind, 5), a.path))
    return assets
