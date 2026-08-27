import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

/** shadcn/ui 约定的 class 合并工具。 */
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

/** 把任意值折叠为单行文本（去首尾 / 合并空白）。 */
export function clean(v: unknown): string {
  return (v == null ? "" : String(v)).replace(/\s+/g, " ").trim();
}

/** 折叠 + 截断（超长加省略号）。 */
export function clip(v: unknown, n: number): string {
  const s = clean(v);
  return s.length > n ? s.slice(0, n) + "…" : s;
}

/** 空值安全数组。 */
export function asList<T>(v: T[] | null | undefined): T[] {
  return Array.isArray(v) ? v : [];
}

// ---------------------------------------------------------------------------
// 项目名称 / 摘要派生（M25 v2.1）：前端从 assets.narrative 确定性派生，不新增后端字段。
// ---------------------------------------------------------------------------

const SENTENCE_SPLIT = /[。．.!?！？；;\n]+/;
const CLAUSE_SPLIT = /[，,、：:]+/;
const LEADING_FILLER = /^(本项目|本系统|该项目|该系统|这个项目|当前项目)\s*/;

/** 项目短名：取叙事首句的首个有信息量从句，去掉口头前缀后截断（默认 ≤28 字）。 */
export function projectName(v: unknown, max = 28): string {
  const s = clean(v);
  if (!s) return "";
  const stripped = s.replace(LEADING_FILLER, "");
  const first = stripped.split(SENTENCE_SPLIT)[0] ?? "";
  const clauses = first.split(CLAUSE_SPLIT).map((c) => c.trim()).filter(Boolean);
  const head = clauses.find((c) => c.length >= 4) ?? clauses[0] ?? first;
  return clip(head, max);
}

/** 项目摘要（≤50 字）：优先取叙事第二句（避开作为短名的第一句），单句时取整句。 */
export function projectSummary(v: unknown, max = 50): string {
  const s = clean(v);
  if (!s) return "";
  const sentences = s.split(SENTENCE_SPLIT).map((c) => c.trim()).filter(Boolean);
  const body = sentences[1] ?? sentences[0] ?? "";
  return clip(body, max);
}
