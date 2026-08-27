"use client";

import { Accordion, AccordionContent, AccordionItem, AccordionTrigger } from "@/components/ui/accordion";
import { Badge } from "@/components/ui/badge";
import { EmptyState } from "@/components/ui/empty-state";
import type { Experiment } from "@/lib/types";
import { join } from "@/lib/format";
import { asList, clean } from "@/lib/utils";

function Row({ label, value }: { label: string; value: string }) {
  if (!value) return null;
  return (
    <div className="flex gap-2 text-xs">
      <span className="w-20 shrink-0 text-muted-foreground">{label}</span>
      <span className="break-words">{value}</span>
    </div>
  );
}

/** Experiment Matrix（可展开，默认展开 E1 其余折叠）：实验 / 目的 / 自变量 / 对比模型 / 指标 / 对应 RQ。 */
export function ExperimentMatrix({ experiments }: { experiments?: Experiment[] | null }) {
  const exps = asList(experiments);
  if (!exps.length) {
    return <EmptyState title="暂无实验矩阵" description="路线图尚未生成实验计划。" />;
  }

  return (
    <Accordion type="multiple" defaultValue={["exp-0"]} className="w-full rounded-lg border px-4">
      {exps.map((e, i) => (
        <AccordionItem key={i} value={`exp-${i}`}>
          <AccordionTrigger className="text-left">
            <span className="flex flex-wrap items-center gap-2">
              <Badge variant="secondary">{clean(e.rq) || "—"}</Badge>
              <span className="font-medium">{clean(e.experiment) || "（未命名实验）"}</span>
            </span>
          </AccordionTrigger>
          <AccordionContent className="space-y-1.5 pb-4">
            <Row label="目的" value={clean(e.purpose)} />
            <Row label="自变量" value={clean(e.independent_variable)} />
            <Row label="对比模型" value={join(e.baselines)} />
            <Row label="指标" value={join(e.metrics)} />
            <Row label="对应 RQ" value={clean(e.rq)} />
          </AccordionContent>
        </AccordionItem>
      ))}
    </Accordion>
  );
}
