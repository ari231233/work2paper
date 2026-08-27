"use client";

import { innovationBoundary } from "@/lib/derive";
import type { Evaluation } from "@/lib/types";
import { Badge } from "@/components/ui/badge";

/** Innovation Boundary：论文只主张强项（≥ 中），弱项留作 limitation，不主张。 */
export function InnovationBoundary({ evaluation }: { evaluation?: Evaluation | null }) {
  const { strong, weak } = innovationBoundary(evaluation);

  return (
    <div className="space-y-3 rounded-lg border p-4">
      <h4 className="text-sm font-semibold">Innovation Boundary（创新边界）</h4>
      <div>
        <div className="mb-1 text-xs font-medium text-emerald-700 dark:text-emerald-400">主张（Existing 之上的 New）</div>
        <div className="flex flex-wrap gap-1.5">
          {strong.length ? strong.map((s) => <Badge key={s} variant="success">{s}</Badge>) : <span className="text-xs text-muted-foreground">—</span>}
        </div>
      </div>
      <div>
        <div className="mb-1 text-xs font-medium text-muted-foreground">不主张（留作 limitation）</div>
        <div className="flex flex-wrap gap-1.5">
          {weak.length ? weak.map((s) => <Badge key={s} variant="outline">{s}</Badge>) : <span className="text-xs text-muted-foreground">—</span>}
        </div>
      </div>
      <p className="text-xs text-muted-foreground">
        论文只主张强项，弱项明确写进 limitation——避免被 reviewer 以「边界不清」攻击。
      </p>
    </div>
  );
}
