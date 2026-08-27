"use client";

import { useMemo, useState } from "react";
import { usePathname, useRouter } from "next/navigation";
import {
  Bot,
  FileText,
  Loader2,
  RefreshCcw,
  Search,
  Sparkles,
  Swords,
  X,
} from "lucide-react";

import { useProject } from "@/hooks/use-project";
import { decisionFor, selectedPair, whyThis } from "@/lib/derive";
import { evidenceLabel, noveltyBandLabel, VERDICT_LABELS } from "@/lib/format";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import { clean, clip, projectName, projectSummary } from "@/lib/utils";
import type { Idea, Evaluation, Verdict } from "@/lib/types";

interface AskResult {
  title: string;
  lines: string[];
  tone?: "default" | "warning";
}

/**
 * 「研究助手」（M25 v3.3 改名，原 Ask PaperMine）：
 * 保留快捷操作（映射到 M24 模块化重跑端点），不做伪对话——后端无自由对话端点。
 * 定位：优化当前 Idea / 重新评估 / 补充文献 / 挑战 / 生成实验。
 */
export function AskPaperMine() {
  const { dossier, ideas, gaps, roadmap, action, refineIdea, evaluateIdea, retrieveMore } =
    useProject();
  const router = useRouter();
  const pathname = usePathname();
  const [open, setOpen] = useState(false);
  const [result, setResult] = useState<AskResult | null>(null);

  const selected = selectedPair(dossier);
  const detailMatch = pathname.match(/^\/ideas\/([^/]+)/);
  const detailId = detailMatch ? decodeURIComponent(detailMatch[1]) : null;

  const current: { idea: Idea | null; evaluation?: Evaluation } = useMemo(() => {
    const iid = detailId ?? selected.idea?.idea_id;
    if (!iid) return { idea: null };
    const found = ideas.find((x) => String(x.idea.idea_id) === String(iid));
    return found ? { idea: found.idea, evaluation: found.evaluation } : { idea: selected.idea, evaluation: selected.evaluation };
  }, [detailId, selected.idea, selected.evaluation, ideas]);

  const currentIdeaId = current.idea?.idea_id;
  const firstGapId = current.idea?.gap_refs?.[0] || gaps[0]?.gap_id || null;
  const decision = decisionFor(dossier, currentIdeaId);

  async function run(title: string, fn: () => Promise<unknown>, map: (r: any) => AskResult) {
    setResult(null);
    try {
      const r = await fn();
      setResult(map(r));
    } catch (err) {
      setResult({
        title: "操作失败",
        lines: ["服务暂时不可用，请稍后重试。"],
        tone: "warning",
      });
    }
  }

  const busy = Boolean(action);

  return (
    <>
      {/* 右下角固定入口 */}
      <button
        onClick={() => setOpen((v) => !v)}
        className="fixed bottom-5 right-5 z-40 flex items-center gap-2 rounded-full bg-primary px-4 py-3 text-sm font-medium text-primary-foreground shadow-lg transition-transform hover:scale-105"
      >
        {open ? <X className="h-4 w-4" /> : <Bot className="h-4 w-4" />}
        研究助手
      </button>

      {open && (
        <>
          <div className="fixed inset-0 z-40 bg-black/30" onClick={() => setOpen(false)} />
          <div className="fixed bottom-20 right-5 z-50 flex max-h-[80vh] w-[400px] flex-col overflow-hidden rounded-xl border bg-card shadow-xl">
            <div className="flex items-center justify-between border-b px-4 py-3">
              <div className="flex items-center gap-2">
                <Sparkles className="h-4 w-4 text-primary" />
                <span className="text-sm font-semibold">研究助手</span>
                <span className="text-xs text-muted-foreground">优化当前 Idea</span>
              </div>
              <Button variant="ghost" size="icon" onClick={() => setOpen(false)}>
                <X className="h-4 w-4" />
              </Button>
            </div>

            <div className="flex-1 space-y-4 overflow-y-auto p-4">
              {/* 上下文 */}
              <section className="space-y-1.5 text-xs text-muted-foreground">
                <div className="font-medium text-foreground">上下文（自动携带）</div>
                <div>
                  项目：{projectName(dossier?.assets?.narrative) || "（未命名）"}
                  {projectSummary(dossier?.assets?.narrative)
                    ? ` · ${projectSummary(dossier?.assets?.narrative)}`
                    : ""}
                </div>
                <div>当前 Idea：{currentIdeaId ? <code>{currentIdeaId}</code> : "（无）"}</div>
                {current.idea && <div>主张：{clip(current.idea.claim, 80)}</div>}
                {current.idea?.literature_refs?.length ? (
                  <div>文献引用：{current.idea.literature_refs.length} 篇</div>
                ) : null}
                {current.idea?.gap_refs?.length ? (
                  <div>来源 gap：{current.idea.gap_refs.join("、")}</div>
                ) : null}
                {decision && <div>当前判断：{decision.summary}</div>}
              </section>

              <Separator />

              {/* 快捷按钮 */}
              <section className="grid grid-cols-1 gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  disabled={!currentIdeaId || busy}
                  onClick={() =>
                    run("优化当前 Idea", () => refineIdea(currentIdeaId!), (r) => ({
                      title: "已优化当前 Idea",
                      lines: [
                        `主张：${clip(r.idea.claim, 120)}`,
                        `假设：${clip(r.idea.novelty_hypothesis, 120)}`,
                        r.degraded ? "（离线确定性降级，低置信）" : "",
                      ].filter(Boolean),
                      tone: r.degraded ? "warning" : "default",
                    }))
                  }
                >
                  <Sparkles className="h-4 w-4" /> 优化当前 Idea
                </Button>

                <Button
                  variant="outline"
                  size="sm"
                  disabled={!currentIdeaId || busy}
                  onClick={() =>
                    run("重新评估", () => evaluateIdea(currentIdeaId!), (r) => ({
                      title: "已重新评估",
                      lines: [
                        `创新程度 ${r.evaluation.novelty_score}（${noveltyBandLabel(r.evaluation.novelty_band)}）`,
                        `建议：${VERDICT_LABELS[r.evaluation.verdict as Verdict] ?? r.evaluation.verdict} · 证据强度 ${evidenceLabel(r.evaluation.evidence_validation?.evidence)}`,
                        r.evaluation.rework_reason ? `回炉原因：${clip(r.evaluation.rework_reason, 80)}` : "",
                      ].filter(Boolean),
                    }))
                  }
                >
                  <RefreshCcw className="h-4 w-4" /> 重新评估
                </Button>

                <Button
                  variant="outline"
                  size="sm"
                  disabled={!firstGapId || busy}
                  onClick={() =>
                    run("补充文献", () => retrieveMore(firstGapId!), (r) => ({
                      title: "已补充文献",
                      lines: [
                        `gap ${r.gap?.gap_id ?? firstGapId} 新增 ${r.added_papers?.length ?? 0} 篇`,
                        ...(r.added_papers ?? []).map((t: string) => `· ${clip(t, 80)}`),
                      ],
                    }))
                  }
                >
                  <Search className="h-4 w-4" /> 补充文献
                </Button>

                <Button
                  variant="outline"
                  size="sm"
                  disabled={!currentIdeaId}
                  onClick={() => {
                    setOpen(false);
                    router.push(`/ideas/${currentIdeaId}?tab=risk`);
                  }}
                >
                  <Swords className="h-4 w-4" /> 挑战这个 Idea
                </Button>

                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => {
                    setOpen(false);
                    router.push("/roadmap");
                  }}
                >
                  <FileText className="h-4 w-4" /> 生成实验
                </Button>
              </section>

              {/* 结果 */}
              {busy && (
                <div className="flex items-center gap-2 text-sm text-muted-foreground">
                  <Loader2 className="h-4 w-4 animate-spin" /> 正在执行 {action}…
                </div>
              )}
              {result && (
                <section className="rounded-lg border p-3">
                  <div className="mb-1 flex items-center gap-1.5 text-sm font-medium">
                    {result.tone === "warning" ? (
                      <Badge variant="warning">降级</Badge>
                    ) : (
                      <Badge variant="secondary">完成</Badge>
                    )}
                    {result.title}
                  </div>
                  <ul className="space-y-1 text-xs text-muted-foreground">
                    {result.lines.map((l, i) => (
                      <li key={i} className="break-words">{l}</li>
                    ))}
                  </ul>
                </section>
              )}

              {/* 推荐理由（作为结论锚点） */}
              {current.idea && (
                <section className="rounded-lg bg-muted/60 p-3 text-xs">
                  <div className="mb-1 font-medium">为什么推荐这个 Idea</div>
                  <ul className="space-y-1 text-muted-foreground">
                    {whyThis(current.idea, current.evaluation).map((s, i) => (
                      <li key={i}>· {s}</li>
                    ))}
                  </ul>
                </section>
              )}
            </div>

            <div className="border-t px-4 py-2 text-[11px] text-muted-foreground">
              操作走模块化重跑（优化 / 重新评估 / 补充文献），只重跑受影响环节，不整条 Pipeline 重跑。
            </div>
          </div>
        </>
      )}
    </>
  );
}
