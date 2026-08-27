"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import {
  Background,
  Controls,
  Handle,
  MarkerType,
  Position,
  ReactFlow,
  type Edge,
  type Node,
  type NodeProps,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";

import { useProject } from "@/hooks/use-project";
import { gapEvidenceLevel, selectedPair } from "@/lib/derive";
import { yearOf } from "@/lib/format";
import type { Dossier, LiteratureEntry, Paper } from "@/lib/types";
import { asList, clean, clip, cn } from "@/lib/utils";
import { EmptyState } from "@/components/ui/empty-state";
import { GapDetail, type GapRef } from "./gap-detail";

// ---------------------------------------------------------------------------
// 节点
// ---------------------------------------------------------------------------

function QueryNode({ data }: NodeProps) {
  return (
    <div className="react-flow__node-queryNode">
      <div className="text-xs font-semibold">{String(data.label ?? "")}</div>
      <div className="mt-0.5 text-[11px] text-muted-foreground">{String(data.queryShort ?? "")}</div>
      <div className="mt-1 text-[11px] font-medium">{String(data.meta ?? "")}</div>
      <div className="mt-0.5 text-[11px] text-primary/70">{data.expanded ? "点击收起论文" : "点击展开论文"}</div>
    </div>
  );
}

function PaperNode({ data }: NodeProps) {
  return (
    <div className="react-flow__node-paperNode" title={String(data.fullTitle ?? "")}>
      <Handle type="source" position={Position.Right} />
      <div className="text-xs font-medium leading-snug">{String(data.label ?? "")}</div>
    </div>
  );
}

function GapNode({ data }: NodeProps) {
  const selected = Boolean(data.selected);
  const level = String(data.evidenceLevel ?? "");
  const kind = String(data.kind ?? "");
  return (
    <div
      className={cn(
        "react-flow__node-gapNode",
        selected && "selected",
        kind === "contradiction" && "contradiction",
        level === "strong" && "strong",
        (level === "weak" || level === "moderate" || level === "medium") && "weak"
      )}
      title={String(data.fullLabel ?? "")}
    >
      <Handle type="target" position={Position.Left} />
      <div className="text-xs font-semibold">{String(data.gapId ?? "")}</div>
      <div className="mt-0.5 text-[11px] opacity-80">{String(data.label ?? "")}</div>
    </div>
  );
}

const nodeTypes = { queryNode: QueryNode, paperNode: PaperNode, gapNode: GapNode };

// ---------------------------------------------------------------------------
// 图构建：按 Query 分组；仅展开的条目渲染其论文与 gap（M25 v2.4 改动 ①②③）
// ---------------------------------------------------------------------------

const COL_W = 560;
const GAP_OFFSET = 300;
const HEAD_Y = 96;

interface PaperRef {
  nodeId: string;
  entryIndex: number;
  title: string;
  paper: Paper;
}

function buildGraph(
  literature: LiteratureEntry[],
  expanded: Set<number>,
  selectedGapId: string | null
) {
  const nodes: Node[] = [];
  const edges: Edge[] = [];
  const gapByKey: Record<string, GapRef> = {};
  const paperByKey: Record<string, PaperRef> = {};
  const gapNodeIdByGapId: Record<string, string> = {};

  asList(literature).forEach((entry, ei) => {
    const papers = asList(entry?.papers).filter((p) => clean(p.title));
    const gaps = asList(entry?.contradiction_graph?.gaps).filter((g) => clean(g.gap_id));
    const isExpanded = expanded.has(ei);
    const colX = ei * COL_W;

    let relationCount = 0;
    gaps.forEach((g) => {
      if (g.type === "contradiction") relationCount += asList(g.paper_refs).length;
      else relationCount += papers.length;
    });

    nodes.push({
      id: `q${ei}`,
      type: "queryNode",
      position: { x: colX, y: 0 },
      data: {
        label: `Query ${ei + 1}`,
        queryShort: clip(clean(entry?.query), 44),
        meta: `${papers.length} 篇论文 · ${gaps.length} 个 gap · ${relationCount} 条关联`,
        expanded: isExpanded,
      },
    });

    if (!isExpanded) return;

    const paperIds = papers.map((_, pi) => `e${ei}-p${pi}`);
    papers.forEach((p, pi) => {
      const nodeId = paperIds[pi];
      nodes.push({
        id: nodeId,
        type: "paperNode",
        position: { x: colX, y: HEAD_Y + pi * 60 },
        data: { label: clip(p.title, 26), fullTitle: p.title, paperKey: `e${ei}:${clean(p.title)}` },
      });
      paperByKey[`e${ei}:${clean(p.title)}`] = {
        nodeId,
        entryIndex: ei,
        title: clean(p.title),
        paper: p,
      };
    });

    gaps.forEach((g, gi) => {
      const nodeId = `e${ei}-${g.gap_id}`;
      nodes.push({
        id: nodeId,
        type: "gapNode",
        position: { x: colX + GAP_OFFSET, y: HEAD_Y + gi * 150 },
        data: {
          label: clip(clean(g.claim_point) || clean(g.angle), 30),
          fullLabel: clean(g.claim_point) || clean(g.angle) || "（未命名结论点）",
          gapId: g.gap_id,
          kind: g.type,
          evidenceLevel: gapEvidenceLevel(g),
          selected: clean(g.gap_id) === selectedGapId,
        },
      });

      const label = clean(g.claim_point) || clean(g.angle) || "（未命名结论点）";
      gapByKey[clean(g.gap_id)] = {
        gapId: clean(g.gap_id),
        entryIndex: ei,
        label,
        kind: g.type ?? "gap",
        description: clean(g.description),
        angle: clean(g.angle),
        evidenceLevel: gapEvidenceLevel(g),
        basis: clean(g.gap_hypothesis?.basis),
        scope: clean(g.gap_hypothesis?.scope),
        coverage: papers.length,
        query: clean(entry?.query),
        sources: asList(entry?.sources),
      };
      gapNodeIdByGapId[clean(g.gap_id)] = nodeId;

      if (g.type === "contradiction") {
        asList(g.paper_refs).forEach((refTitle, ri) => {
          const pi = papers.findIndex((p) => clean(p.title) === clean(refTitle));
          if (pi >= 0) {
            edges.push({
              id: `${nodeId}-in-${ri}`,
              source: paperIds[pi],
              target: nodeId,
              type: "smoothstep",
              markerEnd: { type: MarkerType.ArrowClosed },
            });
          }
        });
      } else {
        paperIds.forEach((pid) => {
          edges.push({
            id: `${nodeId}-${pid}`,
            source: pid,
            target: nodeId,
            type: "smoothstep",
            markerEnd: { type: MarkerType.ArrowClosed },
          });
        });
      }
    });
  });

  return { nodes, edges, gapByKey, paperByKey, gapNodeIdByGapId };
}

// ---------------------------------------------------------------------------
// 推荐 idea 对应的 gap / 首个 gap
// ---------------------------------------------------------------------------

function recommendedGapId(dossier: Dossier | null | undefined): string | null {
  const sel = selectedPair(dossier);
  return asList(sel.idea?.gap_refs).map(clean).find(Boolean) ?? null;
}

function firstGap(literature: LiteratureEntry[]): { gapId: string; entryIndex: number } | null {
  for (let ei = 0; ei < asList(literature).length; ei += 1) {
    const gap = asList(literature[ei]?.contradiction_graph?.gaps).find((g) => clean(g.gap_id));
    if (gap) return { gapId: clean(gap.gap_id), entryIndex: ei };
  }
  return null;
}

function entryIndexOfGap(literature: LiteratureEntry[], gapId: string | null): number {
  if (!gapId) return -1;
  for (let ei = 0; ei < asList(literature).length; ei += 1) {
    if (asList(literature[ei]?.contradiction_graph?.gaps).some((g) => clean(g.gap_id) === gapId)) {
      return ei;
    }
  }
  return -1;
}

// ---------------------------------------------------------------------------
// 图例（M25 v2.4 改动 ⑥）
// ---------------------------------------------------------------------------

function Legend() {
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5 text-[11px] text-muted-foreground">
      <span className="flex items-center gap-1.5">
        <span className="h-3 w-3 rounded border bg-card" /> 论文
      </span>
      <span className="flex items-center gap-1.5">
        <span className="h-3 w-3 rounded border border-primary/40 bg-accent" /> Gap
      </span>
      <span className="flex items-center gap-1.5">
        <span className="h-3 w-3 rounded bg-emerald-500" /> 强证据
      </span>
      <span className="flex items-center gap-1.5">
        <span className="h-3 w-3 rounded bg-amber-500" /> 弱证据
      </span>
      <span className="flex items-center gap-1.5">
        <span className="h-3 w-3 rounded bg-red-500" /> 矛盾
      </span>
      <span className="flex items-center gap-1.5">
        <span className="font-medium">→</span> 关联方向（论文 → Gap）
      </span>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 论文详情（点论文节点后右侧面板展示完整标题 / 理解 / 证据卡）
// ---------------------------------------------------------------------------

function PaperDetail({ paper }: { paper: Paper }) {
  const u = paper.understanding;
  const card = paper.evidence_card;
  const meta = [clean(paper.venue), yearOf(paper), clean(paper.source)].filter(Boolean).join(" · ");

  return (
    <div className="space-y-2 text-sm">
      <div className="font-medium leading-snug">{clean(paper.title) || "（无标题）"}</div>
      {meta ? <div className="text-xs text-muted-foreground">{meta}</div> : null}
      <dl className="space-y-1.5 text-xs text-muted-foreground">
        {u?.claim ? (
          <div>
            <dt className="inline font-medium text-foreground">核心主张：</dt>
            <dd className="inline">{u.claim}</dd>
          </div>
        ) : null}
        {u?.method ? (
          <div>
            <dt className="inline font-medium text-foreground">方法：</dt>
            <dd className="inline">{u.method}</dd>
          </div>
        ) : null}
        {u?.conclusion ? (
          <div>
            <dt className="inline font-medium text-foreground">结论：</dt>
            <dd className="inline">{clip(u.conclusion, 160)}</dd>
          </div>
        ) : null}
        {card?.dataset ? (
          <div>
            <dt className="inline font-medium text-foreground">数据集：</dt>
            <dd className="inline">{card.dataset}</dd>
          </div>
        ) : null}
        {card?.baseline ? (
          <div>
            <dt className="inline font-medium text-foreground">基线：</dt>
            <dd className="inline">{card.baseline}</dd>
          </div>
        ) : null}
        {card?.metric ? (
          <div>
            <dt className="inline font-medium text-foreground">指标：</dt>
            <dd className="inline">{card.metric}</dd>
          </div>
        ) : null}
        {card?.main_gain ? (
          <div>
            <dt className="inline font-medium text-foreground">提升：</dt>
            <dd className="inline">{clip(card.main_gain, 80)}</dd>
          </div>
        ) : null}
        {card?.evidence_source ? (
          <div>
            <dt className="inline font-medium text-foreground">证据来源：</dt>
            <dd className="inline">{card.evidence_source}</dd>
          </div>
        ) : null}
      </dl>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 主组件
// ---------------------------------------------------------------------------

export function EvidenceGraph() {
  const { literature, dossier, projectId } = useProject();
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const [selectedGapId, setSelectedGapId] = useState<string | null>(null);
  const [selectedPaperKey, setSelectedPaperKey] = useState<string | null>(null);
  const didInitRef = useRef(false);
  const prevProjectRef = useRef<string | null>(projectId);

  const defaultGapId = recommendedGapId(dossier) ?? firstGap(literature)?.gapId ?? null;

  // 首次拿到文献时默认展开/选中「推荐 idea 对应 gap」，否则回退首个 gap（右侧面板不留空）；
  // 切换项目时重置图状态，重新按新项目默认选中。
  useEffect(() => {
    const projectChanged = prevProjectRef.current !== projectId;
    prevProjectRef.current = projectId;
    if (projectChanged) {
      didInitRef.current = false;
      setSelectedGapId(null);
      setSelectedPaperKey(null);
      setExpanded(new Set());
    }
    if (didInitRef.current || !asList(literature).length) return;
    didInitRef.current = true;
    const gid = recommendedGapId(dossier) ?? firstGap(literature)?.gapId ?? null;
    if (!gid) return;
    const ei = entryIndexOfGap(literature, gid);
    setSelectedGapId(gid);
    if (ei >= 0) setExpanded((prev) => new Set(prev).add(ei));
  }, [projectId, literature, dossier]);

  const { nodes, edges, gapByKey, paperByKey } = useMemo(
    () => buildGraph(literature, expanded, selectedGapId),
    [literature, expanded, selectedGapId]
  );

  const hasNodes = nodes.length > 0;
  const activeGapId = selectedGapId ?? defaultGapId ?? "";
  const activeGap = activeGapId ? gapByKey[activeGapId] : undefined;
  const activePaper = selectedPaperKey ? paperByKey[selectedPaperKey] : null;

  if (!hasNodes) {
    return <EmptyState title="暂无证据图" description="没有检索到论文或 gap，无法构建 Evidence Graph。" />;
  }

  return (
    <div className="space-y-3">
      <Legend />

      <div className="grid gap-4 lg:grid-cols-[1fr_340px]">
        <div className="h-[520px] overflow-hidden rounded-xl border bg-card">
          <ReactFlow
            nodes={nodes}
            edges={edges}
            nodeTypes={nodeTypes}
            fitView
            fitViewOptions={{ padding: 0.25 }}
            minZoom={0.35}
            maxZoom={1.8}
            nodesDraggable={false}
            nodesConnectable={false}
            onNodeClick={(_e, node) => {
              if (node.type === "queryNode") {
                const ei = Number(String(node.id).replace(/^q/, ""));
                setExpanded((prev) => {
                  const next = new Set(prev);
                  if (next.has(ei)) next.delete(ei);
                  else next.add(ei);
                  return next;
                });
              } else if (node.type === "gapNode") {
                setSelectedGapId(String(node.data.gapId ?? ""));
                setSelectedPaperKey(null);
              } else if (node.type === "paperNode") {
                setSelectedPaperKey(String(node.data.paperKey ?? ""));
                setSelectedGapId(null);
              }
            }}
            onPaneClick={() => {
              // 回到默认关键 gap，右侧面板不留空
              setSelectedGapId(defaultGapId);
              setSelectedPaperKey(null);
            }}
            proOptions={{ hideAttribution: true }}
          >
            <Background gap={20} />
            <Controls />
          </ReactFlow>
        </div>

        {/* 右侧详情面板：首屏默认展示关键 gap（不留空） */}
        <div className="rounded-xl border bg-card p-4">
          <h3 className="mb-3 text-sm font-semibold">
            {activePaper ? "论文详情" : "Gap 证据"}
          </h3>
          {activePaper ? (
            <PaperDetail paper={activePaper.paper} />
          ) : activeGap ? (
            <GapDetail gap={activeGap} />
          ) : (
            <p className="text-sm text-muted-foreground">点击 Query 展开论文，点击 gap 节点展开其证据。</p>
          )}
        </div>
      </div>
    </div>
  );
}
