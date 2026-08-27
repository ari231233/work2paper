"use client";

import Link from "next/link";

import type { IdeaWithEval } from "@/lib/types";
import type { DecisionInfo } from "@/lib/derive";
import { thesisFitScore } from "@/lib/derive";
import { evidenceLabel, feasibilityLabel, num, typeLabel } from "@/lib/format";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { DecisionBadge } from "@/components/decision/decision-badge";
import { clean, clip } from "@/lib/utils";

function Chip({ label, value, tone }: { label: string; value: string; tone?: "default" | "warning" }) {
  return (
    <div className="rounded-md bg-muted/60 px-2 py-1 text-center">
      <div className="text-[10px] uppercase tracking-wide text-muted-foreground">{label}</div>
      <div className={tone === "warning" ? "text-sm font-semibold text-amber-600" : "text-sm font-semibold"}>
        {value}
      </div>
    </div>
  );
}

export function IdeaCard({ pair, decision }: { pair: IdeaWithEval; decision?: DecisionInfo | null }) {
  const { idea, evaluation } = pair;
  const ev = evaluation;
  const fit = thesisFitScore(ev);

  return (
    <Link href={`/ideas/${idea.idea_id}`} className="group">
      <Card className="h-full transition-shadow group-hover:shadow-md">
        <CardContent className="flex h-full flex-col gap-2.5 p-4">
          <div className="flex flex-wrap items-center gap-2">
            <code className="text-sm font-semibold text-primary">{idea.idea_id}</code>
            <DecisionBadge info={decision} />
          </div>

          <p className="line-clamp-2 text-sm font-medium leading-snug">{clean(idea.claim)}</p>
          <p className="line-clamp-2 text-xs text-muted-foreground">{clip(idea.novelty_hypothesis, 120)}</p>

          <div className="mt-auto flex items-center gap-2 text-xs text-muted-foreground">
            <Badge variant="outline">{typeLabel(ev)}</Badge>
          </div>

          <div className="grid grid-cols-5 gap-1.5">
            <Chip label="Thesis Fit" value={fit ? String(fit) : "—"} />
            <Chip label="Novelty" value={num(ev?.novelty_score)} />
            <Chip label="Evidence" value={evidenceLabel(ev?.evidence_validation?.evidence as never)} tone={ev?.evidence_validation?.evidence === "weak" ? "warning" : undefined} />
            <Chip label="Feasibility" value={feasibilityLabel(ev?.data_feasibility as never)} />
            <Chip label="Workload" value={`${num(ev?.workload_hours)}h`} />
          </div>
        </CardContent>
      </Card>
    </Link>
  );
}
