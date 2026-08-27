"use client";

import Image from "next/image";
import { useEffect, useState, type CSSProperties } from "react";

type ProgressMascotProps = {
  mode: "uploading" | "analyzing";
  percent?: number;
};

function formatElapsed(seconds: number): string {
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  return minutes ? `${minutes} 分 ${rest.toString().padStart(2, "0")} 秒` : `${rest} 秒`;
}

export function ProgressMascot({ mode, percent = 0 }: ProgressMascotProps) {
  const [elapsed, setElapsed] = useState(0);

  useEffect(() => {
    setElapsed(0);
    const startedAt = Date.now();
    const timer = window.setInterval(() => {
      setElapsed(Math.floor((Date.now() - startedAt) / 1000));
    }, 1000);
    return () => window.clearInterval(timer);
  }, [mode]);

  const safePercent = Math.max(0, Math.min(100, percent));
  const style = { "--mascot-progress": safePercent / 100 } as CSSProperties;
  const uploaded = mode === "uploading" && safePercent >= 100;

  return (
    <div className="rounded-xl border border-primary/20 bg-gradient-to-br from-primary/5 via-card to-cyan-50 p-5 shadow-sm dark:to-cyan-950/20">
      <div className="flex items-center justify-between gap-4 text-sm">
        <div>
          <div className="font-semibold text-foreground">
            {mode === "uploading"
              ? uploaded ? "文件已上传，正在整理导入内容…" : "正在上传项目…"
              : "科研助手正在分析项目…"}
          </div>
          <div className="mt-1 text-xs text-muted-foreground">
            {mode === "analyzing" ? "分析时长取决于项目规模与网络，请保持页面开启。" : "请保持页面开启，上传完成后会自动显示预览。"}
          </div>
        </div>
        <div className="shrink-0 text-right">
          {mode === "uploading" ? <div className="font-mono font-semibold text-primary">{safePercent}%</div> : null}
          <div className="text-xs text-muted-foreground">已用时 {formatElapsed(elapsed)}</div>
        </div>
      </div>

      <div className={`mascot-progress mt-4 ${mode === "analyzing" ? "is-indeterminate" : ""}`} style={style}>
        <div className="mascot-track">
          <div className="mascot-fill" />
        </div>
        <div className="mascot-runner" aria-hidden="true">
          <div className="mascot-dance">
            <Image src="/whale-researcher.png" alt="" width={72} height={72} priority />
          </div>
        </div>
      </div>
    </div>
  );
}
