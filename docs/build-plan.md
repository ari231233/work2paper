# papermine 构建计划（多聊天框任务拆分）

> 把系统拆成 7 个模块（M1–M7），每个模块交给一个独立聊天框搭建。
> 本文件是**接口契约的唯一事实源**：不同聊天框互不共享上下文，集成能否成功，全靠这份契约。

---

## 0. 怎么用这份文档

1. 每个「模块任务卡」= 一个新聊天框的工作内容。
2. 新聊天框开头，粘贴对应模块的任务卡，并让它先读：
   - `docs/architecture.md`（§3 自进化层、§4 Dossier schema、§5 六 Agent、§7 LLM 接口）
   - `docs/engineering.md`（版本/数据/记忆规范）
3. 模块做完：跑冒烟测试 + `git commit`（Conventional Commits，描述中文）。

## 1. 协作总则（每个框都必须遵守）

- Python 3.8 兼容；模块头加 `from __future__ import annotations`
- **只改自己模块的文件** + 必要的接口文件；不碰别人模块的实现
- **接口契约冻结**（见 §3）：函数签名 / schema 以本文件为准，不得私自改
- 交付前 `python -m papermine examples/sample-project` 冒烟测试不得回归
- 提交用 Conventional Commits（`feat:` `fix:` `test:` 等）

## 2. 依赖关系与开工顺序

```
已完成（M0）：确定性管线 + storage.py + config.py + 工程规范

第一波（可并行，2 个框）：
  M1 LLM 接入层 ──────┐
  M2 Dossier 数据层 ──┤
                      ├──> 第二波（可并行，4 个框）：
                      │      M3 ①项目理解
                      │      M4 ②问题抽象
                      │      M5 ③检索 + ④创新点
                      │      M6 ⑤评估 + ⑥路线
                      └──────> 第三波（1 个框）：
                              M7 编排器 + ⑦经验沉淀 + 经验库
                              最后：端到端联调 + CLI `analyze` 命令
```

> M1 和 M2 互不依赖，可同时开两个框；M3–M6 依赖 M1+M2，可再开 4 个框并行；M7 依赖全部，最后做。

## 3. 冻结的接口契约

### 3.1 LLMProvider（M1 产出，全局通用）

```python
# papermine/llm.py
class LLMProvider(Protocol):
    def complete(self, system: str, user: str,
                 schema: dict, temperature: float = 0.2) -> dict:
        """返回符合 schema 的 dict；失败抛 LLMError / SchemaError"""

def get_provider() -> LLMProvider:
    """读 config（papermine/config.py），无 key 返回 NullProvider"""
```

### 3.2 Dossier（M2 产出，全局通用）

字段与 `docs/architecture.md` §4 **完全一致**：

```python
# papermine/dossier.py
class Dossier:
    meta: dict            # project_id / version / llm_backend / prompt_versions
    assets: dict          # facts / narrative / evidence
    problems: list
    literature: list
    ideas: list
    evaluations: list
    roadmap: dict
    human_decisions: list
    def save(self, run_dir) -> None: ...
    def load(self, run_dir) -> "Dossier": ...
    def snapshot(self) -> None: ...   # 写 dossier.history/
```

### 3.3 Agent 统一约定

每个 Agent 模块暴露一个入口函数 `run(...)`，**原地写 Dossier 对应字段**，由编排器按序调用：

```python
# agents/understand.py (M3)
def run(project_dir: str, dossier: Dossier, llm: LLMProvider) -> None

# agents/abstract.py (M4)
def run(dossier: Dossier, llm: LLMProvider) -> None

# agents/ideate.py (M5)
def run(dossier: Dossier, llm: LLMProvider) -> None

# agents/evaluate.py (M6)
def run(dossier: Dossier, llm: LLMProvider) -> None

# agents/plan.py (M6)
def run(dossier: Dossier, llm: LLMProvider) -> None

# agents/reflect.py (M7)
def run(dossier: Dossier, llm: LLMProvider) -> None
```

---

## 4. 模块任务卡

### M1 — LLM 接入层

- **目标**：统一的 LLM 抽象 + DeepSeek 实现 + 无 key 兜底 + 结构化输出。
- **依赖**：`papermine/config.py`（已存在）。
- **产出**：`papermine/llm.py`（+ 单测 `tests/test_llm.py`）。
- **接口**：见 §3.1。
- **要点**：
  - DeepSeek 走 OpenAI 兼容接口（`base_url`/`model`/`api_key` 从 config 读）。
  - 结构化输出：请求 JSON mode，返回后校验是否满足传入的 `schema`，失败重试 2 次，仍失败抛 `SchemaError`。
  - `NullProvider`：无 key 时 `complete()` 返回空 dict 或抛 `LLMError`，让上层降级到确定性规则。
  - HTTP 客户端用 **httpx**（本项目首个第三方依赖）。
- **验收**：配好 `.env` 的 key 后能拿到一次结构化 JSON；无 key 时正确降级。

### M2 — Dossier 数据层

- **目标**：研究档案的数据结构 + 落盘 + 版本化 + 快照。
- **依赖**：`papermine/storage.py`（已存在）。
- **产出**：`papermine/dossier.py` + `papermine/schemas/dossier.schema.json`（+ 单测）。
- **接口**：见 §3.2。
- **要点**：字段与架构文档 §4 对齐；`save/load` 复用 `storage.save_json/load_json`（带 `_schema_version`）；`snapshot()` 把当前版本写进 `dossier.history/`；`bump_version()` 递增 `meta.version`。
- **验收**：单测 round-trip（save→load 字段不丢）+ 版本迁移钩子可用。

### M3 — ① 项目理解 Agent

- **目标**：项目目录 → `dossier.assets`（facts + narrative + evidence）。
- **依赖**：M1、M2 + 已存在的 `scanner.py`/`extractor/`/`knowledge.py`。
- **产出**：`papermine/agents/__init__.py` + `agents/understand.py`（+ 单测）。
- **接口**：`run(project_dir, dossier, llm)`。
- **要点**：先用确定性层扫描出 facts（复用 `knowledge.extract_elements`）；再用 LLM 基于 facts 生成 `narrative` + 事实语义纠偏；证据沿用确定性层的 evidence。
- **验收**：对 `examples/sample-project` 跑，`dossier.assets` 有完整 facts + 非空 narrative + 证据。

### M4 — ② 问题抽象 Agent

- **目标**：`dossier.assets` → `dossier.problems`。
- **依赖**：M1、M2。
- **产出**：`agents/abstract.py` + `prompts/abstract.md`（带 version 头）+ 单测。
- **接口**：`run(dossier, llm)`。
- **要点**：每个问题必须含 `formulation / motivation / why_not_engineering / evidence_refs`；`why_not_engineering` 强制论证"为何不是纯工程"；无 LLM 时降级为确定性规则（简单按任务生成问题骨架）。
- **验收**：sample 项目跑出 ≥2 个带 `why_not_engineering` 的问题。

### M5 — ③ 知识检索 + ④ 创新点生成（核心闭环）

- **目标**：`problems` → 文献检索 → `literature`；`problems`+`literature` → `ideas`。
- **依赖**：M1、M2。
- **产出**：`papermine/retrieval.py` + `agents/ideate.py` + `prompts/ideate.md` + 单测。
- **接口**：
  ```python
  # retrieval.py
  def search_literature(queries, cache_dir, llm) -> list[dict]   # 返回 literature[]
  # agents/ideate.py
  def run(dossier, llm) -> None
  ```
- **要点**：检索走 arXiv + Semantic Scholar API（httpx）；带"查询改写循环"（llm 改 query，最多 3 轮）；结果缓存到 `literature_cache/`；每个 idea 必须引用 `literature_refs` 并写 `novelty_hypothesis`。
- **验收**：对 sample 项目能检索到真实论文 + 生成 ≥2 个带引用的 idea；网络不可用时优雅降级（literature 留空、idea 仍按规则生成）。

### M6 — ⑤ 可行性评估 + ⑥ 路线规划

- **目标**：`ideas` → `evaluations`（证据驱动）；`evaluations` → `roadmap`。
- **依赖**：M1、M2。
- **产出**：`agents/evaluate.py` + `agents/plan.py` + 对应 prompts + 单测。
- **接口**：见 §3.3。
- **要点**：评估**证据驱动**——novelty 对照 `literature.gap_note`、数据可得性对照 `assets.facts`、档位对照检索到的 venue 分布；`verdict ∈ {proceed, rework, drop}`；路线图含 `selected_idea/paper_type/outline/experiment_plan/timeline/missing_items`。
- **验收**：对 sample 项目产出评估（含 verdict）+ 一份 roadmap。

### M7 — 编排器 + ⑦ 经验沉淀 + 经验库 v1

- **目标**：把 M3–M6 串成状态机，加检查点暂停、回退、经验沉淀与检索注入。
- **依赖**：M1–M6 全部。
- **产出**：`papermine/orchestrator.py` + `papermine/experience.py` + `agents/reflect.py` + 单测。
- **接口**：
  ```python
  # orchestrator.py
  def run_pipeline(project_dir: str, auto: bool = False) -> str   # 返回 run_id
  # experience.py
  def record_decision(run_id, checkpoint, decision, note) -> None
  def retrieve(scope: str, k: int = 3) -> list[dict]
  # agents/reflect.py
  def run(dossier, llm) -> None   # 蒸馏经验写 experience/semantic.jsonl
  ```
- **要点**：状态机 `UNDERSTAND→☑1→ABSTRACT→☑2→RETRIEVE⇄GENERATE→☑3→EVALUATE→☑4→PLAN→☑5→REFLECT`；检查点默认暂停等输入（`auto=True` 跳过）；回退有最大轮数；每状态迁移后 `dossier.snapshot()`；经验条目带 `confidence/support_count`。
- **验收**：`python -m papermine analyze examples/sample-project` 端到端跑通（含检查点暂停/续跑），结束写出一条经验。

---

## 5. 全部完成后的集成收尾（一个框或本框）

1. CLI 新增 `analyze` 子命令（调 `orchestrator.run_pipeline`）+ `resume`/`status`。
2. 复用 `report.py` 渲染最终 Markdown 报告（含六元组 + 候选点 + 路线图）。
3. 补 CI（pytest 全量）+ 更新 README 快速开始。
4. 打 tag `v0.2.0`。

## 6. 已定：HTTP 客户端 = httpx

**HTTP 客户端已定为 httpx**（M1、M5 都用），已加入 `pyproject.toml` 的 `dependencies`。这是本项目首个第三方依赖。后续若需新增依赖，先经本框确认许可。
