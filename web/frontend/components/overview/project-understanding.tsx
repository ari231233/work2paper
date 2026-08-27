"use client";

import { BookOpenText } from "lucide-react";

import { useProject } from "@/hooks/use-project";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { clean } from "@/lib/utils";

/** Overview「项目理解」区：展示完整项目叙事（顶栏只保留派生短名+摘要）。 */
export function ProjectUnderstanding() {
  const { dossier } = useProject();
  const narrative = clean(dossier?.assets?.narrative);
  const facts = dossier?.assets?.facts;

  const tags: { label: string; items?: string[] }[] = [
    { label: "任务", items: facts?.tasks },
    { label: "方法", items: facts?.methods },
    { label: "数据", items: facts?.data },
    { label: "场景", items: facts?.scenarios },
    { label: "指标", items: facts?.metrics },
  ];

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="flex items-center gap-2 text-base">
          <BookOpenText className="h-4 w-4 text-primary" /> 项目理解（Project Understanding）
        </CardTitle>
        <CardDescription>完整项目叙事（由项目理解 Agent 生成）</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {narrative ? (
          <p className="text-sm leading-relaxed text-muted-foreground">{narrative}</p>
        ) : (
          <p className="text-sm text-muted-foreground">（无项目叙事）</p>
        )}

        <div className="flex flex-wrap gap-x-4 gap-y-1.5">
          {tags
            .filter((t) => t.items?.length)
            .map((t) => (
              <span key={t.label} className="flex flex-wrap items-center gap-1 text-xs">
                <span className="text-muted-foreground">{t.label}：</span>
                {t.items!.slice(0, 6).map((item) => (
                  <Badge key={item} variant="outline">
                    {item}
                  </Badge>
                ))}
              </span>
            ))}
        </div>
      </CardContent>
    </Card>
  );
}
