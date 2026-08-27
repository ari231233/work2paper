"use client";

import {
  PolarAngleAxis,
  PolarGrid,
  PolarRadiusAxis,
  Radar,
  RadarChart,
  ResponsiveContainer,
  Tooltip,
} from "recharts";

import { radarData } from "@/lib/derive";
import type { Evaluation } from "@/lib/types";

export function ContributionRadar({ evaluation }: { evaluation?: Evaluation | null }) {
  const data = radarData(evaluation);

  return (
    <div className="h-72 w-full">
      <ResponsiveContainer width="100%" height="100%">
        <RadarChart data={data} cx="50%" cy="50%" outerRadius="72%">
          <PolarGrid stroke="hsl(var(--border))" />
          <PolarAngleAxis dataKey="dim" tick={{ fontSize: 12 }} />
          <PolarRadiusAxis domain={[0, 100]} tickCount={5} tick={{ fontSize: 10 }} />
          <Radar
            name="贡献强度"
            dataKey="score"
            stroke="hsl(var(--primary))"
            fill="hsl(var(--primary))"
            fillOpacity={0.35}
            isAnimationActive
          />
          <Tooltip
            formatter={(value, _name, props) => {
              const item = (props as { payload?: { strength?: string } } | undefined)?.payload?.strength;
              return [`${value}（${item ?? "—"}）`, "贡献强度"];
            }}
          />
        </RadarChart>
      </ResponsiveContainer>
    </div>
  );
}
