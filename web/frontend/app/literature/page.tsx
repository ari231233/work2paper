"use client";

import { useProject } from "@/hooks/use-project";
import { evidenceCoverage } from "@/lib/derive";
import { EvidenceGraph } from "@/components/literature/evidence-graph";
import { Landscape } from "@/components/literature/landscape";
import { Separator } from "@/components/ui/separator";

export default function LiteraturePage() {
  const { dossier } = useProject();

  return (
    <div className="space-y-6 p-6">
      <div>
        <h2 className="text-lg font-semibold">文献与证据（Literature &amp; Gap）</h2>
        <p className="text-sm text-muted-foreground">{evidenceCoverage(dossier?.literature)}</p>
      </div>

      <section>
        <h3 className="mb-3 text-sm font-semibold">证据图（Evidence Graph，点击 gap 展开证据）</h3>
        <EvidenceGraph />
      </section>

      <Separator />

      <section>
        <h3 className="mb-3 text-sm font-semibold">研究图景（Research Landscape，论文卡片）</h3>
        <Landscape />
      </section>
    </div>
  );
}
