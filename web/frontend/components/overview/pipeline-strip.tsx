"use client";

import { Fragment } from "react";
import { Check, ChevronRight } from "lucide-react";

import { useProject } from "@/hooks/use-project";
import { pipelineSteps } from "@/lib/derive";
import { cn } from "@/lib/utils";

/** 横向流程进度条（Overview 顶部）：项目理解 → 研究问题 → 文献检索 → 创新点 → 可行性评估 → 路线图 → 经验沉淀。 */
export function PipelineStrip() {
  const { dossier } = useProject();
  const steps = pipelineSteps(dossier);

  return (
    <div className="flex items-center gap-1 overflow-x-auto rounded-xl border bg-card p-2">
      {steps.map((s, i) => (
        <Fragment key={s.key}>
          <div
            className={cn(
              "flex shrink-0 items-center gap-1.5 rounded-full px-2.5 py-1 text-xs",
              s.status === "done" && "bg-emerald-100 text-emerald-800 dark:bg-emerald-900/40 dark:text-emerald-200",
              s.status === "current" && "bg-primary/10 text-primary",
              s.status === "pending" && "text-muted-foreground/60"
            )}
          >
            {s.status === "done" ? (
              <Check className="h-3 w-3" />
            ) : (
              <span
                className={cn(
                  "h-1.5 w-1.5 rounded-full",
                  s.status === "current" ? "animate-pulse bg-primary" : "bg-muted-foreground/40"
                )}
              />
            )}
            {s.label}
          </div>
          {i < steps.length - 1 && (
            <ChevronRight className="h-3 w-3 shrink-0 text-muted-foreground/50" />
          )}
        </Fragment>
      ))}
    </div>
  );
}
