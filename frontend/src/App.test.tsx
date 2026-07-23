import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ProjectStateView } from "./types/workspace";
import { ThemeProvider } from "./app/theme";

const api = vi.hoisted(() => ({
  listProjects: vi.fn(),
  profiles: vi.fn(),
  getProject: vi.fn(),
  eventStreamUrl: vi.fn(() => "/events"),
  runControl: vi.fn(),
  createProject: vi.fn(),
  deleteProject: vi.fn(),
  updateSettings: vi.fn(),
  sendBookInput: vi.fn(),
  approveBook: vi.fn(),
  approveArc: vi.fn(),
  submitFeedback: vi.fn(),
  exportManuscript: vi.fn()
}));

vi.mock("./api/workspace-client", () => ({ workspaceApi: api }));

import { App } from "./App";

class FakeEventSource {
  static instances: FakeEventSource[] = [];
  listeners = new Map<string, () => void>();

  constructor(_url: string) {
    FakeEventSource.instances.push(this);
  }

  addEventListener(kind: string, listener: () => void) {
    this.listeners.set(kind, listener);
  }

  emit(kind: string) {
    this.listeners.get(kind)?.();
  }

  close() {}
}

function failureState(): ProjectStateView {
  return {
    project: {
      project_id: "project-a",
      title: "测试小说",
      operation_mode: "participatory",
      lifecycle_status: "active",
      run_status: "failure_paused",
      wait_reason_code: "agent_task_failed",
      current_arc_id: null,
      current_chapter_id: null,
      committed_chapter_count: 0,
      created_at_ms: 1,
      updated_at_ms: 2
    },
    settings_lock_version: 1,
    default_profile_id: "grok-4.5",
    book_profile_id: null,
    arc_profile_id: null,
    chapter_profile_id: null,
    evaluator_profile_id: null,
    run: {
      run_id: "run-a",
      run_number: 1,
      status: "failure_paused",
      desired_state: "running",
      lock_version: 3,
      wait_reason_code: "agent_task_failed",
      blocking_task_id: "task-a",
      failure_code: "typed_output_invalid",
      failure_ref_id: "error-ref",
      started_at_ms: 1,
      finished_at_ms: null
    },
    book: {
      book_id: "book-a",
      lifecycle_status: "planning",
      current_baseline_id: null,
      baseline_version: null,
      approved_title: null,
      minimum_chapter_count: null,
      maximum_chapter_count: null,
      workspace_state: "drafting",
      workspace_lock_version: 1,
      semantic_repair_count: 0,
      semantic_repair_limit: 5,
      discussion: {
        schema_id: "book-discussion-state-v1",
        turn_count: 0,
        direction_draft: "",
        discussion_summary: "",
        confirmed_decisions: [],
        superseded_decisions: [],
        unresolved_questions: [],
        assumptions: [],
        contradictions: [],
        selected_title: null,
        selected_title_source: null,
        question: null,
        suggestions: [],
        readiness_status: "awaiting_agent",
        readiness_reason: "等待 Agent"
      },
      transcript: { schema_id: "book-transcript-v1", messages: [] },
      pending_submission_id: null,
      pending_review_id: null,
      pending_review_decision: null
    },
    current_arc: null,
    current_chapter: null,
    latest_event_sequence: 7,
    commands: [
      { command_id: "start_run", enabled: false, reason: "已开始" },
      { command_id: "pause_run", enabled: false, reason: "失败" },
      { command_id: "resume_run", enabled: false, reason: "失败不可继续" },
      { command_id: "retry_failed_task", enabled: true, reason: "显式重试" },
      { command_id: "send_book_input", enabled: false, reason: "失败" },
      { command_id: "approve_book", enabled: false, reason: "失败" },
      { command_id: "approve_arc", enabled: false, reason: "失败" },
      { command_id: "submit_feedback", enabled: false, reason: "失败" },
      { command_id: "export_markdown", enabled: false, reason: "未完成" }
    ],
    recent_tasks: []
  };
}

describe("App authoritative workspace", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    FakeEventSource.instances = [];
    vi.stubGlobal("EventSource", FakeEventSource);
    window.localStorage.setItem("novelpilot.workspace.project-id", "project-a");
    api.listProjects.mockResolvedValue([]);
    api.profiles.mockResolvedValue({ selected_profile_id: null, profiles: [] });
    api.getProject.mockResolvedValue(failureState());
  });

  it("never turns a read or SSE refresh into resume/retry", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <ThemeProvider><App /></ThemeProvider>
      </QueryClientProvider>
    );

    expect(await screen.findByText("流程已在失败边界暂停")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "继续" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "重试失败任务" })).toBeEnabled();
    expect(api.runControl).not.toHaveBeenCalled();

    FakeEventSource.instances[0]?.emit("domain_event");
    await waitFor(() => expect(api.getProject.mock.calls.length).toBeGreaterThan(1));
    expect(api.runControl).not.toHaveBeenCalled();
  });
});
