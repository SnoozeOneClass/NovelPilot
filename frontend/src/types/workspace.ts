export type OperationMode = "full_auto" | "participatory";

export type CommandId =
  | "start_run"
  | "pause_run"
  | "resume_run"
  | "retry_failed_task"
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
  blocking_task_id: string | null;
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

export interface BookStateView {
  book_id: string;
  lifecycle_status: string;
  current_baseline_id: string | null;
  baseline_version: number | null;
  approved_title: string | null;
  minimum_chapter_count: number | null;
  maximum_chapter_count: number | null;
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

export interface ArcStateView {
  arc_id: string;
  ordinal: number;
  purpose: string;
  lifecycle_status: string;
  current_baseline_id: string | null;
  baseline_version: number | null;
  target_chapter_count: number | null;
  recommended_target_chapter_count: number | null;
  committed_chapter_count: number;
  workspace_state: string;
  workspace_lock_version: number;
  semantic_repair_count: number;
  semantic_repair_limit: number;
  pending_submission_id: string | null;
  pending_review_id: string | null;
  pending_review_decision: string | null;
  approval_gate_id: string | null;
  approval_gate_state: string | null;
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
}

export interface AgentTaskStateView {
  task_id: string;
  run_id: string;
  role: string;
  task_kind: string;
  scope_layer: string;
  arc_id: string | null;
  chapter_id: string | null;
  status: string;
  delivery_state: string;
  profile_id: string;
  model_id: string;
  attempt_id: string | null;
  attempt_number: number | null;
  attempt_status: string | null;
  retry_kind: string | null;
  provider_request_count: number | null;
  transport_retry_count: number | null;
  model_request_count: number | null;
  input_tokens: number | null;
  output_tokens: number | null;
  error_code: string | null;
  error_ref_id: string | null;
  diagnostic_ref_id: string | null;
  created_at_ms: number;
  updated_at_ms: number;
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
  api_family: string;
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
