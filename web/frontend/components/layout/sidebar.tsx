"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { FlaskConical, LayoutDashboard, Lightbulb, Map, ScrollText } from "lucide-react";

import { cn } from "@/lib/utils";
import { PipelineProgress } from "./pipeline-progress";

const NAV = [
  { href: "/", label: "Overview", sub: "推荐与结论", icon: LayoutDashboard },
  { href: "/literature", label: "Literature & Gap", sub: "文献与证据", icon: ScrollText },
  { href: "/ideas", label: "Ideas", sub: "候选创新点", icon: Lightbulb },
  { href: "/roadmap", label: "Roadmap", sub: "路线图", icon: Map },
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
