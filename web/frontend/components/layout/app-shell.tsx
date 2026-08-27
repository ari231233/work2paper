"use client";

import type { ReactNode } from "react";

import { useProject } from "@/hooks/use-project";
import { Sidebar } from "./sidebar";
import { Topbar } from "./topbar";
import { AskPaperMine } from "./ask-papermine";
import { ErrorNotice } from "./error-notice";
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
              <ErrorNotice message={error} variant="full" />
            ) : (
              <>
                {error ? <ErrorNotice message={error} /> : null}
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
