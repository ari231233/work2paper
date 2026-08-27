"use client";

import Link from "next/link";
import { ArrowRight, CheckCircle2, ShieldAlert, Sparkles, Star } from "lucide-react";

import { useProject } from "@/hooks/use-project";
import {
  computeMetrics,
  nextActions,
  riskItems,
  selectedPair,
  whyThis,
} from "@/lib/derive";
import { starRating, typeLabel, VERDICT_LABELS, VERDICT_TONES } from "@/lib/format";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Separator } from "@/components/ui/separator";
import { MetricTiles } from "./metric-tiles";
import { clean, clip } from "@/lib/utils";

const TONE_BADGE: Record<string, "default" | "success" | "warning" | "destructive"> = {
  good: "success",
  warn: "warning",
  bad: "destructive",
};

export function RecommendationCard() {
  const { dossier, roadmap } = useProject();
  const { idea, evaluation } = selectedPair(dossier);
  const metrics = computeMetrics(evaluation, roadmap);
  const risks = riskItems(roadmap, evaluation);

  if (!idea) {
    return (
      <Card>
        <CardContent className="py-10 text-center text-sm text-muted-foreground">
          暂无候选创新点。
        </CardContent>
      </Card>
    );
  }

  return (
    <Card className="border-primary/30 shadow-md">
      <CardHeader className="pb-3">
        <div className="flex flex-wrap items-center gap-2">
          <span className="flex items-center gap-1.5 rounded-full bg-primary/10 px-3 py-1 text-xs font-semibold text-primary">
            <Sparkles className="h-3.5 w-3.5" /> Research Recommendation
          </span>
          {evaluation?.verdict && (
            <Badge variant={TONE_BADGE[VERDICT_TONES[evaluation.verdict]] ?? "default"}>
              {VERDICT_LABELS[evaluation.verdict]}
            </Badge>
          )}
          <Badge variant="outline">{typeLabel(evaluation)}</Badge>
          <Badge variant="outline">{clean(roadmap?.paper_type) || "论文类型待定"}</Badge>
        </div>
        <CardTitle className="pt-2 text-lg leading-snug">
          <Link href={`/ideas/${idea.idea_id}`} className="hover:underline">
            <code className="mr-1 text-primary">{idea.idea_id}</code>
            {clean(idea.claim)}
          </Link>
        </CardTitle>
        <CardDescription className="flex items-center gap-1.5 pt-1">
          <Star className="h-3.5 w-3.5 text-amber-500" />
          {starRating(evaluation?.verdict, evaluation?.novelty_score)}
        </CardDescription>
      </CardHeader>

      <CardContent className="space-y-5">
        {/* 指标组：Thesis Fit / Evidence / Feasibility / Novelty / Risk */}
        <MetricTiles metrics={[metrics.thesisFit, metrics.evidence, metrics.feasibility, metrics.novelty, metrics.risk]} />

        <Separator />

        <div className="grid gap-5 lg:grid-cols-2">
          {/* Why */}
          <section>
            <h3 className="mb-2 text-sm font-semibold">为什么推荐（Why）</h3>
            <ul className="space-y-1.5 text-sm text-muted-foreground">
              {whyThis(idea, evaluation).map((s, i) => (
                <li key={i} className="flex gap-2">
                  <span className="mt-1 h-1.5 w-1.5 shrink-0 rounded-full bg-primary" />
                  <span>{s}</span>
                </li>
              ))}
            </ul>
          </section>

          {/* Risk */}
          <section>
            <h3 className="mb-2 flex items-center gap-1.5 text-sm font-semibold">
              <ShieldAlert className="h-4 w-4 text-amber-500" /> 主要风险（Main Risk）
            </h3>
            {risks.length ? (
              <ul className="space-y-2 text-sm">
                {risks.slice(0, 2).map((r, i) => (
                  <li key={i} className="rounded-md bg-muted/60 p-2.5">
                    <div className="font-medium text-foreground">{r.risk}</div>
                    <div className="mt-0.5 text-xs text-muted-foreground">→ {r.branch}</div>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="text-sm text-muted-foreground">（无明确风险分支）</p>
            )}
          </section>
        </div>

        <Separator />

        {/* Next 3 Actions */}
        <section>
          <h3 className="mb-2 text-sm font-semibold">接下来 3 步（Next Actions）</h3>
          <ol className="space-y-1.5">
            {nextActions(roadmap).map((a, i) => (
              <li key={i} className="flex items-start gap-2 text-sm">
                <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-emerald-600" />
                <span className="text-muted-foreground">{clip(a, 140)}</span>
              </li>
            ))}
          </ol>
        </section>

        <div className="flex justify-end">
          <Button asChild variant="secondary" size="sm">
            <Link href={`/ideas/${idea.idea_id}`}>
              查看 {idea.idea_id} 详情与证据 <ArrowRight className="h-4 w-4" />
            </Link>
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}
