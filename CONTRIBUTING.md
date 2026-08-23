# 贡献指南

欢迎贡献！请先阅读本文件。

## 开发环境

- Python ≥ 3.8，纯标准库（当前无第三方依赖）
- 克隆后 `pip install -e .` 即可开发

## 开发流程

1. Fork 并 clone 仓库
2. 新建分支：`git checkout -b feat/你的改动`
3. 改动 + 验证
4. 提交（遵循 Conventional Commits，描述用中文）：`feat: ...`
5. 推送并开 Pull Request

## 提交规范

- `feat:` 新功能 / `fix:` 修复 / `docs:` 文档 / `refactor:` 重构 / `test:` 测试 / `chore:` 杂项

## 测试

- 冒烟测试：
  ```bash
  python -m papermine examples/sample-project
  ```
- 正式单元测试将在 Phase 1 引入 pytest。

## 代码风格

- 遵循现有代码风格（Python 3.8 兼容，模块头加 `from __future__ import annotations`）
- 提交前确保示例项目输出正常（可作为回归基准）

## 行为准则

请遵守 [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)。
