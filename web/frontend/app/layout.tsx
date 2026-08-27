import type { Metadata } from "next";

import "./globals.css";
import { ProjectProvider } from "@/hooks/use-project";
import { AppShell } from "@/components/layout/app-shell";

export const metadata: Metadata = {
  title: "PaperMine 科研决策工作台",
  description: "把横向项目工作挖掘成候选论文点，并给出可执行的论文路线图（科研决策工作台）。",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN">
      <body>
        <ProjectProvider>
          <AppShell>{children}</AppShell>
        </ProjectProvider>
      </body>
    </html>
  );
}
