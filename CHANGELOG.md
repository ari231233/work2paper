# Changelog

本项目遵循 [Semantic Versioning](https://semver.org/) 与 [Keep a Changelog](https://keepachangelog.com/zh-CN/) 格式。

## [Unreleased]

### Added
- M29 多源分层文献检索：新增 OpenAlex、Crossref、DBLP，与 arXiv、Semantic Scholar 共同召回；每个研究方向目标 8 篇、最多 12 篇
- M29 相关性分层：高度相关优先，不足时通过确定性宽化查询补充部分相关论文，并在 Web 显示匹配依据与覆盖状态
- M28 主流科研文档解析：支持 PDF 文本层、扫描型 PDF 中英文 OCR、DOCX 正文/表格/页眉页脚、PPTX 正文/表格/演讲者备注，以及两类 Office 文档的内嵌图片 OCR
- M28 文档正文证据：优先向项目理解 Agent 提供主稿标题/摘要短摘录，并对重复文档和 archive/tmp 历史副本降权
- M26 Web 本地分发：新增 `papermine web`，统一启动/关闭 FastAPI 与 Next.js，支持自定义端口、开发模式和自动打开浏览器
- M26 同源 API 代理：前端统一请求 `/api`，运行时代理到本地 FastAPI，避免构建时固化后端端口
- M27 Web 项目导入：支持选择项目文件夹或上传 ZIP，导入预览确认后再启动分析
- M27 导入安全层：ZIP Slip / 符号链接 / ZIP Bomb 防护，文件数和大小限制，依赖目录及敏感文件默认排除
- M27 导入进度反馈：文件夹/ZIP 显示真实上传百分比，分析阶段显示 Q 版科研助手动画与当前耗时

### Changed
- 面向普通开源用户重写系统架构文档，按当前实现说明本地 Web、项目解析、六阶段分析、文献证据、Dossier、LLM 与隐私数据流；README 不再公开导航到开发任务计划
- 文献缓存升级到 v2；多来源论文按 DOI、arXiv ID、规范化标题去重并合并来源记录
- 五个公开检索源并行请求，单源失败不阻塞其他来源，扩展检索不增加 LLM 改写轮数
- 新增 `documents` 可选依赖；`web` 安装现在一并包含本地文档解析与 OCR 运行组件，不要求额外安装 OCR 程序
- 英文关键词改用词元边界匹配，避免 `text` 命中 `context`、`fault` 命中 `default`；补充航空航天、避障、轨迹跟踪与飞行控制领域信号
- 项目理解 prompt 升级到 v2，正文直接证据优先于孤立关键词计数，发送给 LLM 的证据数量设有上限
- Web 可选依赖新增 `python-multipart`；核心运行依赖仍只有 `httpx`
- 用户项目先复制到 `~/.papermine/imports/<import_id>/source/`，分析不修改原始目录
- Web 安装说明改用可直接复制的 GitHub 仓库地址，并补齐项目主页、源码、Issue 与 Changelog 元数据
- README Web 安装教程重写为 Windows 小白流程，补充环境验证、虚拟环境、API Key、日常启动、更新与故障排查
- Windows 教程固定使用 `D:\papermine`，并通过 `PAPERMINE_HOME` 将导入与分析数据迁至 `D:\papermine-data`
- 测试从 364 增至 396 个

### Fixed
- `papermine web` 启动前检查 8000/3000 端口占用，并确认新子进程仍存活，避免把残留旧服务误判为启动成功

## [0.3.0]

### Added
- Web Demo「科研决策工作台」：FastAPI 后端（M24，REST API 围绕 Dossier + 模块化重跑）+ Next.js 前端 5 页面（M25：Overview / Ideas / Idea Detail / Literature & Gap / Roadmap）
- 前端打磨（M25 v2/v3）：统一决策状态语义（首选探索方向 / 建议实施 / 暂不建议）、顶栏重构、状态闪回修复、证据图默认视图重做、中英文与颜色体系统一、卡片精简
- 报告重构（M23）：两层报告 = Decision Report（决策建议书）+ Evidence Appendix（证据附录）
- 论文路线图重构（M22）：7 部分结构（论文主线 / 研究问题 / 实验矩阵 / MVP / 成功标准 / 风险分支 / 阶段出口）
- 面向硕士的创新点理解（M21）：贡献类型分类（A-E）+ 贡献矩阵 + 攻击测试
- Score Calibration（M20）：novelty 从 LLM 自由打分改为规则 + LLM 解释
- 论文级证据卡（M19）：文献理解升级为可溯源证据卡（dataset / baseline / metric / main_gain 等 8 字段）
- gap 假设证据级别（M18）：消除 Gap Mining 伪创新（absence of evidence ≠ evidence of absence）
- Agent Trace 执行轨迹（M13）：`papermine trace <run_id>` 观测
- Evidence Validation Agent（M12）：证据强度 + 理由，weak 随 verdict 回炉细化 claim
- Policy Optimizer（M8 v2）：按 usage + effect + evidence 自动优化策略置信度 / 生命周期 / 检索优先级
- 文献理解 / 矛盾挖掘 / 假设生成六步流水线（M5 v2）

### Fixed
- 模块化重跑后同步重生成 report.md / report.json（M24 v2）
- 证据卡抽取修正：正向约束防过度保守（M19 v2）
- 前端 TS 构建错误修复（tsconfig target + 回调参数类型标注）

### Changed
- 性能优化（M14-M16）：566s → 125.6s（4.5×），消除回炉循环 + 模型分级 + 批量/缓存 + 并行
- 测试从 162 增至 364 个

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
