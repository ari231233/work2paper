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

export function AppShell({ children }: { children: ReactNode }) {
  const { loading, error } = useProject();

  return (
    <TooltipProvider delayDuration={200}>
      <div className="flex h-screen flex-col overflow-hidden">
        <Topbar />
        <div className="flex flex-1 overflow-hidden">
          <Sidebar />
          <main className="relative flex-1 overflow-y-auto">
            {loading ? <LoadingState /> : error ? <ErrorState message={error} /> : children}
          </main>
        </div>
        <AskPaperMine />
      </div>
    </TooltipProvider>
  );
}
