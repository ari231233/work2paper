"use client";

import { Swords } from "lucide-react";

import type { Evaluation, Roadmap } from "@/lib/types";
import { riskItems } from "@/lib/derive";
import { ATTACK_KEYS, ATTACK_LABELS } from "@/lib/format";
import { Badge } from "@/components/ui/badge";
import { clean } from "@/lib/utils";

export function ReviewerRisk({ evaluation, roadmap }: { evaluation?: Evaluation | null; roadmap?: Roadmap | null }) {
  const attacks = evaluation?.contribution?.attacks;
  const risks = riskItems(roadmap, evaluation);

  return (
    <div className="space-y-4">
      {/* 攻击测试（M21 attack test → reviewer risk 的源头） */}
      {attacks ? (
        <section className="space-y-3">
          <h4 className="flex items-center gap-1.5 text-sm font-semibold">
            <Swords className="h-4 w-4 text-amber-500" /> Reviewer 攻击测试
          </h4>
          {ATTACK_KEYS.map((key) => {
            const a = attacks[key];
            if (!clean(a?.attack)) return null;
            return (
              <div key={key} className="rounded-lg border p-3">
                <div className="flex items-center gap-2">
                  <Badge variant="warning">{ATTACK_LABELS[key]}</Badge>
                </div>
                <div className="mt-1.5 text-sm font-medium">{clean(a!.attack)}</div>
                <div className="mt-1 text-xs text-muted-foreground">
                  <span className="font-medium text-foreground">提前回答：</span>
                  {clean(a!.answer) || "（未预回答）"}
                </div>
              </div>
            );
          })}
        </section>
      ) : null}

      {/* 风险分支（M22：具体风险 → 具体转向） */}
      <section className="space-y-3">
        <h4 className="text-sm font-semibold">风险分支与转向预案</h4>
        {risks.length ? (
          risks.map((r, i) => (
            <div key={i} className="rounded-lg border p-3">
              <div className="text-sm font-medium">{r.risk}</div>
              <div className="mt-1 text-xs text-muted-foreground">→ {r.branch}</div>
            </div>
          ))
        ) : (
          <p className="text-sm text-muted-foreground">（无风险分支）</p>
        )}
      </section>
    </div>
  );
}
