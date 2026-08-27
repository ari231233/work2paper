"use client";

import type { ReactNode } from "react";
import { AlertTriangle } from "lucide-react";

import { useProject } from "@/hooks/use-project";
import { Sidebar } from "./sidebar";
import { Topbar } from "./topbar";
import { AskPaperMine } from "./ask-papermine";
import { Skeleton } from "@/components/ui/skeleton";
import { TooltipProvider } from "@/components/ui/tooltip";

function LoadingState() {
  return (
    <div className="space-y-4 p-6">
      <Skeleton className="h-8 w-1/3" />
      <Skeleton className="h-40 w-full" />
      <div className="grid grid-cols-3 gap-4">
        <Skeleton className="h-28" />
        <Skeleton className="h-28" />
        <Skeleton className="h-28" />
      </div>
    </div>
  );
}

function ErrorState({ message }: { message: string }) {
  return (
    <div className="flex h-full items-center justify-center p-8">
      <div className="max-w-lg rounded-xl border bg-card p-6 text-center shadow">
        <AlertTriangle className="mx-auto mb-3 h-8 w-8 text-amber-500" />
        <h2 className="text-sm font-semibold">无法加载项目数据</h2>
        <p className="mt-2 break-words text-xs text-muted-foreground">{message}</p>
        <p className="mt-3 text-xs text-muted-foreground">
          请先启动 M24 后端：<code className="rounded bg-muted px-1">python -m web</code>（默认
          127.0.0.1:8000），或执行一次分析。
        </p>
      </div>
    </div>
  );
}

/** 重新验证失败时的非阻塞提示：保留旧数据，不整页挡内容（M25 v2.2）。 */
function ErrorBanner({ message }: { message: string }) {
  return (
    <div className="flex items-center gap-2 border-b bg-amber-50 px-4 py-2 text-xs text-amber-800 dark:bg-amber-900/30 dark:text-amber-200">
      <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
      <span className="truncate">{message}</span>
    </div>
  );
}

export function AppShell({ children }: { children: ReactNode }) {
  const { loading, error, dossier } = useProject();
  // 已有旧数据时不整页挡内容：首次加载失败才显示全屏错误，重新验证失败只显示顶部横条。
  const hasData = Boolean(dossier);

  return (
    <TooltipProvider delayDuration={200}>
      <div className="flex h-screen flex-col overflow-hidden">
        <Topbar />
        <div className="flex flex-1 overflow-hidden">
          <Sidebar />
          <main className="relative flex-1 overflow-y-auto">
            {loading && !hasData ? (
              <LoadingState />
            ) : error && !hasData ? (
              <ErrorState message={error} />
            ) : (
              <>
                {error ? <ErrorBanner message={error} /> : null}
                {children}
              </>
            )}
          </main>
        </div>
        <AskPaperMine />
      </div>
    </TooltipProvider>
  );
}
