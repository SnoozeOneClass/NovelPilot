import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
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
  listeners = new Map<string, (event: Event) => void>();

  constructor(_url: string) {
    FakeEventSource.instances.push(this);
  }

  addEventListener(kind: string, listener: (event: Event) => void) {
    this.listeners.set(kind, listener);
  }

  emit(kind: string, data?: object) {
    const event = data === undefined
      ? new Event(kind)
      : new MessageEvent(kind, { data: JSON.stringify(data) });
    this.listeners.get(kind)?.(event);
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
      failure_source_kind: "agent_task",
      blocking_task_id: "task-a",
      blocking_action_key: null,
      failure_code: "typed_output_invalid",
      failure_ref_id: "error-ref",
      started_at_ms: 1,
      finished_at_ms: null
    },
    book: {
      book_id: "book-a",
      lifecycle_status: "planning",
      current_baseline_id: null,
      latest_completion_review_id: null,
      current_progress_handoff_id: null,
      current_completion_id: null,
      baseline_version: null,
      approved_title: null,
      whole_book_scale_guidance: null,
      arc_contract_count: null,
      final_arc_ordinal: null,
      topology_effective_after_arc_ordinal: null,
      arc_topology: [],
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
    creator_input_request: null,
    recent_feedback: [],
    latest_event_sequence: 7,
    commands: [
      { command_id: "start_run", enabled: false, reason: "已开始" },
      { command_id: "pause_run", enabled: false, reason: "失败" },
      { command_id: "resume_run", enabled: false, reason: "失败不可继续" },
      { command_id: "retry_failed_task", enabled: true, reason: "显式重试" },
      { command_id: "retry_failed_action", enabled: false, reason: "失败来源不匹配" },
      { command_id: "send_book_input", enabled: false, reason: "失败" },
      { command_id: "approve_book", enabled: false, reason: "失败" },
      { command_id: "approve_arc", enabled: false, reason: "失败" },
      { command_id: "submit_feedback", enabled: false, reason: "失败" },
      { command_id: "export_markdown", enabled: false, reason: "未完成" }
    ],
    recent_tasks: []
  };
}

function creatorWaitState(): ProjectStateView {
  const state = failureState();
  return {
    ...state,
    project: {
      ...state.project,
      run_status: "waiting_for_user",
      wait_reason_code: "arc_parent_review_needs_user"
    },
    run: {
      ...state.run,
      status: "waiting_for_user",
      wait_reason_code: "arc_parent_review_needs_user",
      failure_source_kind: null,
      blocking_task_id: null,
      failure_code: null,
      failure_ref_id: null
    },
    creator_input_request: {
      review_kind: "arc_parent",
      review_id: "arc-parent-review-a",
      route_layer: "arc",
      book_id: "book-a",
      arc_id: "arc-a",
      automatic_correction_round: 0,
      question: {
        controlled_fact: "Whether the witness knowingly concealed the statement.",
        question: "Did the witness knowingly conceal the altered statement?",
        evidence: ["Committed evidence cannot establish the witness's private intent."]
      }
    },
    commands: state.commands.map((item) => (
      item.command_id === "submit_feedback"
        ? { ...item, enabled: true, reason: "Answer the creator-owned question." }
        : item.command_id === "retry_failed_task"
          ? { ...item, enabled: false, reason: "This is not a task failure." }
          : item
    ))
  };
}

function arcOutlineState(): ProjectStateView {
  const state = failureState();
  return {
    ...state,
    book: {
      ...state.book,
      lifecycle_status: "active",
      current_baseline_id: "book-baseline-a",
      baseline_version: 1,
      approved_title: "测试小说",
      whole_book_scale_guidance: "约二十章，仅作为创作者的规模建议。",
      arc_contract_count: 2,
      final_arc_ordinal: 2,
      topology_effective_after_arc_ordinal: 0,
      arc_topology: [
        {
          ordinal: 1,
          whole_book_role: "建立核心谜团",
          core_goal: "取得第一组可验证物证",
          handoff_from_previous: "承接创作者批准的前提",
          exit_conditions: ["形成通往终局弧的稳定交接"],
          is_final: false,
          lifecycle_status: "active"
        },
        {
          ordinal: 2,
          whole_book_role: "解决核心谜团",
          core_goal: "用正式证据完成全书承诺",
          handoff_from_previous: "承接 Arc 1 的正式收束",
          exit_conditions: ["所有 Book 完成要求均有正式证据"],
          is_final: true,
          lifecycle_status: "planned"
        }
      ]
    },
    current_arc: {
      arc_id: "arc-a",
      ordinal: 1,
      is_final: false,
      assigned_book_baseline_id: "book-baseline-a",
      lifecycle_status: "active",
      current_baseline_id: "arc-v2",
      latest_closure_review_id: null,
      current_closure_id: null,
      baseline_version: 2,
      closure_cumulative_chapter_count: 2,
      cumulative_committed_chapter_count: 1,
      arc_committed_chapter_count: 1,
      workspace_state: "idle",
      workspace_lock_version: 4,
      semantic_repair_count: 0,
      semantic_repair_limit: 5,
      pending_submission_id: null,
      pending_review_id: null,
      pending_review_decision: null,
      approval_gate_id: null,
      approval_gate_state: null,
      revision_origin: "automatic_arc_recovery",
      automatic_correction_round: 1,
      outline: {
        arc_id: "arc-a",
        arc_ordinal: 1,
        current_baseline_id: "arc-v2",
        current_baseline_version: 2,
        entries: [
          {
            book_ordinal: 1,
            arc_ordinal: 1,
            status: "committed",
            chapter_id: "chapter-1",
            actual_chapter_title: "正式第一章",
            assignment: {
              title: "第一章计划",
              core_event: "建立第一份矛盾证词。",
              hook: "把物证问题交给下一章。",
              scenes: ["发现矛盾", "保全证词"]
            },
            source_arc_baseline_id: "arc-v1",
            source_arc_baseline_version: 1
          },
          {
            book_ordinal: 2,
            arc_ordinal: 2,
            status: "planned",
            chapter_id: null,
            actual_chapter_title: null,
            assignment: {
              title: "未来章",
              core_event: "验证后继 Arc 计划中的物证。",
              hook: "把完整证据交给收束评估。",
              scenes: ["复核物证", "形成结论"]
            },
            source_arc_baseline_id: "arc-v2",
            source_arc_baseline_version: 2
          }
        ]
      }
    }
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

  it("replaces abandoned live prose when a frozen task attempt restarts", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <ThemeProvider><App /></ThemeProvider>
      </QueryClientProvider>
    );
    await screen.findByText("失败后暂停");
    const source = FakeEventSource.instances[0];
    const base = {
      project_id: "project-a",
      task_id: "chapter-draft",
      attempt_id: "attempt-a",
      delta: null,
      provider_request_number: null,
      provider_request_limit: null,
      reason: null,
      retry_delay_ms: null
    };

    source?.emit("agent_live", { ...base, kind: "task_started" });
    source?.emit("agent_live", { ...base, kind: "prose_delta", delta: "abandoned prose" });
    expect(await screen.findByText("abandoned prose")).toBeInTheDocument();

    source?.emit("agent_live", {
      ...base,
      kind: "attempt_restarting",
      provider_request_number: 1,
      provider_request_limit: 6,
      reason: "provider_stream_incomplete",
      retry_delay_ms: 1000
    });
    await waitFor(() => expect(screen.queryByText("abandoned prose")).not.toBeInTheDocument());

    source?.emit("agent_live", { ...base, kind: "prose_delta", delta: "replacement prose" });
    expect(await screen.findByText("replacement prose")).toBeInTheDocument();
  });

  it("renders an owning-layer creator question and submits a deferred answer", async () => {
    const state = creatorWaitState();
    state.recent_feedback = [{
      feedback_id: "queued-feedback-a",
      feedback_kind: "unsolicited",
      status: "routed",
      content: "Preserve the witness's ambiguity.",
      route_layer: "arc",
      book_id: "book-a",
      arc_id: "arc-a",
      chapter_id: null,
      captured_run_id: "run-a",
      captured_book_baseline_id: "book-baseline-a",
      captured_arc_baseline_id: "arc-baseline-a",
      captured_chapter_baseline_id: null,
      arc_parent_review_id: null,
      book_parent_review_id: null,
      arc_closure_review_id: null,
      book_completion_review_id: null,
      resulting_correction_lineage_id: null,
      dismiss_reason_code: null,
      applied_command_id: null,
      created_at_ms: 3,
      routed_at_ms: 3,
      applied_at_ms: null
    }];
    api.getProject.mockResolvedValue(state);
    api.submitFeedback.mockResolvedValue({
      receipt_id: "feedback-receipt",
      replayed: false,
      state
    });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <ThemeProvider><App /></ThemeProvider>
      </QueryClientProvider>
    );

    expect(
      await screen.findByText("Did the witness knowingly conceal the altered statement?")
    ).toBeInTheDocument();
    expect(screen.getByText("Preserve the witness's ambiguity.")).toBeInTheDocument();
    expect(screen.getByText("已排队；当前原子动作不受影响")).toBeInTheDocument();
    const layer = screen.getAllByRole("combobox").find(
      (item) => (item as HTMLSelectElement).value === "arc"
    );
    expect(layer).toBeDefined();
    expect(layer).toBeDisabled();
    const answer = screen.getByPlaceholderText(
      "回答上面的创作者问题。输入会在当前原子动作结束后注入。"
    );
    fireEvent.change(answer, {
      target: { value: "Yes. The witness concealed it to protect Mara." }
    });
    fireEvent.click(screen.getByRole("button", { name: "提交回答" }));
    await waitFor(() => expect(api.submitFeedback).toHaveBeenCalledWith(
      "project-a",
      {
        content: "Yes. The witness concealed it to protect Mara.",
        route_layer: "arc"
      },
      expect.any(String)
    ));
  });

  it("renders one coherent Arc outline with status and version provenance", async () => {
    api.getProject.mockResolvedValue(arcOutlineState());
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <ThemeProvider><App /></ThemeProvider>
      </QueryClientProvider>
    );

    expect(await screen.findByText("正式第一章")).toBeInTheDocument();
    expect(screen.getByText("原规划：第一章计划")).toBeInTheDocument();
    expect(screen.getByText("未来章")).toBeInTheDocument();
    expect(screen.getByText("场景：发现矛盾 → 保全证词")).toBeInTheDocument();
    expect(screen.getByText("Arc v1")).toBeInTheDocument();
    expect(screen.getByText("Arc v2")).toBeInTheDocument();
    expect(screen.getByText("正式 Arc 拓扑")).toBeInTheDocument();
    expect(
      screen.getAllByText("约二十章，仅作为创作者的规模建议。")
    ).toHaveLength(2);
    expect(screen.getByText("建立核心谜团")).toBeInTheDocument();
    expect(screen.getByText("用正式证据完成全书承诺")).toBeInTheDocument();
    expect(screen.getByText("Arc 2 · 最终弧")).toBeInTheDocument();
  });
});
