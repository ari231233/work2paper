# Changelog

本项目遵循 [Semantic Versioning](https://semver.org/) 与 [Keep a Changelog](https://keepachangelog.com/zh-CN/) 格式。

## [Unreleased]

### Planned
- Phase 1：Dossier + `LLMProvider`（DeepSeek）+ ① 项目理解 + ② 问题抽象 + 经验库 v1

## [0.1.0]

### Added
- 确定性管线：资产扫描 → 六元组抽取 → 论文点生成 → 评估打分 → Markdown/JSON 报告
- Python AST 代码静态分析 + 文档关键词抽取
- 示例横向项目（工业预测性维护）
- 存储骨架（`~/.papermine` 布局、JSON/JSONL 读写、schema 版本化 + 迁移钩子）
- 架构文档（6-Agent 闭环 + 自进化层）与工程规范文档
- MIT 许可证、贡献指南、行为准则、CI 冒烟测试
