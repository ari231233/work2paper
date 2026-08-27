"use client";

import Link from "next/link";

import { useProject } from "@/hooks/use-project";
import { PaperStory } from "@/components/roadmap/paper-story";
import { ResearchQuestions } from "@/components/roadmap/research-questions";
import { ExperimentMatrix } from "@/components/roadmap/experiment-matrix";
import { KanbanTimeline } from "@/components/roadmap/kanban-timeline";
import { SuccessRisk } from "@/components/roadmap/success-risk";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { clean } from "@/lib/utils";

export default function RoadmapPage() {
  const { dossier, roadmap } = useProject();

  return (
    <div className="space-y-5 p-6">
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="text-lg font-semibold">论文路线图（Roadmap）</h2>
        <Badge variant="outline">{clean(roadmap?.paper_type) || "论文类型待定"}</Badge>
        {roadmap?.selected_idea && (
          <Badge variant="accent">
            选中 <Link href={`/ideas/${roadmap.selected_idea}`} className="underline">{roadmap.selected_idea}</Link>
          </Badge>
        )}
      </div>

      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-base">论文主线（Paper Story）</CardTitle>
        </CardHeader>
        <CardContent>
          <PaperStory story={roadmap?.core_story} />
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-base">研究问题（Research Questions）</CardTitle>
        </CardHeader>
        <CardContent>
          <ResearchQuestions questions={roadmap?.research_questions} />
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-base">实验矩阵（Experiment Matrix，可展开）</CardTitle>
        </CardHeader>
        <CardContent>
          <ExperimentMatrix experiments={roadmap?.experiment_matrix} />
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-base">时间线（Timeline / Kanban）</CardTitle>
        </CardHeader>
        <CardContent>
          <KanbanTimeline stages={roadmap?.stage_exits} />
        </CardContent>
      </Card>

      <SuccessRisk roadmap={roadmap} />
    </div>
  );
}
