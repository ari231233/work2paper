// M24 FastAPI 的薄封装 fetch client。
// 前端只经 API 访问（不直接碰 Dossier / Agent），与 docs/web-demo.md「Next.js → REST API → FastAPI」一致。

import type {
  EvaluateResponse,
  GapsResponse,
  HistoryResponse,
  ImportAnalyzeResponse,
  ImportRecord,
  IdeasResponse,
  LiteratureResponse,
  ProjectPayload,
  ProjectsResponse,
  RefineResponse,
  RetrieveMoreResponse,
  RoadmapResponse,
} from "./types";

// M26：浏览器统一访问同源 /api，由 Next.js 在运行时代理到 FastAPI。
// 避免把后端端口烘焙进前端构建产物，也消除本地 CORS/双端口配置负担。
const BASE_URL = "/api";

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
    const isForm = typeof FormData !== "undefined" && init?.body instanceof FormData;
    res = await fetch(`${BASE_URL}${path}`, {
      ...init,
      headers: isForm
        ? init?.headers
        : { "Content-Type": "application/json", ...(init?.headers || {}) },
    });
  } catch (err) {
    throw new ApiError(0, `无法连接后端（${BASE_URL}）。请先运行 \`python -m papermine web\`。`);
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

function uploadRequest<T>(path: string, body: FormData, onProgress?: (percent: number) => void): Promise<T> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `${BASE_URL}${path}`);
    xhr.responseType = "text";
    xhr.upload.onprogress = (event) => {
      if (!event.lengthComputable) return;
      onProgress?.(Math.min(100, Math.round((event.loaded / event.total) * 100)));
    };
    xhr.onerror = () => reject(
      new ApiError(0, `无法连接后端（${BASE_URL}）。请先运行 \`python -m papermine web\`。`),
    );
    xhr.onload = () => {
      const payload = (() => {
        try { return JSON.parse(xhr.responseText || "{}"); } catch { return {}; }
      })();
      if (xhr.status < 200 || xhr.status >= 300) {
        const detail = typeof payload?.detail === "string" ? payload.detail : `HTTP ${xhr.status}`;
        reject(new ApiError(xhr.status, detail));
        return;
      }
      onProgress?.(100);
      resolve(payload as T);
    };
    xhr.send(body);
  });
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

  importFolder: (files: File[], paths: string[], projectName?: string, onProgress?: (percent: number) => void) => {
    const body = new FormData();
    files.forEach((file) => body.append("files", file, file.name));
    paths.forEach((path) => body.append("paths", path));
    if (projectName?.trim()) body.append("project_name", projectName.trim());
    return uploadRequest<ImportRecord>("/imports/folder", body, onProgress);
  },
  importArchive: (file: File, projectName?: string, onProgress?: (percent: number) => void) => {
    const body = new FormData();
    body.append("file", file, file.name);
    if (projectName?.trim()) body.append("project_name", projectName.trim());
    return uploadRequest<ImportRecord>("/imports/archive", body, onProgress);
  },
  getImport: (id: string) => request<ImportRecord>(`/imports/${id}`),
  analyzeImport: (id: string) =>
    request<ImportAnalyzeResponse>(`/imports/${id}/analyze`, {
      method: "POST",
      body: JSON.stringify({ auto: true }),
    }),
};
