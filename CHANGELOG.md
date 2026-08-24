# Changelog

本项目遵循 [Semantic Versioning](https://semver.org/) 与 [Keep a Changelog](https://keepachangelog.com/zh-CN/) 格式。

## [Unreleased]

## [0.2.1]

### Added
- 报告新增「文献检索结果」段，展示 query / 命中论文 / gap_note / 来源（M9）
- 检索相关性优化：聚焦关键词翻译 + arXiv 标题字段约束 + 相关性过滤（M10）
- 多维加权 novelty 评分：5 维度 + 0~100 总分 + 分数段→动作映射（Reject/Weak Reject/Revise/Accept/Priority）（M11）

### Fixed
- 代理支持（`PAPERMINE_PROXY`，.env 配置，LLM/检索可走代理）
- 检索修复：arXiv 301 重定向、中文查询翻译为英文、Semantic Scholar 429 退避重试

### Changed
- httpx 复用长连接（连接池 + keep-alive + TLS 复用），消除每次调用新建连接
- 测试从 147 增至 162 个

## [0.2.0]

### Added
- 6-Agent 闭环：项目理解 / 问题抽象 / 知识检索 ⇄ 创新点生成 / 可行性评估 / 论文路线规划
- 编排器（状态机 + 检查点暂停 + 回退）+ `analyze` / `resume` / `status` 子命令
- 研究档案 Dossier（版本化 + 快照 + 证据溯源）
- `LLMProvider` 抽象（DeepSeek + `NullProvider` 确定性降级）
- 文献检索（arXiv + Semantic Scholar，查询改写循环 + 缓存）
- 自进化层 v2：经验升级为「策略」（结构化 policy + LLM 注入）、effect / 生命周期（candidate→active→degraded→retired）、applicability 门控防污染、跨任务去领域化
- 147 个单元测试 + CI（pytest 全量 + 冒烟）

### Changed
- 运行时依赖：新增 `httpx`（首个第三方依赖）
- 报告：由六元组 + 候选点，升级为研究问题 + 创新点 + 评估 + 路线图

## [0.1.0]

### Added
- 确定性管线：资产扫描 → 六元组抽取 → 论文点生成 → 评估打分 → Markdown/JSON 报告
- Python AST 代码静态分析 + 文档关键词抽取
- 示例横向项目（工业预测性维护）
- 存储骨架（`~/.papermine` 布局、JSON/JSONL 读写、schema 版本化 + 迁移钩子）
- 架构文档（6-Agent 闭环 + 自进化层）与工程规范文档
- MIT 许可证、贡献指南、行为准则、CI 冒烟测试
