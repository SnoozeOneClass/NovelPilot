import type {
  CreateProjectInput,
  ManuscriptExportResult,
  MutationResponse,
  OperationMode,
  ProfileListResponse,
  ProjectListItem,
  ProjectStateView
} from "../types/workspace";

const configuredApiBase = import.meta.env.VITE_API_BASE_URL?.trim().replace(/\/$/, "");
const apiBase = configuredApiBase || "";

function apiUrl(path: string): string {
  return `${apiBase}${path}`;
}

function mutationHeaders(idempotencyKey: string): HeadersInit {
  return {
    "Content-Type": "application/json",
    "Idempotency-Key": idempotencyKey
  };
}

async function readError(response: Response): Promise<string> {
  try {
    const value = await response.json() as { error?: { message?: string } };
    return value.error?.message ?? `请求失败：${response.status}`;
  } catch {
    return `请求失败：${response.status}`;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(apiUrl(path), init);
  if (!response.ok) throw new Error(await readError(response));
  return await response.json() as T;
}

function postMutation<T>(
  path: string,
  idempotencyKey: string,
  body?: T
): Promise<MutationResponse> {
  return request<MutationResponse>(path, {
    method: "POST",
    headers: mutationHeaders(idempotencyKey),
    body: body === undefined ? undefined : JSON.stringify(body)
  });
}

export const workspaceApi = {
  listProjects: () => request<ProjectListItem[]>("/api/projects"),
  getProject: (projectId: string) =>
    request<ProjectStateView>(`/api/projects/${encodeURIComponent(projectId)}`),
  profiles: () => request<ProfileListResponse>("/api/profiles"),
  createProject: (input: CreateProjectInput, idempotencyKey: string) =>
    postMutation("/api/projects", idempotencyKey, input),
  deleteProject: async (projectId: string, idempotencyKey: string) => {
    return await request<{ project_id: string; deleted: boolean }>(
      `/api/projects/${encodeURIComponent(projectId)}`,
      { method: "DELETE", headers: { "Idempotency-Key": idempotencyKey } }
    );
  },
  updateSettings: (
    projectId: string,
    input: {
      expected_lock_version: number;
      operation_mode: OperationMode;
      default_profile_id: string | null;
      book_profile_id: string | null;
      arc_profile_id: string | null;
      chapter_profile_id: string | null;
      evaluator_profile_id: string | null;
    },
    idempotencyKey: string
  ) => request<MutationResponse>(`/api/projects/${encodeURIComponent(projectId)}/settings`, {
    method: "PUT",
    headers: mutationHeaders(idempotencyKey),
    body: JSON.stringify(input)
  }),
  runControl: (
    projectId: string,
    action: "start" | "pause" | "resume" | "retry",
    expectedLockVersion: number,
    idempotencyKey: string
  ) => postMutation(
    `/api/projects/${encodeURIComponent(projectId)}/run/${action}`,
    idempotencyKey,
    { expected_lock_version: expectedLockVersion }
  ),
  sendBookInput: (
    projectId: string,
    input: {
      expected_workspace_lock_version: number;
      message: string;
      suggestion_id?: string;
    },
    idempotencyKey: string
  ) => postMutation(
    `/api/projects/${encodeURIComponent(projectId)}/book/input`,
    idempotencyKey,
    input
  ),
  approveBook: (projectId: string, idempotencyKey: string) =>
    postMutation(`/api/projects/${encodeURIComponent(projectId)}/book/approve`, idempotencyKey),
  approveArc: (
    projectId: string,
    targetChapterCount: number | null,
    idempotencyKey: string
  ) => postMutation(
    `/api/projects/${encodeURIComponent(projectId)}/arc/approve`,
    idempotencyKey,
    { target_chapter_count: targetChapterCount }
  ),
  submitFeedback: (
    projectId: string,
    input: {
      content: string;
      route_layer: "book" | "arc" | "chapter";
      expected_workspace_lock_version: number;
    },
    idempotencyKey: string
  ) => postMutation(
    `/api/projects/${encodeURIComponent(projectId)}/feedback`,
    idempotencyKey,
    input
  ),
  exportManuscript: (projectId: string) =>
    request<ManuscriptExportResult>(
      `/api/projects/${encodeURIComponent(projectId)}/export`,
      { method: "POST" }
    ),
  eventStreamUrl: (projectId: string, after: number) =>
    apiUrl(`/api/projects/${encodeURIComponent(projectId)}/events/stream?after=${after}`)
};
