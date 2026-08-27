"use client";

import { useState } from "react";
import { Check } from "lucide-react";

import type { StageExit } from "@/lib/types";
import { Badge } from "@/components/ui/badge";
import { EmptyState } from "@/components/ui/empty-state";
import { asList, clean, cn } from "@/lib/utils";

/** Timeline = Kanban：阶段列 + 可勾选任务卡 + 出口（交付物）。 */
export function KanbanTimeline({ stages }: { stages?: StageExit[] | null }) {
  const [done, setDone] = useState<Set<string>>(new Set());
  const list = asList(stages);

  if (!list.length) {
    return <EmptyState title="暂无时间线" description="路线图尚未生成阶段出口时间线。" />;
  }

  function toggle(key: string) {
    setDone((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  return (
    <div className="grid auto-cols-[260px] grid-flow-col gap-4 overflow-x-auto pb-2">
      {list.map((s, si) => {
        const tasks = asList(s.tasks);
        const doneCount = tasks.filter((_, ti) => done.has(`${si}-${ti}`)).length;
        return (
          <div key={si} className="flex flex-col rounded-lg border bg-card shadow-sm">
            <div className="flex items-center justify-between border-b px-3 py-2">
              <span className="text-sm font-semibold">{clean(s.stage)}</span>
              <Badge variant="secondary">
                {doneCount}/{tasks.length}
              </Badge>
            </div>
            <ul className="flex-1 space-y-2 p-2">
              {tasks.map((t, ti) => {
                const key = `${si}-${ti}`;
                const checked = done.has(key);
                return (
                  <li key={key}>
                    <button
                      onClick={() => toggle(key)}
                      className="flex w-full items-start gap-2 rounded-md bg-muted/50 p-2 text-left text-xs transition-colors hover:bg-muted"
                    >
                      <span
                        className={cn(
                          "mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded border",
                          checked ? "border-emerald-500 bg-emerald-500 text-white" : "border-border"
                        )}
                      >
                        {checked && <Check className="h-3 w-3" />}
                      </span>
                      <span className={cn(checked && "text-muted-foreground line-through")}>{t}</span>
                    </button>
                  </li>
                );
              })}
              {!tasks.length && <li className="px-2 text-xs text-muted-foreground">（无任务）</li>}
            </ul>
            <div className="border-t px-3 py-2 text-xs text-muted-foreground">
              出口：{clean(s.exit_criteria) || "—"}
            </div>
          </div>
        );
      })}
    </div>
  );
}
