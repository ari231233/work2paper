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
6. **性能不回归**：跑 `papermine trace <run_id>`，核心环节调用次数对照基线（IDEATE ≤ 2 次、EVALUATE ≤ 3 次、LLM ≤ 50 次，见 `lessons-learned.md` §7），无回升

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

### M14 — 减少无效 Agent 调用（治回炉循环）【第一优先】

- **目标**：消除「评估 rework → 回炉 → 重复执行」导致的重复调用（trace 显示 IDEATE/EVALUATE/ABSTRACT/PLAN 都跑了 4 次）。
- **方向 ② 动态 Agent 路由**：根据 idea 质量（evidence 强度）动态决定是否回炉、回炉到哪一步；evidence 强的 idea 不强制回炉，回炉只重跑受影响环节。
- **方向 ① Workflow 固化**：把「检索 → 文献理解 → 矛盾挖掘」固化为只跑一次，回炉时复用结果，不重跑。
- **产出**：改造 `orchestrator.py`（路由 + 固化逻辑）+ 单测。
- **验收**：同一分析，IDEATE/EVALUATE 调用次数明显下降（理想从 4 次降到 1~2 次）；`papermine trace` 显示总耗时下降（目标 ~150s 内）。

### M15 — 减少 LLM 调用（模型分级 + 批量推理）【第二优先】

- **目标**：降低 127 次 LLM 调用（384s）的成本。
- **方向 ③ 模型分级**：翻译 / gap_note / 简单校验用便宜快模型，`deepseek-chat` 只留给 ideate / evaluate 这类核心推理。
- **方向 ④ 批量推理**：多个 idea 的评估 / 生成合并成一次 LLM 调用（batch），而非逐个调用。
- **产出**：改造 `llm.py`（模型选择）+ 各 agent 批量调用 + 单测。
- **验收**：LLM 调用次数下降；总耗时下降；单测不回归。

### M16 — 工具优化（缓存 + 并行）【第二优先】

- **目标**：减少工具调用的等待与重复。
- **方向 ⑤ 缓存**：LLM 调用缓存（相同输入 → 复用输出）；检索缓存已生效（0 次 HTTP）。
- **方向 ⑥ 并行工具调用**：IDEATE 里多篇论文的文献理解、多个 idea 的评估并行执行。
- **产出**：改造 `llm.py`（缓存）+ `ideate.py` / `evaluate.py`（并行）+ 单测。
- **验收**：相同输入不重复调 LLM；并行后耗时下降。

### M17 — Context 优化（上下文压缩 + Memory 分层）【第三优先】

- **目标**：降低上下文长度，减少 token 成本与延迟。
- **方向 ⑦ 上下文压缩**：文献理解后的长内容压缩后再喂给后续 agent。
- **方向 ⑧ Memory 分层**：常用信息放浅层，减少重复传输。
- **产出**：改造各 agent 的上下文构造 + 单测。
- **验收**：单次 LLM 调用的输入 token 下降；耗时下降。

### M18 — Evidence Level（gap 假设的证据级别）【第二优先】

- **目标**：消除 Gap Mining 的「伪创新」——把 gap 从「事实断言」改为「证据有界的假设」，并标注证据级别。
- **核心原则**：**absence of evidence ≠ evidence of absence**。LLM 只能证明「检索到的论文没做」，不能证明「整个领域没人做」。
- **改动**：
  1. M5 v2 的 Gap Mining 输出改为 `gap_hypothesis`（不再输出全称断言）：
     ```json
     {
       "claim": "尚未发现联合建模的统一框架（假设，非事实）",
       "evidence_level": "weak | moderate | strong",
       "basis": "基于检索到的 8 篇相关论文，其中 3 篇涉及联合建模，但均未提出统一框架",
       "scope": "检索范围：arXiv + Semantic Scholar，query X，共 N 篇"
     }
     ```
  2. `evidence_level` 由检索样本量、系统性、相关性、有无反例共同决定。
  3. 下游 M11（Gap 维度）+ M12（文献对拍）消费 `evidence_level`：weak 时 novelty / 证据强度打折。
- **验收**：gap 输出均为「假设」形式（"基于 N 篇论文未发现…"），无全称断言（如"领域无人做"）；evidence_level 正确反映证据量。

### M19 — Paper Evidence Card（论文级证据卡）【第二优先】

- **目标**：把 M5 v2 的「文献理解」从 abstract 级升级为「论文级证据卡」，让 novelty 判断有真实依据（baseline / dataset / metric / gain / limitation）。
- **关键原则**：每个字段必须能溯源（`evidence_source`）；提取不到的字段**标 null，禁止 LLM 编造**（否则 novelty 会漂）。
- **schema**（每篇论文固定）：
  ```json
  {
    "title": "",
    "dataset": "",
    "baseline": "",
    "metric": "",
    "main_gain": "",
    "limitation": "",
    "claim_strength": "",
    "evidence_source": "abstract | fulltext | table"
  }
  ```
- **要点**：
  1. 从 abstract 提取能提取的字段（dataset / baseline / metric / gain 常在 abstract 里出现）。
  2. 提取不到的字段标 `null`，绝不编造。
  3. `evidence_source` 标明证据来源层级：abstract = 弱证据，fulltext / table = 强证据。
  4. 下游 M11 / M12 消费这些字段：null 字段要反映到 novelty / 证据强度（不能拿「没提取到」当「没有」）。
  5. 与 M18 的关系：`evidence_source` 是 M18 的 `evidence_level` 的底层输入——论文级证据卡的来源层级，决定了 gap 假设的证据级别。
- **硬约束**：当前检索（arXiv + Semantic Scholar）只给 abstract，故第一版 `evidence_source` 大概率全是 `abstract`；fulltext / table 需后续「全文下载 + 表格解析」另立项。
- **验收**：每篇论文有证据卡，字段要么有值要么 null，`evidence_source` 正确，无编造的 baseline / gain。

### M20 — Score Calibration（评分校准）【第二优先】

- **目标**：把 M11 的 novelty 各维度从「LLM 自由打分」改为「规则 + LLM 解释」——每个分数都有明确来源（回答了哪些问题、规则怎么算），而非看似科学的任意数字。
- **核心原则**：**数字来源可追溯**。每个维度用一组校准问题（rubric），LLM 只负责「答题 + 给证据」，分数由规则算出。
- **示例（方法新颖性 Method Novelty，0~5）**：

  | 问题 | 规则 |
  |---|---|
  | Q1 是否只是已有模块组合？ | yes → 封顶 ≤ 3 |
  | Q2 是否改变核心 optimization objective？ | yes → +1 |
  | Q3 是否提出新的学习机制？ | yes → +1 |

- **要点**：
  1. 为 M11 的 5 个维度各设计一组校准问题（方法新颖性用上表模板，其余 4 维照「问题 → 规则」模式）。
  2. LLM 对每个问题输出 yes/no + 证据（引用 gap_note / M19 证据卡），但**分数由规则计算**，不是 LLM 直接给分。
  3. 报告展示「问题 → 答案 → 规则 → 得分」完整链路，分数可追溯。
  4. 保留 M19 的证据卡作为答题依据（每个 yes/no 都要能引用证据）。
- **验收**：对 sample 项目，每个维度分数都能追溯到「回答了哪些问题、规则怎么算」；相同答案 → 相同分数（可复现）。

### M21 — 面向硕士的创新点理解（类型分类 + 贡献矩阵 + 攻击测试）【第一优先】

- **目标**：针对硕士生（创新点要求较博士宽松），把评估从「novelty 打分 → accept/reject」改为「创新类型分类 + 贡献矩阵 + 攻击测试」，避免"模块组合 → 误 reject"。
- **背景**：当前 `idea → novelty score → accept/reject` 会把 "Transformer + CNN" 判成低 novelty 直接 reject，但这类组合在硕士论文里可能有价值（框架集成 / 应用创新）。

#### M21.1 Contribution Type Classifier（创新类型分类）

先分类、不评分：

- 类型 A：新模块创新（Method Innovation）
- 类型 B：已有方法的新组合（Framework Integration）
- 类型 C：已有方法迁移到新场景（Application Innovation）
- 类型 D：问题重新建模（Problem Formulation）
- 类型 E：训练策略创新（Training Strategy Innovation）

#### M21.2 Contribution Matrix（创新贡献矩阵）

不输出 "novelty=71.5"，输出矩阵：

| 贡献类型 | 强度 | 原因 |
|---|---|---|
| 方法创新 | 低 | 没有新模块 |
| 框架创新 | 中高 | 两个任务产生交互 |
| 问题创新 | 高 | 重新定义联合任务 |
| 工程价值 | 高 | 容易落地 |

#### M21.3 Attack Test（已有工作攻击测试）

对每个 idea，Agent 自动生成攻击并提前回答：

- **Attack 1（消融）**：删除核心模块，剩下什么？（如：异常检测辅助 RUL → 删异常检测 → 普通 RUL 预测，说明异常检测是贡献）
- **Attack 2（简单拼接）**：A→B 换成 A+B concat 是否等效？等效 → 机制创新弱；dynamic weighting 有效 → 贡献成立
- **Attack 3（reviewer 视角）**：reviewer 会说 "merely a combination"？提前准备反驳（如：共享 representation、anomaly score 参与 optimization、消融证明 interaction 有效）

- **与 M11/M12/M20 的关系**：本卡是评估的「前置重构」——分类 / 矩阵 / 攻击测试**先于** novelty 评分；novelty 评分（M11/M20）降级为「其中一维参考」，不再作为「直接 reject 依据」；verdict 按「贡献类型」差异化，而非一刀切 novelty 分数。
- **验收**：对 sample 项目，每个 idea 先输出「类型 + 贡献矩阵 + 攻击测试」，而非直接 novelty 分；模块组合类 idea 不再被直接 reject。

### M22 — 论文路线图重构（M6 升级）【第一优先】

- **目标**：把路线图从「泛泛的时间线 + 缺口清单」升级为「可执行、可裁剪、有出口、有成功标准、有风险预案」的学生友好路线图——学生读完能直接开始写代码，且知道「哪些不做也能发」。
- **前置**：M6（路线规划）+ M21（创新点理解，提供论文主线的输入）。

**新路线图结构（7 部分，替代现有 timeline / missing_items）：**

1. **论文主线（Core Story）**：把 idea 压缩成「现状 / 问题 / 方法 / 贡献」四段。
2. **Research Questions（2~4 个）**：每个 RQ 对应后续实验（如 RQ1→主实验、RQ2→数据量实验…）。
3. **Experiment Matrix（实验表）**：`实验 | 目的 | 自变量 | 对比模型 | 指标 | 对应 RQ`。
4. **Minimum Viable Paper（最小可发表版本）**：必须完成 vs 可选扩展，而非"什么都需要补"。
5. **Success Criteria（成功/失败标准）**：做到什么程度算 idea 成立；未达成的失败条件 + 转向方案。
6. **风险分支（Risk Branches）**：具体风险 → 具体转向（如"始终 XGBoost 最好 → 转分析失效条件"），而非泛泛的"局限性"。
7. **阶段出口时间线**：`阶段 + 任务 + 交付物`（如"Week 1 数据集跑通 → 出口：baseline 可复现"），而非纯日期。

- **验收**：对 sample 项目，路线图包含上述 7 部分；一个学生读完能直接开始写代码、且知道"哪些不做也能发"。

### M23 — 报告重构：两层报告（Decision Report + Evidence Appendix）【第一优先】

- **目标**：把报告从「Agent 内部推理的人类可读版」改为「导师给学生的研究建议书」——**两层报告**：默认看到精简的 Decision Report（约当前 25~35% 信息量），完整证据放 Appendix。核心原则：**默认给结论，细节藏附录**。
- **前置**：M9 / M9 v2（报告渲染）+ M22（路线图 7 部分）。

**两层结构：**

**Layer 1 — Decision Report（默认，精简）：**

```
# Papermine Research Report
0. Executive Summary    （推荐 idea + 推荐理由 + 主要风险 + 下一步动作）
1. Project Understanding（项目叙事 + 研究目标）
2. Research Questions    （3 个核心问题）
3. Literature Landscape  （关键论文 + 主要方向 + 证据覆盖度）
4. Candidate Ideas       （候选 idea 排名表）
5. Recommended Idea      （创新点 + 贡献类型 + innovation boundary + 风险 + 证据强度）
6. Paper Roadmap         （Paper Story / RQ / Method / Experiment Matrix / Baselines / Metrics / Success Criteria / Risk & Fallback / MVP / Timeline）
7. Immediate Next Actions（3 条）
```

**Layer 2 — Evidence Appendix（完整证据，后置）：**

```
A. Literature Evidence   （完整文献检索 + 证据卡）
B. Gap Mining            （gap 表格，展开才显示完整依据）
C. Hypotheses
D. Full Novelty Evaluation（Q1/Q2/Q3/Q4 完整评分过程）
E. Attack Tests
F. Human Decisions
```

**7 条具体改动：**

1. **两层报告**：Decision（默认）→ Appendix（后置）。
2. **Executive Summary 最前**：推荐方向 + 推荐程度（★）+ 论文类型 + 工作量 + 证据强度 + 主要风险 + 「为什么推荐」+「当前最重要的 3 个动作」。
3. **候选 idea 先排名表**：`Idea | 类型 | Novelty | Evidence | Feasibility | 推荐`，不逐个铺全文；「点击/继续阅读 iN 详细证据」才展开。
4. **贡献矩阵可视化**：正文只留进度条（如 `Framework █████ 高`），Q1/Q2/Q3/Q4 完整评分放 Appendix。
5. **Attack Test 改为「主要风险 / Reviewer Risk」**：`风险 → 原因 → 如何加强 → 关键实验`，表达更自然（本质还是 Attack Test）。
6. **Gap 压成表格**：`Gap | 研究空白假设 | Evidence | Coverage`（如 `g1 | AD 与 RUL 协同机制未充分研究 | Weak | 2 papers`），展开才显示完整依据。
7. **最终报告结构**按上述 Layer 1 + Layer 2。

- **验收**：报告默认是精简的 Decision Report（长度 ≈ 当前 25~35%），完整证据在 Appendix；一个学生能 2 分钟内看完 Decision Report，并明确知道「推荐哪个、为什么、下一步做什么」。

---

## 5. 全部完成后的集成收尾（一个框或本框）

1. CLI 新增 `analyze` 子命令（调 `orchestrator.run_pipeline`）+ `resume`/`status`。
2. 复用 `report.py` 渲染最终 Markdown 报告（含六元组 + 候选点 + 路线图）。
3. 补 CI（pytest 全量）+ 更新 README 快速开始。
4. 打 tag `v0.2.0`。

## 6. 已定：HTTP 客户端 = httpx

**HTTP 客户端已定为 httpx**（M1、M5 都用），已加入 `pyproject.toml` 的 `dependencies`。这是本项目首个第三方依赖。后续若需新增依赖，先经本框确认许可。
