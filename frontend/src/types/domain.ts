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
  title: string | null;
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
  title: string | null;
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

export interface SetupMessage {
  id: string;
  turn: number;
  role: "user" | "assistant";
  content: string;
  created_at: string;
  profile_id: string | null;
  model_snapshot: string | null;
  migrated: boolean;
}

export interface SetupSuggestion {
  id: string;
  label: string;
  message: string;
}

export interface SetupReadinessSignal {
  status: "continue" | "ready";
  reason: string;
}

export interface SupersededDecision {
  turn: number;
  decision: string;
  replacement: string | null;
  reason: string;
  user_evidence: string;
}

export interface ConfirmedDecisionCoverage {
  decision: string;
  candidate_evidence: string;
}

export interface BookDirectionConstraints {
  confirmed: string[];
  must_preserve: string[];
  must_avoid: string[];
  creative_freedoms: string[];
  open_decisions: string[];
}

export interface BookDirectionReviewIssue {
  severity: "warning" | "blocking";
  kind: string;
  message: string;
  evidence: string[];
  suggested_question: string | null;
}

export interface BookDirectionReview {
  status: "passed" | "blocked";
  summary: string;
  issues: BookDirectionReviewIssue[];
  signals: string[];
}

export interface RecommendedBookTitle {
  title: string;
  rationale: string;
}

export interface BookDirectionCandidate {
  revision: number;
  created_at: string;
  direction_markdown: string;
  constraints: BookDirectionConstraints;
  confirmed_decision_coverage: ConfirmedDecisionCoverage[];
  recommended_titles: RecommendedBookTitle[];
  rolling_plan_markdown: string;
  review: BookDirectionReview;
  direction_path: string;
  constraints_path: string;
  title_suggestions_path: string;
  rolling_plan_path: string;
  verification_path: string;
  profile_id: string;
  model_snapshot: string;
  review_model_snapshot: string;
}

export interface SetupStateDocument {
  schema_version: number;
  revision: number;
  phase: "discussing" | "review_ready" | "review_blocked" | "approved";
  approved: boolean;
  approved_at: string | null;
  approved_title: string | null;
  title_selection_source: "recommended" | "custom" | null;
  migrated_from_schema_version: number | null;
  turn_count: number;
  candidate_revision_counter: number;
  messages: SetupMessage[];
  direction_draft: string;
  discussion_summary: string;
  confirmed_decisions: string[];
  superseded_decisions: SupersededDecision[];
  unresolved_questions: string[];
  assumptions: string[];
  contradictions: string[];
  question: string | null;
  suggestions: SetupSuggestion[];
  readiness: SetupReadinessSignal;
  candidate: BookDirectionCandidate | null;
  direction_draft_version_path: string | null;
  discussion_state_version_path: string | null;
  discussion_transcript_version_path: string | null;
  last_context_snapshot_path: string | null;
  last_profile_id: string | null;
  last_model_snapshot: string | null;
}

export type ArcReviewStatus = "not_required" | "awaiting_review" | "approved";

export interface CurrentArcState {
  arc_id: string;
  status: string;
  plan_path: string;
  human_review: ArcReviewStatus;
  approved_at: string | null;
  recommended_target_chapter_count: number;
  target_chapter_count: number;
  completed_chapter_ids: string[];
  completed_at: string | null;
}

export interface ExperimentFixtureIssue {
  code: string;
  message: string;
}

export interface ExperimentFixtureCheckpoint {
  source_project_name: string;
  source_project_id: string;
  source_title: string | null;
  active_arc_id: string;
  completed_arc_ids: string[];
  warmup_chapter_ids: string[];
  recommended_target_chapter_count: number;
  target_chapter_count: number;
  checkpoint_fingerprint: string;
}

export interface ExperimentFixtureSummary {
  fixture_id: string;
  created_at: string;
  relative_path: string;
  checkpoint: ExperimentFixtureCheckpoint;
}

export interface ExperimentFixtureStatus {
  eligible: boolean;
  issues: ExperimentFixtureIssue[];
  checkpoint: ExperimentFixtureCheckpoint | null;
  existing_fixture: ExperimentFixtureSummary | null;
}

export interface ExperimentFixtureCreateResponse {
  created: boolean;
  fixture: ExperimentFixtureSummary;
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
    (value.seq === undefined || isNullableNumber(value.seq)) &&
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
  | "continue_book_discussion"
  | "review_book_direction"
  | "approve_book_direction"
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
