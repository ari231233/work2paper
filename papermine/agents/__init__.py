"""papermine Agent 子包。

六个 Agent 由编排器按序调用，各自**原地写 Dossier 对应字段**（docs/build-plan.md §3.3）：

    understand(①) -> abstract(②) -> ideate(③④) -> evaluate/plan(⑤⑥) -> reflect(⑦)

每个 Agent 暴露同名入口函数 ``run(...)``，签名以 docs/build-plan.md §3.3 冻结契约为准。
"""
from __future__ import annotations

from . import understand
from . import abstract
from . import evaluate
from . import plan

__all__ = ["understand", "abstract", "evaluate", "plan"]
