"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useParams, useSearchParams } from "next/navigation";
import { ArrowLeft } from "lucide-react";

import { useProject } from "@/hooks/use-project";
import { computeMetrics, matrixRows } from "@/lib/derive";
import { join, starRating, typeLabel, VERDICT_LABELS } from "@/lib/format";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { MetricTiles } from "@/components/overview/metric-tiles";
import { ContributionRadar } from "./contribution-radar";
import { InnovationBoundary } from "./innovation-boundary";
import { ReviewerRisk } from "./reviewer-risk";
import { EvidenceTab } from "./evidence-tab";
import { ExperimentMatrix } from "@/components/roadmap/experiment-matrix";
import { EmptyState } from "@/components/ui/empty-state";
import { asList, clean } from "@/lib/utils";

const VERDICT_BADGE: Record<string, "success" | "warning" | "destructive"> = {
  proceed: "success",
  rework: "warning",
  drop: "destructive",
};

export function IdeaDetailClient() {
  const params = useParams<{ id: string }>();
  const search = useSearchParams();
  const id = decodeURIComponent(params.id);
  const searchTab = search.get("tab") || "overview";
  const [tab, setTab] = useState(searchTab);

  useEffect(() => {
    setTab(searchTab);
  }, [searchTab]);

  const { dossier, roadmap } = useProject();
  const pair = asList(dossier?.ideas)
    .map((idea) => ({ idea, evaluation: dossier?.evaluations?.find((e) => e.idea_ref === idea.idea_id) }))
    .find((p) => String(p.idea.idea_id) === String(id));

  if (!pair) {
    return <EmptyState title="未找到该 idea" description={`ideas 里没有 ${id}。`} />;
  }

  const { idea, evaluation } = pair;
  const metrics = computeMetrics(evaluation, roadmap);
  const isSelected = String(roadmap?.selected_idea) === String(id);
  const rows = matrixRows(evaluation);

  return (
    <div className="space-y-5 p-6">
      <div className="flex items-center gap-2">
        <Button asChild variant="ghost" size="sm">
          <Link href="/ideas">
            <ArrowLeft className="h-4 w-4" /> Ideas
          </Link>
        </Button>
      </div>

      {/* 头部 */}
      <Card>
        <CardContent className="space-y-4 p-5">
          <div className="flex flex-wrap items-center gap-2">
            <code className="text-lg font-semibold text-primary">{idea.idea_id}</code>
            {isSelected && (
              <Badge variant="accent">⭐ Recommended</Badge>
            )}
            {evaluation?.verdict && (
              <Badge variant={VERDICT_BADGE[evaluation.verdict]}>{VERDICT_LABELS[evaluation.verdict]}</Badge>
            )}
            <Badge variant="outline">{typeLabel(evaluation)}</Badge>
            <span className="text-xs text-muted-foreground">
              {starRating(evaluation?.verdict, evaluation?.novelty_score)}
            </span>
          </div>

          <h1 className="text-xl font-semibold leading-snug">{clean(idea.claim)}</h1>
          <p className="text-sm text-muted-foreground">{clean(idea.novelty_hypothesis)}</p>

          <MetricTiles
            metrics={[metrics.thesisFit, metrics.evidence, metrics.feasibility, metrics.novelty, metrics.risk]}
          />
        </CardContent>
      </Card>

      <Tabs value={tab} onValueChange={setTab} className="w-full">
        <TabsList className="grid w-full grid-cols-5">
          <TabsTrigger value="overview">Overview</TabsTrigger>
          <TabsTrigger value="contribution">Contribution</TabsTrigger>
          <TabsTrigger value="evidence">Evidence</TabsTrigger>
          <TabsTrigger value="risk">Reviewer Risk</TabsTrigger>
          <TabsTrigger value="experiments">Experiments</TabsTrigger>
        </TabsList>

        <TabsContent value="overview">
          <div className="grid gap-4 lg:grid-cols-2">
            <Card>
              <CardContent className="space-y-3 p-5">
                <h3 className="text-sm font-semibold">来源与引用</h3>
                <div className="text-sm">
                  <span className="text-muted-foreground">关联问题：</span>
                  <code>{clean(idea.problem_ref) || "—"}</code>
                </div>
                <div>
                  <div className="mb-1 text-xs text-muted-foreground">文献引用（literature_refs）</div>
                  <ul className="space-y-1 text-sm">
                    {asList(idea.literature_refs).map((t) => (
                      <li key={t} className="text-muted-foreground">· {t}</li>
                    ))}
                    {!asList(idea.literature_refs).length && <li className="text-muted-foreground">（无）</li>}
                  </ul>
                </div>
                <div className="text-sm">
                  <span className="text-muted-foreground">来源 gap：</span>
                  <code>{join(idea.gap_refs, "、", 20) || "—"}</code>
                </div>
                <div className="text-sm">
                  <span className="text-muted-foreground">来源假设：</span>
                  <code>{join(idea.hypothesis_refs, "、", 20) || "—"}</code>
                </div>
              </CardContent>
            </Card>

            <Card>
              <CardContent className="space-y-3 p-5">
                <h3 className="text-sm font-semibold">评估要点</h3>
                <div className="grid grid-cols-2 gap-3 text-sm">
                  <div>
                    <div className="text-xs text-muted-foreground">工作量</div>
                    <div>≈ {evaluation?.workload_hours ?? "—"} h</div>
                  </div>
                  <div>
                    <div className="text-xs text-muted-foreground">数据可得性</div>
                    <div>{evaluation?.data_feasibility ?? "—"}</div>
                  </div>
                  <div className="col-span-2">
                    <div className="text-xs text-muted-foreground">建议档位</div>
                    <div className="text-muted-foreground">{clean(evaluation?.venue_guess) || "—"}</div>
                  </div>
                </div>
                {evaluation?.rework_reason && (
                  <div className="rounded-md bg-amber-50 p-2.5 text-xs text-amber-800 dark:bg-amber-900/30 dark:text-amber-200">
                    回炉原因：{clean(evaluation.rework_reason)}
                  </div>
                )}
                {evaluation?.contribution?.degraded && (
                  <div className="text-xs text-amber-600">（贡献分析为确定性降级产物，低置信）</div>
                )}
              </CardContent>
            </Card>
          </div>
        </TabsContent>

        <TabsContent value="contribution">
          <div className="grid gap-4 lg:grid-cols-[1fr_320px]">
            <Card>
              <CardContent className="space-y-4 p-5">
                <div>
                  <h3 className="text-sm font-semibold">创新类型</h3>
                  <p className="mt-1 text-sm text-muted-foreground">
                    {evaluation?.contribution?.type ? (
                      <>
                        <Badge variant="secondary">{evaluation.contribution.type}</Badge>{" "}
                        {clean(evaluation.contribution.type_label)}
                      </>
                    ) : (
                      "（未分类）"
                    )}
                  </p>
                  {evaluation?.contribution?.reason && (
                    <p className="mt-1 text-xs text-muted-foreground">{clean(evaluation.contribution.reason)}</p>
                  )}
                </div>

                <ContributionRadar evaluation={evaluation} />

                <div>
                  <h3 className="mb-2 text-sm font-semibold">贡献矩阵</h3>
                  <div className="overflow-hidden rounded-lg border">
                    <table className="w-full text-sm">
                      <thead className="bg-muted/60 text-left text-xs text-muted-foreground">
                        <tr>
                          <th className="px-3 py-2">维度</th>
                          <th className="px-3 py-2">强度</th>
                          <th className="px-3 py-2">原因</th>
                        </tr>
                      </thead>
                      <tbody>
                        {rows.map((r) => (
                          <tr key={r.key} className="border-t">
                            <td className="px-3 py-2 font-medium">{r.label}</td>
                            <td className="px-3 py-2"><Badge variant="outline">{r.strength}</Badge></td>
                            <td className="px-3 py-2 text-xs text-muted-foreground">{r.reason || "—"}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              </CardContent>
            </Card>

            <div className="space-y-4">
              <InnovationBoundary evaluation={evaluation} />
            </div>
          </div>
        </TabsContent>

        <TabsContent value="evidence">
          <Card>
            <CardContent className="p-5">
              <EvidenceTab evaluation={evaluation} />
            </CardContent>
          </Card>
        </TabsContent>

        <TabsContent value="risk">
          <Card>
            <CardContent className="p-5">
              <ReviewerRisk evaluation={evaluation} roadmap={roadmap} />
            </CardContent>
          </Card>
        </TabsContent>

        <TabsContent value="experiments">
          <div className="space-y-3">
            {isSelected ? (
              <p className="text-xs text-muted-foreground">实验矩阵来自 Roadmap（针对选中 idea {id}）。</p>
            ) : (
              <p className="text-xs text-amber-600">
                提示：路线图当前针对 {roadmap?.selected_idea ?? "—"}，实验矩阵是其计划，仅供参考。
              </p>
            )}
            <ExperimentMatrix experiments={roadmap?.experiment_matrix} />
          </div>
        </TabsContent>
      </Tabs>
    </div>
  );
}
