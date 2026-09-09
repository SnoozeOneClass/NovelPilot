import {
  decodeAuthoringProfiles,
  decodeAuthoringProject,
  decodeAuthoringProjects,
  decodeApiErrorMessage,
  decodeRunAction,
  decodeModelSettings,
  type AuthoringProfile,
  type AuthoringProject,
  type CreateAuthoringProject,
  type RunAction,
  type ModelSettingsDocument,
  type SaveModelProfile,
  type UpdateAuthoringProfiles,
  type ManuscriptSnapshot
} from "../types/authoring";

type Decoder<T> = (value: unknown) => T | null;

function apiUrl(path: string): string {
  return path;
}

async function request<T>(path: string, decoder: Decoder<T>, init?: RequestInit, secret?: string): Promise<T> {
  const response = await fetch(apiUrl(path), init);
  const value: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = decodeApiErrorMessage(value) ?? `请求失败（${response.status}）`;
    throw new Error(secret ? detail.split(secret).join("[已隐藏密钥]") : detail);
  }
  const decoded = decoder(value);
  if (decoded === null) throw new Error("服务返回了无法识别的数据");
  return decoded;
}

function exportUrl(projectId: string, format: "markdown" | "txt"): string {
  return apiUrl(`/api/authoring/projects/${encodeURIComponent(projectId)}/export?format=${format}`);
}

function json(method: string, body?: unknown): RequestInit {
  return {
    method,
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body)
  };
}

export const authoringApi = {
  listProjects: (): Promise<AuthoringProject[]> =>
    request("/api/authoring/projects", decodeAuthoringProjects),
  getProject: (projectId: string): Promise<AuthoringProject> =>
    request(`/api/authoring/projects/${encodeURIComponent(projectId)}`, decodeAuthoringProject),
  createProject: (body: CreateAuthoringProject): Promise<AuthoringProject> =>
    request("/api/authoring/projects", decodeAuthoringProject, json("POST", body)),
  startOrResume: (projectId: string): Promise<RunAction> =>
    request(`/api/authoring/projects/${encodeURIComponent(projectId)}/run`, decodeRunAction, json("POST")),
  pause: (projectId: string): Promise<RunAction> =>
    request(`/api/authoring/projects/${encodeURIComponent(projectId)}/pause`, decodeRunAction, json("POST")),
  cancel: (projectId: string): Promise<RunAction> =>
    request(`/api/authoring/projects/${encodeURIComponent(projectId)}/cancel`, decodeRunAction, json("POST")),
  updateProfiles: (projectId: string, body: UpdateAuthoringProfiles): Promise<AuthoringProject> =>
    request(`/api/authoring/projects/${encodeURIComponent(projectId)}/profiles`, decodeAuthoringProject, json("PUT", body)),
  profiles: (): Promise<AuthoringProfile[]> =>
    request("/api/authoring/profiles", decodeAuthoringProfiles),
  modelSettings: (): Promise<ModelSettingsDocument> =>
    request("/api/authoring/model-settings", decodeModelSettings),
  saveModelProfile: (profileId: string, body: SaveModelProfile): Promise<ModelSettingsDocument> =>
    request(`/api/authoring/model-settings/profiles/${encodeURIComponent(profileId)}`,
      decodeModelSettings, json("PUT", body), body.api_key),
  validateModelProfile: (profileId: string): Promise<ModelSettingsDocument> =>
    request(`/api/authoring/model-settings/profiles/${encodeURIComponent(profileId)}/validate`,
      decodeModelSettings, json("POST")),
  selectDefaultProfile: (profileId: string): Promise<ModelSettingsDocument> =>
    request("/api/authoring/model-settings/default", decodeModelSettings, json("PUT", { profile_id: profileId })),
  manuscript: async (projectId: string, signal?: AbortSignal): Promise<ManuscriptSnapshot> => {
    const response = await fetch(exportUrl(projectId, "txt"), { signal });
    if (!response.ok) {
      const value: unknown = await response.json().catch(() => null);
      throw new Error(decodeApiErrorMessage(value) ?? `正文读取失败（${response.status}）`);
    }
    return { text: await response.text(), etag: response.headers.get("ETag") };
  },
  eventStreamUrl: (projectId: string, after: number): string =>
    apiUrl(`/api/authoring/projects/${encodeURIComponent(projectId)}/events?after=${after}`),
  exportUrl
};
