"use client";

import { useMemo, useState } from "react";

import { useProject } from "@/hooks/use-project";
import { rankedIdeas, thesisFitScore } from "@/lib/derive";
import { IdeaCard } from "@/components/ideas/idea-card";
import { EmptyState } from "@/components/ui/empty-state";
import type { IdeaWithEval } from "@/lib/types";
import { clean } from "@/lib/utils";

type SortKey = "thesisFit" | "novelty" | "feasibility" | "evidence" | "workload";

const SORT_OPTIONS: { key: SortKey; label: string }[] = [
  { key: "thesisFit", label: "Thesis Fit" },
  { key: "novelty", label: "Novelty" },
  { key: "feasibility", label: "Feasibility" },
  { key: "evidence", label: "Evidence" },
  { key: "workload", label: "Workload" },
];

const FEAS_ORDER: Record<string, number> = { high: 3, medium: 2, low: 1 };
const EVID_ORDER: Record<string, number> = { strong: 3, medium: 2, weak: 1 };

function sortBy(pairs: IdeaWithEval[], key: SortKey): IdeaWithEval[] {
  const arr = [...pairs];
  const v = (n: unknown) => (typeof n === "number" && !Number.isNaN(n) ? n : 0);
  arr.sort((a, b) => {
    switch (key) {
      case "thesisFit":
        return thesisFitScore(b.evaluation) - thesisFitScore(a.evaluation);
      case "novelty":
        return v(b.evaluation?.novelty_score) - v(a.evaluation?.novelty_score);
      case "feasibility":
        return (FEAS_ORDER[b.evaluation?.data_feasibility ?? ""] ?? 0) - (FEAS_ORDER[a.evaluation?.data_feasibility ?? ""] ?? 0);
      case "evidence":
        return (EVID_ORDER[b.evaluation?.evidence_validation?.evidence ?? ""] ?? 0) - (EVID_ORDER[a.evaluation?.evidence_validation?.evidence ?? ""] ?? 0);
      case "workload":
        return v(a.evaluation?.workload_hours) - v(b.evaluation?.workload_hours);
      default:
        return 0;
    }
  });
  return arr;
}

export default function IdeasPage() {
  const { dossier } = useProject();
  const [sort, setSort] = useState<SortKey>("thesisFit");

  const ranked = useMemo(() => rankedIdeas(dossier?.ideas, dossier?.evaluations), [dossier]);
  const sorted = useMemo(() => sortBy(ranked, sort), [ranked, sort]);
  const selectedId = clean(dossier?.roadmap?.selected_idea);

  return (
    <div className="space-y-5 p-6">
      <div className="flex items-center justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold">Ideas（候选创新点池）</h2>
          <p className="text-sm text-muted-foreground">共 {ranked.length} 个候选，点击进入详情看贡献与风险。</p>
        </div>
        <div className="flex items-center gap-2">
          <span className="text-xs text-muted-foreground">排序</span>
          <select
            value={sort}
            onChange={(e) => setSort(e.target.value as SortKey)}
            className="h-8 rounded-md border bg-background px-2 text-xs"
          >
            {SORT_OPTIONS.map((o) => (
              <option key={o.key} value={o.key}>
                {o.label}
              </option>
            ))}
          </select>
        </div>
      </div>

      {sorted.length ? (
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
          {sorted.map((pair) => (
            <IdeaCard
              key={pair.idea.idea_id}
              pair={pair}
              recommended={String(pair.idea.idea_id) === selectedId}
            />
          ))}
        </div>
      ) : (
        <EmptyState title="暂无候选创新点" description="离线/未执行分析时没有 ideas。" />
      )}
    </div>
  );
}
