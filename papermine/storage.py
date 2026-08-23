"""数据与记忆持久化：~/.papermine 目录布局 + JSON/JSONL 读写（带 schema_version 与迁移钩子）。

对应工程规范 docs/engineering.md §2、§3。
"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List

# 环境变量覆盖数据根（便于测试 / 迁移）
ENV_HOME = "PAPERMINE_HOME"

# 迁移注册表：schema_name -> {旧版本: 迁移函数}
SCHEMA_MIGRATIONS: Dict[str, Dict[int, Callable[[Dict[str, Any]], Dict[str, Any]]]] = {}


def data_root() -> Path:
    """返回数据根目录 ~/.papermine（可用 PAPERMINE_HOME 覆盖）。"""
    override = os.environ.get(ENV_HOME)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".papermine"


def layout() -> Dict[str, Path]:
    root = data_root()
    return {
        "root": root,
        "runs": root / "runs",
        "experience": root / "experience",
        "literature_cache": root / "literature_cache",
        "logs": root / "logs",
        "config": root / "config",
    }


def ensure_layout() -> Path:
    """创建全部目录，返回根路径。"""
    root = data_root()
    for p in layout().values():
        p.mkdir(parents=True, exist_ok=True)
    return root


def new_run_id() -> str:
    """run_id = 时间戳 + 短随机串。"""
    ts = time.strftime("%Y%m%d-%H%M%S")
    return "{}-{}".format(ts, uuid.uuid4().hex[:8])


def run_dir(run_id: str) -> Path:
    return layout()["runs"] / run_id


def experience_dir() -> Path:
    return layout()["experience"]


def _migrate(schema_name: str, data: Dict[str, Any]) -> Dict[str, Any]:
    version = int(data.get("_schema_version", 1))
    chain = SCHEMA_MIGRATIONS.get(schema_name, {})
    while version in chain:
        data = chain[version](data)
        data["_schema_version"] = version + 1
        version += 1
    return data


def load_json(path: Path, schema_name: str) -> Dict[str, Any]:
    """读取 JSON 并沿迁移链升级到最新 schema。"""
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    return _migrate(schema_name, data)


def save_json(path: Path, data: Dict[str, Any], schema_name: str, schema_version: int) -> None:
    """原子写入 JSON，内嵌 _schema / _schema_version。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(data)
    payload["_schema"] = schema_name
    payload["_schema_version"] = schema_version
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def append_jsonl(path: Path, obj: Dict[str, Any]) -> None:
    """追加一条到 JSONL（经验库 append-only）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(obj, ensure_ascii=False) + "\n")


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    """读取整个 JSONL，忽略损坏行。"""
    if not path.exists():
        return []
    out: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out
