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
