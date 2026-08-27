"use client";

import { useState } from "react";
import { Loader2, Search } from "lucide-react";

import { useProject } from "@/hooks/use-project";
import { gapEvidenceLabel } from "@/lib/format";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { clip, clean } from "@/lib/utils";

/** 展平的 gap 引用（供证据图与详情面板共用）。 */
export interface GapRef {
  gapId: string;
  entryIndex: number;
  label: string;
  kind: string;
  description: string;
  angle: string;
  evidenceLevel: string | null;
  basis: string;
  scope: string;
  coverage: number;
  query: string;
  sources: string[];
}

/** Gap 详情面板：证据强度 / 依据 / 范围 + 「补充文献」操作（M25 v2.4 改动 ⑤⑦）。 */
export function GapDetail({ gap }: { gap: GapRef }) {
  const { retrieveMore, action } = useProject();
  const [added, setAdded] = useState<string[] | null>(null);
  const busy = action === "retrieve-more";

  async function onRetrieve() {
    setAdded(null);
    try {
      const r = await retrieveMore(gap.gapId);
      setAdded(r.added_papers ?? []);
    } catch {
      // 错误经 context.error 呈现，这里不重复展示
    }
  }

  return (
    <div className="space-y-2.5 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <Badge variant="secondary">{gap.gapId}</Badge>
        <Badge variant={gap.kind === "contradiction" ? "destructive" : "accent"}>
          {gap.kind === "contradiction" ? "矛盾" : "缺口"}
        </Badge>
        <Badge variant="outline">{gapEvidenceLabel(gap.evidenceLevel)}证据</Badge>
      </div>

      <div className="font-medium leading-snug">{gap.label}</div>

      <dl className="space-y-1.5 text-xs text-muted-foreground">
        <div>
          <dt className="inline font-medium text-foreground">Evidence 强度：</dt>
          <dd className="inline">{gapEvidenceLabel(gap.evidenceLevel)}（coverage {gap.coverage} papers）</dd>
        </div>
        <div>
          <dt className="inline font-medium text-foreground">Why：</dt>
          <dd className="inline">{gap.description || "—"}</dd>
        </div>
        {gap.angle ? (
          <div>
            <dt className="inline font-medium text-foreground">角度：</dt>
            <dd className="inline">{gap.angle}</dd>
          </div>
        ) : null}
        {gap.basis ? (
          <div>
            <dt className="inline font-medium text-foreground">依据：</dt>
            <dd className="inline">{gap.basis}</dd>
          </div>
        ) : null}
        {gap.scope ? (
          <div>
            <dt className="inline font-medium text-foreground">范围：</dt>
            <dd className="inline">{gap.scope}</dd>
          </div>
        ) : null}
        {gap.query ? (
          <div>
            <dt className="inline font-medium text-foreground">检索 Query：</dt>
            <dd className="inline">{clip(gap.query, 120)}</dd>
          </div>
        ) : null}
        {gap.sources?.length ? (
          <div>
            <dt className="inline font-medium text-foreground">来源：</dt>
            <dd className="inline">{clean(gap.sources.join("、"))}</dd>
          </div>
        ) : null}
      </dl>

      <div className="rounded-md bg-amber-50 px-2.5 py-2 text-xs text-amber-800 dark:bg-amber-900/30 dark:text-amber-200">
        ⚠ Based only on current retrieval——「没搜到 ≠ 不存在」，此 gap 是证据有界的假设。
      </div>

      <Button variant="outline" size="sm" onClick={onRetrieve} disabled={busy}>
        {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <Search className="h-4 w-4" />}
        补充文献
      </Button>

      {added && (
        <div className="rounded-md bg-muted/60 p-2 text-xs text-muted-foreground">
          {added.length
            ? `新增 ${added.length} 篇：${added.map((t) => clip(t, 60)).join("、")}`
            : "未发现新文献（已是最新检索结果）。"}
        </div>
      )}
    </div>
  );
}
