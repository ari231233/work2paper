"use client";

import { useState } from "react";
import { RefreshCw } from "lucide-react";

import { useProject } from "@/hooks/use-project";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { shortDate } from "@/lib/format";
import { clean, projectName, projectSummary } from "@/lib/utils";
import type { ProjectSummary } from "@/lib/types";

const STATE_LABELS: Record<string, string> = {
  UNDERSTAND: "项目理解",
  ABSTRACT: "问题抽象",
  IDEATE: "检索与创新",
  EVALUATE: "评估",
  PLAN: "路线",
  REFLECT: "沉淀",
  DONE: "完成",
};

function shortRunId(id: string): string {
  return id.length > 12 ? `${id.slice(0, 12)}…` : id;
}

function optionLabel(p: ProjectSummary, isCurrent: boolean, currentName: string): string {
  const name = isCurrent && currentName ? currentName : p.project_id;
  const date = shortDate(p.updated_at);
  return date ? `${name} · ${date}` : name || "未命名项目";
}

export function Topbar() {
  const { projects, projectId, selectProject, dossier, status, refresh, refreshing } = useProject();
  const [lastRefresh, setLastRefresh] = useState<Date | null>(null);

  const narrative = clean(dossier?.assets?.narrative);
  const name = projectName(narrative) || "未命名项目";
  const summary = projectSummary(narrative);
  const runId = clean(dossier?.meta?.run_id) || projectId || "";
  const backend = dossier?.meta?.llm_backend || "未知后端";
  const version = dossier?.meta?.version;
  const state = status?.state ? (STATE_LABELS[status.state] ?? status.state) : null;

  async function handleRefresh() {
    await refresh();
    setLastRefresh(new Date());
  }

  return (
    <header className="flex h-14 shrink-0 items-center gap-3 border-b bg-background/95 px-4">
      <div className="min-w-0 flex-1">
        <h1 className="truncate text-sm font-semibold">{name}</h1>
        {summary ? <p className="truncate text-xs text-muted-foreground">{summary}</p> : null}
      </div>

      <div className="ml-auto flex items-center gap-3">
        {/* 次级信息区（弱化）：run_id / backend / version / 状态 */}
        <div
          className="hidden shrink-0 text-right text-[11px] leading-tight text-muted-foreground/70 md:block"
          title={`run ${runId}`}
        >
          <div>run {shortRunId(runId)}</div>
          <div>
            {backend} · v{version ?? "—"}
            {state ? ` · ${state}` : ""}
          </div>
        </div>

        {/* 项目选择器：项目名（当前可派生）+ 日期；其余项目用 project_id + 日期 */}
        <select
          value={projectId ?? ""}
          onChange={(e) => e.target.value && selectProject(e.target.value)}
          className="h-8 max-w-[260px] rounded-md border bg-background px-2 text-xs"
          title="切换项目（= run）"
        >
          {projects.length === 0 && <option value="">无可用项目</option>}
          {projects.map((p) => (
            <option key={p.project_id} value={p.project_id}>
              {optionLabel(p, p.project_id === projectId, name)}
            </option>
          ))}
        </select>

        {/* 刷新：Tooltip + 刷新后时间反馈 */}
        <div className="flex items-center gap-1.5">
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                variant="ghost"
                size="icon"
                onClick={handleRefresh}
                disabled={refreshing}
                aria-label="刷新数据"
              >
                <RefreshCw className={refreshing ? "h-4 w-4 animate-spin" : "h-4 w-4"} />
              </Button>
            </TooltipTrigger>
            <TooltipContent>
              刷新数据{lastRefresh ? `（上次 ${lastRefresh.toLocaleTimeString()}）` : ""}
            </TooltipContent>
          </Tooltip>
          {lastRefresh && (
            <span className="hidden text-[10px] text-muted-foreground/70 sm:block">
              已刷新 {lastRefresh.toLocaleTimeString()}
            </span>
          )}
        </div>
      </div>
    </header>
  );
}
