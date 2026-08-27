"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";

import { api } from "@/lib/api";
import type {
  Dossier,
  GapPayload,
  HistoryResponse,
  IdeaWithEval,
  LiteratureEntry,
  ProjectSummary,
  RefineResponse,
  RetrieveMoreResponse,
  Roadmap,
  RunStatus,
  EvaluateResponse,
} from "@/lib/types";

type ActionKind = "refine" | "evaluate" | "retrieve-more" | "analyze";

interface ProjectContextValue {
  projects: ProjectSummary[];
  projectId: string | null;
  dossier: Dossier | null;
  decisionReport: string | null;
  status: RunStatus | null;
  ideas: IdeaWithEval[];
  literature: LiteratureEntry[];
  gaps: GapPayload[];
  roadmap: Roadmap | null;
  history: HistoryResponse | null;
  loading: boolean;
  error: string | null;
  /** 当前正在执行的操作（Ask PaperMine 快捷按钮反馈用） */
  action: ActionKind | null;
  selectProject: (id: string) => Promise<void>;
  refresh: () => Promise<void>;
  refineIdea: (ideaId: string) => Promise<RefineResponse>;
  evaluateIdea: (ideaId: string) => Promise<EvaluateResponse>;
  retrieveMore: (gapId: string) => Promise<RetrieveMoreResponse>;
  analyze: () => Promise<void>;
}

const ProjectContext = createContext<ProjectContextValue | null>(null);

export function ProjectProvider({ children }: { children: ReactNode }) {
  const [projects, setProjects] = useState<ProjectSummary[]>([]);
  const [projectId, setProjectId] = useState<string | null>(null);
  const [dossier, setDossier] = useState<Dossier | null>(null);
  const [decisionReport, setDecisionReport] = useState<string | null>(null);
  const [status, setStatus] = useState<RunStatus | null>(null);
  const [ideas, setIdeas] = useState<IdeaWithEval[]>([]);
  const [literature, setLiterature] = useState<LiteratureEntry[]>([]);
  const [gaps, setGaps] = useState<GapPayload[]>([]);
  const [roadmap, setRoadmap] = useState<Roadmap | null>(null);
  const [history, setHistory] = useState<HistoryResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [action, setAction] = useState<ActionKind | null>(null);
  const loadingRef = useRef(false);

  const loadProject = useCallback(async (id: string) => {
    loadingRef.current = true;
    setLoading(true);
    setError(null);
    try {
      const [proj, ideasRes, litRes, gapsRes, roadmapRes, histRes] = await Promise.all([
        api.getProject(id),
        api.getIdeas(id),
        api.getLiterature(id),
        api.getGaps(id),
        api.getRoadmap(id),
        api.getHistory(id),
      ]);
      setProjectId(id);
      setDossier(proj.dossier ?? null);
      setDecisionReport(proj.decision_report ?? null);
      setStatus((proj.status as RunStatus | null) ?? null);
      setIdeas(ideasRes.ideas ?? []);
      setLiterature(litRes.literature ?? []);
      setGaps(gapsRes.gaps ?? []);
      setRoadmap(roadmapRes.roadmap ?? null);
      setHistory(histRes ?? null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      loadingRef.current = false;
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const { projects: list } = await api.listProjects();
        if (cancelled) return;
        setProjects(list ?? []);
        const latest = list?.[0]?.project_id;
        if (latest) await loadProject(latest);
        else {
          setLoading(false);
          setError("暂无可用项目（本地没有 run）。请先运行后端 `python -m web` 或执行一次分析。");
        }
      } catch (err) {
        if (cancelled) return;
        setLoading(false);
        setError(err instanceof Error ? err.message : String(err));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [loadProject]);

  const selectProject = useCallback(
    async (id: string) => {
      await loadProject(id);
    },
    [loadProject]
  );

  const refresh = useCallback(async () => {
    if (projectId) await loadProject(projectId);
  }, [projectId, loadProject]);

  const runAction = useCallback(
    async <T,>(kind: ActionKind, fn: () => Promise<T>): Promise<T> => {
      setAction(kind);
      setError(null);
      try {
        const res = await fn();
        await refresh();
        return res;
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
        throw err;
      } finally {
        setAction(null);
      }
    },
    [refresh]
  );

  const refineIdea = useCallback(
    (ideaId: string) => {
      if (!projectId) return Promise.reject(new Error("无当前项目"));
      return runAction("refine", () => api.refineIdea(projectId, ideaId));
    },
    [projectId, runAction]
  );

  const evaluateIdea = useCallback(
    (ideaId: string) => {
      if (!projectId) return Promise.reject(new Error("无当前项目"));
      return runAction("evaluate", () => api.evaluateIdea(projectId, ideaId));
    },
    [projectId, runAction]
  );

  const retrieveMore = useCallback(
    (gapId: string) => {
      if (!projectId) return Promise.reject(new Error("无当前项目"));
      return runAction("retrieve-more", () => api.retrieveMore(projectId, gapId));
    },
    [projectId, runAction]
  );

  const analyze = useCallback(async () => {
    if (!projectId) return;
    await runAction("analyze", () => api.analyze(projectId));
  }, [projectId, runAction]);

  const value = useMemo<ProjectContextValue>(
    () => ({
      projects,
      projectId,
      dossier,
      decisionReport,
      status,
      ideas,
      literature,
      gaps,
      roadmap,
      history,
      loading,
      error,
      action,
      selectProject,
      refresh,
      refineIdea,
      evaluateIdea,
      retrieveMore,
      analyze,
    }),
    [
      projects,
      projectId,
      dossier,
      decisionReport,
      status,
      ideas,
      literature,
      gaps,
      roadmap,
      history,
      loading,
      error,
      action,
      selectProject,
      refresh,
      refineIdea,
      evaluateIdea,
      retrieveMore,
      analyze,
    ]
  );

  return <ProjectContext.Provider value={value}>{children}</ProjectContext.Provider>;
}

export function useProject(): ProjectContextValue {
  const ctx = useContext(ProjectContext);
  if (!ctx) throw new Error("useProject 必须在 <ProjectProvider> 内使用");
  return ctx;
}
