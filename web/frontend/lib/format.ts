// 展示格式化：把后端枚举 / 数值渲染成产品语言（中文标签、★、档位）。

import type { EvidenceLevel, Feasibility, Strength, Verdict } from "./types";
import { clean } from "./utils";

export const STRENGTH_ORDER: Record<Strength, number> = {
  none: 0,
  low: 1,
  medium: 2,
  medium_high: 3,
  high: 4,
};

export const STRENGTH_LABELS: Record<Strength, string> = {
  none: "无",
  low: "低",
  medium: "中",
  medium_high: "中高",
  high: "高",
};

export const STRENGTH_BLOCKS: Record<Strength, string> = {
  none: "▫▫▫▫▫",
  low: "█░░░░",
  medium: "██░░░",
  medium_high: "███░░",
  high: "█████",
};

export const MATRIX_LABELS: Record<string, string> = {
  method: "方法创新",
  framework: "框架创新",
  application: "应用创新",
  problem: "问题创新",
  training: "训练策略创新",
  engineering: "工程价值",
};

export const MATRIX_DIMENSIONS = [
  "method",
  "framework",
  "application",
  "problem",
  "training",
  "engineering",
] as const;

export const TYPE_SHORT: Record<string, string> = {
  A: "方法创新",
  B: "框架集成",
  C: "应用创新",
  D: "问题建模",
  E: "训练策略",
};

export const ATTACK_KEYS = ["ablation", "concatenation", "reviewer"] as const;
export const ATTACK_LABELS: Record<string, string> = {
  ablation: "Attack 1 · 消融",
  concatenation: "Attack 2 · 简单拼接",
  reviewer: "Attack 3 · Reviewer 视角",
};

export const VERDICT_LABELS: Record<Verdict, string> = {
  proceed: "推荐",
  rework: "可改进",
  drop: "不建议",
};

export const VERDICT_TONES: Record<Verdict, "good" | "warn" | "bad"> = {
  proceed: "good",
  rework: "warn",
  drop: "bad",
};

export const FEASIBILITY_LABELS: Record<Feasibility, string> = {
  high: "高",
  medium: "中",
  low: "低",
};

export const EVIDENCE_LABELS: Record<EvidenceLevel, string> = {
  weak: "弱",
  medium: "中",
  strong: "强",
};

export const GAP_EVIDENCE_LABELS: Record<string, string> = {
  weak: "弱",
  moderate: "中",
  strong: "强",
};

// novelty 分数段标签 → 产品语言（M25 v2.3：Weak Reject → 证据不足，暂不通过）
export const NOVELTY_BAND_LABELS: Record<string, string> = {
  Reject: "基本无创新",
  "Weak Reject": "证据不足，暂不通过",
  Revise: "需改进",
  Accept: "可接受",
  Priority: "优先",
};

export const CHECK_STATUS_LABELS: Record<string, string> = {
  ok: "到位",
  concern: "需补强",
  missing: "缺失",
};

export function strengthLabel(s?: Strength): string {
  return (s && STRENGTH_LABELS[s]) || "—";
}

export function strengthBlock(s?: Strength): string {
  return (s && STRENGTH_BLOCKS[s]) || "·····";
}

export function strengthOrder(s?: Strength): number {
  return (s && STRENGTH_ORDER[s]) ?? 0;
}

/** verdict + novelty 合成的推荐程度（★），与后端 report 口径一致。 */
export function starRating(verdict?: Verdict, novelty?: number): string {
  if (!verdict) return "☆☆☆☆☆（无评估）";
  if (verdict === "drop") return "★☆☆☆☆（不建议）";
  if (verdict === "rework") return "★★☆☆☆（改进后再议）";
  const n = typeof novelty === "number" && !Number.isNaN(novelty) ? novelty : 0;
  if (n >= 80) return "★★★★★（强烈推荐）";
  if (n >= 70) return "★★★★☆（推荐）";
  if (n >= 60) return "★★★☆☆（可尝试）";
  return "★★☆☆☆（谨慎）";
}

export function typeLabel(ev?: { contribution?: { type?: string; type_label?: string } } | null): string {
  const c = ev?.contribution;
  if (!c) return "—";
  const short = c.type ? TYPE_SHORT[c.type] : undefined;
  const label = clean(c.type_label).split("（")[0];
  if (c.type && short) return `${c.type} · ${short}`;
  return label || c.type || "—";
}

export function evidenceLabel(lv?: EvidenceLevel | null): string {
  return (lv && EVIDENCE_LABELS[lv]) || "—";
}

export function gapEvidenceLabel(lv?: string | null): string {
  return (lv && GAP_EVIDENCE_LABELS[lv]) || lv || "—";
}

export function noveltyBandLabel(band?: string | null): string {
  if (!band) return "—";
  return NOVELTY_BAND_LABELS[band] ?? band;
}

export function feasibilityLabel(f?: Feasibility | null): string {
  return (f && FEASIBILITY_LABELS[f]) || "—";
}

/** 数字 -> 干净字符串（58.0 -> "58"）。 */
export function num(x?: number | null): string {
  if (typeof x !== "number" || Number.isNaN(x)) return "—";
  return Number.isInteger(x) ? String(x) : String(Math.round(x * 10) / 10);
}

export function hours(x?: number | null): string {
  if (typeof x !== "number" || Number.isNaN(x)) return "—";
  return `≈ ${num(x)} h`;
}

export function yearOf(p?: { year?: number } | null): string {
  const y = p?.year;
  return y ? String(y) : "";
}

/** ISO 时间戳 -> 短日期（YYYY-MM-DD），无效则返回空串。 */
export function shortDate(ts?: string | null): string {
  if (!ts) return "";
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return "";
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${d.getFullYear()}-${m}-${day}`;
}

export function join(v?: string[] | null, sep = "、", limit = 6): string {
  const items = (v ?? []).filter((s) => clean(s));
  const shown = items.slice(0, limit);
  const rest = items.length > limit ? ` +${items.length - limit}` : "";
  return shown.join(sep) + rest;
}
