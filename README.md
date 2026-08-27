# papermine

从**横向项目工作**（代码 + 文档）中挖掘**候选论文点**的本地工具。

面向科研领域学生：做了很多横向/工程工作，却不知道哪些能写论文。papermine 把散落在代码、周报、结题报告里的工作，转成一份「研究问题 + 候选创新点 + 可行性评估 + 论文路线图」，帮你看清自己的科研价值。

> 定位：只做「看见 + 评估」，**不代写正文**。这是学术诚信红线，也是产品边界。

> ⚖️ **学术诚信声明**：本工具是「选题 / 研究助理」，只产出候选点、评估与路线图，**不代写论文正文**；所有产出需人工核验与改写。

> 🔒 **隐私承诺**：确定性分析在本地完成；接入 LLM 后，仅将**脱敏后的结构化事实**发送给你配置的 LLM 服务，不发送完整源码与原始数据；可切换本地模型实现完全离线。

> 📐 架构（6-Agent 闭环 + 自进化层）见 [`docs/architecture.md`](docs/architecture.md)；工程规范见 [`docs/engineering.md`](docs/engineering.md)；多模块任务拆分见 [`docs/build-plan.md`](docs/build-plan.md)；踩坑与复盘见 [`docs/lessons-learned.md`](docs/lessons-learned.md)。

## 核心链路

```
项目理解 → 问题抽象 → 知识检索 ⇄ 创新点生成 → 可行性评估 → 论文路线规划 → 经验沉淀（自进化）
```

外加一个**自进化层**：每次分析结束后蒸馏经验（去领域化的原则 + 行为策略），跨任务、跨项目积累，越用越懂科研。

## 快速开始

```bash
# 安装核心 CLI（运行时依赖仅 httpx）
pip install -e .

# 配置 DeepSeek key（可选；不配则走确定性降级）
cp .env.example .env        # 填入 DEEPSEEK_API_KEY

# 端到端分析（--auto 跳过检查点暂停）
python -m papermine analyze examples/sample-project --auto

# 逐检查点确认、查进度、续跑
python -m papermine analyze examples/sample-project
python -m papermine status <run_id>
python -m papermine resume <run_id>
```

分析结果写入 `~/.papermine/runs/<run_id>/report.md`。

## 本地 Web 客户端

Web 客户端需要 Python 3.8+ 与 Node.js 18+。首次安装：

```bash
git clone <repository-url>
cd papermine
pip install -e ".[web]"

cd web/frontend
npm ci
npm run build
cd ../..

# 同时启动后端和前端，并自动打开浏览器
papermine web
```

常用选项：

```bash
papermine web --no-browser
papermine web --api-port 8100 --web-port 3100
papermine web --dev
```

默认仅监听 `127.0.0.1`，不会向局域网或公网开放。进入 Web 后点击左侧「新建分析」，可以选择整个项目文件夹或上传 ZIP，在预览确认后创建分析 run。导入过程会把项目复制到 `~/.papermine/imports/`，不会修改用户的原始项目。

## 输出报告

一份 Markdown 报告，包含：

- **项目叙事 + 研究问题**：把工程任务抽象成可研究问题（含"为何不是纯工程"）
- **候选创新点**：带 novelty 假设 + 文献引用
- **可行性评估**：证据驱动的打分 + verdict（proceed / rework / drop）
- **论文路线图**：论文类型、大纲、实验计划、时间线

## 目录结构

```
papermine/
  cli.py           命令行入口（analyze / resume / status / trace / web）
  web_launcher.py  本地 Web 双服务统一启动与关闭
  orchestrator.py  编排器（状态机 + 检查点 + 回退）
  dossier.py       研究档案（单项目唯一事实源，版本化）
  llm.py           LLM 抽象（DeepSeek + NullProvider 降级）
  experience.py    经验库（自进化层：策略 / effect / 生命周期）
  retrieval.py     文献检索（arXiv + Semantic Scholar）
  policy.py        策略注入（结构化 policy + LLM 解释）
  agents/          六个 Agent + ⑦ 经验沉淀
  storage.py       数据目录（~/.papermine）读写
  report.py        Markdown 报告渲染
  extractor/       Python AST 静态分析 + 文档读取
examples/sample-project/   示例横向项目（工业预测性维护）
web/                       FastAPI API + Next.js 科研决策工作台
```

## 已知边界

- 无 DeepSeek key 时走确定性降级，产出较模板化（配 key 后为 LLM 语义级）
- 检索走 arXiv / Semantic Scholar；知网后置、需遵守其 ToS
- 代码静态分析目前仅覆盖 Python；docx / pptx / pdf 仅识别为资产、不解析内容

## 后续方向

- 团队 / 实验室共享经验库（`scope` 字段已预留）
- 向量检索（M1 当前是 applicability 标签/关键词匹配）
- MCP Server / Skill 外壳（核心引擎已就位）
- F3 结果信号闭环：论文发表结果回填 `effect`

## 开源与贡献

- 许可证：[MIT](LICENSE)
- 变更记录：[CHANGELOG.md](CHANGELOG.md)
- 贡献指南：[CONTRIBUTING.md](CONTRIBUTING.md)
- 行为准则：[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)

欢迎提 Issue 与 Pull Request，详见[贡献指南](CONTRIBUTING.md)。
