import type {
  LlmProfileMutation,
  LlmProfilePublic,
  LlmProfileTestResult,
  LlmProfilesDocument,
  LiteraryReviewRecord,
  LiteraryReviewRequest,
  CurrentArcApprovalResponse,
  CurrentArcState,
  DeleteProjectsResponse,
  ExperimentFixtureCreateResponse,
  ExperimentFixtureStatus,
  ChapterRetryResponse,
  ArtifactSummary,
  AgentPolicy,
  BookRevisionState,
  OperationMode,
  ProjectCompletionAudit,
  ProjectReadiness,
  ProjectSummary,
  RunCommandResponse,
  SetupStateDocument,
  StaleRunRecoveryResponse
} from "../types/domain";

const jsonHeaders = { "Content-Type": "application/json" };
const projectListTimeoutMs = 5_000;
const configuredApiBase = import.meta.env.VITE_API_BASE_URL?.trim().replace(/\/$/, "");
const apiBase = configuredApiBase || "";

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
  const message = typeof value.message === "string" ? value.message : null;
  const issues = extractErrorDetail(value.issues);
  if (message && issues) return `${message} ${issues}`;
  if (message) return message;
  if (issues) return issues;
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

async function request<T>(path: string, init?: RequestInit, timeoutMs?: number): Promise<T> {
  const timeoutController = timeoutMs ? new AbortController() : null;
  const timeoutId = timeoutController
    ? window.setTimeout(() => timeoutController.abort(), timeoutMs)
    : null;
  try {
    const response = await fetch(apiUrl(path), {
      ...init,
      signal: timeoutController?.signal ?? init?.signal
    });
    if (!response.ok) {
      throw new Error(await readErrorMessage(response));
    }
    return (await response.json()) as T;
  } catch (error) {
    if (timeoutController?.signal.aborted) {
      throw new Error("连接本地服务超时，请确认后端已经启动。", { cause: error });
    }
    throw error;
  } finally {
    if (timeoutId !== null) window.clearTimeout(timeoutId);
  }
}

export const api = {
  listProjects: () => request<ProjectSummary[]>("/api/projects", undefined, projectListTimeoutMs),
  activeProject: () => request<ProjectSummary | null>("/api/projects/active"),
  createProject: (operation_mode: OperationMode) =>
    request<ProjectSummary>("/api/projects", {
      method: "POST",
      headers: jsonHeaders,
      body: JSON.stringify({ operation_mode })
    }),
  openProject: (name: string) =>
    request<ProjectSummary>("/api/projects/open", {
      method: "POST",
      headers: jsonHeaders,
      body: JSON.stringify({ name })
    }),
  closeProject: () => request<{ closed: boolean }>("/api/projects/close", { method: "POST" }),
  deleteProjects: (project_ids: string[]) =>
    request<DeleteProjectsResponse>("/api/projects/delete", {
      method: "POST",
      headers: jsonHeaders,
      body: JSON.stringify({ project_ids })
    }),
  updateProjectMode: (operation_mode: OperationMode) =>
    request<ProjectSummary>("/api/projects/active/mode", {
      method: "PATCH",
      headers: jsonHeaders,
      body: JSON.stringify({ operation_mode })
    }),
  updateAgentPolicy: (agent_policy: AgentPolicy) =>
    request<ProjectSummary>("/api/projects/active/agent-policy", {
      method: "PATCH",
      headers: jsonHeaders,
      body: JSON.stringify({ agent_policy })
    }),
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
  continueSetupDiscussion: (message: string) =>
    request<SetupStateDocument>("/api/setup/turn", {
      method: "POST",
      headers: jsonHeaders,
      body: JSON.stringify({ message })
    }),
  prepareSetupReview: () =>
    request<SetupStateDocument>("/api/setup/prepare-review", { method: "POST" }),
  approveSetup: (candidate_revision: number, title: string) =>
    request<SetupStateDocument>("/api/setup/approve", {
      method: "POST",
      headers: jsonHeaders,
      body: JSON.stringify({ candidate_revision, title })
    }),
  pendingBookRevision: () =>
    request<BookRevisionState | null>("/api/book-revisions/pending"),
  approveBookRevision: (revision_id: string, expected_base_book_version: number) =>
    request<BookRevisionState>("/api/book-revisions/approve", {
      method: "POST",
      headers: jsonHeaders,
      body: JSON.stringify({ revision_id, expected_base_book_version })
    }),
  currentArc: () => request<CurrentArcState | null>("/api/arcs/current"),
  approveCurrentArc: (target_chapter_count: number) =>
    request<CurrentArcApprovalResponse>("/api/arcs/current/approve", {
      method: "POST",
      headers: jsonHeaders,
      body: JSON.stringify({ target_chapter_count })
    }),
  experimentFixtureStatus: () =>
    request<ExperimentFixtureStatus>("/api/experiments/fixtures/status"),
  freezeExperimentFixture: () =>
    request<ExperimentFixtureCreateResponse>("/api/experiments/fixtures", {
      method: "POST"
    }),
  startRun: () => request<RunCommandResponse>("/api/runs/start", { method: "POST" }),
  pauseRun: () => request<{ status: string }>("/api/runs/pause", { method: "POST" }),
  resumeRun: () => request<RunCommandResponse>("/api/runs/resume", { method: "POST" }),
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
