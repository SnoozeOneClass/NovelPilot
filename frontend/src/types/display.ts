import type {
  ArtifactSummary,
  GateStatus,
  HarnessEvent,
  LiteraryReviewDecision,
  LlmProtocol,
  OperationMode,
  RunNextAction,
  RunStatus
} from "./domain";

const operationModeLabels: Record<OperationMode, string> = {
  full_auto: "全自动",
  participatory: "参与模式"
};

const protocolLabels: Record<LlmProtocol, string> = {
  "openai-compatible": "OpenAI 兼容协议",
  "anthropic-compatible": "Anthropic 兼容协议"
};

const runStatusLabels: Record<string, string> = {
  idle: "空闲",
  running: "运行中",
  pause_requested: "等待安全暂停",
  paused: "已暂停",
  waiting_for_user: "等待用户",
  failed: "失败"
};

const gateStatusLabels: Record<GateStatus | string, string> = {
  passed: "通过",
  pending: "待处理",
  failed: "未通过"
};

const genericStatusLabels: Record<string, string> = {
  ...gateStatusLabels,
  audited: "已审计",
  available: "可用",
  candidate: "候选",
  committed: "已提交",
  completed: "已完成",
  delta: "流式输出",
  generated: "已生成",
  planned: "已规划",
  prepared: "已准备",
  recorded: "已记录",
  requested: "已请求",
  revised: "已修订",
  reviewed: "已审查",
  started: "进行中",
  warning: "警告"
};

const loopLayerLabels: Record<HarnessEvent["loop_layer"], string> = {
  book: "全书流程",
  story_arc: "故事弧流程",
  chapter: "章节流程",
  system: "系统"
};

const atomicActionLabels: Record<string, string> = {
  advance_run_until_stop: "推进运行到停止点",
  advance_to_next_checkpoint: "推进到下一安全检查点",
  approve_book_direction: "批准全书方向",
  approve_book_loop: "批准全书流程",
  approve_current_arc: "批准当前故事弧",
  assemble_context: "装配章节上下文",
  assemble_book_direction_review_context: "装配全书方向审阅上下文",
  assemble_book_discussion_context: "装配全书讨论上下文",
  assess_setup_readiness: "评估设定是否充分",
  chapter_complete: "章节完成检查点",
  collect_book_settings: "收集全书设定",
  commit_state_patch: "提交状态补丁",
  continue_book_discussion: "继续全书方向讨论",
  draft_chapter: "写作候选正文",
  extract_candidate_observations: "抽取候选观测",
  generate_candidate_state_patch: "生成候选状态补丁",
  generate_chapter_goal: "生成章节目标",
  pause_run: "请求暂停",
  personalize_setup_question: "生成设定追问",
  plan_current_arc: "规划当前故事弧",
  prepare_chapter_retry: "准备章节重试",
  process_user_feedback: "处理用户反馈",
  record_book_feedback: "记录全书反馈",
  recover_stale_run: "恢复卡住的运行",
  review_current_arc: "审查当前故事弧",
  revise_current_arc_plan: "修订当前故事弧计划",
  safe_checkpoint: "安全检查点",
  select_llm_profile: "选择 LLM 配置",
  semantic_review: "语义审查",
  synthesize_and_review_book_direction: "整理并审阅全书方向",
  synthesize_book_direction: "整理全书方向候选",
  update_operation_mode: "更换运行模式",
  review_book_direction: "审阅全书方向候选",
  verify_chapter: "验证章节",
  write_final_chapter: "提交正式章节"
};

const eventKindLabels: Record<string, string> = {
  artifact_written: "产物已写入",
  atomic_action_started: "原子动作开始",
  approved_book_artifact_written: "已批准全书产物已写入",
  book_direction_candidate_reviewed: "全书方向候选已审阅",
  book_direction_stale_review_discarded: "过期的全书方向审阅结果已丢弃",
  book_direction_constraints_written: "候选全书约束已写入",
  book_direction_draft_updated: "全书方向草稿已更新",
  book_direction_candidate_written: "候选全书方向已写入",
  book_direction_review_context_assembled: "全书方向审阅上下文已装配",
  book_direction_review_failed: "全书方向整理或审阅失败",
  book_discussion_context_assembled: "全书讨论上下文已装配",
  book_discussion_stale_result_discarded: "过期的全书讨论结果已丢弃",
  book_discussion_turn_completed: "全书讨论轮次已完成",
  book_discussion_turn_failed: "全书讨论轮次失败",
  book_loop_approved: "全书流程已批准",
  book_rolling_contract_candidate_written: "候选滚动规划契约已写入",
  book_setup_required: "需要全书设定",
  chapter_retry_prepared: "章节重试已准备",
  export_completed: "导出完成",
  feedback_artifact_written: "反馈产物已写入",
  feedback_processed: "反馈已处理",
  llm_output_delta: "模型可见输出",
  llm_stream_progress: "模型流式进度",
  llm_profile_required: "需要 LLM 配置",
  operation_mode_changed: "运行模式已更换",
  pause_ignored: "暂停请求已忽略",
  pause_requested: "已请求暂停",
  routing_decision: "路由决策",
  run_failed: "运行失败",
  run_paused: "运行已暂停",
  run_recovered: "运行锁已恢复",
  run_recovery_ignored: "恢复请求已忽略",
  run_resumed: "运行已恢复",
  run_started: "运行已启动",
  run_step_budget_reached: "步数预算已到达",
  safe_checkpoint_reached: "到达安全检查点",
  setup_answered: "设定回答已记录",
  setup_followup_assessment_failed: "设定追问评估失败",
  setup_followup_question_created: "设定追问已生成",
  setup_question_personalization_failed: "设定问题生成失败",
  setup_question_personalized: "设定问题已生成",
  setup_ready_for_approval: "设定可批准",
  state_patch_candidate_created: "候选状态补丁已生成",
  state_patch_committed: "状态补丁已提交",
  state_patch_rejected: "状态补丁被拒绝",
  story_arc_approved: "故事弧已批准",
  story_arc_review_required: "需要故事弧审查",
  user_feedback: "用户反馈",
  verification_completed: "章节验证完成"
};

const routingDecisionLabels: Record<string, string> = {
  apply_to_current_chapter_context: "注入当前章节上下文",
  apply_at_next_safe_checkpoint: "从下一安全检查点生效",
  approve_book_loop: "等待批准全书流程",
  ask_user: "询问用户",
  await_user_approval: "等待用户明确批准",
  call_book_discussion_model: "调用全书讨论模型",
  commit: "提交",
  continue: "继续",
  continue_discussion: "继续全书讨论",
  escalate_to_book_loop: "升级到全书 loop",
  fallback_to_default_question: "回退到默认问题",
  none: "无路由",
  pause: "暂停",
  plan_next_arc: "规划下一故事弧",
  ready_for_approval: "等待批准",
  review_available: "可以整理并审阅",
  review_book_direction: "审阅全书方向候选",
  reload_book_discussion: "重新载入最新全书讨论",
  retry_book_direction_review: "重试全书方向整理与审阅",
  retry_book_discussion_turn: "重试本轮全书讨论",
  retry: "重试",
  revise: "修订",
  revise_current_arc_plan: "修订当前故事弧计划",
  start_or_resume_harness: "启动或继续 harness",
  synthesize_book_direction: "整理全书方向候选",
  rewrite: "重写"
};

const artifactKindLabels: Record<string, string> = {
  arc_plan: "故事弧计划",
  arc_revision: "故事弧修订",
  book_direction: "全书方向",
  book_direction_candidate: "候选全书方向",
  book_direction_constraints: "全书约束",
  book_direction_draft: "全书方向草稿",
  book_discussion_transcript: "全书讨论记录",
  book_feedback: "全书反馈",
  book_rolling_contract: "滚动规划契约",
  book_rolling_contract_candidate: "候选滚动规划契约",
  candidate_observations: "候选观测",
  candidate_state_patch: "候选状态补丁",
  committed_state_patch: "已提交状态补丁",
  context_snapshot: "上下文快照",
  draft: "候选正文",
  export: "导出稿",
  final: "正式章节",
  other: "其他文件",
  retry_manifest: "重试清单",
  review: "语义审查",
  state_patch_rejection: "状态补丁拒绝",
  verification: "章节验证"
};

const gateIdLabels: Record<string, string> = {
  active_llm_profile: "LLM 配置",
  book_setup: "全书设定",
  completion_evidence: "完成证据",
  literary_quality_review: "文学可用性审查",
  live_provider_smoke: "真实 LLM 冒烟",
  output_secret_audit: "输出密钥审计",
  run_control: "运行控制"
};

const runNextActionLabels: Record<RunNextAction["id"], string> = {
  continue_book_discussion: "继续全书方向讨论",
  review_book_direction: "整理并审阅全书方向",
  approve_book_direction: "批准全书方向候选",
  configure_llm_profile: "配置 LLM",
  repair_project_state: "修复项目状态",
  wait_for_safe_checkpoint: "等待安全检查点",
  recover_stale_run: "恢复卡住的运行",
  inspect_failure: "检查失败原因",
  retry_current_chapter: "重试当前章节",
  approve_story_arc: "批准当前故事弧",
  start_run: "启动 harness",
  resume_run: "继续 harness"
};

export function formatOperationMode(mode: OperationMode): string {
  return operationModeLabels[mode];
}

export function formatProjectTitle(title: string | null | undefined): string {
  return title?.trim() || "未命名新书";
}

export function formatProtocol(protocol: LlmProtocol): string {
  return protocolLabels[protocol];
}

export function formatRunStatus(status: RunStatus | string | null | undefined): string {
  return formatMapped(status, runStatusLabels, "未设置");
}

export function formatGateStatus(status: GateStatus | string): string {
  return genericStatusLabels[status] ?? status;
}

export function formatGenericStatus(status: string | null | undefined): string {
  return formatMapped(status, genericStatusLabels, "未设置");
}

export function formatLoopLayer(
  layer: HarnessEvent["loop_layer"] | string | null | undefined
): string {
  if (!layer) {
    return "系统";
  }
  return layer in loopLayerLabels
    ? loopLayerLabels[layer as HarnessEvent["loop_layer"]]
    : layer;
}

export function formatAtomicAction(action: string | null | undefined): string {
  return formatMapped(action, atomicActionLabels, "空闲");
}

export function formatEventKind(kind: string): string {
  return eventKindLabels[kind] ?? kind;
}

export function formatRoutingDecision(decision: string | null | undefined): string {
  return formatMapped(decision, routingDecisionLabels, "空闲");
}

export function formatArtifactTitle(summary: ArtifactSummary): string {
  return artifactKindLabels[summary.kind] ?? summary.title;
}

export function formatArtifactDetail(detail: string): string {
  const exact: Record<string, string> = {
    "Artifact exists without a matching durable event; inspect before using it as recovered harness evidence.":
      "该产物没有匹配的 durable event；作为恢复证据使用前请先检查。",
    "Candidate-only observations; not canon.": "候选观测，仅用于审查，尚未进入正史。",
    "Commit allowed.": "验证通过，可以提交。",
    "events.jsonl records this artifact write.": "events.jsonl 已记录这次产物写入。",
    "Missing file.": "文件缺失。",
    "No reason recorded.": "没有记录原因。"
  };
  if (exact[detail]) {
    return exact[detail];
  }
  const sourceMatch = detail.match(/^(\d+) sources, (\d+) exclusions$/);
  if (sourceMatch) {
    return `${sourceMatch[1]} 个来源，${sourceMatch[2]} 项排除`;
  }
  const proposedMatch = detail.match(/^(\d+) proposed operations$/);
  if (proposedMatch) {
    return `${proposedMatch[1]} 个候选操作`;
  }
  const committedMatch = detail.match(/^(\d+) committed operations$/);
  if (committedMatch) {
    return `${committedMatch[1]} 个已提交操作`;
  }
  const archivedMatch = detail.match(/^(.+): (\d+) archived artifacts$/);
  if (archivedMatch) {
    return `${formatRetryScope(archivedMatch[1])}：已归档 ${archivedMatch[2]} 个失败产物`;
  }
  const bytesMatch = detail.match(/^(\d+) bytes$/);
  if (bytesMatch) {
    return `${bytesMatch[1]} 字节`;
  }
  const messageCountMatch = detail.match(/^(\d+) messages$/);
  if (messageCountMatch) {
    return `${messageCountMatch[1]} 条消息`;
  }
  const constraintCountMatch = detail.match(/^(\d+) structured constraints$/);
  if (constraintCountMatch) {
    return `${constraintCountMatch[1]} 项结构化约束`;
  }
  return detail;
}

export function formatEventStatus(status: HarnessEvent["status"] | string): string {
  return formatGenericStatus(status);
}

export function formatGateId(id: string): string {
  return gateIdLabels[id] ?? id;
}

export function formatRunNextAction(action: RunNextAction): string {
  return runNextActionLabels[action.id] ?? action.id;
}

export function formatRunNextActionMessage(message: string): string {
  const exact: Record<string, string> = {
    "A harness action is already running; wait for the next safe checkpoint.":
      "harness 原子动作正在运行，等待下一个安全检查点。",
    "Recover a stale run lock after a stopped backend process before resuming.":
      "检测到可能是后端停止后遗留的运行锁。先恢复为 paused，再从已提交状态继续。",
    "A required project readiness gate failed and must be repaired before running.":
      "必要的项目准备门失败，需要先修复再运行。",
    "Continue the open-ended Book Direction discussion.":
      "继续开放式全书方向讨论。",
    "Explicitly approve the reviewed candidate Book Direction.":
      "明确批准已审阅的全书方向候选。",
    "Prepare the current Book Direction draft for review.":
      "将当前全书方向草稿整理为候选并进行独立审阅。",
    "Answer the remaining required book setup questions.": "请回答剩余的必填全书设定问题。",
    "Approve the collected book setup before the harness starts.":
      "请先批准已收集的全书设定，再启动 harness。",
    "Continue the existing harness run from committed state.":
      "从已提交状态继续现有 harness 运行。",
    "Inspect the latest harness failure before resuming.": "继续前请先检查最近一次 harness 失败。",
    "Participatory mode is waiting for approval of the current story arc plan.":
      "参与模式正在等待批准当前故事弧计划。",
    "Resume the harness from the latest committed checkpoint.":
      "从最近的已提交检查点继续 harness。",
    "Select an enabled LLM profile with a stored API key.":
      "请选择已启用且保存了 API Key 的 LLM 配置。",
    "Start the harness run.": "启动 harness 运行。"
  };
  if (exact[message]) {
    return exact[message];
  }
  const retry = message.match(/^Prepare a retry for the current chapter (.+)\.$/);
  if (retry) {
    return `准备重试当前章节 ${retry[1]}。`;
  }
  return message;
}

export function formatGateMessage(message: string): string {
  const exact: Record<string, string> = {
    "Book setup is approved.": "全书设定已批准。",
    "Book setup is approved but required book artifacts are incomplete.":
      "全书设定已批准，但必要的全书产物不完整。",
    "Book direction must be discussed, reviewed, and explicitly approved.":
      "全书方向需要经过讨论、候选审阅和用户明确批准。",
    "Book setup must be completed and approved before the harness can run.":
      "需要先完成并批准全书设定，harness 才能运行。",
    "Completion audit summarizes live-provider and literary-review evidence.":
      "完成审查会汇总真实 LLM 冒烟和文学审查证据。",
    "Completion audit summarizes output-secret, live-provider, and literary-review evidence.":
      "完成审查会汇总输出密钥、真实 LLM 冒烟和文学审查证据。",
    "Literary review waits for a completed live smoke project.":
      "文学审查需要等待真实 LLM 冒烟项目完成。",
    "Live provider smoke has not passed.": "真实 LLM 冒烟尚未通过。",
    "Literary/usefulness review approved the generated chapter and state patch.":
      "文学可用性审查已批准生成章节和状态补丁。",
    "Live provider smoke report and required artifacts are present.":
      "真实 LLM 冒烟报告和必要产物已齐全。",
    "Live smoke report references artifacts outside the smoke project.":
      "真实 LLM 冒烟报告引用了项目外的产物。",
    "Live smoke report references missing required artifacts.":
      "真实 LLM 冒烟报告引用的必要产物不存在。",
    "No live smoke report found. Run `npm.cmd run smoke:live -- --profile-id <profile-id>`.":
      "还没有真实 LLM 冒烟报告。请运行 `npm.cmd run smoke:live -- --profile-id <profile-id>`。",
    "Output contains configured LLM profile API keys or base URLs.":
      "输出项目中发现了已配置 LLM profile 的 API Key 或 base URL。",
    "Output contains no configured LLM profile API keys or base URLs.":
      "输出项目中没有发现已配置 LLM profile 的 API Key 或 base URL。",
    "Select an enabled LLM profile with a stored API key.":
      "请选择已启用且保存了 API Key 的 LLM 配置。"
  };
  if (exact[message]) {
    return exact[message];
  }
  return translateKnownMessagePatterns(message);
}

export function formatEvidence(value: string): string {
  const gateMatch = value.match(/^([a-z_]+):(passed|pending|failed)$/);
  if (gateMatch) {
    return `${formatGateId(gateMatch[1])}：${formatGateStatus(gateMatch[2])}`;
  }
  return (
    evidenceValueLabels[value] ??
    runStatusLabels[value] ??
    operationModeLabels[value as OperationMode] ??
    protocolLabels[value as LlmProtocol] ??
    value
  );
}

export function formatEventMessage(message: string): string {
  const exact: Record<string, string> = {
    "Approved book-level artifact committed after explicit user approval.":
      "用户明确批准后，正式全书产物已提交。",
    "Book direction discussion failed; no candidate state was advanced.":
      "全书方向讨论失败，候选状态没有推进。",
    "Book direction discussion model started.": "全书方向讨论模型已开始工作。",
    "Book discussion state changed; the stale model result was discarded.":
      "全书讨论状态已经更新，本次过期的模型结果已丢弃。",
    "Book discussion state changed; the stale review result was discarded.":
      "全书讨论状态已经更新，本次过期的审阅结果已丢弃。",
    "Book direction discussion turn completed and the candidate draft was updated.":
      "本轮全书方向讨论已完成，候选草稿已更新。",
    "Book direction synthesis or review failed; approval remains locked.":
      "全书方向整理或审阅失败，批准入口仍保持锁定。",
    "Candidate book direction review context assembled.":
      "候选全书方向的审阅上下文已装配。",
    "Candidate book-level artifact written for review.":
      "候选全书产物已写入，等待审阅。",
    "Candidate Book Direction has blocking issues and remains unapproved.":
      "候选全书方向存在阻断问题，仍未获批准。",
    "Candidate Book Direction is ready for explicit user approval.":
      "候选全书方向已通过审阅，等待用户明确批准。",
    "Controlled context assembled for the next book discussion turn.":
      "下一轮全书讨论的受控上下文已装配。",
    "Independently reviewing the candidate Book Direction.":
      "正在独立审阅候选全书方向。",
    "Synthesizing a candidate Book Direction for user review.":
      "正在整理供用户审阅的全书方向候选。",
    "The complete candidate Book Direction draft was updated.":
      "完整的候选全书方向草稿已更新。",
    "User explicitly approved the reviewed Book Direction.":
      "用户已明确批准经过审阅的全书方向。",
    "Book loop approved by user.": "用户已批准全书流程。",
    "Book setup answer recorded.": "全书设定回答已记录。",
    "Book setup has enough information for approval.": "全书设定信息已足够，可以批准。",
    "Book setup must be approved before the harness can continue.":
      "全书设定必须先批准，harness 才能继续。",
    "Book setup needs one more user decision before approval.":
      "全书设定还需要一个用户决策才能批准。",
    "Book setup next question personalized from prior answers.":
      "已根据前面的回答生成下一轮设定问题。",
    "Book setup question personalization failed; using default question.":
      "设定问题生成失败，已使用默认问题。",
    "Book setup follow-up assessment failed; setup can proceed to approval.":
      "设定追问评估失败，本轮设定仍可进入批准。",
    "Book-level feedback memo recorded.": "全书级反馈备忘已记录。",
    "Harness run resumed from committed state.": "Harness 已从已提交状态恢复运行。",
    "Harness run started.": "Harness 运行已启动。",
    "Manuscript export completed.": "全书稿件导出完成。",
    "Model visible output.": "模型可见输出。",
    "Pause requested; it will apply at the next safe checkpoint.":
      "已请求暂停，会在下一个安全检查点生效。",
    "Participatory mode requires approval of the current story arc plan.":
      "参与模式需要先批准当前故事弧计划。",
    "Recovered stale run lock; harness is paused and can resume from committed state.":
      "已恢复陈旧运行锁；harness 已停在 paused，可从已提交状态继续。",
    "Stale run recovery ignored because no run lock is present.":
      "没有正在遗留的运行锁，本次恢复请求已忽略。"
  };
  if (exact[message]) {
    return exact[message];
  }
  return translateKnownMessagePatterns(message);
}

export function formatEventStatusLine(event: HarnessEvent): string {
  const route = event.routing_decision
    ? ` · ${formatRoutingDecision(event.routing_decision)}`
    : "";
  return `${formatLoopLayer(event.loop_layer)} · ${formatAtomicAction(event.atomic_action)} · ${formatEventStatus(event.status)}${route}`;
}

export function formatEventFlag(status: ArtifactSummary["event_status"]): string | null {
  if (status === "recorded") {
    return "事件已记录";
  }
  if (status === "missing") {
    return "缺少事件";
  }
  return null;
}

export function formatSummaryFlags(summary: ArtifactSummary): string[] {
  return [
    summary.candidate ? "候选" : null,
    summary.committed ? "已提交" : null,
    summary.routing_decision
      ? `路由：${formatRoutingDecision(summary.routing_decision)}`
      : null,
    summary.profile_id ? `配置：${summary.profile_id}` : null,
    summary.model_snapshot ? `模型：${summary.model_snapshot}` : null,
    formatEventFlag(summary.event_status)
  ].filter((flag): flag is string => flag !== null);
}

export function formatLiteraryDecision(decision: LiteraryReviewDecision): string {
  return decision === "approved" ? "通过" : "不通过";
}

export function formatOptionalId(value: string | null | undefined): string {
  return value && value !== "none" ? value : "未设置";
}

function formatMapped(
  value: string | null | undefined,
  labels: Record<string, string>,
  fallback: string
): string {
  if (!value || value === "none" || value === "idle") {
    return labels[value ?? ""] ?? fallback;
  }
  return labels[value] ?? value;
}

function formatRetryScope(value: string): string {
  const labels: Record<string, string> = {
    chapter: "章节",
    state_patch: "状态补丁"
  };
  return labels[value] ?? value;
}

const evidenceValueLabels: Record<string, string> = {
  ending_tendency: "结局倾向",
  genre_promise: "类型承诺",
  no_active_runner: "当前进程没有活动 runner",
  reader_promise: "读者回报",
  protagonist_direction: "主角方向",
  world_constraints: "世界约束"
};

function translateKnownMessagePatterns(message: string): string {
  const activeReady = message.match(/^Active LLM profile is ready: (.+)$/);
  if (activeReady) {
    return `当前 LLM 配置已就绪：${activeReady[1]}`;
  }
  const activeDisabled = message.match(/^Active LLM profile is disabled: (.+)$/);
  if (activeDisabled) {
    return `当前 LLM 配置已禁用：${activeDisabled[1]}`;
  }
  const activeMissing = message.match(/^Active LLM profile is missing: (.+)$/);
  if (activeMissing) {
    return `当前 LLM 配置不存在：${activeMissing[1]}`;
  }
  const activeNoKey = message.match(/^Active LLM profile has no stored API key: (.+)$/);
  if (activeNoKey) {
    return `当前 LLM 配置没有保存 API Key：${activeNoKey[1]}`;
  }
  const runInProgress = message.match(/^A harness run is already in progress: (.+)$/);
  if (runInProgress) {
    return `已有 harness 运行正在进行：${formatRunStatus(runInProgress[1])}`;
  }
  const runControl = message.match(
    /^Run control accepts a start or resume command from status: (.+)$/
  );
  if (runControl) {
    return `当前状态允许启动或继续：${formatRunStatus(runControl[1])}`;
  }
  const smokeStatus = message.match(/^Live smoke report status is not passed: (.+)$/);
  if (smokeStatus) {
    return `真实 LLM 冒烟报告未通过：${smokeStatus[1]}`;
  }
  const smokeMissingEntries = message.match(
    /^Live smoke report is missing required artifact entries: (.+)$/
  );
  if (smokeMissingEntries) {
    return `真实 LLM 冒烟报告缺少必要产物条目：${smokeMissingEntries[1]}`;
  }
  const reviewMissing = message.match(/^Literary review has not been recorded: (.+)$/);
  if (reviewMissing) {
    return `尚未记录文学审查：${reviewMissing[1]}`;
  }
  const reviewRejected = message.match(/^Literary review decision is not approved: (.+)$/);
  if (reviewRejected) {
    return `文学审查结论未通过：${reviewRejected[1]}`;
  }
  const reviewMissingFields = message.match(/^Literary review is missing fields: (.+)$/);
  if (reviewMissingFields) {
    return `文学审查缺少字段：${reviewMissingFields[1]}`;
  }
  const exported = message.match(/^Exported (.+)$/);
  if (exported) {
    return `已导出：${exported[1]}`;
  }
  const retryPrepared = message.match(/^Retry prepared: (.+)$/);
  if (retryPrepared) {
    return `已准备重试：${retryPrepared[1]}`;
  }
  const recorded = message.match(/^Recorded (.+)$/);
  if (recorded) {
    return `已记录：${recorded[1]}`;
  }
  const noRunningPause = message.match(/^No running harness action to pause: (.+)\.$/);
  if (noRunningPause) {
    return `当前没有可暂停的 harness 动作：${formatRunStatus(noRunningPause[1])}。`;
  }
  const arcApproved = message.match(/^(.+) approved for chapter writing\.$/);
  if (arcApproved) {
    return `${arcApproved[1]} 已批准，可以进入章节写作。`;
  }
  const assembling = message.match(/^Assembling controlled context for (.+)\.$/);
  if (assembling) {
    return `正在为 ${assembling[1]} 装配受控上下文。`;
  }
  const snapshot = message.match(/^Context snapshot written for (.+)\.$/);
  if (snapshot) {
    return `${snapshot[1]} 的上下文快照已写入。`;
  }
  const generatingGoal = message.match(/^Generating goal for (.+)\.$/);
  if (generatingGoal) {
    return `正在生成 ${generatingGoal[1]} 的章节目标。`;
  }
  const goalWritten = message.match(/^Chapter goal written for (.+)\.$/);
  if (goalWritten) {
    return `${goalWritten[1]} 的章节目标已写入。`;
  }
  const drafting = message.match(/^Drafting (.+)\.$/);
  if (drafting) {
    return `正在写作 ${drafting[1]} 的候选正文。`;
  }
  const draftWritten = message.match(/^Candidate draft written for (.+)\.$/);
  if (draftWritten) {
    return `${draftWritten[1]} 的候选正文已写入。`;
  }
  const observations = message.match(/^Candidate observations written for (.+)\.$/);
  if (observations) {
    return `${observations[1]} 的候选观测已写入。`;
  }
  const review = message.match(/^Semantic review written for (.+)\.$/);
  if (review) {
    return `${review[1]} 的语义审查已写入。`;
  }
  const verifying = message.match(/^Verifying (.+)\.$/);
  if (verifying) {
    return `正在验证 ${verifying[1]}。`;
  }
  const verification = message.match(/^Verification completed for (.+)\.$/);
  if (verification) {
    return `${verification[1]} 的验证已完成。`;
  }
  const final = message.match(/^Final chapter prose committed for (.+)\.$/);
  if (final) {
    return `${final[1]} 的正式章节正文已提交。`;
  }
  const patchCandidate = message.match(/^Candidate state patch written for (.+)\.$/);
  if (patchCandidate) {
    return `${patchCandidate[1]} 的候选状态补丁已写入。`;
  }
  const patchCommitted = message.match(/^State patch committed for (.+)\.$/);
  if (patchCommitted) {
    return `${patchCommitted[1]} 的状态补丁已提交。`;
  }
  return message;
}
