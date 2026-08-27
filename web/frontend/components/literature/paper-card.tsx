"use client";

import type { Paper } from "@/lib/types";
import { RELEVANCE_LABELS, type Relevance } from "@/lib/derive";
import { Badge } from "@/components/ui/badge";
import { clip, clean } from "@/lib/utils";
import { yearOf } from "@/lib/format";

const RELEVANCE_BADGE: Record<Relevance, "success" | "warning" | "outline"> = {
  relevant: "success",
  partial: "warning",
  peripheral: "outline",
};

function Field({ label, value }: { label: string; value?: string | null }) {
  const v = clean(value);
  if (!v) return null;
  return (
    <div className="flex gap-1.5 text-xs">
      <span className="shrink-0 text-muted-foreground">{label}：</span>
      <span className="break-words">{v}</span>
    </div>
  );
}

export function PaperCard({ paper, relevance }: { paper: Paper; relevance: Relevance }) {
  const meta = [clean(paper.venue), yearOf(paper), clean(paper.source)].filter(Boolean).join(" · ");
  const card = paper.evidence_card;
  const u = paper.understanding;

  return (
    <div className="rounded-lg border bg-card p-3 shadow-sm">
      <div className="mb-1.5 flex items-start justify-between gap-2">
        <h4 className="text-sm font-medium leading-snug">{clean(paper.title) || "（无标题）"}</h4>
        <Badge variant={RELEVANCE_BADGE[relevance]} className="shrink-0">
          {RELEVANCE_LABELS[relevance]}
        </Badge>
      </div>
      {meta && <div className="mb-1.5 text-xs text-muted-foreground">{meta}</div>}

      <div className="space-y-1 border-t pt-2">
        <Field label="核心主张" value={u?.claim} />
        <Field label="方法" value={u?.method} />
        <Field label="结论" value={clip(u?.conclusion, 160)} />
        {card ? (
          <div className="mt-1.5 flex flex-wrap gap-x-3 gap-y-1 border-t pt-1.5">
            <Field label="数据集" value={card.dataset} />
            <Field label="基线" value={card.baseline} />
            <Field label="指标" value={card.metric} />
            <Field label="提升" value={clip(card.main_gain, 80)} />
          </div>
        ) : null}
        {card?.evidence_source ? (
          <div className="text-[11px] text-muted-foreground">证据来源：{card.evidence_source}</div>
        ) : null}
      </div>
    </div>
  );
}
