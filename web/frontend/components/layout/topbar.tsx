"use client";

import { RefreshCw } from "lucide-react";

import { useProject } from "@/hooks/use-project";
import { Button } from "@/components/ui/button";
import { clean } from "@/lib/utils";

export function Topbar() {
  const { projects, projectId, selectProject, dossier, status, refresh, loading } = useProject();

  return (
    <header className="flex h-14 shrink-0 items-center gap-3 border-b bg-background/95 px-4">
      <div className="min-w-0">
        <h1 className="truncate text-sm font-semibold">
          {clean(dossier?.assets?.narrative) || "未命名项目"}
        </h1>
        <p className="truncate text-xs text-muted-foreground">
          {projectId ? `run ${projectId}` : "未加载"} ·{" "}
          {dossier?.meta?.llm_backend || "（未知后端）"} · 版本 v{dossier?.meta?.version ?? "—"}
          {status?.state ? ` · ${status.state}` : ""}
        </p>
      </div>

      <div className="ml-auto flex items-center gap-2">
        <select
          value={projectId ?? ""}
          onChange={(e) => e.target.value && selectProject(e.target.value)}
          className="h-8 max-w-[240px] rounded-md border bg-background px-2 text-xs"
          title="切换项目（= run）"
        >
          {projects.length === 0 && <option value="">无可用项目</option>}
          {projects.map((p) => (
            <option key={p.project_id} value={p.project_id}>
              {p.project_id}
            </option>
          ))}
        </select>
        <Button variant="ghost" size="icon" onClick={() => refresh()} disabled={loading} title="刷新数据">
          <RefreshCw className={loading ? "h-4 w-4 animate-spin" : "h-4 w-4"} />
        </Button>
      </div>
    </header>
  );
}
