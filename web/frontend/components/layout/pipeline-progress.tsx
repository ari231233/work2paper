"use client";

import { Check, Circle } from "lucide-react";

import { pipelineSteps, type PipelineStep } from "@/lib/derive";
import { useProject } from "@/hooks/use-project";
import { cn } from "@/lib/utils";

function StepDot({ step }: { step: PipelineStep }) {
  if (step.status === "done")
    return <Check className="h-3.5 w-3.5 text-emerald-600" />;
  return <Circle className={cn("h-3.5 w-3.5", step.status === "current" ? "text-primary" : "text-muted-foreground/40")} />;
}

export function PipelineProgress() {
  const { dossier } = useProject();
  const steps = pipelineSteps(dossier);

  return (
    <div className="space-y-2">
      <div className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
        流程进度
      </div>
      <ol className="space-y-1.5">
        {steps.map((s) => (
          <li key={s.key} className="flex items-center gap-2 text-sm">
            <StepDot step={s} />
            <span
              className={cn(
                "leading-tight",
                s.status === "pending" ? "text-muted-foreground/60" : "text-foreground",
                s.status === "current" && "font-medium text-primary"
              )}
            >
              {s.label}
            </span>
            {s.status === "current" && (
              <span className="ml-auto h-1.5 w-1.5 animate-pulse rounded-full bg-primary" />
            )}
          </li>
        ))}
      </ol>
    </div>
  );
}
