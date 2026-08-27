"use client";

import { CheckCircle2, ShieldAlert, Target } from "lucide-react";

import type { Roadmap } from "@/lib/types";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { asList, clean } from "@/lib/utils";

export function SuccessRisk({ roadmap }: { roadmap?: Roadmap | null }) {
  const mvp = roadmap?.minimum_viable_paper;
  const sc = roadmap?.success_criteria;
  const risks = asList(roadmap?.risk_branches);

  return (
    <div className="grid gap-4 lg:grid-cols-3">
      {/* MVP */}
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="flex items-center gap-2 text-base">
            <CheckCircle2 className="h-4 w-4 text-emerald-600" /> 最小可发表版本（MVP）
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-3 text-sm">
          <div>
            <div className="mb-1 text-xs font-medium text-muted-foreground">必须完成</div>
            <ul className="space-y-1">
              {asList(mvp?.must_have).map((m) => (
                <li key={m} className="flex gap-1.5 text-muted-foreground">
                  <span className="text-emerald-600">·</span>
                  <span>{m}</span>
                </li>
              ))}
            </ul>
          </div>
          <div>
            <div className="mb-1 text-xs font-medium text-muted-foreground">可选扩展（不做也能发）</div>
            <ul className="space-y-1">
              {asList(mvp?.optional).map((m) => (
                <li key={m} className="flex gap-1.5 text-muted-foreground">
                  <span>·</span>
                  <span>{m}</span>
                </li>
              ))}
            </ul>
          </div>
        </CardContent>
      </Card>

      {/* Success Criteria */}
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="flex items-center gap-2 text-base">
            <Target className="h-4 w-4 text-primary" /> 成功 / 失败标准
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-3 text-sm">
          <div>
            <div className="mb-1 text-xs font-medium text-emerald-700 dark:text-emerald-400">成功（idea 成立）</div>
            <ul className="space-y-1">
              {asList(sc?.success).map((s) => (
                <li key={s} className="flex gap-1.5 text-muted-foreground"><span className="text-emerald-600">·</span><span>{s}</span></li>
              ))}
            </ul>
          </div>
          <div>
            <div className="mb-1 text-xs font-medium text-red-700 dark:text-red-400">失败条件</div>
            <ul className="space-y-1">
              {asList(sc?.failure).map((s) => (
                <li key={s} className="flex gap-1.5 text-muted-foreground"><span className="text-red-500">·</span><span>{s}</span></li>
              ))}
            </ul>
          </div>
          {clean(sc?.pivot) && (
            <div className="rounded-md bg-muted/60 p-2 text-xs text-muted-foreground">
              <span className="font-medium text-foreground">转向方案：</span>
              {clean(sc?.pivot)}
            </div>
          )}
        </CardContent>
      </Card>

      {/* Risk Branches */}
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="flex items-center gap-2 text-base">
            <ShieldAlert className="h-4 w-4 text-amber-500" /> 风险分支
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-2 text-sm">
          {risks.length ? (
            risks.map((r, i) => (
              <div key={i} className="rounded-md border p-2.5">
                <div className="font-medium">{clean(r.risk)}</div>
                <div className="mt-0.5 text-xs text-muted-foreground">→ {clean(r.branch)}</div>
              </div>
            ))
          ) : (
            <p className="text-muted-foreground">（无风险分支）</p>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
