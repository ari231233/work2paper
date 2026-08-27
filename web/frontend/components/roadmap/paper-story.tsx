"use client";

import type { CoreStory } from "@/lib/types";
import { clean } from "@/lib/utils";

const FIELDS: [keyof CoreStory, string][] = [
  ["status_quo", "现状（Current Work）"],
  ["problem", "问题（Research Question）"],
  ["method", "方法（Method）"],
  ["contribution", "贡献（Contribution）"],
];

/** Paper Story：把 idea 压缩成「现状 / 问题 / 方法 / 贡献」四段。 */
export function PaperStory({ story }: { story?: CoreStory | null }) {
  const items = FIELDS.filter(([k]) => clean(story?.[k]));

  return (
    <div className="space-y-3">
      {items.map(([key, label], i) => (
        <div key={key} className="flex gap-3">
          <div className="flex flex-col items-center">
            <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-primary/10 text-xs font-semibold text-primary">
              {i + 1}
            </span>
            {i < items.length - 1 && <span className="w-px flex-1 bg-border" />}
          </div>
          <div className="pb-1">
            <div className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">{label}</div>
            <p className="mt-0.5 text-sm">{clean(story?.[key])}</p>
          </div>
        </div>
      ))}
      {!items.length && <p className="text-sm text-muted-foreground">（无论文主线）</p>}
    </div>
  );
}
