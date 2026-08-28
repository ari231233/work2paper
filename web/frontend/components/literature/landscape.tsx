"use client";

import { useMemo } from "react";

import { useProject } from "@/hooks/use-project";
import { buildFocus, flatPapers, relevanceOf } from "@/lib/derive";
import { PaperCard } from "./paper-card";
import { Badge } from "@/components/ui/badge";
import { EmptyState } from "@/components/ui/empty-state";
import { asList, clean, clip } from "@/lib/utils";

/** Research Landscape：按 query 分组的 paper cards（带 Relevant / Partial / Peripheral 标签）。 */
export function Landscape() {
  const { dossier, literature } = useProject();
  const focus = useMemo(() => buildFocus(dossier), [dossier]);
  const flat = flatPapers(literature);

  if (!flat.length) {
    return <EmptyState title="暂无文献" description="离线/无结果，或尚未执行文献检索。" />;
  }

  return (
    <div className="space-y-6">
      {literature.map((entry, idx) => {
        const papers = asList(entry.papers).filter((p) => clean(p.title));
        const gaps = asList(entry.contradiction_graph?.gaps).filter((g) => clean(g.gap_id));
        const target = Number(entry.target_count ?? 8);
        const sufficient = entry.coverage_status
          ? entry.coverage_status === "sufficient"
          : papers.length >= 7;
        const high = Number(entry.high_count ?? papers.filter((p) => p.relevance_level === "high").length);
        const partial = Number(entry.partial_count ?? papers.filter((p) => p.relevance_level === "partial").length);
        if (!papers.length) return null;
        return (
          <div key={idx}>
            <div className="mb-2 flex flex-wrap items-center gap-2">
              <Badge variant="secondary">Query {idx + 1}</Badge>
              <span className="text-xs text-muted-foreground">{clip(entry.query, 140)}</span>
              <span className="text-xs text-muted-foreground/70">
                · 共 {papers.length} 篇 / 目标 {target} 篇 · {gaps.length} 个 gap
              </span>
              <Badge variant={sufficient ? "success" : "warning"}>
                {sufficient ? "覆盖基本充足" : "证据覆盖不足"}
              </Badge>
              {(high > 0 || partial > 0) && (
                <span className="text-xs text-muted-foreground">
                  高度相关 {high} · 部分相关 {partial}
                </span>
              )}
            </div>
            <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
              {papers.map((paper) => {
                const rel = relevanceOf({ paper, entryIndex: idx, entry }, focus);
                return <PaperCard key={paper.title} paper={paper} relevance={rel} />;
              })}
            </div>
          </div>
        );
      })}
    </div>
  );
}
