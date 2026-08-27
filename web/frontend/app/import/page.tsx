"use client";

import { useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { Archive, CheckCircle2, FolderUp, Loader2, ShieldCheck, Upload } from "lucide-react";

import { api } from "@/lib/api";
import type { ImportRecord } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KiB`;
  return `${(value / 1024 / 1024).toFixed(1)} MiB`;
}

export default function ImportPage() {
  const router = useRouter();
  const folderInput = useRef<HTMLInputElement | null>(null);
  const zipInput = useRef<HTMLInputElement | null>(null);
  const [projectName, setProjectName] = useState("");
  const [record, setRecord] = useState<ImportRecord | null>(null);
  const [phase, setPhase] = useState<"idle" | "uploading" | "analyzing">("idle");
  const [error, setError] = useState<string | null>(null);

  const excludedSummary = useMemo(() => {
    if (!record) return "";
    const sensitive = record.excluded_files.filter((f) => f.reason === "sensitive_file").length;
    const ignored = record.excluded_files.length - sensitive;
    return `${ignored} 个依赖/构建文件，${sensitive} 个敏感文件`;
  }, [record]);

  async function uploadFolder(files: FileList | null) {
    if (!files?.length) return;
    setPhase("uploading");
    setError(null);
    try {
      const items = Array.from(files);
      const paths = items.map((file) => file.webkitRelativePath || file.name);
      const fallback = paths[0]?.split("/")[0] || "";
      const result = await api.importFolder(items, paths, projectName || fallback);
      setProjectName(result.project_name);
      setRecord(result);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setPhase("idle");
      if (folderInput.current) folderInput.current.value = "";
    }
  }

  async function uploadZip(files: FileList | null) {
    const file = files?.[0];
    if (!file) return;
    setPhase("uploading");
    setError(null);
    try {
      const result = await api.importArchive(file, projectName || file.name.replace(/\.zip$/i, ""));
      setProjectName(result.project_name);
      setRecord(result);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setPhase("idle");
      if (zipInput.current) zipInput.current.value = "";
    }
  }

  async function analyze() {
    if (!record) return;
    setPhase("analyzing");
    setError(null);
    try {
      const result = await api.analyzeImport(record.import_id);
      if (!result.project.project_id) throw new Error("分析完成但未返回 run_id");
      window.location.assign("/");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setPhase("idle");
    }
  }

  return (
    <div className="mx-auto max-w-5xl space-y-6 p-6">
      <div>
        <h1 className="text-2xl font-semibold">导入项目并开始分析</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          PaperMine 会把项目复制到本机私有数据目录，分析不会修改原始文件。
        </p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>项目名称</CardTitle>
          <CardDescription>可选；留空时从文件夹或 ZIP 名称自动识别。</CardDescription>
        </CardHeader>
        <CardContent>
          <input
            value={projectName}
            onChange={(event) => setProjectName(event.target.value)}
            placeholder="例如：工业设备预测性维护"
            className="h-10 w-full rounded-md border bg-background px-3 text-sm outline-none focus:ring-2 focus:ring-ring"
          />
        </CardContent>
      </Card>

      {!record ? (
        <div className="grid gap-4 md:grid-cols-2">
          <Card className="border-dashed">
            <CardHeader>
              <FolderUp className="mb-2 h-8 w-8 text-primary" />
              <CardTitle>选择项目文件夹</CardTitle>
              <CardDescription>导入代码、README 和项目文档，并保留目录结构。</CardDescription>
            </CardHeader>
            <CardContent>
              <input
                ref={(node) => {
                  folderInput.current = node;
                  node?.setAttribute("webkitdirectory", "");
                  node?.setAttribute("directory", "");
                }}
                type="file"
                multiple
                className="hidden"
                onChange={(event) => void uploadFolder(event.target.files)}
              />
              <Button className="w-full" onClick={() => folderInput.current?.click()} disabled={phase !== "idle"}>
                {phase === "uploading" ? <Loader2 className="animate-spin" /> : <FolderUp />}
                选择文件夹
              </Button>
            </CardContent>
          </Card>

          <Card className="border-dashed">
            <CardHeader>
              <Archive className="mb-2 h-8 w-8 text-primary" />
              <CardTitle>上传 ZIP</CardTitle>
              <CardDescription>适合从 GitHub 下载或从其他设备导出的项目。</CardDescription>
            </CardHeader>
            <CardContent>
              <input
                ref={zipInput}
                type="file"
                accept=".zip,application/zip"
                className="hidden"
                onChange={(event) => void uploadZip(event.target.files)}
              />
              <Button variant="outline" className="w-full" onClick={() => zipInput.current?.click()} disabled={phase !== "idle"}>
                {phase === "uploading" ? <Loader2 className="animate-spin" /> : <Upload />}
                选择 ZIP
              </Button>
            </CardContent>
          </Card>
        </div>
      ) : (
        <Card>
          <CardHeader>
            <div className="flex items-start justify-between gap-4">
              <div>
                <CardTitle className="flex items-center gap-2">
                  <CheckCircle2 className="h-5 w-5 text-[hsl(var(--success))]" />
                  导入预览
                </CardTitle>
                <CardDescription className="mt-2">确认项目内容后再开始分析。</CardDescription>
              </div>
              <span className="rounded-full bg-[hsl(var(--success)/0.12)] px-3 py-1 text-xs text-[hsl(var(--success))]">
                已安全复制
              </span>
            </div>
          </CardHeader>
          <CardContent className="space-y-5">
            <div className="grid gap-3 sm:grid-cols-4">
              <div className="rounded-lg bg-muted p-3"><div className="text-xs text-muted-foreground">项目</div><div className="mt-1 font-medium">{record.project_name}</div></div>
              <div className="rounded-lg bg-muted p-3"><div className="text-xs text-muted-foreground">来源</div><div className="mt-1 font-medium">{record.source_type === "zip" ? "ZIP" : "文件夹"}</div></div>
              <div className="rounded-lg bg-muted p-3"><div className="text-xs text-muted-foreground">文件</div><div className="mt-1 font-medium">{record.file_count}</div></div>
              <div className="rounded-lg bg-muted p-3"><div className="text-xs text-muted-foreground">大小</div><div className="mt-1 font-medium">{formatBytes(record.total_size)}</div></div>
            </div>

            <div className="rounded-lg border p-4 text-sm">
              <div className="flex items-center gap-2 font-medium"><ShieldCheck className="h-4 w-4 text-primary" />安全检查</div>
              <p className="mt-2 text-muted-foreground">已排除 {record.excluded_files.length} 个文件：{excludedSummary}。</p>
              {record.warnings.map((warning) => <p key={warning} className="mt-1 text-[hsl(var(--warning-foreground))]">{warning}</p>)}
            </div>

            <details className="rounded-lg border p-4 text-sm">
              <summary className="cursor-pointer font-medium">查看已导入文件（{record.included_files.length}）</summary>
              <div className="mt-3 max-h-48 overflow-auto font-mono text-xs text-muted-foreground">
                {record.included_files.slice(0, 500).map((file) => <div key={file}>{file}</div>)}
              </div>
            </details>

            {error ? <div className="rounded-lg border border-destructive/30 bg-destructive/5 p-3 text-sm text-destructive">{error}</div> : null}

            <div className="flex justify-end gap-3">
              <Button variant="outline" onClick={() => { setRecord(null); setError(null); }} disabled={phase !== "idle"}>重新选择</Button>
              <Button onClick={() => void analyze()} disabled={phase !== "idle"}>
                {phase === "analyzing" ? <Loader2 className="animate-spin" /> : null}
                {phase === "analyzing" ? "正在分析，请勿关闭页面" : "确认并开始分析"}
              </Button>
            </div>
          </CardContent>
        </Card>
      )}

      {error && !record ? <div className="rounded-lg border border-destructive/30 bg-destructive/5 p-4 text-sm text-destructive">{error}</div> : null}
      <Button variant="ghost" onClick={() => router.push("/")}>返回科研工作台</Button>
    </div>
  );
}
