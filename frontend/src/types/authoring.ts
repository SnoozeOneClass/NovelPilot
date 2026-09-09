export type AuthoringStatus =
  | "ready"
  | "running"
  | "paused"
  | "failure_paused"
  | "cancelled"
  | "completed";

export interface AuthoringActivity {
  sequence: number;
  kind: string;
  payload: Record<string, unknown>;
  created_at: string;
}

export interface AuthoringProject {
  project_id: string;
  brief: string;
  title: string | null;
  status: AuthoringStatus;
  stage: "preparing" | "writing" | "finishing" | "complete";
  target_chapters: number;
  target_words: number | null;
  completed_chapters: number;
  progress_percent: number;
  failure_reason: string | null;
  latest_event_sequence: number;
  profile_bindings: Record<string, string>;
  recent_activity: AuthoringActivity[];
}

export interface AuthoringEvent extends AuthoringActivity {
  project_id: string;
}

export interface AuthoringProfile {
  profile_id: string;
  display_name: string;
  model_id: string;
  capability_status: "missing" | "stale" | "ready";
  context_window: number | null;
  max_output_tokens: number | null;
}

export interface ModelSettingsProfile extends AuthoringProfile {
  api_family: "openai_responses" | "anthropic_messages";
  base_url: string;
  enabled: boolean;
  has_api_key: boolean;
  request_options: Record<string, unknown>;
  input_price_per_million: number;
  output_price_per_million: number;
  cache_price_per_million: number;
  metadata_version: number | null;
}

export interface ModelSettingsDocument {
  selected_profile_id: string | null;
  profiles: ModelSettingsProfile[];
}

export interface SaveModelProfile {
  display_name: string;
  api_family: ModelSettingsProfile["api_family"];
  base_url: string;
  model_id: string;
  enabled: boolean;
  api_key?: string;
  request_options: Record<string, unknown>;
  context_window: number;
  max_output_tokens: number;
  input_price_per_million: number;
  output_price_per_million: number;
  cache_price_per_million: number;
}

export interface UpdateAuthoringProfiles {
  default_profile_id: string | null;
  architect_profile_id: string | null;
  writer_profile_id: string | null;
  editor_profile_id: string | null;
  arbiter_profile_id: string | null;
  fallback_profile_id: string | null;
  architect_fallback_profile_id: string | null;
  writer_fallback_profile_id: string | null;
  editor_fallback_profile_id: string | null;
  arbiter_fallback_profile_id: string | null;
}

export interface ManuscriptSnapshot {
  text: string;
  etag: string | null;
}

export interface CreateAuthoringProject {
  brief: string;
  target_chapters?: number;
  target_words?: number;
  default_profile_id?: string;
  architect_profile_id?: string;
  writer_profile_id?: string;
  editor_profile_id?: string;
}

export interface RunAction {
  accepted: boolean;
  ownership: "started_here" | "already_running_here" | "owned_elsewhere";
  project: AuthoringProject;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isNullableString(value: unknown): value is string | null {
  return value === null || typeof value === "string";
}

function isInteger(value: unknown, minimum = 0): value is number {
  return typeof value === "number" && Number.isInteger(value) && value >= minimum;
}

function decodeActivity(value: unknown): AuthoringActivity | null {
  if (!isRecord(value) || !isInteger(value.sequence, 1)
    || typeof value.kind !== "string" || !isRecord(value.payload)
    || typeof value.created_at !== "string") return null;
  return {
    sequence: value.sequence,
    kind: value.kind,
    payload: value.payload,
    created_at: value.created_at
  };
}

export function decodeAuthoringProject(value: unknown): AuthoringProject | null {
  if (!isRecord(value) || typeof value.project_id !== "string"
    || typeof value.brief !== "string" || !isNullableString(value.title)
    || typeof value.status !== "string" || typeof value.stage !== "string"
    || !isInteger(value.target_chapters, 1) || !isNullableString(value.failure_reason)
    || !(value.target_words === null || isInteger(value.target_words, 1))
    || !isInteger(value.completed_chapters) || !isInteger(value.progress_percent)
    || value.progress_percent > 100 || !isInteger(value.latest_event_sequence)
    || !isRecord(value.profile_bindings)
    || !Array.isArray(value.recent_activity)) return null;
  if (!["ready", "running", "paused", "failure_paused", "cancelled", "completed"].includes(value.status)
    || !["preparing", "writing", "finishing", "complete"].includes(value.stage)) return null;
  const activity = value.recent_activity.map(decodeActivity);
  if (activity.some((item) => item === null)
    || Object.values(value.profile_bindings).some((item) => typeof item !== "string")) return null;
  const decodedActivity = activity as AuthoringActivity[];
  const latestEventSequence = value.latest_event_sequence;
  if (value.completed_chapters > value.target_chapters
    || decodedActivity.some((item, index) => index > 0
      && item.sequence <= decodedActivity[index - 1]!.sequence)
    || decodedActivity.some((item) => item.sequence > latestEventSequence)) return null;
  return {
    project_id: value.project_id,
    brief: value.brief,
    title: value.title,
    status: value.status as AuthoringStatus,
    stage: value.stage as AuthoringProject["stage"],
    target_chapters: value.target_chapters,
    target_words: value.target_words,
    completed_chapters: value.completed_chapters,
    progress_percent: value.progress_percent,
    failure_reason: value.failure_reason,
    latest_event_sequence: latestEventSequence,
    profile_bindings: value.profile_bindings as Record<string, string>,
    recent_activity: decodedActivity
  };
}

export function decodeAuthoringProjects(value: unknown): AuthoringProject[] | null {
  if (!Array.isArray(value)) return null;
  const projects = value.map(decodeAuthoringProject);
  return projects.some((item) => item === null) ? null : projects as AuthoringProject[];
}

export function decodeRunAction(value: unknown): RunAction | null {
  if (!isRecord(value) || typeof value.accepted !== "boolean"
    || typeof value.ownership !== "string"
    || !["started_here", "already_running_here", "owned_elsewhere"].includes(value.ownership)) return null;
  const project = decodeAuthoringProject(value.project);
  return project === null ? null : {
    accepted: value.accepted,
    ownership: value.ownership as RunAction["ownership"],
    project
  };
}

export function decodeAuthoringProfiles(value: unknown): AuthoringProfile[] | null {
  if (!Array.isArray(value)) return null;
  const result: AuthoringProfile[] = [];
  for (const item of value) {
    if (!isRecord(item) || typeof item.profile_id !== "string"
      || typeof item.display_name !== "string" || typeof item.model_id !== "string"
      || !["missing", "stale", "ready"].includes(String(item.capability_status))
      || !(item.context_window === null || isInteger(item.context_window, 1))
      || !(item.max_output_tokens === null || isInteger(item.max_output_tokens, 1))) return null;
    result.push(item as unknown as AuthoringProfile);
  }
  return result;
}

function isPrice(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value) && value >= 0;
}

export function decodeModelSettings(value: unknown): ModelSettingsDocument | null {
  if (!isRecord(value) || !isNullableString(value.selected_profile_id)
    || !Array.isArray(value.profiles)) return null;
  const profiles: ModelSettingsProfile[] = [];
  for (const item of value.profiles) {
    if (!isRecord(item) || decodeAuthoringProfiles([item]) === null
      || (item.api_family !== "openai_responses" && item.api_family !== "anthropic_messages")
      || typeof item.base_url !== "string" || typeof item.enabled !== "boolean"
      || typeof item.has_api_key !== "boolean" || !isRecord(item.request_options)
      || !isPrice(item.input_price_per_million) || !isPrice(item.output_price_per_million)
      || !isPrice(item.cache_price_per_million)
      || !(item.metadata_version === null || isInteger(item.metadata_version, 1))) return null;
    // Project only the editable, secret-free response fields; never retain an API key.
    profiles.push({
      profile_id: item.profile_id as string,
      display_name: item.display_name as string,
      model_id: item.model_id as string,
      capability_status: item.capability_status as ModelSettingsProfile["capability_status"],
      context_window: item.context_window as number | null,
      max_output_tokens: item.max_output_tokens as number | null,
      api_family: item.api_family,
      base_url: item.base_url,
      enabled: item.enabled,
      has_api_key: item.has_api_key,
      request_options: item.request_options,
      input_price_per_million: item.input_price_per_million,
      output_price_per_million: item.output_price_per_million,
      cache_price_per_million: item.cache_price_per_million,
      metadata_version: item.metadata_version
    });
  }
  if (new Set(profiles.map((item) => item.profile_id)).size !== profiles.length) return null;
  return { selected_profile_id: value.selected_profile_id, profiles };
}

export function modelAvailabilityLabel(profile: ModelSettingsProfile | undefined): string {
  if (!profile) return "模型不存在";
  if (!profile.enabled) return "已停用";
  if (!profile.has_api_key) return "未设置密钥";
  if (profile.context_window === null || profile.max_output_tokens === null) return "待补充容量设置";
  if (profile.capability_status === "stale") return "配置已变更，需重新验证";
  if (profile.capability_status === "missing") return "尚未验证";
  return "已验证，可使用";
}

export function isModelReady(profile: ModelSettingsProfile | undefined): boolean {
  return profile !== undefined && profile.enabled && profile.has_api_key
    && profile.capability_status === "ready"
    && profile.context_window !== null && profile.max_output_tokens !== null;
}

export function authoringProfilesBody(bindings: Record<string, string>): UpdateAuthoringProfiles {
  return {
    default_profile_id: bindings.default ?? null,
    architect_profile_id: bindings.architect ?? null,
    writer_profile_id: bindings.writer ?? null,
    editor_profile_id: bindings.editor ?? null,
    arbiter_profile_id: bindings.arbiter ?? null,
    fallback_profile_id: bindings.fallback ?? null,
    architect_fallback_profile_id: bindings["fallback:architect"] ?? null,
    writer_fallback_profile_id: bindings["fallback:writer"] ?? null,
    editor_fallback_profile_id: bindings["fallback:editor"] ?? null,
    arbiter_fallback_profile_id: bindings["fallback:arbiter"] ?? null
  };
}

export function decodeAuthoringEvent(data: string): AuthoringEvent | null {
  let value: unknown;
  try { value = JSON.parse(data); } catch { return null; }
  if (!isRecord(value) || typeof value.project_id !== "string") return null;
  const activity = decodeActivity(value);
  return activity === null ? null : { ...activity, project_id: value.project_id };
}

export function decodeApiErrorMessage(value: unknown): string | null {
  if (!isRecord(value)) return null;
  if (isRecord(value.error) && typeof value.error.message === "string") {
    return value.error.message;
  }
  return typeof value.detail === "string" ? value.detail : null;
}

export function authoringStatusLabel(status: AuthoringStatus): string {
  return {
    ready: "等待开始",
    running: "正在创作",
    paused: "已暂停",
    failure_paused: "遇到问题",
    cancelled: "已取消",
    completed: "创作完成"
  }[status];
}

export function authoringStageLabel(stage: AuthoringProject["stage"]): string {
  return {
    preparing: "准备故事",
    writing: "正文创作",
    finishing: "整理成稿",
    complete: "成稿已完成"
  }[stage];
}

export function authoringActivityLabel(activity: AuthoringActivity): string {
  const chapter = typeof activity.payload.chapter_number === "number"
    ? `第 ${activity.payload.chapter_number} 章` : "";
  const labels: Record<string, string> = {
    project_created: "作品已创建",
    run_running: "开始自动创作",
    chapter_committed: `${chapter}已完成`,
    review_saved: "完成阶段质量检查",
    summary_saved: "已整理故事进展",
    outline_extended: "已安排后续情节",
    run_paused: "创作已暂停",
    run_cancelled: "创作已取消",
    run_completed: "整本作品已完成",
    run_failure_paused: "创作遇到问题，已安全暂停"
  };
  return labels[activity.kind] ?? "创作进度已更新";
}
