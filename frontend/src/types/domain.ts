export type OperationMode = "full_auto" | "participatory";
export type RunStatus =
  | "idle"
  | "running"
  | "pause_requested"
  | "paused"
  | "waiting_for_user"
  | "failed";

export type LlmProtocol = "openai-compatible" | "anthropic-compatible";

export interface ProjectMetadata {
  schema_version: number;
  project_id: string;
  title: string;
  operation_mode: OperationMode;
  active_profile_id: string | null;
  active_arc_id: string | null;
  active_chapter_id: string | null;
  run_status: RunStatus;
  created_at: string;
  updated_at: string;
}

export interface ProjectSummary {
  name: string;
  title: string;
  path: string;
  metadata: ProjectMetadata;
}

export interface LlmProfilePublic {
  id: string;
  name: string;
  protocol: LlmProtocol;
  base_url: string;
  model: string;
  enabled: boolean;
  has_api_key: boolean;
}

export interface LlmProfilesDocument {
  schema_version: number;
  active_profile_id: string | null;
  profiles: LlmProfilePublic[];
}

export interface LlmProfileTestResult {
  profile_id: string;
  ok: boolean;
  model_snapshot: string;
  provider_snapshot: string;
  message: string;
}

export interface LlmProfileMutation {
  id: string;
  name: string;
  protocol: LlmProtocol;
  base_url: string;
  api_key?: string | null;
  model: string;
  enabled: boolean;
}

export interface SetupOption {
  id: string;
  label: string;
  description: string;
}

export interface SetupQuestion {
  id: string;
  title: string;
  prompt: string;
  options: SetupOption[];
  required: boolean;
  source: "default" | "llm";
  profile_id: string | null;
  model_snapshot: string | null;
}

export interface SetupAnswer {
  question_id: string;
  answer: string;
  answered_at: string;
}

export interface SetupStateDocument {
  schema_version: number;
  approved: boolean;
  approved_at: string | null;
  ready_for_approval: boolean;
  readiness_assessed_at: string | null;
  readiness_profile_id: string | null;
  questions: SetupQuestion[];
  answers: SetupAnswer[];
  next_question: SetupQuestion | null;
}

export type ArcReviewStatus = "not_required" | "awaiting_review" | "approved";

export interface CurrentArcState {
  arc_id: string;
  status: string;
  plan_path: string;
  human_review: ArcReviewStatus;
  approved_at: string | null;
  target_chapter_count: number;
  completed_chapter_ids: string[];
  completed_at: string | null;
}

export interface CurrentArcApprovalResponse {
  arc: CurrentArcState;
  run_status: RunStatus;
}

export interface ChapterRetryResponse {
  status: RunStatus;
  retry_scope: string;
  artifact_path: string;
}

export interface StaleRunRecoveryResponse {
  status: RunStatus;
  previous_status: RunStatus;
}

export interface HarnessEvent {
  seq: number | null;
  event_id: string;
  timestamp: string;
  project_id: string;
  run_id: string | null;
  kind: string;
  loop_layer: "book" | "story_arc" | "chapter" | "system";
  atomic_action: string | null;
  status: "started" | "delta" | "completed" | "failed" | "requested";
  artifact_path: string | null;
  routing_decision: string | null;
  message: string;
  payload: Record<string, unknown>;
}

const harnessLoopLayers = new Set(["book", "story_arc", "chapter", "system"]);
const harnessEventStatuses = new Set(["started", "delta", "completed", "failed", "requested"]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isNullableString(value: unknown): value is string | null {
  return value === null || typeof value === "string";
}

function isNullableNumber(value: unknown): value is number | null {
  return value === null || typeof value === "number";
}

export function isHarnessEvent(value: unknown): value is HarnessEvent {
  if (!isRecord(value)) {
    return false;
  }

  return (
    isNullableNumber(value.seq) &&
    typeof value.event_id === "string" &&
    typeof value.timestamp === "string" &&
    typeof value.project_id === "string" &&
    isNullableString(value.run_id) &&
    typeof value.kind === "string" &&
    typeof value.loop_layer === "string" &&
    harnessLoopLayers.has(value.loop_layer) &&
    isNullableString(value.atomic_action) &&
    typeof value.status === "string" &&
    harnessEventStatuses.has(value.status) &&
    isNullableString(value.artifact_path) &&
    isNullableString(value.routing_decision) &&
    typeof value.message === "string" &&
    isRecord(value.payload)
  );
}

export function harnessEventTextDelta(event: HarnessEvent): string | null {
  const value = event.payload.text_delta;
  return typeof value === "string" ? value : null;
}

export function harnessVisibleOutputForLatestAction(events: HarnessEvent[]): string {
  const latestDeltaIndex = findLatestEventIndex(
    events,
    (event) => event.kind === "llm_output_delta"
  );
  if (latestDeltaIndex === -1) {
    return "";
  }

  const latestDelta = events[latestDeltaIndex];
  const boundaryIndex = findLatestEventIndex(
    events.slice(0, latestDeltaIndex),
    (event) =>
      event.kind === "atomic_action_started" &&
      event.run_id === latestDelta.run_id &&
      event.loop_layer === latestDelta.loop_layer &&
      event.atomic_action === latestDelta.atomic_action
  );
  const fallbackBoundaryIndex = findLatestEventIndex(
    events.slice(0, latestDeltaIndex),
    (event) => event.kind !== "llm_output_delta"
  );
  const startIndex =
    (boundaryIndex === -1 ? fallbackBoundaryIndex : boundaryIndex) + 1;

  return events
    .slice(startIndex, latestDeltaIndex + 1)
    .filter(
      (event) =>
        event.kind === "llm_output_delta" &&
        event.run_id === latestDelta.run_id &&
        event.loop_layer === latestDelta.loop_layer &&
        event.atomic_action === latestDelta.atomic_action
    )
    .map(harnessEventTextDelta)
    .filter((textDelta): textDelta is string => textDelta !== null)
    .join("");
}

function findLatestEventIndex(
  events: HarnessEvent[],
  predicate: (event: HarnessEvent) => boolean
): number {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    if (predicate(events[index])) {
      return index;
    }
  }
  return -1;
}

export interface ArtifactSummary {
  path: string;
  kind: string;
  title: string;
  status: string;
  detail: string;
  candidate: boolean;
  committed: boolean;
  routing_decision: string | null;
  signals: string[];
  event_status: "recorded" | "missing" | "untracked";
  event_note: string | null;
  profile_id: string | null;
  model_snapshot: string | null;
}

export type GateStatus = "passed" | "pending" | "failed";
export type LiteraryReviewDecision = "approved" | "rejected";

export interface ReadinessGate {
  id: string;
  status: GateStatus;
  required: boolean;
  message: string;
  evidence: string[];
}

export type RunNextActionId =
  | "answer_book_setup"
  | "approve_book_setup"
  | "configure_llm_profile"
  | "repair_project_state"
  | "wait_for_safe_checkpoint"
  | "recover_stale_run"
  | "inspect_failure"
  | "retry_current_chapter"
  | "approve_story_arc"
  | "start_run"
  | "resume_run";

export interface RunNextAction {
  id: RunNextActionId;
  command: string | null;
  requires_user: boolean;
  can_auto_continue: boolean;
  message: string;
  evidence: string[];
}

export interface ProjectReadiness {
  status: GateStatus;
  can_start_run: boolean;
  gates: ReadinessGate[];
  next_action: RunNextAction;
}

export interface CompletionGate {
  id: string;
  status: GateStatus;
  message: string;
  evidence: string[];
}

export interface ProjectCompletionAudit {
  status: GateStatus;
  gates: CompletionGate[];
}

export interface LiteraryReviewRequest {
  decision: LiteraryReviewDecision;
  reviewer: string;
  chapter_assessment: string;
  state_patch_assessment: string;
  notes: string;
}

export interface LiteraryReviewRecord {
  schema_version: number;
  decision: LiteraryReviewDecision;
  reviewer: string;
  reviewed_at: string;
  chapter_assessment: string;
  state_patch_assessment: string;
  notes: string;
  smoke_report: string;
  reviewed_artifacts: Record<string, string>;
  literary_review_json: string;
  literary_review_markdown: string;
}
