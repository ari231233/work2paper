"use client";

import Link from "next/link";
import { ArrowRight, CheckCircle2, ShieldAlert, Sparkles } from "lucide-react";

import { useProject } from "@/hooks/use-project";
import {
  computeMetrics,
  decisionFor,
  nextActions,
  riskItems,
  selectedPair,
  whyThis,
} from "@/lib/derive";
import { typeLabel } from "@/lib/format";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import { MetricTiles } from "./metric-tiles";
import { DecisionBadge } from "@/components/decision/decision-badge";
import { clean, clip } from "@/lib/utils";

/**
 * Overview 首屏精简（M25 v3.2）：只保留「当前判断 / 为什么 / 最大阻塞 / 下一步动作」，
 * 指标明细、完整推荐理由、全部风险分支折叠到 Accordion（Why/Risk 下移）。
 */
export function RecommendationCard() {
  const { dossier, roadmap } = useProject();
  const { idea, evaluation } = selectedPair(dossier);
  const metrics = computeMetrics(evaluation, roadmap);
  const risks = riskItems(roadmap, evaluation);
  const decision = decisionFor(dossier, idea?.idea_id);
  const reasons = whyThis(idea, evaluation);

  if (!idea) {
    return (
      <Card>
        <CardContent className="py-10 text-center text-sm text-muted-foreground">
          暂无候选创新点。
        </CardContent>
      </Card>
    );
  }

  const blocker = risks[0];
  const actions = nextActions(roadmap);

  return (
    <Card className="border-primary/30 shadow-md">
      <CardHeader className="pb-3">
        <div className="flex flex-wrap items-center gap-2">
          <span className="flex items-center gap-1.5 rounded-full bg-primary/10 px-3 py-1 text-xs font-semibold text-primary">
            <Sparkles className="h-3.5 w-3.5" /> 研究推荐
          </span>
          <DecisionBadge info={decision} />
          <Badge variant="outline">{typeLabel(evaluation)}</Badge>
          <Badge variant="outline">{clean(roadmap?.paper_type) || "论文类型待定"}</Badge>
        </div>
        <CardTitle className="pt-2 text-lg leading-snug">
          <Link href={`/ideas/${idea.idea_id}`} className="hover:underline">
            <code className="mr-1 text-primary">{idea.idea_id}</code>
            {clean(idea.claim)}
          </Link>
        </CardTitle>
        {decision && (
          <CardDescription className="pt-1">
            当前判断：{decision.summary}
            {decision.reason ? `（${decision.reason}）` : ""}
          </CardDescription>
        )}
      </CardHeader>

      <CardContent className="space-y-4">
        {/* 为什么（精简为一句） */}
        <section>
          <h3 className="mb-1.5 text-sm font-semibold">为什么（Why）</h3>
          <p className="text-sm leading-relaxed text-muted-foreground">{reasons.join("；")}</p>
        </section>

        {/* 最大阻塞 */}
        <section>
          <h3 className="mb-1.5 flex items-center gap-1.5 text-sm font-semibold">
            <ShieldAlert className="h-4 w-4 text-danger" /> 最大阻塞
          </h3>
          {blocker ? (
            <div className="rounded-md border-l-4 border-danger bg-muted/60 p-2.5">
              <div className="text-sm font-medium text-foreground">{blocker.risk}</div>
              <div className="mt-0.5 text-xs text-muted-foreground">→ {blocker.branch}</div>
            </div>
          ) : (
            <p className="text-sm text-muted-foreground">（无明确阻塞）</p>
          )}
          {risks.length > 1 && (
            <p className="mt-1 text-xs text-muted-foreground">
              另有 {risks.length - 1} 条风险，见下方「全部风险分支」。
            </p>
          )}
        </section>

        {/* 下一步动作 */}
        <section>
          <h3 className="mb-1.5 text-sm font-semibold">下一步动作（Next Actions）</h3>
          <ol className="space-y-1.5">
            {actions.map((a, i) => (
              <li key={i} className="flex items-start gap-2 text-sm">
                <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-success" />
                <span className="text-muted-foreground">{clip(a, 140)}</span>
              </li>
            ))}
          </ol>
        </section>

        {/* 折叠区：指标明细 / 完整推荐理由 / 全部风险分支 */}
        <Accordion type="multiple" className="rounded-lg border px-4">
          <AccordionItem value="metrics">
            <AccordionTrigger className="text-sm">
              指标明细（论文契合度 / 证据强度 / 可行性 / 创新程度 / 评审风险）
            </AccordionTrigger>
            <AccordionContent className="pb-4">
              <MetricTiles
                metrics={[metrics.thesisFit, metrics.evidence, metrics.feasibility, metrics.novelty, metrics.risk]}
              />
            </AccordionContent>
          </AccordionItem>
          <AccordionItem value="why">
            <AccordionTrigger className="text-sm">完整推荐理由</AccordionTrigger>
            <AccordionContent className="pb-4">
              <ul className="space-y-1.5 text-sm text-muted-foreground">
                {reasons.map((s, i) => (
                  <li key={i} className="flex gap-2">
                    <span className="mt-1 h-1.5 w-1.5 shrink-0 rounded-full bg-primary" />
                    <span>{s}</span>
                  </li>
                ))}
              </ul>
            </AccordionContent>
          </AccordionItem>
          <AccordionItem value="risk">
            <AccordionTrigger className="text-sm">全部风险分支（{risks.length}）</AccordionTrigger>
            <AccordionContent className="pb-4">
              {risks.length ? (
                <ul className="space-y-2 text-sm">
                  {risks.map((r, i) => (
                    <li key={i} className="rounded-md bg-muted/60 p-2.5">
                      <div className="font-medium text-foreground">{r.risk}</div>
                      <div className="mt-0.5 text-xs text-muted-foreground">→ {r.branch}</div>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="text-sm text-muted-foreground">（无明确风险分支）</p>
              )}
            </AccordionContent>
          </AccordionItem>
        </Accordion>

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
