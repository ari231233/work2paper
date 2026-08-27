# PaperMine 科研决策工作台（M25 前端）

普通用户请在仓库根目录使用 `papermine web`，它会统一启动 FastAPI 与 Next.js。以下命令仅用于前端开发。

把 PaperMine 的 `report` 从「阅读器」升级为「科研决策工作台」——左导航 / 状态，中结论 / 分析，
右证据 / 操作。数据全部来自 M24 的 FastAPI（`web/` 后端），前端**不直接碰 Dossier / Agent**。

技术栈：Next.js（App Router）+ React + Tailwind CSS + shadcn/ui + Recharts + React Flow（`@xyflow/react`）。

## 目录结构

```
web/frontend/
├── app/                     # 工作台页面 + M27 项目导入页
├── components/
│   ├── layout/              # 左导航 + 状态 + Ask PaperMine
│   ├── overview/            # Research Recommendation 大卡片
│   ├── literature/          # Research Landscape + Evidence Graph
│   ├── ideas/               # 候选池
│   ├── idea-detail/         # Tab + 贡献雷达图 + Innovation Boundary
│   ├── roadmap/             # Paper Story / RQ / 实验矩阵 / Kanban
│   └── ui/                  # shadcn/ui 风格基础组件
├── lib/                     # types / api / derive（指标推导，纯函数）
└── hooks/                   # useProject（项目上下文 + 数据加载 + 操作）
```

## 运行

1. 先起后端（M24）：

   ```bash
   # 需要 fastapi + uvicorn（见仓库根 pyproject.toml 的 [web] extra）
   python -m web          # 默认 127.0.0.1:8000
   ```

2. 再起前端：

   ```bash
   cd web/frontend
   npm install
   npm run dev            # 默认 http://127.0.0.1:3000
   ```

   后端地址可通过环境变量覆盖：`NEXT_PUBLIC_API_BASE_URL=http://127.0.0.1:8000`。

## 演示路径

打开项目 → Overview 看推荐 → Ideas 看候选 → 进 `i5` 看贡献与风险 → 看 Evidence 来源
→ 进 Roadmap 看未来几周的实验计划。

## 说明

- **UI 用产品语言**（文献 / 创新点 / 路线图），不使用 M1–M16 内部模块命名。
- **右下角固定「Ask PaperMine」**：contextual 入口，自动携带当前项目 / 当前 idea / 文献引用 /
  gap / 评估上下文，并提供快捷按钮（强化这个 Idea / 补充文献 / 生成实验 / 挑战这个 Idea / 重新评分），
  对应 M24 的模块化重跑端点（`refine` / `evaluate` / `retrieve-more`），只跑受影响环节。
- 页面展示的部分指标（如「Thesis Fit 硕士契合度」「风险」「Relevant/Partial/Peripheral 标签」）
  由后端 Dossier 的真实字段**确定性推导**得出（见 `lib/derive.ts`），不编造数据，且 UI 标注「推导」。
