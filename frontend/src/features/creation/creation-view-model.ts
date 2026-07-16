import type {
  BookRevisionState,
  CurrentArcState,
  HarnessEvent,
  ProjectReadiness,
  ProjectSummary
} from "../../types/domain";

export type CreationStage =
  | "ready_to_start"
  | "book_revision"
  | "story_arc_review"
  | "planning_arc"
  | "writing_chapter"
  | "evaluating_chapter"
  | "repairing_chapter"
  | "continuing"
  | "chapter_recovery"
  | "failed"
  | "completed";

export type CreationPrimaryAction =
  | "start"
  | "approve_story_arc"
  | "approve_book_revision"
  | "retry_chapter"
  | "retry_failed_run"
  | "recover_stale"
  | null;

export interface CreationViewModel {
  stage: CreationStage;
  eyebrow: string;
  title: string;
  description: string;
  primaryAction: CreationPrimaryAction;
  isRunning: boolean;
  hasStarted: boolean;
}

interface CreationViewModelInput {
  project: ProjectSummary;
  readiness: ProjectReadiness | null;
  currentArc: CurrentArcState | null;
  bookRevision: BookRevisionState | null;
  events: HarnessEvent[];
}

function hasRunStarted(events: HarnessEvent[]): boolean {
  return events.some((event) => event.kind === "run_started" || event.kind === "run_resumed");
}

export function deriveCreationViewModel({
  project,
  readiness,
  currentArc,
  bookRevision,
  events
}: CreationViewModelInput): CreationViewModel {
  const metadata = project.metadata;
  const started = hasRunStarted(events);
  const nextAction = readiness?.next_action;
  const isRunning = metadata.run_status === "running" || metadata.run_status === "pause_requested";

  if (bookRevision) {
    return {
      stage: "book_revision",
      eyebrow: "全书契约审查",
      title: "未来方向需要你的批准",
      description: "上层修订已通过评测，只有明确批准后才会替换未来全书契约。",
      primaryAction: "approve_book_revision",
      isRunning,
      hasStarted: started
    };
  }
  if (currentArc?.human_review === "awaiting_review" || nextAction?.id === "approve_story_arc") {
    return {
      stage: "story_arc_review",
      eyebrow: "故事弧审查",
      title: `${currentArc?.arc_id ?? "当前故事弧"} 已完成规划`,
      description: "审阅本故事弧的目标、冲突和章节节奏；批准后会自动进入章节创作。",
      primaryAction: "approve_story_arc",
      isRunning,
      hasStarted: started
    };
  }
  if (!started && nextAction?.id === "start_run") {
    return {
      stage: "ready_to_start",
      eyebrow: "方向已批准",
      title: "准备开始连续创作",
      description: "开始后将自动规划故事弧并持续写作，只在故事弧审查或异常失败时等待你。",
      primaryAction: "start",
      isRunning,
      hasStarted: false
    };
  }
  if (nextAction?.id === "retry_current_chapter") {
    return {
      stage: "chapter_recovery",
      eyebrow: "自动修订已停止",
      title: "当前章节需要继续自动修订",
      description: "正文和候选证据已保留。你可以继续一次有界自动修订，或先查看详细证据。",
      primaryAction: "retry_chapter",
      isRunning,
      hasStarted: started
    };
  }
  if (nextAction?.id === "retry_provider_connection" || nextAction?.id === "retry_failed_run") {
    const providerFailure = nextAction.id === "retry_provider_connection";
    return {
      stage: "failed",
      eyebrow: providerFailure ? "模型连接已中断" : "创作步骤未能继续",
      title: "Harness 已保留现场",
      description: providerFailure
        ? "旧候选与失败证据均已保留；模型恢复后可从已提交状态重新执行失败步骤。"
        : "当前失败可以从已提交状态开启一次新的有界尝试；旧现场仍保留，不会覆盖已提交正文。",
      primaryAction: "retry_failed_run",
      isRunning,
      hasStarted: started
    };
  }
  if (nextAction?.id === "recover_stale_run") {
    return {
      stage: "failed",
      eyebrow: "运行异常中断",
      title: "需要恢复异常运行状态",
      description: "仅在确认后台已无活跃请求时恢复；这不是正常创作步骤。",
      primaryAction: "recover_stale",
      isRunning,
      hasStarted: started
    };
  }
  if (metadata.run_status === "failed" || nextAction?.id === "inspect_failure") {
    return {
      stage: "failed",
      eyebrow: "创作未能继续",
      title: "Harness 已保留现场",
      description: "查看详细证据确认失败原因；已提交正文和正史不会被静默覆盖。",
      primaryAction: null,
      isRunning,
      hasStarted: started
    };
  }
  if (isRunning && metadata.active_chapter_id) {
    const latestAction = [...events].reverse().find((event) => event.loop_layer === "chapter")?.atomic_action;
    const evaluating = latestAction?.includes("evaluation") || (
      latestAction === "run_chapter_agent" && events.at(-1)?.kind.includes("evaluation")
    );
    const repairing = latestAction?.includes("repair") || latestAction === "commit_state_patch";
    return {
      stage: repairing ? "repairing_chapter" : evaluating ? "evaluating_chapter" : "writing_chapter",
      eyebrow: `正在创作 ${metadata.active_chapter_id}`,
      title: repairing ? "正在核对章节证据" : evaluating ? "正在审查章节候选" : "章节正文正在生成",
      description: "正文会留在页面中持续可读，后续审查和提交不会用等待遮罩替换它。",
      primaryAction: null,
      isRunning: true,
      hasStarted: started
    };
  }
  if (isRunning || nextAction?.id === "resume_run" && nextAction.can_auto_continue) {
    return {
      stage: "continuing",
      eyebrow: "连续创作",
      title: "正在进入下一个内部阶段",
      description: "这是自动衔接，不需要手动点击继续。",
      primaryAction: null,
      isRunning,
      hasStarted: started
    };
  }
  if (metadata.active_arc_id) {
    return {
      stage: "planning_arc",
      eyebrow: "故事弧规划",
      title: `正在准备 ${metadata.active_arc_id}`,
      description: "计划完成后会在当前页面进入故事弧审查。",
      primaryAction: null,
      isRunning,
      hasStarted: started
    };
  }
  return {
    stage: "completed",
    eyebrow: "创作状态",
    title: started ? "等待下一项可执行任务" : "等待开始创作",
    description: "当前没有需要人工处理的创作任务。",
    primaryAction: null,
    isRunning,
    hasStarted: started
  };
}
