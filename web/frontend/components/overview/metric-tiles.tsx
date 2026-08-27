"use client";

import { Info } from "lucide-react";

import type { Metric, MetricTone } from "@/lib/derive";
import { Badge } from "@/components/ui/badge";
import { Progress } from "@/components/ui/progress";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";

const TONE_BAR: Record<MetricTone, string> = {
  good: "bg-emerald-500",
  warn: "bg-amber-500",
  bad: "bg-red-500",
  neutral: "bg-slate-400",
};

const TONE_TEXT: Record<MetricTone, string> = {
  good: "text-emerald-700 dark:text-emerald-400",
  warn: "text-amber-700 dark:text-amber-400",
  bad: "text-red-700 dark:text-red-400",
  neutral: "text-slate-600 dark:text-slate-300",
};

export function MetricTile({ metric }: { metric: Metric }) {
  return (
    <div className="rounded-lg border bg-card p-3">
      <div className="flex items-center justify-between gap-1">
        <span className="text-xs font-medium text-muted-foreground">{metric.label}</span>
        <span className="flex items-center gap-1">
          {metric.derived && (
            <Badge variant="outline" className="px-1 py-0 text-[10px] text-muted-foreground">
              推导
            </Badge>
          )}
          <Tooltip>
            <TooltipTrigger asChild>
              <Info className="h-3.5 w-3.5 cursor-help text-muted-foreground/70" />
            </TooltipTrigger>
            <TooltipContent className="max-w-[280px]">{metric.detail}</TooltipContent>
          </Tooltip>
        </span>
      </div>
      <div className={cn("mt-1 text-2xl font-semibold leading-none", TONE_TEXT[metric.tone])}>
        {metric.display}
      </div>
      <div className="mt-1 truncate text-xs text-muted-foreground">{metric.level}</div>
      <Progress value={metric.value} className="mt-2 h-1.5" indicatorClassName={TONE_BAR[metric.tone]} />
    </div>
  );
}

export function MetricTiles({ metrics }: { metrics: Metric[] }) {
  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
      {metrics.map((m) => (
        <MetricTile key={m.key} metric={m} />
      ))}
    </div>
  );
}
