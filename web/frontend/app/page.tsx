"use client";

import { ScrollText, Target } from "lucide-react";

import { useProject } from "@/hooks/use-project";
import { RecommendationCard } from "@/components/overview/recommendation-card";
import { PipelineStrip } from "@/components/overview/pipeline-strip";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { evidenceCoverage } from "@/lib/derive";
import { asList, clean } from "@/lib/utils";

export default function OverviewPage() {
  const { dossier, roadmap, gaps } = useProject();

  const rqs = asList(roadmap?.research_questions).filter((q) => clean(q.question));
  const directions = [...new Set(gaps.map((g) => clean(g.claim_point)).filter(Boolean))].slice(0, 5);

  return (
    <div className="space-y-5 p-6">
      <PipelineStrip />

      <RecommendationCard />

      <div className="grid gap-5 lg:grid-cols-2">
        {/* Research Questions */}
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="flex items-center gap-2 text-base">
              <Target className="h-4 w-4 text-primary" /> 研究问题（Research Questions）
            </CardTitle>
            <CardDescription>路线图要回答的核心问题</CardDescription>
          </CardHeader>
          <CardContent>
            {rqs.length ? (
              <ul className="space-y-2 text-sm">
                {rqs.slice(0, 4).map((q) => (
                  <li key={q.id ?? q.question} className="flex gap-2">
                    <Badge variant="secondary" className="h-5 shrink-0">
                      {clean(q.id) || "RQ"}
                    </Badge>
                    <span className="text-muted-foreground">{clean(q.question)}</span>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="text-sm text-muted-foreground">（未生成研究问题）</p>
            )}
          </CardContent>
        </Card>

        {/* Literature Landscape summary */}
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="flex items-center gap-2 text-base">
              <ScrollText className="h-4 w-4 text-primary" /> 文献图景（Literature Landscape）
            </CardTitle>
            <CardDescription>证据覆盖度与主要研究方向</CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            <p className="text-sm text-muted-foreground">{evidenceCoverage(dossier?.literature)}</p>
            {directions.length ? (
              <div className="flex flex-wrap gap-1.5">
                {directions.map((d) => (
                  <Badge key={d} variant="outline">
                    {d}
                  </Badge>
                ))}
              </div>
            ) : null}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
