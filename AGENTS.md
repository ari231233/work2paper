# papermine 项目指令（本仓库通用）

你在 `D:\trysomething` 目录工作，这是 **papermine** 项目——把横向项目工作（代码 + 文档）挖掘成候选论文点的本地工具。目标形态是 6-Agent 闭环 + 自进化层 + 核心引擎/多外壳。

## 动手前必读（从磁盘读真实内容）

- `docs/architecture.md` —— 架构：§3 自进化层、§4 Dossier schema、§5 六个 Agent、§7 LLM 接口
- `docs/engineering.md` —— 版本 / 数据 / Agent 记忆管理规范
- `docs/build-plan.md` —— 构建计划：§1 协作总则、§3 冻结接口契约、模块任务卡

## 硬性规则

- 严格遵守 `docs/build-plan.md` §3 的**冻结接口契约**，不得私自改函数签名 / schema
- 改动会跨模块波及的内容（§3/§4 schema、接口契约、字段名）前，先 `git status && git log --oneline -10` 确认已建模块状态；若已有模块依赖它，先标记"需协作类迁移"再改文档
- 只改自己模块的文件，不碰其他模块的实现
- **性能不回归**：不得破坏 M14-M16 的性能优化（「检索→文献理解→矛盾挖掘」只跑一次、回炉复用文献基础、LLM 批量/缓存/并行）；交付前跑 `papermine trace <run_id>` 验证核心环节调用次数无回升（基线见 `lessons-learned.md` §7：IDEATE ≤ 2 次、EVALUATE ≤ 3 次、LLM ≤ 50 次）
- Python 3.8 兼容，模块头加 `from __future__ import annotations`
- 交付前跑 `python -m papermine examples/sample-project` 冒烟测试，不得回归
- 完成后用 Conventional Commits 提交（描述中文）
- 依赖：`httpx` 是当前唯一第三方依赖；新增依赖需先经主控聊天确认
