"""M16 — 并行工具调用：轻量 ThreadPoolExecutor 封装（I/O 密集的 LLM 调用并行执行）。

对应 docs/build-plan.md §4 M16 方向⑥：

- ``map_parallel(fn, items)``：对 ``items`` 并行执行 ``fn``，**结果严格保持输入顺序**；
- 单元素 / 显式关闭时退化为顺序执行（确定性、便于单测）；
- 环境变量 ``PAPERMINE_PARALLEL=0``（或 false/no/off）可整体关闭并行；
- 复制当前 ``contextvars`` 上下文到 worker，使 trace 的 ``current_stage()`` 在并行调用里仍可读。

选线程而非进程：LLM 调用是 HTTP I/O 密集，GIL 不构成瓶颈；且避免序列化 Dossier/schema 的开销。
"""
from __future__ import annotations

import contextvars
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, List, TypeVar

T = TypeVar("T")

# 单次并行调度的最大 worker 数（LLM 后端并发有上限，保守取值）
DEFAULT_MAX_WORKERS = 4

# 关闭并行的环境变量取值（大小写不敏感）
_DISABLED_VALUES = frozenset({"0", "false", "no", "off"})


def parallel_enabled() -> bool:
    """并行是否开启（默认开启；``PAPERMINE_PARALLEL=0`` 等关闭）。"""
    return os.environ.get("PAPERMINE_PARALLEL", "1").strip().lower() not in _DISABLED_VALUES


def map_parallel(fn: Callable[[Any], T], items: List[Any],
                 max_workers: int = DEFAULT_MAX_WORKERS) -> List[T]:
    """对 ``items`` 并行执行 ``fn``，返回按输入顺序排列的结果列表。

    - 关闭并行 / ``len(items) <= 1`` 时退化为顺序 ``[fn(x) for x in items]``；
    - 并行时复制当前上下文（recorder / span 栈）到 worker，保证 trace 阶段标注正确；
    - 任一 ``fn`` 抛异常会传播（``ThreadPoolExecutor.map`` 语义），与顺序执行一致。
    """
    if not parallel_enabled() or len(items) <= 1:
        return [fn(x) for x in items]

    workers = max(1, min(max_workers, len(items)))
    # 每个任务一份独立的上下文快照：``Context`` 同一时刻只能被一个线程 enter，
    # 共享单个 ctx 会在并发下抛 "already entered"，故逐项 copy。
    contexts = [contextvars.copy_context() for _ in items]

    def _run(item: Any, ctx: Any) -> T:
        return ctx.run(fn, item)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        return list(executor.map(_run, items, contexts))
