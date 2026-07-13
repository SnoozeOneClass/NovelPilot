import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { HarnessEvent, ProjectReadiness, ProjectSummary } from "../../types/domain";
import { WorkbenchView } from "./WorkbenchView";

const project: ProjectSummary = {
  name: "project-1",
  title: "测试小说",
  path: "D:/output/project-1",
  metadata: {
    schema_version: 1,
    project_id: "project-1",
    title: "测试小说",
    operation_mode: "participatory",
    active_profile_id: "profile-1",
    active_arc_id: null,
    active_chapter_id: null,
    run_status: "idle",
    created_at: "2026-07-13T00:00:00Z",
    updated_at: "2026-07-13T00:00:00Z"
  }
};

const readiness: ProjectReadiness = {
  status: "passed",
  can_start_run: true,
  gates: [{ id: "book_setup", status: "passed", required: true, message: "方向已批准", evidence: [] }],
  next_action: { id: "start_run", command: "start", requires_user: true, can_auto_continue: false, message: "可以启动", evidence: [] }
};

const handlers = {
  onStart: vi.fn(async () => undefined),
  onResume: vi.fn(async () => undefined),
  onExport: vi.fn(async () => undefined),
  onSelectArtifact: vi.fn(),
  onOpenEvidence: vi.fn(),
  onOpenStory: vi.fn()
};

function renderWorkbench(events: HarnessEvent[] = [], modelOutput = "") {
  render(
    <WorkbenchView
      project={events.length ? { ...project, metadata: { ...project.metadata, run_status: "running", active_arc_id: "arc-001" } } : project}
      events={events}
      currentArc={null}
      summaries={[]}
      modelOutput={modelOutput}
      activeArtifact={null}
      canonCounts={{}}
      readiness={readiness}
      canStart={!events.length}
      canResume={false}
      busy={false}
      {...handlers}
    />
  );
}

describe("WorkbenchView", () => {
  it("shows readiness gates and the start command while idle", () => {
    renderWorkbench();

    expect(screen.getByText("全书设定")).toBeInTheDocument();
    expect(screen.getByText("通过")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /启动/ })).toBeEnabled();
    expect(screen.getByText("等待启动 Harness")).toBeInTheDocument();
  });

  it("shows harness events and provider-visible output while running", () => {
    const event: HarnessEvent = {
      seq: 1,
      event_id: "event-1",
      timestamp: "2026-07-13T01:00:00Z",
      project_id: "project-1",
      run_id: "run-1",
      kind: "atomic_action_started",
      loop_layer: "story_arc",
      atomic_action: "plan_current_arc",
      status: "started",
      artifact_path: null,
      routing_decision: null,
      message: "正在规划当前故事弧",
      payload: {}
    };

    renderWorkbench([event], "这是 Provider 返回的可见输出");

    expect(screen.getByText("事件流")).toBeInTheDocument();
    expect(screen.getAllByText("正在规划当前故事弧")).not.toHaveLength(0);
    expect(screen.getByText("这是 Provider 返回的可见输出")).toBeInTheDocument();
  });

  it("scopes pipeline progress and checkpoints to the active chapter", () => {
    const previousCheckpoint: HarnessEvent = {
      seq: 1, event_id: "old", timestamp: "2026-07-13T00:00:00Z", project_id: "project-1", run_id: "run-1", kind: "safe_checkpoint_reached", loop_layer: "chapter", atomic_action: "semantic_review", status: "completed", artifact_path: "chapters/chapter-001/review.md", routing_decision: null, message: "chapter-001 complete", payload: { chapter_id: "chapter-001" }
    };
    const currentDraft: HarnessEvent = {
      ...previousCheckpoint, seq: 2, event_id: "current", kind: "atomic_action_started", atomic_action: "assemble_context", status: "started", artifact_path: null, message: "Assembling controlled context for chapter-002.", payload: {}
    };
    render(
      <WorkbenchView
        project={{ ...project, metadata: { ...project.metadata, active_chapter_id: "chapter-002", run_status: "running" } }}
        events={[previousCheckpoint, currentDraft]}
        currentArc={null}
        summaries={[]}
        modelOutput=""
        activeArtifact={null}
        canonCounts={{}}
        readiness={readiness}
        canStart={false}
        canResume={false}
        busy={false}
        {...handlers}
      />
    );

    const pipeline = screen.getByRole("heading", { name: "章节 Pipeline" }).parentElement as HTMLElement;
    expect(within(pipeline).getByText("装配上下文").closest("div")).toHaveTextContent("进行中");
    expect(within(pipeline).getByText("语义审查").closest("div")).toHaveTextContent("等待");
    expect(screen.getByText("安全检查点").closest("div")).toHaveTextContent("等待中");
  });
});
