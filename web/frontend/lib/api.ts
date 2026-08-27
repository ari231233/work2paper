// M24 FastAPI 的薄封装 fetch client。
// 前端只经 API 访问（不直接碰 Dossier / Agent），与 docs/web-demo.md「Next.js → REST API → FastAPI」一致。

import type {
  EvaluateResponse,
  GapsResponse,
  HistoryResponse,
  IdeasResponse,
  LiteratureResponse,
  ProjectPayload,
  ProjectsResponse,
  RefineResponse,
  RetrieveMoreResponse,
  RoadmapResponse,
} from "./types";

const BASE_URL = (
  process.env.NEXT_PUBLIC_API_BASE_URL || "http://127.0.0.1:8000"
).replace(/\/+$/, "");

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${BASE_URL}${path}`, {
      ...init,
      headers: { "Content-Type": "application/json", ...(init?.headers || {}) },
    });
  } catch (err) {
    throw new ApiError(0, `无法连接后端（${BASE_URL}）。请先运行 \`python -m web\`。`);
  }
  if (!res.ok) {
    let detail = "";
    try {
      const body = await res.json();
      detail = typeof body?.detail === "string" ? body.detail : JSON.stringify(body);
    } catch {
      /* ignore */
    }
    throw new ApiError(res.status, detail || `HTTP ${res.status}`);
  }
  return (await res.json()) as T;
}

export const api = {
  baseUrl: BASE_URL,

  health: () => request<{ status: string; service: string }>("/health"),

  listProjects: () => request<ProjectsResponse>("/projects"),

  getProject: (id: string) => request<ProjectPayload>(`/projects/${id}`),
  getIdeas: (id: string) => request<IdeasResponse>(`/projects/${id}/ideas`),
  getIdea: (id: string, ideaId: string) =>
    request<{ idea: unknown; evaluation?: unknown }>(`/projects/${id}/ideas/${ideaId}`),
  getLiterature: (id: string) => request<LiteratureResponse>(`/projects/${id}/literature`),
  getGaps: (id: string) => request<GapsResponse>(`/projects/${id}/gaps`),
  getRoadmap: (id: string) => request<RoadmapResponse>(`/projects/${id}/roadmap`),
  getHistory: (id: string) => request<HistoryResponse>(`/projects/${id}/history`),

  createProject: (projectDir: string) =>
    request<ProjectPayload>("/projects", { method: "POST", body: JSON.stringify({ project_dir: projectDir }) }),
  analyze: (id: string) => request<ProjectPayload>(`/projects/${id}/analyze`, { method: "POST" }),

  refineIdea: (id: string, ideaId: string) =>
    request<RefineResponse>(`/projects/${id}/ideas/${ideaId}/refine`, { method: "POST" }),
  evaluateIdea: (id: string, ideaId: string) =>
    request<EvaluateResponse>(`/projects/${id}/ideas/${ideaId}/evaluate`, { method: "POST" }),
  retrieveMore: (id: string, gapId: string) =>
    request<RetrieveMoreResponse>(`/projects/${id}/gaps/${gapId}/retrieve-more`, { method: "POST" }),
};
