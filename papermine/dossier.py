"""研究档案 Dossier：单项目唯一事实源。

数据结构与 `docs/architecture.md` §4 **完全一致**；落盘复用 `papermine/storage.py`
（内嵌 `_schema` / `_schema_version`，读取时沿迁移链自动升级）；版本化与快照遵循
`docs/engineering.md` §2、§3。

冻结接口见 `docs/build-plan.md` §3.2：

    class Dossier:
        meta / assets / problems / literature / ideas /
        evaluations / roadmap / human_decisions
        def save(self, run_dir) -> None
        def load(run_dir) -> "Dossier"       # 类方法：Dossier.load(run_dir)
        def snapshot(self) -> None           # 写 dossier.history/
        def bump_version(self) -> int        # 递增 meta.version
"""
from __future__ import annotations

import copy
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import storage

# 数据文件内嵌的 schema 名与版本（唯一事实源见 schemas/dossier.schema.json）
SCHEMA_NAME = "dossier"
SCHEMA_VERSION = 1

DOSSIER_FILENAME = "dossier.json"
HISTORY_DIRNAME = "dossier.history"

# assets.facts 的六元组 + 可复用组件，与 architecture §4 一致
_FACTS_KEYS = ("tasks", "methods", "data", "scenarios", "metrics", "libraries", "modules")


def _new_facts() -> Dict[str, List[str]]:
    """返回空的 facts 容器（七类列表）。"""
    return {k: [] for k in _FACTS_KEYS}


def _new_roadmap() -> Dict[str, Any]:
    """返回带默认键的空 roadmap，与 architecture §4 对齐。"""
    return {
        "selected_idea": None,
        "paper_type": "",
        "outline": [],
        "experiment_plan": [],
        "timeline": {},
        "missing_items": [],
    }


def register_migration(
    from_version: int, migrate: Callable[[Dict[str, Any]], Dict[str, Any]]
) -> None:
    """登记一个 dossier 的 schema 迁移函数（from_version -> from_version+1）。

    storage.load_json 读取旧数据时会沿 SCHEMA_MIGRATIONS 链自动升级。
    未来 schema bump 时在此登记迁移，保证老数据不损坏（engineering.md §2.2）。
    """
    storage.SCHEMA_MIGRATIONS.setdefault(SCHEMA_NAME, {})[from_version] = migrate


class Dossier:
    """单项目研究档案。所有 Agent 只通过它读写，是单项目唯一事实源。"""

    def __init__(
        self, project_id: Optional[str] = None, llm_backend: Optional[str] = None
    ) -> None:
        self.meta: Dict[str, Any] = {
            "project_id": project_id or uuid.uuid4().hex,
            "version": 1,
            "llm_backend": llm_backend,
            "prompt_versions": {},
        }
        self.assets: Dict[str, Any] = {
            "facts": _new_facts(),
            "narrative": "",
            "evidence": [],
        }
        self.problems: List[dict] = []
        self.literature: List[dict] = []
        self.ideas: List[dict] = []
        self.evaluations: List[dict] = []
        self.roadmap: Dict[str, Any] = _new_roadmap()
        self.human_decisions: List[dict] = []
        self._run_dir: Optional[Path] = None

    def __repr__(self) -> str:
        return "Dossier(project_id={!r}, version={})".format(
            self.meta.get("project_id"), self.meta.get("version")
        )

    # ---- 数据 <=> dict ----

    def to_dict(self) -> Dict[str, Any]:
        """返回纯 dict（不含 _schema/_schema_version），深拷贝以免外部改动内部状态。"""
        return copy.deepcopy(
            {
                "meta": self.meta,
                "assets": self.assets,
                "problems": self.problems,
                "literature": self.literature,
                "ideas": self.ideas,
                "evaluations": self.evaluations,
                "roadmap": self.roadmap,
                "human_decisions": self.human_decisions,
            }
        )

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Dossier":
        """从 dict 构建 Dossier；缺字段时用默认结构兜底（用于 load / 迁移）。"""
        d = cls.__new__(cls)
        meta = data.get("meta") or {}
        d.meta = {
            "project_id": meta.get("project_id") or uuid.uuid4().hex,
            "version": int(meta.get("version", 1)),
            "llm_backend": meta.get("llm_backend"),
            "prompt_versions": dict(meta.get("prompt_versions") or {}),
        }
        assets = data.get("assets") or {}
        facts = dict(_new_facts())
        facts.update(assets.get("facts") or {})
        d.assets = {
            "facts": facts,
            "narrative": assets.get("narrative", ""),
            "evidence": list(assets.get("evidence") or []),
        }
        d.problems = list(data.get("problems") or [])
        d.literature = list(data.get("literature") or [])
        d.ideas = list(data.get("ideas") or [])
        d.evaluations = list(data.get("evaluations") or [])
        roadmap = dict(_new_roadmap())
        roadmap.update(data.get("roadmap") or {})
        d.roadmap = roadmap
        d.human_decisions = list(data.get("human_decisions") or [])
        d._run_dir = None
        return d

    # ---- 版本化 ----

    def bump_version(self) -> int:
        """递增 meta.version（append-only 版本号），返回新版本号。"""
        self.meta["version"] = int(self.meta.get("version", 0)) + 1
        return self.meta["version"]

    # ---- 落盘 ----

    def save(self, run_dir) -> None:
        """把当前状态原子写入 <run_dir>/dossier.json，并记录 run_dir 供 snapshot 使用。"""
        self._run_dir = Path(run_dir)
        storage.save_json(
            self._run_dir / DOSSIER_FILENAME, self.to_dict(), SCHEMA_NAME, SCHEMA_VERSION
        )

    @classmethod
    def load(cls, run_dir) -> "Dossier":
        """从 <run_dir>/dossier.json 读取（沿迁移链升级），返回新的 Dossier。"""
        run_dir = Path(run_dir)
        data = storage.load_json(run_dir / DOSSIER_FILENAME, SCHEMA_NAME)
        data.pop("_schema", None)
        data.pop("_schema_version", None)
        d = cls.from_dict(data)
        d._run_dir = run_dir
        return d

    def snapshot(self) -> None:
        """把当前版本写进 <run_dir>/dossier.history/，供回滚到任意检查点。

        快照文件名为 ``v{version:04d}-{毫秒时间戳}.json``，内容即当前 dossier 全量。
        """
        if self._run_dir is None:
            raise ValueError("snapshot() 前需先 save(run_dir) 以确定 run 目录")
        hist_dir = self._run_dir / HISTORY_DIRNAME
        hist_dir.mkdir(parents=True, exist_ok=True)
        version = int(self.meta.get("version", 1))
        fname = "v{:04d}-{}.json".format(version, int(time.time() * 1000))
        storage.save_json(hist_dir / fname, self.to_dict(), SCHEMA_NAME, SCHEMA_VERSION)
