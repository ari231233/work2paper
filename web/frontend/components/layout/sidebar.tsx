"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { FlaskConical, FolderUp, LayoutDashboard, Lightbulb, Map, ScrollText } from "lucide-react";

import { cn } from "@/lib/utils";
import { PipelineProgress } from "./pipeline-progress";

const NAV = [
  { href: "/", label: "总览", sub: "Overview", icon: LayoutDashboard },
  { href: "/literature", label: "文献与证据", sub: "Literature & Gap", icon: ScrollText },
  { href: "/ideas", label: "候选创新点", sub: "Ideas", icon: Lightbulb },
  { href: "/roadmap", label: "路线图", sub: "Roadmap", icon: Map },
];

export function Sidebar() {
  const pathname = usePathname();

  return (
    <aside className="flex h-full w-60 shrink-0 flex-col border-r bg-card">
      <div className="flex items-center gap-2 px-4 py-4">
        <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-primary text-primary-foreground">
          <FlaskConical className="h-4 w-4" />
        </div>
        <div className="leading-tight">
          <div className="text-sm font-semibold">PaperMine</div>
          <div className="text-xs text-muted-foreground">科研决策工作台</div>
        </div>
      </div>

      <nav className="flex flex-1 flex-col gap-1 px-3 py-2">
        <Link
          href="/import"
          className={cn(
            "mb-2 flex items-center gap-2 rounded-lg border px-3 py-2 text-sm font-medium transition-colors",
            pathname === "/import"
              ? "border-primary bg-primary text-primary-foreground"
              : "border-primary/30 bg-primary/5 text-primary hover:bg-primary/10"
          )}
        >
          <FolderUp className="h-4 w-4" />
          新建分析
        </Link>
        {NAV.map((item) => {
          const active = item.href === "/" ? pathname === "/" : pathname.startsWith(item.href);
          const Icon = item.icon;
          return (
            <Link
              key={item.href}
              href={item.href}
              className={cn(
                "flex items-center gap-3 rounded-lg px-3 py-2 text-sm transition-colors",
                active
                  ? "bg-accent text-accent-foreground"
                  : "text-muted-foreground hover:bg-muted hover:text-foreground"
              )}
            >
              <Icon className="h-4 w-4" />
              <span className="flex-1">
                <span className="block font-medium leading-tight">{item.label}</span>
                <span className="block text-xs opacity-70">{item.sub}</span>
              </span>
            </Link>
          );
        })}
      </nav>

      <div className="border-t p-4">
        <PipelineProgress />
      </div>
    </aside>
  );
}
