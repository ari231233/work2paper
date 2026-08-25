# papermine 构建计划（多聊天框任务拆分）

> 把系统拆成 7 个模块（M1–M7），每个模块交给一个独立聊天框搭建。
> 本文件是**接口契约的唯一事实源**：不同聊天框互不共享上下文，集成能否成功，全靠这份契约。

---

## 0. 怎么用这份文档

背景与硬性规则已写在仓库根 `AGENTS.md`（DSH 会自动注入到本仓库的聊天），**开新框只需一句 + 任务卡**：

> 实现 `docs/build-plan.md` 里的模块 **M_x**，动手前先读 §3 冻结接口契约与相关文档。
>
> （然后粘贴对应模块的任务卡）

模块做完：跑冒烟测试 + `git commit`（Conventional Commits，描述中文）。

## 1. 协作总则（每个框都必须遵守）

> **任务划分原则**：任务按**模块**划分，不按先后。禁止立项一个任务去修改其他模块的文件；若某模块需因设计变更而升级，应作为**该模块的新版本**（如 `M7 v2`）立项，由该模块负责，而不是把改动塞进新任务跨模块改旧模块。

- Python 3.8 兼容；模块头加 `from __future__ import annotations`
- **只改自己模块的文件** + 必要的接口文件；不碰别人模块的实现
- **接口契约冻结**（见 §3）：函数签名 / schema 以本文件为准，不得私自改
- 交付前 `python -m papermine examples/sample-project` 冒烟测试不得回归
- 提交用 Conventional Commits（`feat:` `fix:` `test:` 等）

### 交付自检清单（每个框交付时必须附带）

交付时在最终回复里给出自检报告，供人 review 验证：

1. **改动文件**：`git diff --stat` 的结果（应只含本模块文件）
2. **测试结果**：冒烟测试 + 本模块单测的通过情况
3. **契约核对**：逐条对照 §3 接口契约，确认函数签名 / schema 一致
4. **降级路径**：无 API key / 网络失败时的实际行为
5. **遗留问题**：已知未完成项 / 假设 / 需后续模块配合的点

### 遗留问题处理规则（三分流 + 进 Issue）

交付时把「遗留问题」分成三类，分别处理：

1. **阻塞类**（契约不符 / 冒烟回归 / 无 key 就崩 / 字段写错）→ 当场解决，不解决不算交付
2. **可延后类**（性能 / 风格 / 边角 case / 更多测试）→ 记 GitHub Issue，打标签 `module/Mx` + `deferred`
3. **需协作类**（要等别的模块定了才能定）→ 记 Issue，标注依赖哪个模块

判据一句话：**会让下一个模块接不上、或让整包跑挂吗？会 → 当场解决；不会 → 记录延后。** 集成收尾阶段（§5）统一清账。

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

### M5 v2 — 文献理解 + 矛盾挖掘 + 假设生成（M5 升级）

- **目标**：把 M5 的「检索 → 创新」加深为六步流水线，让 idea 从「文献矛盾 / gap」里长出来，而非凭空组合。
- **前置**：M5 已建（`retrieval.py` + `agents/ideate.py`），本任务升级 M5 内部。
- **数据流**：
  ```
  problems → Retrieval(已有) → literature
           → Understanding(新) → literature 附结构化理解
           → Contradiction/Gap Mining(新) → gap / contradiction_graph
           → Hypothesis Generation(新) → hypotheses
           → Idea Generation(已有) → ideas
  ```
- **产出**：改造 `retrieval.py`（或新增 `literature.py`）+ `agents/ideate.py`（或新增 hypothesis 模块）+ 单测。
- **要点**：
  1. **Literature Understanding**：对每篇命中论文，LLM 提取结构化理解（claim / 方法 / 结论 / 适用条件 / 局限），存进 literature 条目。
  2. **Contradiction / Gap Mining**：跨论文比较，找出「同一结论点结论冲突」（矛盾）或「无人覆盖的角度」（gap），产出 contradiction_graph。
  3. **Hypothesis Generation**：从 gap 生成**可证伪假设**（if-then 形式），作为 idea 的前置。
  4. **Idea Generation 复用假设**：每个 idea 必须引用其来源 gap / 矛盾（evidence 可追溯）。
- **验收**：
  1. 单测 + 冒烟不回归；
  2. 对 sample 项目，能从文献挖出 ≥1 条矛盾或 gap，并据此生成假设 → idea；
  3. idea 的证据里能看到来源 gap / 矛盾。

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

### M8 — 进化机制落地（M7 升级）

- **目标**：把 `architecture.md` §3 新版进化机制落到代码——经验从"记忆"升级为"策略"，加 effect / 生命周期 / 去领域化 / 混合注入。
- **前置**：M7 已建（`experience.py` / `reflect.py` / `orchestrator.py` + 测试），本任务是对它们的**升级改造**（会动 M7 文件，见 delta）。
- **产出**：改造 `papermine/experience.py` + `papermine/agents/reflect.py` + `papermine/orchestrator.py`（可新增 policy 注入辅助模块），同步更新 3 个测试文件。
- **迁移 delta**（旧 → 新，对齐 §3.6）：
  - `scope` → `source_domain` + `applicability`
  - `trigger` → `applicability.preconditions`
  - `insight` → `principle`（去领域化）
  - `action` → `policy.target` + `policy.directive`（结构化）
  - `support_count` 保留；`status` 增加 `degraded` / `retired`
  - 新增 `effect`（F3 落点）
- **要点**：
  1. 经验条目 schema 按 §3.6 升级；去重键从 `scope+insight` 改为 `principle+applicability`
  2. 检索（M1）从 scope 匹配改为 **applicability 门控**（不匹配不注入）
  3. reflect 蒸馏时**去领域化**（生成 `principle`）+ 生成结构化 `policy`（target+directive）+ 填 `effect`
  4. 运行时**混合注入**：把 `policy` 渲染成对应 Agent 的行为约束注入（结构决定位置，LLM 执行约束）
  5. 生命周期 `candidate → active → degraded → retired`，由 `support_count` + `effect` 驱动
- **验收**：
  1. 更新后的 3 个单测通过 + 冒烟 `python -m papermine examples/sample-project` 不回归
  2. 手动构造一条带 `policy` + `applicability` + `effect` 的经验，验证：applicability 不匹配时不注入、匹配时 policy 注入到对应 target 的 Agent
  3. 交付自检清单 + 遗留问题三分流（按 §1）

### M8 v2 — Policy Optimizer（M8 升级）

- **目标**：让 policy 从「被动更新」（靠人工 review / 稀疏结果信号）升级为「自动优化」——根据 policy 使用情况 + 效果，自动更新 confidence / 生命周期 / 优先级。
- **前置**：M8 已建（`experience.py` + `reflect.py` + `orchestrator.py`），本任务在 M8 基础上增强。
- **产出**：改造 `experience.py`（或新增 optimizer 模块）+ 单测。
- **要点**：
  1. **记录使用**：policy 每次被注入某 Agent 时，记录 `usage`（注入次数 + 关联 run/idea）。
  2. **效果信号**：复用现有 `effect`（人工 review / F3），并接入 M12 的 evidence 强度作为 idea 质量信号。
  3. **自动更新**：按 usage + effect 自动调 confidence（升/降）、推进生命周期（active/degraded/retired）、调整检索注入优先级（排序）。
  4. **防漂移**：更新设阈值门槛，避免单次信号剧烈波动（沿用 §3.7 生命周期护栏）。
- **验收**：
  1. 单测 + 冒烟不回归；
  2. 构造一条 policy，模拟多次 positive/negative effect，验证 confidence 自动升/降、生命周期自动推进/降级、优先级随之变化。

### M9 — 报告渲染补文献段

- **目标**：`_render_report_md` 增加「文献检索结果」段，让文献验证在报告里可见（现在文献在 dossier 里但报告不展示）。
- **依赖**：M7（`papermine/orchestrator.py` 的 `_render_report_md`）。
- **产出**：修改 `_render_report_md`，新增 literature 段。
- **要点**：每个 query 展示 query、命中论文（标题 + venue + 年份）、gap_note、来源；无文献时标"离线/无结果"。
- **验收**：`report.md` 含「文献检索结果」段，能看到 papers 标题与 gap_note。

### M9 v2 — 报告渲染 M5 v2 新字段（文献理解/矛盾图/假设）

- **目标**：`_render_report_md` 增加 M5 v2 新产出的渲染，让「挖到了什么矛盾、生成了什么假设」在报告里可见（现在这些只在 dossier 里、报告不展示）。
- **前置**：M9（报告渲染补文献段）+ M5 v2（产出 `understanding` / `contradiction_graph` / `hypotheses`）。
- **产出**：修改 `papermine/orchestrator.py` 的 `_render_report_md`。
- **要点**：
  1. 文献检索结果段：每篇论文附带其 `understanding`（claim / 方法 / 结论 / 适用条件）。
  2. 新增「矛盾 / 缺口」段：渲染 `contradiction_graph`（哪些论文在哪一点冲突、gap 在哪）。
  3. 新增「假设」段：渲染 `hypotheses`（可证伪 if-then 假设），并标注哪些 idea 由哪些假设而来。
- **验收**：`report.md` 能看到文献理解、矛盾/缺口、假设三段；idea 与假设的关联可追溯。

### M10 — 检索相关性优化

- **目标**：提高检索返回论文的相关性（当前英文 query 太泛，检回 *Byzantine SGD*、*dark energy* 等无关论文）。
- **依赖**：M5（`papermine/retrieval.py`）。
- **产出**：修改 `retrieval.py` 的翻译/检索/过滤逻辑。
- **要点**：
  1. 翻译产出更聚焦的学术关键词（核心术语组合，而非宽泛短语）；
  2. arXiv 检索加字段约束（如 `ti:` 标题限定），减少词面误命中；
  3. 检索后加相关性过滤（LLM 判相关 或 关键词匹配过滤）。
- **验收**：对 sample 项目检索，返回论文无明显无关项（无 *Byzantine SGD* / *dark energy* 类）。

### M11 — 细化 novelty 评分体系（多维度加权）

- **目标**：把单一的 novelty 0~5 分升级为**多维加权评分**，解决"评分趋同/粒度粗"问题（lessons-learned §3.5）。
- **依赖**：M6（`papermine/agents/evaluate.py`）+ M9（报告渲染，用于展示分维度明细）。
- **产出**：改造 `evaluate.py`（`EVALUATE_SCHEMA` + 评分逻辑）+ 报告渲染 + 单测。

- **评分维度（各维度 0~5，加权归一后总分 0~100）：**

| 维度 | 权重 | 核心问题 |
|---|---:|---|
| 问题新颖性（Problem Novelty） | 20 | 是否提出了一个过去没有被充分解决的问题？ |
| 方法新颖性（Method Novelty） | 35 | 核心方法是否有新的机制，而不是简单组合已有模块？ |
| 技术突破性（Technical Depth） | 20 | 是否解决了关键技术瓶颈？ |
| 与已有工作的差异程度（Gap） | 15 | 相比 SOTA 是否有明确区别？ |
| 可推广价值（Generalization） | 10 | 是否能迁移到其他任务？ |

- **总分 → Agent 建议动作：**

| 分数 | 含义 | Agent 建议动作 |
|-|-|-|
| <40 | 基本没有创新，已有方法变体 | Reject |
| 40-60 | 有一定改进，但偏工程优化 | Weak Reject / 保留想法 |
| 60-70 | 有明确创新点，但需要加强理论或实验 | Revise |
| 70-80 | 值得投入，具备论文潜力 | Accept |
| >80 | 明显创新，可能形成强论文贡献 | Priority |

- **要点**：
  1. `EVALUATE_SCHEMA` 改为要求 5 个维度分 + 每维理由，加权合成 0~100 总分 `novelty_score`（公式：`总分 = Σ(权重×维度分) / 5`，权重合计 100、维度分 0~5）；分数段映射旧 verdict：Reject/Weak Reject → drop、Revise → rework、Accept/Priority → proceed；
  2. 每维分数**必须给出差异化理由**（引用 gap_note / 文献证据），从机制上避免趋同；
  3. 报告渲染展示分维度明细（配合 M9），而不只给一个总分；
  4. 确定性兜底：无 LLM 时按维度规则粗估（如方法组合度、gap 信号有无）。
- **验收**：
  1. 单测通过 + 冒烟不回归；
  2. 对 sample 项目评估，5 个维度分**不全相等**，报告能看到分维度明细。

### M12 — Evidence Validation Agent（证据验证）

- **目标**：验证候选 idea 的 claim 是否有足够证据支撑，输出「证据强度 + 理由」，帮学生判断"这个点子站不站得住"。**不跑实验，只做证据审查**。
- **流程位置**：⑤ 可行性评估（EVALUATE）内部，与 M11 的 novelty 评分并列，作为「证据强度」子审查；evidence=weak 时随 verdict 一起回炉到 ④ 细化 claim。
- **input**：`idea`（claim + novelty_hypothesis + literature_refs）。
- **output**：`evidence`（weak / medium / strong）+ `reason`（为什么弱、如何强化）。
- **检查维度**（4 项，均为"证据审查"而非"实验执行"）：
  1. 有没有类似论文？（文献对拍）
  2. 有没有理论依据？（理论支撑）
  3. 有没有实验设计支持？（别人做过什么、这个 claim 能否被验证）
  4. 这个 claim 是否过强？（claim 强度校准）
- **示例**：
  - idea：`memory 机制可以提升 agent 规划能力`
  - output：`Evidence: weak` + `Reason: 已有 memory work 很多，需要明确区别：不是 memory，而是 adaptive policy memory`
- **依赖**：M5（文献检索，提供对拍依据）+ M11（多维 novelty 评分，维度可复用）。
- **验收**：对 sample 项目，每个 idea 输出 `evidence` 强度 + `reason`。

### M13 — Agent Trace（执行轨迹记录）

- **目标**：记录每一次 Agent 执行轨迹（起止时间 / 耗时 / LLM 调用 / HTTP 调用），用于发现哪些环节拖慢运行速度。
- **产出**：`papermine/trace.py`（轻量记录器）+ orchestrator 集成 + 单测。
- **存储**：`~/.papermine/runs/<run_id>/trace.jsonl`（每行一条轨迹事件，append-only）。
- **记录内容**：
  1. 每个 Agent 的 start/end 时间戳 + 耗时（含 M5 v2 子步骤 / M12 / M8 v2）。
  2. 每个 LLM 调用的耗时 + 模型 + token 数（如可获取）。
  3. 每个 HTTP 检索调用的耗时。
  4. 回炉 / 降级 / 超时等异常信号。
- **要点**：
  1. 轻量零侵入：用 context manager / 装饰器包裹各 Agent 的 `run()`，不改现有接口契约。
  2. 提供 `papermine trace <run_id>` 子命令：按耗时排序汇总各环节，定位瓶颈。
- **验收**：
  1. 单测 + 冒烟不回归；
  2. 跑一次 analyze，`trace.jsonl` 能看到各 Agent 耗时；
  3. `papermine trace <run_id>` 能输出「哪个环节最慢」的排序。

---

## 5. 全部完成后的集成收尾（一个框或本框）

1. CLI 新增 `analyze` 子命令（调 `orchestrator.run_pipeline`）+ `resume`/`status`。
2. 复用 `report.py` 渲染最终 Markdown 报告（含六元组 + 候选点 + 路线图）。
3. 补 CI（pytest 全量）+ 更新 README 快速开始。
4. 打 tag `v0.2.0`。

## 6. 已定：HTTP 客户端 = httpx

**HTTP 客户端已定为 httpx**（M1、M5 都用），已加入 `pyproject.toml` 的 `dependencies`。这是本项目首个第三方依赖。后续若需新增依赖，先经本框确认许可。
