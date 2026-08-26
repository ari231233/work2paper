# Web Demo 设计：科研决策工作台

> 定位：**不是 report.md 的网页版，而是科研决策工作台**——左导航/状态、中结论/分析、右证据/操作。
> 目标：把 PaperMine 从「帮我想创新点」变成「陪我把论文做完」。

---

## 布局总原则

- 左侧：导航 + 状态（流程进度）
- 中央：结论与分析（推荐、对比、主线）
- 右侧：证据与操作（文献、gap、动作按钮）

**内部模块命名与产品 UI 分离**：UI 不用 M1-M16 的模块名，用「文献 / 创新点 / 路线图」等用户语言。

---

## 12 个页面/设计点

### 1. 首页 Project Dashboard
第一屏只显示最重要的：项目名、研究方向、推荐 Idea、Thesis Fit / Evidence / Feasibility / Workload、当前阶段、下一步动作 + 流程进度（项目理解✓ → … → Roadmap ●）。

### 2. 左侧 Navigation
固定导航：Overview / Project / Research Questions / Literature / Research Gaps / Ideas / Evaluation / Roadmap / Experiments / Evidence / History。

### 3. Overview 页面（最值得做好）
- Research Recommendation 大卡片（Recommended: i5 + 指标组：Thesis Fit 86 / Evidence / Feasibility / Novelty / Risk）。
- **novelty 不是唯一主指标**（Thesis Fit 等并列）。
- Why this idea?（三句话）
- Main Risk
- Next 3 Actions

### 4. Ideas 页面 = 候选池
卡片式（i1/i5 等），带 Thesis Fit / Evidence / ⭐Recommended；支持按 Thesis Fit / Novelty / Feasibility / Evidence / Workload 排序。

### 5. Idea Detail（点击后 Drawer/页面）
Tab：Overview / Contribution / Evidence / Reviewer Risk / Experiments。
- Contribution：贡献矩阵，做**雷达图**（Method/Framework/Application/Problem/Training/Engineering）。
- Innovation Boundary 单独展示：Existing / New / Core difference。

### 6. Literature 页面 = Research Landscape（不做文章列表）
左 query，右 paper cards；每张 card：Claim / Method / Relevant to gap / Coverage + `Relevant / Partial / Peripheral` 标签。

### 7. Gap 页面 = Evidence Graph（Web 真正有优势的地方）
```
Paper A ──┐
          ├── Gap G1
Paper B ──┘
```
点 G1 右侧展开：Evidence 强度 / scope（2 papers）/ Why / ⚠ Based only on current retrieval。

### 8. Roadmap 页面（Demo 核心亮点）
- Paper Story（Current Work → Research Question → Experiment → Contribution）
- Research Questions（RQ1-RQ4）
- Experiment Matrix（实验 | Status | Variable | Models | Deliverable），点一行展开详情（purpose/variable/models/metric/success condition）。

### 9. Timeline = Kanban
Phase 1 Baseline → Phase 2 Condition → Phase 3 Contribution → Phase 4 Writing，任务卡片可勾选。

### 10. Agent 交互入口（右下角固定）
「Ask PaperMine」：contextual（自动带 current_project / current_idea / literature_refs / gap / evaluation）+ 快捷按钮（强化这个 Idea / 补充文献 / 生成实验 / 挑战这个 Idea / 重新评分）。

### 11. 模块化重跑（不整个 Pipeline 重跑）
如 Idea 页 Evidence: Weak → [Search More Literature] → 只跑 Retrieval → Gap update → Evaluation update，而非整套重分析。**体现 Agent 在做有目的的研究。**

### 12. History / Evolution 页面
展示 run 历史（i1 Score 54 → 加文献后 Score 48）与**自进化经验**（Agent learned: "gap 证据 <3 篇时避免强 novelty claim" + confidence + used runs）。

---

## 技术栈

- **Frontend**：Next.js + React + Tailwind CSS + shadcn/ui；图表 Recharts；图 React Flow。
- **Backend**：FastAPI（Python 核心不重写）。
- 架构：`Next.js → REST API → FastAPI → PaperMine Python Core → Dossier`。

## REST API（围绕 Dossier）

- 查询：`GET /projects/{id}`、`/ideas`、`/literature`、`/gaps`、`/roadmap`
- 操作：`POST /projects/{id}/analyze`、`POST /ideas/{id}/refine`、`/evaluate`、`POST /gaps/{id}/retrieve-more`

## Demo 第一版范围（5 页面）

```
1 Overview  2 Literature & Gap  3 Ideas  4 Idea Detail  5 Roadmap
```

演示路径：打开项目 → Overview 看推荐 → Ideas 看候选 → 进 i5 看贡献与风险 → 看 Evidence 来源 → 进 Roadmap 看未来 6 周怎么做。
