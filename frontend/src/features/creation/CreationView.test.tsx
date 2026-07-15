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
