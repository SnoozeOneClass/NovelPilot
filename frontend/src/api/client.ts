import type {
  LlmProfileMutation,
  LlmProfilePublic,
  LlmProfileTestResult,
  LlmProfilesDocument,
  LiteraryReviewRecord,
  LiteraryReviewRequest,
  CurrentArcApprovalResponse,
  CurrentArcState,
  ChapterRetryResponse,
  ArtifactSummary,
  OperationMode,
  ProjectCompletionAudit,
  ProjectReadiness,
  ProjectSummary,
  SetupStateDocument,
  StaleRunRecoveryResponse
} from "../types/domain";

const jsonHeaders = { "Content-Type": "application/json" };
const configuredApiBase = import.meta.env.VITE_API_BASE_URL?.trim().replace(/\/$/, "");
const apiBase = configuredApiBase || (import.meta.env.DEV ? "http://127.0.0.1:8000" : "");

export function apiUrl(path: string): string {
  return `${apiBase}${path}`;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function extractErrorDetail(value: unknown): string | null {
  if (typeof value === "string") {
    return value;
  }
  if (Array.isArray(value)) {
    const parts = value
      .map((item) => extractErrorDetail(item))
      .filter((part): part is string => Boolean(part));
    return parts.length > 0 ? parts.join("; ") : null;
  }
  if (!isRecord(value)) {
    return null;
  }

  const detail = extractErrorDetail(value.detail);
  if (detail) {
    return detail;
  }
  if (typeof value.message === "string") {
    return value.message;
  }
  if (typeof value.msg === "string") {
    return value.msg;
  }
  return null;
}

async function readErrorMessage(response: Response): Promise<string> {
  const fallback = `Request failed: ${response.status}`;
  try {
    const contentType = response.headers.get("content-type") ?? "";
    if (contentType.includes("application/json")) {
      const detail = extractErrorDetail(await response.json());
      return detail ?? fallback;
    }
    return (await response.text()) || fallback;
  } catch {
    return fallback;
  }
}

export function formatApiError(error: unknown): string {
  return error instanceof Error ? error.message : "Request failed.";
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(apiUrl(path), init);
  if (!response.ok) {
    throw new Error(await readErrorMessage(response));
  }
  return (await response.json()) as T;
}

export const api = {
  listProjects: () => request<ProjectSummary[]>("/api/projects"),
  activeProject: () => request<ProjectSummary | null>("/api/projects/active"),
  createProject: (title: string, operation_mode: OperationMode) =>
    request<ProjectSummary>("/api/projects", {
      method: "POST",
      headers: jsonHeaders,
      body: JSON.stringify({ title, operation_mode })
    }),
  openProject: (name: string) =>
    request<ProjectSummary>("/api/projects/open", {
      method: "POST",
      headers: jsonHeaders,
      body: JSON.stringify({ name })
    }),
  closeProject: () => request<{ closed: boolean }>("/api/projects/close", { method: "POST" }),
  profiles: () => request<LlmProfilesDocument>("/api/profiles"),
  upsertProfile: (payload: LlmProfileMutation) =>
    request<LlmProfilePublic>("/api/profiles", {
      method: "POST",
      headers: jsonHeaders,
      body: JSON.stringify(payload)
    }),
  selectProfile: (profileId: string) =>
    request<LlmProfilesDocument>(`/api/profiles/${encodeURIComponent(profileId)}/select`, {
      method: "POST"
    }),
  testProfile: (profileId: string) =>
    request<LlmProfileTestResult>(`/api/profiles/${encodeURIComponent(profileId)}/test`, {
      method: "POST"
    }),
  setupState: () => request<SetupStateDocument>("/api/setup/state"),
  answerSetup: (question_id: string, answer: string) =>
    request<SetupStateDocument>("/api/setup/answer", {
      method: "POST",
      headers: jsonHeaders,
      body: JSON.stringify({ question_id, answer })
    }),
  approveSetup: () => request<SetupStateDocument>("/api/setup/approve", { method: "POST" }),
  currentArc: () => request<CurrentArcState | null>("/api/arcs/current"),
  approveCurrentArc: () =>
    request<CurrentArcApprovalResponse>("/api/arcs/current/approve", { method: "POST" }),
  startRun: () => request<{ run_id: string; status: string }>("/api/runs/start", { method: "POST" }),
  pauseRun: () => request<{ status: string }>("/api/runs/pause", { method: "POST" }),
  resumeRun: () => request<{ status: string }>("/api/runs/resume", { method: "POST" }),
  recoverStaleRun: () =>
    request<StaleRunRecoveryResponse>("/api/runs/recover-stale", { method: "POST" }),
  retryCurrentChapter: () =>
    request<ChapterRetryResponse>("/api/runs/retry-current-chapter", { method: "POST" }),
  submitFeedback: (message: string) =>
    request<{ recorded: boolean }>("/api/feedback", {
      method: "POST",
      headers: jsonHeaders,
      body: JSON.stringify({ message })
    }),
  exportManuscript: () =>
    request<{ artifact_path: string }>("/api/export/manuscript", { method: "POST" }),
  readiness: () => request<ProjectReadiness>("/api/readiness"),
  completionAudit: () => request<ProjectCompletionAudit>("/api/completion/audit"),
  recordLiteraryReview: (payload: LiteraryReviewRequest) =>
    request<LiteraryReviewRecord>("/api/completion/literary-review", {
      method: "POST",
      headers: jsonHeaders,
      body: JSON.stringify(payload)
    }),
  listArtifacts: () => request<string[]>("/api/artifacts"),
  artifactSummaries: () => request<ArtifactSummary[]>("/api/artifacts/summary"),
  artifactContent: (path: string) =>
    request<{ path: string; content: string }>(`/api/artifacts/content?path=${encodeURIComponent(path)}`)
};
