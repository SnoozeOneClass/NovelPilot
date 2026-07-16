import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { CurrentArcState, HarnessEvent, ProjectReadiness, ProjectSummary } from "../../types/domain";
import { CreationView } from "./CreationView";

const project: ProjectSummary = {
  name: "project-1",
  title: "退潮前的十一分钟",
  path: "output/project-1",
  metadata: {
    schema_version: 1,
    project_id: "project-1",
    title: "退潮前的十一分钟",
    operation_mode: "participatory",
    active_profile_id: "main",
    active_arc_id: null,
    active_chapter_id: null,
    run_status: "idle",
    created_at: "2026-07-15T00:00:00Z",
    updated_at: "2026-07-15T00:00:00Z"
  }
};

function readiness(id: ProjectReadiness["next_action"]["id"]): ProjectReadiness {
  return {
    status: "pending",
    can_start_run: true,
    gates: [],
    next_action: {
      id,
      command: null,
      requires_user: id !== "resume_run",
      can_auto_continue: id === "resume_run",
      message: id,
      evidence: []
    }
  };
}

function harnessEvent(eventId: string, kind: string, payload: Record<string, unknown>): HarnessEvent {
  return {
    seq: Number(eventId.replace(/\D/g, "")) || null,
    event_id: eventId,
    timestamp: "2026-07-15T00:00:00Z",
    project_id: "project-1",
    run_id: "run-1",
    kind,
    loop_layer: kind === "run_started" ? "system" : "chapter",
    atomic_action: kind === "run_started" ? null : "run_chapter_agent",
    status: "started",
    artifact_path: null,
    routing_decision: null,
    message: kind,
    payload
  };
}

function failedEvent(eventId: string, overrides: Partial<HarnessEvent> = {}): HarnessEvent {
  return {
    ...harnessEvent(eventId, "agent_tool_result", {}),
    status: "failed",
    message: "Agent Tool call was rejected by Harness validation.",
    ...overrides
  };
}

function renderCreation(overrides: Partial<React.ComponentProps<typeof CreationView>> = {}) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const props: React.ComponentProps<typeof CreationView> = {
    project,
    events: [],
    currentArc: null,
    summaries: [],
    readiness: readiness("start_run"),
    bookRevision: null,
    busy: false,
    feedback: "",
    sendingFeedback: false,
    onFeedbackChange: vi.fn(),
    onSendFeedback: vi.fn().mockResolvedValue(true),
    onRequestArcRevision: vi.fn().mockResolvedValue(true),
    onStart: vi.fn().mockResolvedValue(undefined),
    onApproveArc: vi.fn().mockResolvedValue(true),
    onApproveBookRevision: vi.fn().mockResolvedValue(undefined),
    onRetryFailedRun: vi.fn().mockResolvedValue(undefined),
    onRetryChapter: vi.fn().mockResolvedValue(undefined),
    onRecoverStale: vi.fn().mockResolvedValue(undefined),
    onSelectArtifact: vi.fn(),
    ...overrides
  };
  return { props, ...render(<QueryClientProvider client={queryClient}><CreationView {...props} /></QueryClientProvider>) };
}

describe("CreationView", () => {
  it("shows one explicit start and no normal pause or resume controls", async () => {
    const user = userEvent.setup();
    const onStart = vi.fn().mockResolvedValue(undefined);
    renderCreation({ onStart });
    await user.click(screen.getByRole("button", { name: "开始创作" }));
    expect(onStart).toHaveBeenCalledOnce();
    expect(screen.queryByRole("button", { name: /暂停|恢复|继续/ })).not.toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "创作反馈" })).toBeInTheDocument();
  });

  it("does not present a recovered pre-run Book failure as the current blocker", () => {
    renderCreation({
      events: [
        failedEvent("event-26", { run_id: null, loop_layer: "book", atomic_action: "continue_book_discussion" }),
        {
          ...harnessEvent("event-40", "agent_tool_result", {}),
          run_id: null,
          loop_layer: "book",
          atomic_action: "continue_book_discussion",
          status: "completed"
        },
        {
          ...harnessEvent("event-41", "user_feedback", { feedback: "后续保持公平推理。" }),
          run_id: null,
          loop_layer: "book",
          status: "completed"
        }
      ]
    });

    expect(screen.getByRole("button", { name: "开始创作" })).toBeInTheDocument();
    expect(screen.queryByText("当前步骤未能继续")).not.toBeInTheDocument();
    expect(screen.queryByText("Agent Tool call was rejected by Harness validation.")).not.toBeInTheDocument();
    expect(screen.getByText("后续保持公平推理。")).toBeInTheDocument();
  });

  it("shows the latest failure and explicitly retries the current failed step", async () => {
    const user = userEvent.setup();
    const onRetryFailedRun = vi.fn().mockResolvedValue(undefined);
    renderCreation({
      project: { ...project, metadata: { ...project.metadata, run_status: "failed" } },
      readiness: readiness("retry_failed_run"),
      events: [
        harnessEvent("event-100", "run_started", {}),
        failedEvent("event-101")
      ],
      onRetryFailedRun
    });

    expect(screen.getByText("当前步骤未能继续")).toBeInTheDocument();
    expect(screen.getByText("Agent Tool call was rejected by Harness validation.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "重试当前步骤" }));
    expect(onRetryFailedRun).toHaveBeenCalledOnce();
  });

  it("labels a provider failure as reconnecting without submitting feedback", async () => {
    const user = userEvent.setup();
    const onRetryFailedRun = vi.fn().mockResolvedValue(undefined);
    const onSendFeedback = vi.fn().mockResolvedValue(true);
    renderCreation({
      project: { ...project, metadata: { ...project.metadata, run_status: "failed" } },
      readiness: readiness("retry_provider_connection"),
      events: [
        harnessEvent("event-110", "run_started", {}),
        failedEvent("event-111", { message: "Provider authentication is unavailable." })
      ],
      onRetryFailedRun,
      onSendFeedback
    });

    expect(screen.getByText(/模型服务连接意外中断/)).toBeInTheDocument();
    expect(screen.queryByText("Provider authentication is unavailable.")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "重新连接并继续" }));
    expect(onRetryFailedRun).toHaveBeenCalledOnce();
    expect(onSendFeedback).not.toHaveBeenCalled();
  });

  it("keeps the current run failure visible during chapter recovery", () => {
    renderCreation({
      readiness: readiness("retry_current_chapter"),
      events: [
        harnessEvent("event-200", "run_started", {}),
        failedEvent("event-201", { message: "Chapter evaluation exhausted its repair limit." })
      ]
    });

    expect(screen.getByText("当前步骤未能继续")).toBeInTheDocument();
    expect(screen.getByText("Chapter evaluation exhausted its repair limit.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "继续自动修订" })).toBeInTheDocument();
  });

  it("renders actual streamed prose as a read-only document", () => {
    renderCreation({
      project: {
        ...project,
        metadata: { ...project.metadata, run_status: "running", active_chapter_id: "chapter-001" }
      },
      readiness: readiness("resume_run"),
      events: [
        harnessEvent("event-1", "run_started", {}),
        harnessEvent("event-2", "chapter_draft_stream_started", {
          chapter_id: "chapter-001",
          stream_id: "stream-1",
          tool_call_id: "call-1"
        }),
        harnessEvent("event-3", "chapter_draft_delta", {
          chapter_id: "chapter-001",
          stream_id: "stream-1",
          text_delta: "潮水从封站边缘退去。"
        })
      ]
    });
    expect(screen.getByText("潮水从封站边缘退去。")).toBeInTheDocument();
    expect(screen.getByText("实时草稿")).toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: /正文/ })).not.toBeInTheDocument();
  });

  it("keeps arc approval and revision feedback in the same main task", async () => {
    const currentArc: CurrentArcState = {
      arc_id: "arc-001",
      status: "planned",
      plan_path: "arcs/arc-001/plan.md",
      human_review: "awaiting_review",
      approved_at: null,
      recommended_target_chapter_count: 10,
      target_chapter_count: 10,
      completed_chapter_ids: [],
      completed_at: null
    };
    const onApproveArc = vi.fn().mockResolvedValue(true);
    renderCreation({
      project: { ...project, metadata: { ...project.metadata, active_arc_id: "arc-001", run_status: "waiting_for_user" } },
      readiness: readiness("approve_story_arc"),
      currentArc,
      onApproveArc
    });
    expect(screen.getByRole("textbox", { name: "故事弧修改意见" })).toBeInTheDocument();
    expect(screen.getAllByRole("textbox")).toHaveLength(1);
    await userEvent.click(screen.getByRole("button", { name: /批准计划并开始章节创作/ }));
    expect(onApproveArc).toHaveBeenCalledWith(10);
  });
});
