// 展示层推导：把后端 Dossier 的原始字段确定性地转成「科研决策工作台」的展示指标。
//
// 原则（对齐 lessons-learned.md §3.2「指标值要追溯来源」）：
// - 能用后端真实字段的，直接用（novelty_score / evidence / data_feasibility / workload_hours…）；
// - 后端没有的展示指标（Thesis Fit、Risk、Relevant/Partial/Peripheral 标签、流程进度），
//   由这里**确定性推导**，并在 UI 上标注「推导」，绝不把推导值冒充后端原始数据。
// - 纯函数、可复现：相同输入 → 相同输出。

import type {
  Dossier,
  Evaluation,
  EvidenceLevel,
  Feasibility,
  Idea,
  IdeaWithEval,
  LiteratureEntry,
  Paper,
  Roadmap,
  Verdict,
} from "./types";
import {
  ATTACK_KEYS,
  MATRIX_DIMENSIONS,
  MATRIX_LABELS,
  evidenceLabel,
  feasibilityLabel,
  noveltyBandLabel,
  num,
  strengthLabel,
  strengthOrder,
} from "./format";
import { asList, clean, clip } from "./utils";

// ---------------------------------------------------------------------------
// 排名 / 选中
// ---------------------------------------------------------------------------

const VERDICT_PRIO: Record<string, number> = { proceed: 0, rework: 1, drop: 2 };

export function evaluationMap(evaluations?: Evaluation[] | null): Record<string, Evaluation> {
  const out: Record<string, Evaluation> = {};
  for (const ev of asList(evaluations)) {
    if (ev?.idea_ref) out[String(ev.idea_ref)] = ev;
  }
  return out;
}

/** 候选 idea 排名：proceed > rework > drop，同级 novelty 降序、workload 升序（与后端 plan._select_idea 对齐）。 */
export function rankedIdeas(ideas: Idea[] | undefined, evaluations?: Evaluation[] | null): IdeaWithEval[] {
  const evMap = evaluationMap(evaluations);
  const pairs: IdeaWithEval[] = asList(ideas)
    .filter((i) => clean(i.idea_id))
    .map((idea) => ({ idea, evaluation: evMap[String(idea.idea_id)] }));
  pairs.sort((a, b) => {
    const va = a.evaluation?.verdict;
    const vb = b.evaluation?.verdict;
    const pa = va ? VERDICT_PRIO[va] ?? 1 : 1;
    const pb = vb ? VERDICT_PRIO[vb] ?? 1 : 1;
    if (pa !== pb) return pa - pb;
    const na = numValue(a.evaluation?.novelty_score);
    const nb = numValue(b.evaluation?.novelty_score);
    if (na !== nb) return nb - na;
    return numValue(a.evaluation?.workload_hours) - numValue(b.evaluation?.workload_hours);
  });
  return pairs;
}

export function selectedPair(dossier?: Dossier | null): { idea: Idea | null; evaluation?: Evaluation } {
  const ideas = asList(dossier?.ideas);
  const evMap = evaluationMap(dossier?.evaluations);
  const selectedId = clean(dossier?.roadmap?.selected_idea);
  if (selectedId) {
    const idea = ideas.find((i) => String(i.idea_id) === selectedId) ?? null;
    if (idea) return { idea, evaluation: evMap[selectedId] };
  }
  const ranked = rankedIdeas(ideas, dossier?.evaluations);
  if (ranked.length) return { idea: ranked[0].idea, evaluation: ranked[0].evaluation };
  return { idea: null };
}

export function evaluationFor(dossier: Dossier | null | undefined, ideaId?: string): Evaluation | undefined {
  if (!ideaId) return undefined;
  return evaluationMap(dossier?.evaluations)[String(ideaId)];
}

function numValue(v: unknown): number {
  return typeof v === "number" && !Number.isNaN(v) ? v : 0;
}

// ---------------------------------------------------------------------------
// 贡献矩阵 / 雷达 / 创新边界
// ---------------------------------------------------------------------------

export interface MatrixRowView {
  key: string;
  label: string;
  strength: string;
  order: number;
  reason: string;
}

export function matrixRows(ev?: Evaluation | null): MatrixRowView[] {
  const matrix = ev?.contribution?.matrix ?? {};
  return MATRIX_DIMENSIONS.map((key) => {
    const row = matrix[key];
    return {
      key,
      label: MATRIX_LABELS[key] ?? key,
      strength: strengthLabel(row?.strength),
      order: strengthOrder(row?.strength),
      reason: clean(row?.reason),
    };
  });
}

/** 贡献矩阵是否有任一维度 ≥ 中（M21 核心：模块组合类 idea 不因 novelty 低被误 reject）。 */
export function matrixViable(ev?: Evaluation | null): boolean {
  return matrixRows(ev).some((r) => r.order >= 2);
}

/** 雷达图数据：6 维强度档 → 0~100（none 0 / low 25 / medium 50 / medium_high 75 / high 100）。 */
export function radarData(ev?: Evaluation | null): { dim: string; score: number; strength: string }[] {
  return matrixRows(ev).map((r) => ({
    dim: r.label,
    score: r.order * 25,
    strength: r.strength,
  }));
}

/** innovation boundary：论文只主张强项（≥ 中），弱项留作 limitation。 */
export function innovationBoundary(ev?: Evaluation | null): { strong: string[]; weak: string[] } {
  const strong: string[] = [];
  const weak: string[] = [];
  for (const r of matrixRows(ev)) {
    if (r.order >= 2) strong.push(r.label);
    else weak.push(r.label);
  }
  return { strong, weak };
}

// ---------------------------------------------------------------------------
// 指标组：Thesis Fit / Evidence / Feasibility / Novelty / Risk
// ---------------------------------------------------------------------------

export type MetricTone = "good" | "warn" | "bad" | "neutral";

export interface Metric {
  key: string;
  label: string;
  value: number; // 0~100
  display: string; // 主文案
  level: string; // 档位文案
  tone: MetricTone;
  detail: string; // 一句话来源说明
  derived?: boolean; // 是否为前端推导值
}

function clamp(v: number, lo = 0, hi = 100): number {
  return Math.max(lo, Math.min(hi, Math.round(v)));
}

/**
 * Thesis Fit（硕士论文契合度）——后端无此单一字段，由 M21 贡献矩阵 + 可行性 + 工作量推导：
 * - 矩阵分（0~60）：6 个贡献维度强度档（0~4）的均值 /4 × 60；
 * - 可行性分（0~20）：data_feasibility high 20 / medium 12 / low 4；
 * - 工作量分（0~20）：≤160 → 20，≤260 → 14，≤400 → 8，其余 4；
 * - verdict 修正：proceed 0，rework −10，drop −30。
 * 这是「面向硕士生」的契合度视角（M21 的核心修正），非 novelty 替代指标。
 */
export function thesisFitScore(ev?: Evaluation | null): number {
  if (!ev) return 0;
  const rows = matrixRows(ev);
  const avg = rows.length ? rows.reduce((s, r) => s + r.order, 0) / rows.length : 0;
  const matrixScore = (avg / 4) * 60;
  const feas = ev.data_feasibility === "high" ? 20 : ev.data_feasibility === "medium" ? 12 : 4;
  const wl = numValue(ev.workload_hours);
  const workloadScore = wl <= 160 ? 20 : wl <= 260 ? 14 : wl <= 400 ? 8 : 4;
  const verdictAdj = ev.verdict === "drop" ? -30 : ev.verdict === "rework" ? -10 : 0;
  return clamp(matrixScore + feas + workloadScore + verdictAdj);
}

export function thesisFitDetail(ev?: Evaluation | null): string {
  const viable = matrixViable(ev);
  const rows = matrixRows(ev).filter((r) => r.order >= 2);
  const parts = [
    viable ? "贡献矩阵存在可行维度" : "贡献矩阵暂无可行维度",
    rows.length ? `强项：${rows.map((r) => r.label).join("、")}` : "",
    `数据可得性 ${feasibilityLabel(ev?.data_feasibility ?? null)}`,
    `工作量 ${num(ev?.workload_hours)}h`,
  ].filter(Boolean);
  return `推导值（${parts.join("；")}）`;
}

/** 主风险来源：优先 M22 风险分支，缺失回退 M21 攻击测试。 */
export function riskItems(roadmap?: Roadmap | null, ev?: Evaluation | null): { risk: string; branch: string }[] {
  const rbs = asList(roadmap?.risk_branches).filter((rb) => clean(rb?.risk));
  if (rbs.length) return rbs.map((rb) => ({ risk: clean(rb.risk), branch: clean(rb.branch) || "（待定）" }));
  const attacks = ev?.contribution?.attacks;
  if (attacks) {
    return ATTACK_KEYS.map((k) => {
      const a = attacks[k];
      return { risk: clean(a?.attack), branch: clean(a?.answer) || "（待定）" };
    }).filter((x) => x.risk);
  }
  return [];
}

export interface MetricGroup {
  thesisFit: Metric;
  evidence: Metric;
  feasibility: Metric;
  novelty: Metric;
  risk: Metric;
}

export function computeMetrics(
  ev?: Evaluation | null,
  roadmap?: Roadmap | null
): MetricGroup {
  const thesisFit = thesisFitScore(ev);
  const evidence = ev?.evidence_validation?.evidence;
  const feas = ev?.data_feasibility;
  const novelty = ev?.novelty_score;
  const risks = riskItems(roadmap, ev);

  const evVal = evidence === "strong" ? 88 : evidence === "medium" ? 62 : evidence === "weak" ? 35 : 0;
  // 「弱证据」用黄（需补充），不用红；未评估用灰（未知/无证据）。
  const evTone: MetricTone =
    evidence === "strong" ? "good" : evidence === "medium" || evidence === "weak" ? "warn" : "neutral";

  const feasVal = feas === "high" ? 85 : feas === "medium" ? 55 : feas === "low" ? 35 : 0;
  // 数据可得性低 = 需补充（黄），不视作明确阻塞；未评估 = 灰。
  const feasTone: MetricTone =
    feas === "high" ? "good" : feas === "medium" || feas === "low" ? "warn" : "neutral";

  const novVal = numValue(novelty);
  const novTone: MetricTone = novVal >= 70 ? "good" : novVal >= 60 ? "warn" : "neutral";

  // 风险：verdict 定基（drop 80 / rework 55 / proceed 30），evidence 与风险分支数量微调。
  let riskVal = ev?.verdict === "drop" ? 80 : ev?.verdict === "rework" ? 55 : 30;
  if (evidence === "weak") riskVal += 10;
  if (evidence === "strong") riskVal -= 10;
  if (risks.length >= 3) riskVal += 10;
  if (risks.length === 0) riskVal -= 5;
  riskVal = clamp(riskVal);
  const riskTone: MetricTone = riskVal >= 65 ? "bad" : riskVal >= 40 ? "warn" : "good";

  return {
    thesisFit: {
      key: "thesisFit",
      label: "论文契合度",
      value: thesisFit,
      display: String(thesisFit),
      level: thesisFit >= 70 ? "契合度高" : thesisFit >= 50 ? "契合度中" : "契合度低",
      tone: thesisFit >= 70 ? "good" : thesisFit >= 50 ? "warn" : "bad",
      detail: thesisFitDetail(ev),
      derived: true,
    },
    evidence: {
      key: "evidence",
      label: "证据强度",
      value: evVal,
      display: evidenceLabel(evidence as never),
      level: evidence ? `${evidenceLabel(evidence as never)}证据` : "未评估",
      tone: evTone,
      detail: clip(ev?.evidence_validation?.reason, 120) || "证据强度（四维审查）",
    },
    feasibility: {
      key: "feasibility",
      label: "可行性",
      value: feasVal,
      display: feasibilityLabel(feas as never),
      level: `数据可得性${feasibilityLabel(feas as never)}`,
      tone: feasTone,
      detail: `数据可得性 ${feasibilityLabel(feas as never)}；工作量 ${num(ev?.workload_hours)}h`,
    },
    novelty: {
      key: "novelty",
      label: "创新程度",
      value: novVal,
      display: num(novelty),
      level: ev?.novelty_band ? noveltyBandLabel(ev.novelty_band) : "未评估",
      tone: novTone,
      detail: "5 维加权（问题/方法/深度/gap/推广），分数由规则算出",
    },
    risk: {
      key: "risk",
      label: "评审风险",
      value: riskVal,
      display: riskVal >= 65 ? "高" : riskVal >= 40 ? "中" : "低",
      level: `${risks.length} 条风险分支`,
      tone: riskTone,
      detail: risks[0] ? clip(risks[0].risk, 120) : "无明确风险分支",
      derived: true,
    },
  };
}

// ---------------------------------------------------------------------------
// 推荐理由 / 下一步 / 证据覆盖度
// ---------------------------------------------------------------------------

/** 「为什么推荐」：贡献类型 + 强维度 + 证据强度，兜底 novelty 假设（拆成 ≤3 句）。 */
export function whyThis(idea?: Idea | null, ev?: Evaluation | null): string[] {
  const parts: string[] = [];
  const c = ev?.contribution;
  if (c) {
    const tl = clean(c.type_label).split("（")[0];
    const strong = matrixRows(ev)
      .filter((r) => r.order >= 2)
      .map((r) => r.label);
    if (tl) parts.push(`贡献类型：${tl}${strong.length ? `，贡献集中在${strong.join("、")}` : ""}`);
  }
  const evv = ev?.evidence_validation?.evidence;
  if (evv) parts.push(`证据强度 ${evidenceLabel(evv)}${ev?.evidence_validation?.reason ? `：${clip(ev.evidence_validation.reason, 90)}` : ""}`);
  if (idea) parts.push(clip(idea.novelty_hypothesis || idea.claim, 140));
  if (!parts.length) return ["（需人工复核）"];
  return parts.slice(0, 3);
}

/** Immediate Next Actions（3 条）：优先 MVP must_have，缺口时前置采集动作。 */
export function nextActions(roadmap?: Roadmap | null): string[] {
  const mvp = asList(roadmap?.minimum_viable_paper?.must_have).map(clean).filter(Boolean);
  let actions = mvp;
  if (!actions.length) {
    const first = asList(roadmap?.stage_exits)[0];
    actions = asList(first?.tasks).map(clean).filter(Boolean);
  }
  if (!actions.length) {
    actions = [
      "准备评测数据并确定评测协议（train/val/test 划分 + 固定随机种子）",
      "复现 2~3 个代表性 baseline（指标对齐文献）",
      "跑通主实验并起草论文大纲（引言/方法/实验/结论）",
    ];
  }
  const missing = asList(roadmap?.missing_items).map(clean).filter(Boolean).join(" ");
  if (/数据|采集|标注/.test(missing)) {
    actions = ["回填/采集评测数据（路线图 missing_items 提示数据缺口）", ...actions];
  }
  return actions.slice(0, 3);
}

/** Literature Landscape 的「证据覆盖度」一句话。 */
export function evidenceCoverage(literature?: LiteratureEntry[] | null): string {
  const entries = asList(literature);
  let nPapers = 0;
  const srcCount: Record<string, number> = {};
  const gapLevels: string[] = [];
  for (const entry of entries) {
    for (const p of asList(entry?.papers)) {
      nPapers += 1;
      const src = clean(p.evidence_card?.evidence_source) || "abstract";
      srcCount[src] = (srcCount[src] || 0) + 1;
    }
    for (const g of asList(entry?.contradiction_graph?.gaps)) {
      const lv = g.type === "contradiction" ? g.evidence_level : g.gap_hypothesis?.evidence_level;
      if (lv) gapLevels.push(lv);
    }
  }
  const parts: string[] = [];
  parts.push(nPapers ? `共 ${nPapers} 篇论文` : "无文献（离线/无结果）");
  const srcs = Object.entries(srcCount)
    .sort((a, b) => b[1] - a[1])
    .map(([s, c]) => `${c}×${s}`);
  if (srcs.length) parts.push(`证据来源：${srcs.join("、")}`);
  if (gapLevels.length) parts.push(`gap 证据级别：${gapLevels.join("、")}`);
  return parts.join("；");
}

// ---------------------------------------------------------------------------
// 流程进度（UNDERSTAND → … → REFLECT，产品语言，不用内部命名）
// ---------------------------------------------------------------------------

export interface PipelineStep {
  key: string;
  label: string;
  status: "done" | "current" | "pending";
}

export function pipelineSteps(dossier?: Dossier | null): PipelineStep[] {
  const assets = dossier?.assets;
  const steps: { key: string; label: string; done: boolean }[] = [
    { key: "understand", label: "项目理解", done: Boolean(clean(assets?.narrative)) },
    { key: "abstract", label: "研究问题", done: asList(dossier?.problems).length > 0 },
    { key: "retrieve", label: "文献检索", done: asList(dossier?.literature).length > 0 },
    { key: "generate", label: "创新点", done: asList(dossier?.ideas).length > 0 },
    { key: "evaluate", label: "可行性评估", done: asList(dossier?.evaluations).length > 0 },
    {
      key: "plan",
      label: "路线图",
      done: Boolean(dossier?.roadmap?.core_story) || Boolean(clean(dossier?.roadmap?.selected_idea)),
    },
    {
      key: "reflect",
      label: "经验沉淀",
      done: asList(dossier?.human_decisions).some((d) => clean(d.checkpoint) === "cp5"),
    },
  ];
  const firstPending = steps.findIndex((s) => !s.done);
  return steps.map((s, i) => ({
    key: s.key,
    label: s.label,
    status: s.done ? "done" : i === firstPending ? "current" : "pending",
  }));
}

// ---------------------------------------------------------------------------
// 文献：扁平化 + 相关性标签（Relevant / Partial / Peripheral）
// ---------------------------------------------------------------------------

export interface FlatPaper {
  paper: Paper;
  entryIndex: number;
  entry: LiteratureEntry;
}

export function flatPapers(literature?: LiteratureEntry[] | null): FlatPaper[] {
  const out: FlatPaper[] = [];
  asList(literature).forEach((entry, entryIndex) => {
    for (const paper of asList(entry?.papers)) {
      if (clean(paper.title)) out.push({ paper, entryIndex, entry });
    }
  });
  return out;
}

export type Relevance = "relevant" | "partial" | "peripheral";

export interface Focus {
  titles: Set<string>;
  gapIds: Set<string>;
}

/** 以推荐 idea 为「焦点」，构造相关性判据（标题 + gap）。 */
export function buildFocus(dossier?: Dossier | null): Focus {
  const { idea } = selectedPair(dossier);
  return {
    titles: new Set(asList(idea?.literature_refs).map(clean).filter(Boolean)),
    gapIds: new Set(asList(idea?.gap_refs).map(clean).filter(Boolean)),
  };
}

/**
 * 论文相关性三档（推导，透明）：
 * - relevant：被推荐 idea 直接引用（literature_refs）；
 * - partial：与推荐 idea 的来源 gap 同属一个检索条目（参与了 gap 挖掘）；
 * - peripheral：其余（与当前推荐主线关系较远）。
 */
export function relevanceOf(fp: FlatPaper, focus: Focus): Relevance {
  if (fp.paper.relevance_level === "high") return "relevant";
  if (fp.paper.relevance_level === "partial") return "partial";
  const title = clean(fp.paper.title);
  if (focus.titles.has(title)) return "relevant";
  const entryGapIds = asList(fp.entry?.contradiction_graph?.gaps).map((g) => clean(g.gap_id));
  if (entryGapIds.some((id) => focus.gapIds.has(id))) return "partial";
  return "peripheral";
}

export const RELEVANCE_LABELS: Record<Relevance, string> = {
  relevant: "高度相关",
  partial: "部分相关",
  peripheral: "背景参考",
};

export const RELEVANCE_TONES: Record<Relevance, "good" | "warn" | "neutral"> = {
  relevant: "good",
  partial: "warn",
  peripheral: "neutral",
};

/** gap 记录 -> 证据级别（gap 型在 gap_hypothesis，矛盾型在顶层）。 */
export function gapEvidenceLevel(g?: { type?: string; evidence_level?: string; gap_hypothesis?: { evidence_level?: string } } | null): string | null {
  if (!g) return null;
  if (g.type === "contradiction") return g.evidence_level ?? null;
  return g.gap_hypothesis?.evidence_level ?? null;
}

// ---------------------------------------------------------------------------
// 决策状态层（M25 v2.3）：三层决策语义，统一全站「推荐」口径。
//
// 判定规则放在前端、不改后端 schema。派生依据 = verdict + evidence_validation.evidence
// + data_feasibility + 相对排名（selectedPair）：
//   - 建议实施     ：verdict=proceed 且证据（medium/strong）与数据可得性（high/medium）达标；
//   - 首选探索方向 ：候选相对最好（selectedPair），但尚未通过上述证据/可行性门槛；
//   - 暂不建议     ：其余（verdict=drop → 换方向；证据弱 → 证据不足，暂不通过；数据可得性低）。
// 任一 idea 全站只呈现一种决策状态 + 一处相对排名，不再同时出现「Recommended」与「Weak Reject」。
// ---------------------------------------------------------------------------

export type DecisionState = "preferred" | "recommend" | "not_now";
export type DecisionTone = "good" | "warn" | "bad" | "neutral";

export interface DecisionInfo {
  state: DecisionState;
  /** 相对排名（1 起，首选=1；与 rankedIdeas/selectedPair 对齐） */
  rank: number;
  /** 是否相对最好（selectedPair 选中） */
  isSelected: boolean;
  label: string;
  /** 门槛/结论短提示，拼接在 label 后（「首选探索方向｜尚未通过证据门槛」） */
  hint: string;
  summary: string;
  tone: DecisionTone;
  /** 追溯依据（一句话） */
  reason: string;
  evidence: EvidenceLevel | null;
  feasibility: Feasibility | null;
  verdict: Verdict | null;
}

const VERDICT_TEXT: Record<string, string> = { proceed: "推荐", rework: "可改进", drop: "不建议" };

function gateReason(verdict?: Verdict | null, evidence?: EvidenceLevel | null, feas?: Feasibility | null): string {
  const parts: string[] = [];
  if (verdict) parts.push(`verdict=${VERDICT_TEXT[verdict] ?? verdict}`);
  if (evidence) parts.push(`证据${evidenceLabel(evidence)}`);
  if (feas) parts.push(`数据可得性${feasibilityLabel(feas)}`);
  return parts.join(" · ") || "未评估";
}

export function decisionFor(dossier: Dossier | null | undefined, ideaId?: string): DecisionInfo | null {
  if (!ideaId) return null;
  const ranked = rankedIdeas(dossier?.ideas, dossier?.evaluations);
  const idx = ranked.findIndex((p) => String(p.idea.idea_id) === String(ideaId));
  const sel = selectedPair(dossier);
  const isSelected = Boolean(sel.idea && String(sel.idea.idea_id) === String(ideaId));
  const rank = idx >= 0 ? idx + 1 : isSelected ? 1 : 0;

  const ev = evaluationFor(dossier, ideaId);
  const evidence = ev?.evidence_validation?.evidence ?? null;
  const feas = ev?.data_feasibility ?? null;
  const verdict = ev?.verdict ?? null;

  const base = { rank, isSelected, evidence, feasibility: feas, verdict };

  if (!ev) {
    return {
      ...base,
      state: isSelected ? "preferred" : "not_now",
      label: isSelected ? "首选探索方向" : "待评估",
      hint: "尚未评估",
      summary: isSelected ? "首选探索方向｜尚未评估" : "待评估",
      tone: "neutral",
      reason: "该 idea 尚未评估",
    };
  }

  const passedGate =
    verdict === "proceed" &&
    (evidence === "medium" || evidence === "strong") &&
    (feas === "high" || feas === "medium");

  let state: DecisionState;
  let label: string;
  let hint: string;
  let tone: DecisionTone;
  let reason: string;

  if (passedGate) {
    state = "recommend";
    label = "建议实施";
    hint = "证据与可行性达标";
    tone = "good";
    reason = gateReason(verdict, evidence, feas);
  } else if (verdict === "drop") {
    state = "not_now";
    label = "暂不建议";
    hint = "建议换方向";
    tone = "bad";
    reason = `评估结论为不建议（drop）${ev.rework_reason ? `：${clip(ev.rework_reason, 60)}` : ""}`;
  } else if (feas === "low") {
    state = "not_now";
    label = "暂不建议";
    hint = "数据可得性不足";
    tone = "warn";
    reason = "数据可得性低，需先补数据";
  } else if (isSelected) {
    state = "preferred";
    label = "首选探索方向";
    hint = "尚未通过证据门槛";
    tone = "neutral";
    reason = gateReason(verdict, evidence, feas);
  } else if (evidence === "weak") {
    state = "not_now";
    label = "暂不建议";
    hint = "证据不足，暂不通过";
    tone = "warn";
    reason = gateReason(verdict, evidence, feas);
  } else {
    state = "not_now";
    label = "暂不建议";
    hint = "需补证据";
    tone = "warn";
    reason = gateReason(verdict, evidence, feas);
  }

  return { ...base, state, label, hint, summary: `${label}｜${hint}`, tone, reason };
}
