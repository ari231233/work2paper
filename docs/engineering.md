# papermine 工程规范：版本、数据与记忆管理

> 建立代码、数据、Agent 记忆三者的管理规则。目的：可复现、可追溯、可回滚、可迁移。
> 版本：v0.2｜ 状态：已确认，执行中

---

## 1. 项目版本管理

### 1.1 代码仓库

| 项 | 规则 |
|---|---|
| 版本控制 | Git，仓库根 = `D:\trysomething` |
| 分支策略 | trunk-based：`main` + 短期 `feat/*` 分支（单人/小团队够用） |
| Commit 规范 | Conventional Commits：`feat:` `fix:` `docs:` `refactor:` `test:` `chore:`，描述用中文 |
| 版本号 | SemVer（`MAJOR.MINOR.PATCH`），唯一事实源 = `papermine/__init__.py` 的 `__version__` |
| 发布 | 里程碑打 tag，如 `v0.1.0` |

Commit 示例：
```
feat(cli): 新增 --json 结构化输出
fix(knowledge): 修正"目标检测"关键词误报
docs(architecture): 加入自进化层设计
```

### 1.2 三类"可复现性资产"都要版本化（关键）

LLM 系统「改了 prompt / schema / 词典 = 改了行为」，所以版本化范围必须超出代码：

| 资产 | 版本化方式 | 变更要求 |
|---|---|---|
| 代码 | Git + SemVer | 常规 PR/commit |
| Prompt 模板 | `prompts/<agent>.md` 带 `version` 头 | 任何改动必须 bump 版本 |
| Schema（Dossier/经验条目/Agent IO） | `schemas/*.json` 带 `schema_version` | 改动提供 migration |
| 关键词词典 | 视作配置数据，单独记版本 | 改动需跑回归（sample 报告 diff） |

> `prompts/` 与 `schemas/` 位于包内（`papermine/prompts/`、`papermine/schemas/`），随包分发，用包相对路径加载。

**可重放原则**：一次运行的元数据记录 `(code_version, prompt_versions, schema_version)`，三者全匹配才能精确复现。

### 1.3 什么进 Git、什么不进

**✅ 进仓库**：
- 代码、`prompts/`、`schemas/`、`docs/`、`examples/`、测试
- 脱敏后的测试 fixture（用于回归的样例项目 + 期望输出）

**❌ 不进仓库**（`.gitignore`）：
- `.env`（密钥）
- `~/.papermine/`（用户数据：runs / experience / 缓存 / 日志）
- `report.md` / `report.json` 等生成产物
- `__pycache__/`、`.venv/` 等

### 1.4 密钥管理

- 根目录 `.env` 存 `DEEPSEEK_API_KEY`，提供 `.env.example`（只含变量名，不含真实值）。
- 代码只读环境变量，永不硬编码、永不提交真实 key。
- 用 `python-dotenv` 或自研轻量加载器（MVP 可自研，避免新增依赖）。

---

## 2. 数据管理

### 2.1 数据目录布局（集中式、用户级）

所有运行时数据放 `~/.papermine/`（Windows：`%USERPROFILE%\.papermine`），与代码仓库彻底分离：

```
~/.papermine/
├── imports/                       # Web 导入的项目副本
│   └── <import_id>/
│       ├── source/                # 安全过滤后的项目副本
│       └── import.json            # import_record v1 + run_id 关联
├── runs/                          # 每次分析一个 run
│   └── <run_id>/                  # run_id = 时间戳 + 短hash
│       ├── dossier.json           # 研究档案（append-only，带 version）
│       ├── dossier.history/       # 每次状态迁移的快照（可回滚到任意检查点）
│       ├── report.md              # 人读报告
│       └── report.json            # 机器读报告
├── experience/                    # 跨项目记忆（Evolution Layer）
│   ├── episodic.jsonl             # 案例记忆
│   ├── semantic.jsonl             # 语义记忆（模式/规则）
│   └── calibration.jsonl          # 校准记忆
├── literature_cache/              # 文献检索缓存（防限流，带 TTL）
├── logs/                          # 运行日志 + API 审计日志（脱敏后）
└── config/                        # 用户配置（脱敏规则、阈值、默认后端）
```

### 2.2 格式与 schema 版本化

- 结构化数据统一 JSON（UTF-8），每个文件内嵌 `schema_version` 字段。
- 读取时按 `schema_version` 走 migration 函数（旧版本自动升级，不破坏老数据）。
- 报告：Markdown（人读）+ JSON（机器读）双份，同一 run 内内容一致。

### 2.3 数据生命周期

| 数据 | 策略 |
|---|---|
| Dossier | append-only，每次状态迁移 `version+1`；`human_decisions` 完整保留 |
| 导入项目副本 | 与 run 分离；一个 import 可产生多次 run；用户删除前保留在本机 |
| 经验条目 | candidate →（人工确认 或 `support_count≥阈值`）→ 生效；坏经验 mark `retired`（不物理删，可审计） |
| 文献缓存 | 带 `source` + `fetched_at` + TTL，过期刷新；命中优先用缓存 |
| 日志 | 滚动保留 30 天；API 审计日志（上传了什么、脱敏后）单独长期保留 |
| 备份/迁移 | `papermine export` 打包 runs + experience；`papermine import` 恢复 |

### 2.4 数据隔离与所有权

- MVP：单用户私有（`~/.papermine/`）。
- 未来团队/实验室共享：经验条目预留 `scope` 字段（`user` / `team`），数据结构先留位，暂不实现权限。

---

## 3. Agent 记忆的保留

先厘清三种"记忆"边界（避免和数据混淆）：

| 记忆 | 载体 | 生命周期 | 持久化时机 |
|---|---|---|---|
| 工作记忆（短） | 单次运行的 Dossier | 一次分析期间 | **每个状态迁移后立即落盘** |
| 长期记忆 | Experience Store（经验库） | 跨运行、跨项目 | 每次运行结束由 ⑦ 写入 |
| 外部检索记忆 | literature_cache | 跨运行 | 按 TTL 缓存 |

### 3.1 工作记忆：Dossier 落盘策略

- **每个 Agent/状态迁移完成后立即写盘**（不是只在一开始/结束写），保证检查点暂停、崩溃、断电后能续跑。
- `meta.version` 递增；`dossier.history/` 存快照，支持回滚到任意检查点。
- 命令：`papermine resume <run_id>` 从最后检查点继续；`papermine status` 查看 run 进度。

### 3.2 长期记忆：经验库持久化

- **存储格式**：JSONL（每行一条经验条目），便于 append、流式读取、逐条检索。量大后再迁 SQLite / 向量库。
- **晋升流程**：`candidate` →（人工确认 或 `support_count≥阈值`）→ 生效进入 M1 检索池。
- **退役**：mark `retired`，保留原条目供审计，不参与检索。
- **去重**：写入时按 `scope + insight` 相似度去重，避免同一条经验反复堆积。
- **M1 检索**：MVP 用 `scope` / `trigger` 标签 + 关键词匹配；后续接 embedding 相似度。

### 3.3 记忆的边界与安全

- 经验库与 Dossier 均为**本地私有**，不自动上传；仅 LLM 调用时把脱敏后的结构化片段发给云端。
- 审计：每条经验记录 `source_run` + 写入时间，谁、何时、为何写入，可追溯。
- 遗忘权：用户可删除任意经验条目或整个经验库。

---

## 4. 落地顺序（把这些规范变成骨架，再开 Phase 1）

1. `git init` + 写 `.gitignore` + `.env.example`（版本管理落地）
2. 建 `~/.papermine/` 目录结构 + Dossier/经验条目的 JSON 读写模块（数据 + 记忆落地）
3. 建 `prompts/`、`schemas/` 目录（可复现资产落地）
4. 然后才进 Phase 1（Dossier + LLMProvider + ① 项目理解 + ② 问题抽象 + 经验库 v1）

---

## 5. 已定决策

1. ✅ 经验库作用域：**个人私有 + 预留 `scope` 字段**（未来支持实验室/团队共享）。
2. ✅ 数据存储位置：**集中式 `~/.papermine/`**（可用 `PAPERMINE_HOME` 覆盖）。
3. ✅ Git 托管：**先本地 git**，需要时再挂远程。
4. ✅ 版本化范围：代码 + prompt + schema + 关键词词典，全部版本化。
5. ✅ 记忆边界：工作记忆（Dossier，逐状态落盘）、长期记忆（经验库 JSONL）、检索记忆（文献缓存）。
