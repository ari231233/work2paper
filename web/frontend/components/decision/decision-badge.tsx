"use client";

import type { DecisionInfo, DecisionTone } from "@/lib/derive";
import { Badge } from "@/components/ui/badge";

const TONE_VARIANT: Record<DecisionTone, "success" | "warning" | "destructive" | "accent"> = {
  good: "success",
  warn: "warning",
  bad: "destructive",
  neutral: "accent",
};

/**
 * 统一决策状态徽章（M25 v2.3）：任一 idea 只呈现一种决策状态 + 一处相对排名。
 * - 徽章文案 = label｜hint（如「首选探索方向｜尚未通过证据门槛」）；
 * - 相对排名：首选显示「首选」，其余显示「#N」；
 * - title 承载完整判定 + 追溯依据（可复现口径）。
 */
export function DecisionBadge({ info, showRank = true }: { info?: DecisionInfo | null; showRank?: boolean }) {
  if (!info) return null;
  return (
    <span className="inline-flex flex-wrap items-center gap-1.5">
      <Badge
        variant={TONE_VARIANT[info.tone]}
        title={`${info.summary}${info.reason ? `\n${info.reason}` : ""}`}
      >
        {info.summary}
      </Badge>
      {showRank && (
        <span className="text-[11px] text-muted-foreground/80">
          {info.isSelected ? "首选" : info.rank > 0 ? `#${info.rank}` : "—"}
        </span>
      )}
    </span>
  );
}
