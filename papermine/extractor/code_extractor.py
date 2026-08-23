"""代码要素抽取：基于 AST 分析 Python 源码，提取导入 / 符号 / 文本信号。"""
from __future__ import annotations

import ast
from typing import Dict, List

# 导入根模块 -> 规范库名
LIB_ALIASES = {
    "numpy": "numpy",
    "pandas": "pandas",
    "sklearn": "scikit-learn",
    "scikit_learn": "scikit-learn",
    "torch": "pytorch",
    "tensorflow": "tensorflow",
    "keras": "keras",
    "xgboost": "xgboost",
    "lightgbm": "lightgbm",
    "statsmodels": "statsmodels",
    "scipy": "scipy",
    "matplotlib": "matplotlib",
    "seaborn": "seaborn",
    "transformers": "transformers",
    "cv2": "opencv",
    "skimage": "scikit-image",
    "nltk": "nltk",
    "jieba": "jieba",
    "networkx": "networkx",
}

# 可复用组件命名线索（出现在类/函数名中）
REUSABLE_HINTS = (
    "pipeline", "framework", "tool", "util", "common", "base", "abstract",
    "generic", "reusable", "engine", "builder", "factory", "manager",
    "runner", "converter", "cleaner", "preprocess", "loader",
)


def _root_module(name: str) -> str:
    return name.split(".")[0]


def is_reusable_name(name: str) -> bool:
    return any(h in name.lower() for h in REUSABLE_HINTS)


class _Visitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.imports: List[str] = []
        self.functions: List[str] = []
        self.classes: List[str] = []
        self.strings: List[str] = []
        self.names: List[str] = []

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.imports.append(_root_module(alias.name))
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module:
            self.imports.append(_root_module(node.module))
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.functions.append(node.name)
        self.names.append(node.name)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.functions.append(node.name)
        self.names.append(node.name)
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.classes.append(node.name)
        self.names.append(node.name)
        self.generic_visit(node)

    def visit_Str(self, node: ast.Str) -> None:
        self.strings.append(node.s)
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str):
            self.strings.append(node.value)
        self.generic_visit(node)


def analyze_code(source: str) -> Dict:
    """分析单个 Python 源码，返回 imports / functions / classes / names / text 信号。"""
    result = {
        "imports": [],
        "functions": [],
        "classes": [],
        "names": [],
        "text": "",
        "error": None,
    }
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        result["error"] = str(exc)
        return result

    visitor = _Visitor()
    visitor.visit(tree)

    libraries: List[str] = []
    for imp in visitor.imports:
        lib = LIB_ALIASES.get(imp, imp)
        if lib not in libraries:
            libraries.append(lib)

    names = [n for n in visitor.names if len(n) > 2]

    result["imports"] = libraries
    result["functions"] = visitor.functions
    result["classes"] = visitor.classes
    result["names"] = names
    # 文本信号 = 字符串字面量(docstring/日志/注释字符串) + 符号名，供关键词命中
    result["text"] = "\n".join(visitor.strings) + "\n" + "\n".join(names)
    return result
