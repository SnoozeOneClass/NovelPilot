export type OperationMode = "full_auto" | "participatory";
export type ApiFamily = "openai_responses" | "anthropic_messages";

export type AgentLiveEventKind =
  | "task_started"
  | "attempt_restarting"
  | "prose_delta"
  | "prose_committed"
  | "prose_discarded"
  | "task_succeeded"
  | "task_failed";

export interface AgentLiveEvent {
  kind: AgentLiveEventKind;
  project_id: string;
  task_id: string;
  attempt_id: string;
  delta: string | null;
  provider_request_number: number | null;
  provider_request_limit: number | null;
  reason: string | null;
  retry_delay_ms: number | null;
}

const agentLiveEventKinds = new Set<AgentLiveEventKind>([
  "task_started",
  "attempt_restarting",
  "prose_delta",
  "prose_committed",
  "prose_discarded",
  "task_succeeded",
  "task_failed"
]);

function isAgentLiveEventKind(value: unknown): value is AgentLiveEventKind {
  return (
    typeof value === "string"
    && agentLiveEventKinds.has(value as AgentLiveEventKind)
  );
}

function isNullableNumber(value: unknown): value is number | null {
  return value === null || typeof value === "number";
}

export function decodeAgentLiveEvent(data: string): AgentLiveEvent | null {
  let parsed: unknown;
  try {
    parsed = JSON.parse(data);
  } catch {
    return null;
  }
  if (typeof parsed !== "object" || parsed === null) return null;
  const value = parsed as Record<string, unknown>;
  if (
    !isAgentLiveEventKind(value.kind)
    || typeof value.project_id !== "string"
    || typeof value.task_id !== "string"
    || typeof value.attempt_id !== "string"
  ) {
    return null;
  }
  if (value.delta !== null && typeof value.delta !== "string") return null;
  if (!isNullableNumber(value.provider_request_number)) return null;
  if (!isNullableNumber(value.provider_request_limit)) return null;
  if (!isNullableNumber(value.retry_delay_ms)) return null;
  if (value.reason !== null && typeof value.reason !== "string") return null;
  return {
    kind: value.kind,
    project_id: value.project_id,
    task_id: value.task_id,
    attempt_id: value.attempt_id,
    delta: value.delta,
    provider_request_number: value.provider_request_number,
    provider_request_limit: value.provider_request_limit,
    reason: value.reason,
    retry_delay_ms: value.retry_delay_ms
  };
}

export function reduceLiveProse(current: string, event: AgentLiveEvent): string {
  switch (event.kind) {
    case "task_started":
    case "attempt_restarting":
    case "prose_discarded":
    case "task_failed":
      return "";
    case "prose_delta":
      return event.delta === null ? current : current + event.delta;
    case "prose_committed":
    case "task_succeeded":
      return current;
  }
}

export type CommandId =
  | "start_run"
  | "pause_run"
  | "resume_run"
  | "retry_failed_task"
  | "retry_failed_action"
  | "send_book_input"
  | "approve_book"
  | "approve_arc"
  | "submit_feedback"
  | "export_markdown";

export interface ProjectListItem {
  project_id: string;
  title: string | null;
  operation_mode: OperationMode;
  lifecycle_status: string;
  run_status: string;
  wait_reason_code: string | null;
  current_arc_id: string | null;
  current_chapter_id: string | null;
  committed_chapter_count: number;
  created_at_ms: number;
  updated_at_ms: number;
}

export interface RunStateView {
  run_id: string;
  run_number: number;
  status: string;
  desired_state: string;
  lock_version: number;
  wait_reason_code: string | null;
  failure_source_kind: "agent_task" | "harness_action" | null;
  blocking_task_id: string | null;
  blocking_action_key: string | null;
  failure_code: string | null;
  failure_ref_id: string | null;
  started_at_ms: number | null;
  finished_at_ms: number | null;
}

export interface BookSuggestion {
  id: string;
  label: string;
  message: string;
  rationale: string;
  recommended: boolean;
  action: "answer" | "select_title";
  value: string | null;
}

export interface BookDiscussionState {
  schema_id: "book-discussion-state-v1";
  turn_count: number;
  direction_draft: string;
  discussion_summary: string;
  confirmed_decisions: string[];
  superseded_decisions: Array<Record<string, unknown>>;
  unresolved_questions: string[];
  assumptions: string[];
  contradictions: string[];
  selected_title: string | null;
  selected_title_source: "recommended" | "custom" | null;
  question: string | null;
  suggestions: BookSuggestion[];
  readiness_status: "awaiting_agent" | "continue" | "ready";
  readiness_reason: string;
}

export interface BookTranscript {
  schema_id: "book-transcript-v1";
  messages: Array<{
    sequence: number;
    role: "user" | "assistant";
    content: string;
  }>;
}

export interface BookArcContractView {
  ordinal: number;
  whole_book_role: string;
  core_goal: string;
  handoff_from_previous: string;
  exit_conditions: string[];
  is_final: boolean;
  lifecycle_status: "planned" | "active" | "completed";
}

export interface BookStateView {
  book_id: string;
  lifecycle_status: string;
  current_baseline_id: string | null;
  latest_completion_review_id: string | null;
  current_progress_handoff_id: string | null;
  current_completion_id: string | null;
  baseline_version: number | null;
  approved_title: string | null;
  whole_book_scale_guidance: string | null;
  arc_contract_count: number | null;
  final_arc_ordinal: number | null;
  topology_effective_after_arc_ordinal: number | null;
  arc_topology: BookArcContractView[];
  workspace_state: string;
  workspace_lock_version: number;
  semantic_repair_count: number;
  semantic_repair_limit: number;
  discussion: BookDiscussionState;
  transcript: BookTranscript;
  pending_submission_id: string | null;
  pending_review_id: string | null;
  pending_review_decision: string | null;
}

export interface ArcOutlineAssignmentView {
  title: string;
  core_event: string;
  hook: string;
  scenes: string[];
}

export interface ArcOutlineEntryView {
  book_ordinal: number;
  arc_ordinal: number;
  status: "committed" | "drafting" | "planned";
  chapter_id: string | null;
  actual_chapter_title: string | null;
  assignment: ArcOutlineAssignmentView;
  source_arc_baseline_id: string;
  source_arc_baseline_version: number;
}

export interface ArcOutlineView {
  arc_id: string;
  arc_ordinal: number;
  current_baseline_id: string;
  current_baseline_version: number;
  entries: ArcOutlineEntryView[];
}

export interface ArcStateView {
  arc_id: string;
  ordinal: number;
  is_final: boolean;
  assigned_book_baseline_id: string | null;
  lifecycle_status: string;
  current_baseline_id: string | null;
  latest_closure_review_id: string | null;
  current_closure_id: string | null;
  baseline_version: number | null;
  closure_cumulative_chapter_count: number | null;
  cumulative_committed_chapter_count: number;
  arc_committed_chapter_count: number;
  workspace_state: string;
  workspace_lock_version: number;
  semantic_repair_count: number;
  semantic_repair_limit: number;
  pending_submission_id: string | null;
  pending_review_id: string | null;
  pending_review_decision: string | null;
  approval_gate_id: string | null;
  approval_gate_state: string | null;
  revision_origin: string;
  automatic_correction_round: number | null;
  outline: ArcOutlineView | null;
}

export interface ChapterStateView {
  chapter_id: string;
  book_ordinal: number;
  arc_ordinal: number;
  lifecycle_status: string;
  current_baseline_id: string | null;
  chapter_title: string | null;
  workspace_state: string;
  workspace_lock_version: number;
  semantic_repair_count: number;
  semantic_repair_limit: number;
  has_plan: boolean;
  has_prose: boolean;
  has_observations: boolean;
  has_canon_patch: boolean;
  pending_submission_id: string | null;
  pending_review_id: string | null;
  pending_review_decision: string | null;
  revision_origin: string;
  automatic_correction_round: number | null;
}

export interface CreatorInputNeed {
  controlled_fact: string;
  question: string;
  evidence: string[];
}

export interface CreatorInputRequestView {
  review_kind: "arc_parent" | "book_parent" | "arc_closure" | "book_completion";
  review_id: string;
  route_layer: "book" | "arc";
  book_id: string;
  arc_id: string | null;
  automatic_correction_round: 0 | 1;
  question: CreatorInputNeed;
}

export interface FeedbackStateView {
  feedback_id: string;
  feedback_kind: "unsolicited" | "correction_wait_response";
  status: "pending" | "routed" | "applied" | "dismissed";
  content: string;
  route_layer: "book" | "arc" | "chapter" | null;
  book_id: string | null;
  arc_id: string | null;
  chapter_id: string | null;
  captured_run_id: string;
  captured_book_baseline_id: string | null;
  captured_arc_baseline_id: string | null;
  captured_chapter_baseline_id: string | null;
  arc_parent_review_id: string | null;
  book_parent_review_id: string | null;
  arc_closure_review_id: string | null;
  book_completion_review_id: string | null;
  resulting_correction_lineage_id: string | null;
  dismiss_reason_code: string | null;
  applied_command_id: string | null;
  created_at_ms: number;
  routed_at_ms: number | null;
  applied_at_ms: number | null;
}

export interface AgentTaskStateView {
  task_id: string;
  run_id: string;
  role: string;
  task_kind: string;
  scope_layer: string;
  arc_id: string | null;
  chapter_id: string | null;
  task_status: string;
  delivery_state: string;
  profile_id: string;
  model_id: string;
  profile_fingerprint: string;
  output_schema_id: string;
  output_schema_version: number;
  harness_policy_id: string;
  harness_policy_version: number;
  attempt_id: string;
  attempt_number: number;
  retry_kind: string;
  attempt_status: string;
  framework_fingerprint: string;
  provider_request_count: number;
  transport_retry_count: number;
  model_request_count: number;
  input_tokens: number | null;
  output_tokens: number | null;
  total_tokens: number | null;
  error_code: string | null;
  error_category: string | null;
  http_status: number | null;
  error_ref_id: string | null;
  diagnostic_ref_id: string | null;
  created_at_ms: number;
  started_at_ms: number | null;
  finished_at_ms: number | null;
}

export interface ExecutableCommand {
  command_id: CommandId;
  enabled: boolean;
  reason: string;
}

export interface ProjectStateView {
  project: ProjectListItem;
  settings_lock_version: number;
  default_profile_id: string | null;
  book_profile_id: string | null;
  arc_profile_id: string | null;
  chapter_profile_id: string | null;
  evaluator_profile_id: string | null;
  run: RunStateView;
  book: BookStateView;
  current_arc: ArcStateView | null;
  current_chapter: ChapterStateView | null;
  creator_input_request: CreatorInputRequestView | null;
  recent_feedback: FeedbackStateView[];
  latest_event_sequence: number;
  commands: ExecutableCommand[];
  recent_tasks: AgentTaskStateView[];
}

export interface ProfileCapabilities {
  text_output: boolean;
  text_streaming: boolean;
  native_json_schema: boolean;
  tool_calling: boolean;
  usage_reporting: boolean;
  contract_version: number;
}

export interface PublicProfile {
  id: string;
  display_name: string;
  api_family: ApiFamily;
  base_url: string;
  model_id: string;
  request_options: Record<string, unknown>;
  enabled: boolean;
  has_api_key: boolean;
  capability_status: "missing" | "stale" | "ready";
  capabilities: ProfileCapabilities | null;
  configuration_fingerprint: string;
  capability_fingerprint: string | null;
}

export interface ProfileListResponse {
  selected_profile_id: string | null;
  profiles: PublicProfile[];
}

export interface MutationResponse {
  replayed: boolean;
  receipt_id: string;
  state: ProjectStateView;
}

export interface ManuscriptExportResult {
  project_id: string;
  book_baseline_id: string;
  canon_baseline_id: string;
  snapshot_fingerprint: string;
  content_sha256: string;
  byte_count: number;
  path: string;
}

export interface CreateProjectInput {
  project_id: string;
  creator_brief: string;
  operation_mode: OperationMode;
  default_profile_id: string | null;
}
