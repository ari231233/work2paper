"use client";

import type { ResearchQuestion } from "@/lib/types";
import { Badge } from "@/components/ui/badge";
import { asList, clean } from "@/lib/utils";

/** Research Questions：RQ + 对应实验（RQ1→主实验、RQ2→消融…）。 */
export function ResearchQuestions({ questions }: { questions?: ResearchQuestion[] | null }) {
  const rqs = asList(questions);
  if (!rqs.length) {
    return <p className="text-sm text-muted-foreground">（未生成研究问题）</p>;
  }

  return (
    <ul className="space-y-3">
      {rqs.map((q) => (
        <li key={q.id ?? q.question} className="rounded-lg border p-3">
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant="secondary">{clean(q.id) || "RQ"}</Badge>
            <span className="text-sm font-medium">{clean(q.question)}</span>
          </div>
          {asList(q.target_experiments).length ? (
            <div className="mt-1.5 flex flex-wrap gap-1.5">
              {asList(q.target_experiments).map((t) => (
                <Badge key={t} variant="outline">→ {t}</Badge>
              ))}
            </div>
          ) : null}
        </li>
      ))}
    </ul>
  );
}
