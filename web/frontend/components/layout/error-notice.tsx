"use client";

import { useState } from "react";
import { AlertTriangle, ChevronDown, ChevronRight } from "lucide-react";

/**
 * 错误提示产品化（M25 v3.4）：对外只说「服务暂时不可用」，技术细节收进「查看详情」。
 * - variant="banner"：重新验证失败时顶栏横条（保留旧数据，不挡内容）；
 * - variant="full"：首次加载失败的全屏提示。
 */
export function ErrorNotice({
  message,
  variant = "banner",
}: {
  message?: string | null;
  variant?: "banner" | "full";
}) {
  const [open, setOpen] = useState(false);
  const detail = message?.trim();

  if (variant === "full") {
    return (
      <div className="flex h-full items-center justify-center p-8">
        <div className="max-w-lg rounded-xl border bg-card p-6 text-center shadow">
          <AlertTriangle className="mx-auto mb-3 h-8 w-8 text-warning" />
          <h2 className="text-sm font-semibold">服务暂时不可用</h2>
          <p className="mt-2 text-xs text-muted-foreground">请稍后重试，或确认后端服务是否已启动。</p>
          {detail ? (
            <button
              onClick={() => setOpen((v) => !v)}
              className="mt-3 inline-flex items-center gap-1 text-xs font-medium text-primary hover:underline"
            >
              {open ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}
              查看详情
            </button>
          ) : null}
          {open && detail ? (
            <pre className="mt-2 whitespace-pre-wrap break-words rounded-md bg-muted/60 p-3 text-left text-xs text-muted-foreground">
              {detail}
            </pre>
          ) : null}
          <p className="mt-3 text-xs text-muted-foreground">
            请先启动 M24 后端：<code className="rounded bg-muted px-1">python -m web</code>（默认
            127.0.0.1:8000），或执行一次分析。
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="border-b bg-amber-50 px-4 py-2 dark:bg-amber-900/30">
      <div className="flex items-center gap-2 text-xs text-amber-800 dark:text-amber-200">
        <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
        <span className="truncate">服务暂时不可用</span>
        {detail ? (
          <button
            onClick={() => setOpen((v) => !v)}
            className="shrink-0 font-medium underline underline-offset-2"
          >
            {open ? "收起详情" : "查看详情"}
          </button>
        ) : null}
      </div>
      {open && detail ? (
        <pre className="mt-1 whitespace-pre-wrap break-words text-[11px] text-amber-900/80 dark:text-amber-100/80">
          {detail}
        </pre>
      ) : null}
    </div>
  );
}
