"use client";

import type { Evaluation } from "@/lib/types";
import { evidenceLabel, num, CHECK_STATUS_LABELS } from "@/lib/format";
import { Badge } from "@/components/ui/badge";
import { Separator } from "@/components/ui/separator";
import { asList, clean, clip } from "@/lib/utils";

const CHECK_LABELS: Record<string, string> = {
  similar_work: "文献对拍",
  theory_basis: "理论支撑",
  experiment_support: "实验设计支持",
  claim_strength: "claim 强度校准",
};

const STATUS_BADGE: Record<string, "success" | "warning" | "destructive"> = {
  ok: "success",
  concern: "warning",
  missing: "destructive",
};

function CalibrationBlock({ evaluation }: { evaluation: Evaluation }) {
  const cal = evaluation.calibration;
  if (!cal || !Object.keys(cal).length) return null;
  return (
    <section>
      <h4 className="mb-2 text-sm font-semibold">评分校准（问题 → 答案 → 规则 → 得分）</h4>
      <div className="space-y-3">
        {Object.entries(cal).map(([key, dim]) => (
          <div key={key} className="rounded-lg border p-3">
            <div className="text-sm font-medium">
              {clean(dim.label)}（权重 {dim.weight}）· 得分 {num(dim.score)}
            </div>
            <div className="mt-0.5 text-xs text-muted-foreground">{clean(dim.derivation)}</div>
            <ul className="mt-2 space-y-1.5">
              {asList(dim.questions).map((q) => (
                <li key={q.id} className="text-xs">
                  <div>
                    <Badge variant={q.answer === "yes" ? "success" : "outline"} className="mr-1">
                      {q.answer}
                    </Badge>
                    <span className="text-muted-foreground">{clean(q.text)}</span>
                    <span className="text-muted-foreground/70"> — 规则：{clean(q.rule)}</span>
                  </div>
                  {clean(q.evidence) && (
                    <div className="mt-0.5 pl-4 text-muted-foreground/80">证据：{clip(q.evidence, 140)}</div>
                  )}
                </li>
              ))}
            </ul>
          </div>
        ))}
      </div>
    </section>
  );
}

export function EvidenceTab({ evaluation }: { evaluation?: Evaluation | null }) {
  if (!evaluation) {
    return <p className="text-sm text-muted-foreground">该 idea 尚未评估。</p>;
  }
  const evv = evaluation.evidence_validation;
  const dims = evaluation.novelty_dimensions;

  return (
    <div className="space-y-5">
      {/* M12 证据强度 */}
      <section>
        <h4 className="mb-2 text-sm font-semibold">证据强度（Evidence）</h4>
        <div className="rounded-lg border p-3">
          <div className="flex items-center gap-2">
            <Badge variant={evv?.evidence === "strong" ? "success" : evv?.evidence === "medium" ? "warning" : "destructive"}>
              {evidenceLabel(evv?.evidence as never)}
            </Badge>
            <span className="text-sm">{clean(evv?.reason)}</span>
          </div>
          {evv?.degraded && <div className="mt-2 text-xs text-amber-600">（确定性降级产物，低置信）</div>}
          {evv?.checks && (
            <ul className="mt-2 grid gap-1.5 sm:grid-cols-2">
              {Object.entries(evv.checks).map(([k, c]) => (
                <li key={k} className="flex items-start gap-1.5 text-xs">
                  <Badge variant={STATUS_BADGE[c.status ?? ""] ?? "outline"} className="shrink-0">
                    {CHECK_STATUS_LABELS[c.status ?? ""] ?? c.status}
                  </Badge>
                  <span className="text-muted-foreground">
                    <span className="font-medium text-foreground">{CHECK_LABELS[k] ?? k}：</span>
                    {clean(c.note)}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </div>
      </section>

      <Separator />

      {/* M11 多维 novelty */}
      {dims && Object.keys(dims).length ? (
        <section>
          <h4 className="mb-2 text-sm font-semibold">Novelty 分维度（各 0~5，加权合成 {num(evaluation.novelty_score)}）</h4>
          <div className="grid gap-2 sm:grid-cols-2">
            {Object.entries(dims).map(([k, d]) => (
              <div key={k} className="rounded-lg border p-2.5 text-xs">
                <div className="flex items-center justify-between">
                  <span className="font-medium">{k}</span>
                  <span className="text-sm font-semibold">{num(d.score)}</span>
                </div>
                <div className="mt-0.5 text-muted-foreground">{clean(d.reason)}</div>
              </div>
            ))}
          </div>
        </section>
      ) : null}

      {/* M20 校准链路 */}
      <CalibrationBlock evaluation={evaluation} />

      <Separator />

      {/* 证据链（provenance） */}
      <section>
        <h4 className="mb-2 text-sm font-semibold">证据链（provenance）</h4>
        <ul className="space-y-1.5">
          {asList(evaluation.evidence).map((e, i) => (
            <li key={i} className="rounded-md bg-muted/50 px-2.5 py-1.5 text-xs">
              <code className="text-primary/80">{clean(e.source)}</code>
              <div className="mt-0.5 text-muted-foreground">{clean(e.note)}</div>
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}
