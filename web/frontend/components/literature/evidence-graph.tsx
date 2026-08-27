"use client";

import { useMemo, useState } from "react";
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
import { gapEvidenceLevel } from "@/lib/derive";
import { gapEvidenceLabel } from "@/lib/format";
import type { LiteratureEntry } from "@/lib/types";
import { asList, clean, clip, cn } from "@/lib/utils";
import { Badge } from "@/components/ui/badge";
import { EmptyState } from "@/components/ui/empty-state";

function PaperNode({ data }: NodeProps) {
  return (
    <div className="react-flow__node-paperNode">
      <Handle type="source" position={Position.Right} />
      <div className="text-xs font-medium leading-snug">{String(data.label ?? "")}</div>
    </div>
  );
}

function GapNode({ data }: NodeProps) {
  const selected = Boolean(data.selected);
  return (
    <div className={cn("react-flow__node-gapNode", selected && "selected")}>
      <Handle type="target" position={Position.Left} />
      <div className="text-xs font-semibold">{String(data.gapId ?? "")}</div>
      <div className="mt-0.5 text-[11px] opacity-80">{clip(String(data.label ?? ""), 56)}</div>
    </div>
  );
}

const nodeTypes = { paperNode: PaperNode, gapNode: GapNode };

interface GapRef {
  gapId: string;
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

function buildGraph(literature: LiteratureEntry[], selectedId: string | null) {
  const nodes: Node[] = [];
  const edges: Edge[] = [];
  const lookup: Record<string, GapRef> = {};
  let y = 0;

  asList(literature).forEach((entry, ei) => {
    const papers = asList(entry?.papers).filter((p) => clean(p.title));
    const gaps = asList(entry?.contradiction_graph?.gaps).filter((g) => clean(g.gap_id));
    const paperIds = papers.map((_, pi) => `e${ei}-p${pi}`);

    papers.forEach((p, pi) => {
      nodes.push({
        id: `e${ei}-p${pi}`,
        type: "paperNode",
        position: { x: 0, y: y + pi * 100 },
        data: { label: p.title, kind: "paper" },
      });
    });

    gaps.forEach((g, gi) => {
      const id = `e${ei}-${g.gap_id}`;
      const gapY = y + gi * 170;
      nodes.push({
        id,
        type: "gapNode",
        position: { x: 460, y: gapY },
        data: {
          label: g.claim_point,
          gapId: g.gap_id,
          kind: g.type,
          selected: id === selectedId,
        },
      });

      lookup[id] = {
        gapId: clean(g.gap_id),
        label: clean(g.claim_point) || clean(g.angle) || "（未命名结论点）",
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

      if (g.type === "contradiction") {
        asList(g.paper_refs).forEach((refTitle, ri) => {
          const pi = papers.findIndex((p) => clean(p.title) === clean(refTitle));
          if (pi >= 0) {
            edges.push({
              id: `${id}-in-${ri}`,
              source: paperIds[pi],
              target: id,
              type: "smoothstep",
              markerEnd: { type: MarkerType.ArrowClosed },
            });
          }
        });
      } else {
        paperIds.forEach((pid) => {
          edges.push({
            id: `${id}-${pid}`,
            source: pid,
            target: id,
            type: "smoothstep",
            markerEnd: { type: MarkerType.ArrowClosed },
          });
        });
      }
    });

    y += Math.max(papers.length * 100, gaps.length * 170) + 140;
  });

  return { nodes, edges, lookup };
}

export function EvidenceGraph() {
  const { literature } = useProject();
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const { nodes, edges, lookup } = useMemo(
    () => buildGraph(literature, selectedId),
    [literature, selectedId]
  );

  const selected = selectedId ? lookup[selectedId] : null;
  const hasNodes = nodes.length > 0;

  if (!hasNodes) {
    return <EmptyState title="暂无证据图" description="没有检索到论文或 gap，无法构建 Evidence Graph。" />;
  }

  return (
    <div className="grid gap-4 lg:grid-cols-[1fr_320px]">
      <div className="h-[460px] overflow-hidden rounded-xl border bg-card">
        <ReactFlow
          nodes={nodes}
          edges={edges}
          nodeTypes={nodeTypes}
          fitView
          minZoom={0.3}
          maxZoom={1.6}
          nodesDraggable={false}
          nodesConnectable={false}
          onNodeClick={(_e, node) => {
            if (node.type === "gapNode") setSelectedId(node.id);
          }}
          onPaneClick={() => setSelectedId(null)}
          proOptions={{ hideAttribution: true }}
        >
          <Background gap={20} />
          <Controls />
        </ReactFlow>
      </div>

      {/* gap 证据面板 */}
      <div className="rounded-xl border bg-card p-4">
        <h3 className="mb-2 text-sm font-semibold">Gap 证据</h3>
        {selected ? (
          <div className="space-y-2.5 text-sm">
            <div className="flex items-center gap-2">
              <Badge variant="secondary">{selected.gapId}</Badge>
              <Badge variant={selected.kind === "contradiction" ? "destructive" : "accent"}>
                {selected.kind === "contradiction" ? "矛盾" : "缺口"}
              </Badge>
              <Badge variant="outline">{gapEvidenceLabel(selected.evidenceLevel)}证据</Badge>
            </div>
            <div className="font-medium">{selected.label}</div>
            <dl className="space-y-1.5 text-xs text-muted-foreground">
              <div>
                <dt className="inline font-medium text-foreground">Evidence 强度：</dt>
                <dd className="inline">{gapEvidenceLabel(selected.evidenceLevel)}（coverage {selected.coverage} papers）</dd>
              </div>
              <div>
                <dt className="inline font-medium text-foreground">Why：</dt>
                <dd className="inline">{selected.description || "—"}</dd>
              </div>
              {selected.basis && (
                <div>
                  <dt className="inline font-medium text-foreground">依据：</dt>
                  <dd className="inline">{selected.basis}</dd>
                </div>
              )}
              {selected.scope && (
                <div>
                  <dt className="inline font-medium text-foreground">范围：</dt>
                  <dd className="inline">{selected.scope}</dd>
                </div>
              )}
            </dl>
            <div className="rounded-md bg-amber-50 px-2.5 py-2 text-xs text-amber-800 dark:bg-amber-900/30 dark:text-amber-200">
              ⚠ Based only on current retrieval——「没搜到 ≠ 不存在」，此 gap 是证据有界的假设。
            </div>
          </div>
        ) : (
          <p className="text-sm text-muted-foreground">点击右侧图中的 gap 节点，展开其证据（强度 / 范围 / 依据）。</p>
        )}
      </div>
    </div>
  );
}
