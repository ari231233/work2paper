# papermine

从**横向项目工作**（代码 + 文档）中挖掘**候选论文点**的本地工具。

面向科研领域学生：做了很多横向/工程工作，却不知道哪些能写论文。papermine 把散落在代码、周报、结题报告里的工作，转成一份「候选论文点清单 + 评估报告」，帮你看清自己的科研价值。

> 定位：只做「看见 + 评估」，**不代写正文**。这是学术诚信红线，也是产品边界。

> ⚖️ **学术诚信声明**：本工具是「选题 / 研究助理」，只产出候选点、评估与路线图，**不代写论文正文**；所有产出需人工核验与改写。

> 🔒 **隐私承诺**：确定性分析在本地完成；接入 LLM 后，仅将**脱敏后的结构化事实**发送给你配置的 LLM 服务，不发送完整源码与原始数据；可切换本地模型实现完全离线。

> 📐 目标形态是「项目理解 → 问题抽象 → 知识检索 → 创新点生成 → 可行性评估 → 论文路线规划」的 6-Agent 闭环系统，详见 [`docs/architecture.md`](docs/architecture.md)。

> 🛠 代码版本、数据、Agent 记忆的管理规范见 [`docs/engineering.md`](docs/engineering.md)。

> 🧩 多聊天框模块搭建的任务拆分与接口契约见 [`docs/build-plan.md`](docs/build-plan.md)。

## 核心链路

```
资产扫描 -> 要素抽取(六元组) -> 论文点生成 -> 评估打分 -> Markdown 报告
```

六元组：**任务 — 方法 — 数据 — 场景 — 指标**（+ 依赖库 / 可复用组件）。

## 快速开始

零依赖，纯标准库（Python ≥ 3.8），本地运行、数据不出机器。

```bash
# 方式一：模块方式直接运行
python -m papermine examples/sample-project

# 方式二：输出到文件
python -m papermine examples/sample-project -o report.md --json report.json

# 方式三：pip 安装后使用命令行
pip install -e .
papermine examples/sample-project
```

## 输出示例

生成一份 Markdown 报告，包含「项目画像（六元组）」和「候选论文点」卡片，每个点带：

- 一句话贡献、论文类型、建议投稿档位
- 新颖性（★）、预计工作量、数据可得性、主要风险
- 支撑证据（来自哪个文件、命中了什么）

## 目录结构

```
papermine/
  cli.py          命令行入口
  pipeline.py     流水线编排
  scanner.py      资产扫描（代码/文档/配置识别）
  knowledge.py    六元组抽取（关键词词典 + 信号聚合）★扩展点
  mining.py       论文点生成 + 启发式打分        ★扩展点
  report.py       Markdown / JSON 渲染
  models.py       数据模型
  extractor/
    code_extractor.py   Python AST 静态分析
    doc_extractor.py    文档文本读取
examples/sample-project/   示例横向项目（工业预测性维护）
```

## 后续扩展点

1. **语义抽取**：把 `knowledge.py` 的关键词词典替换为本地 LLM（Ollama / 量化小模型）做语义级要素抽取。
2. **跨项目共性挖掘**：支持传入多个项目目录，做方法/任务的聚类，发现"你在重复解决同一类问题"。
3. **文献 gap 对照**：拿候选点去 arXiv / DBLP / 知网检索，生成 related work 骨架。
4. **评估模型**：把启发式打分换成数据驱动的新颖性/难度评估。

## MVP 已知边界

- 关键词 + 规则驱动，抽取粒度较粗，适合跑通链路、验证价值。
- 代码静态分析目前只覆盖 Python；docx/pptx/pdf 仅识别为文档资产、不做内容解析。
- 单个项目即可运行；跨项目共性挖掘是下一阶段重点。

## 开源与贡献

- 许可证：[MIT](LICENSE)
- 变更记录：[CHANGELOG.md](CHANGELOG.md)
- 贡献指南：[CONTRIBUTING.md](CONTRIBUTING.md)
- 行为准则：[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)

欢迎提 Issue 与 Pull Request，详见[贡献指南](CONTRIBUTING.md)。
