import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../../api/client";
import type { BookDirectionCandidate, HarnessEvent, SetupStateDocument } from "../../types/domain";
import { SetupConversation } from "./SetupConversation";

const candidate: BookDirectionCandidate = {
  revision: 1,
  created_at: "2026-07-13T00:00:00Z",
  direction_markdown: "# 全书方向\n\n这是一份经过综合的候选方向。",
  constraints: {
    confirmed: ["线索必须公平"],
    must_preserve: ["人物关系持续变化"],
    must_avoid: ["临时设定解围"],
    creative_freedoms: ["当前故事弧自由规划"],
    open_decisions: []
  },
  confirmed_decision_coverage: [{ decision: "线索必须公平", candidate_evidence: "公平线索" }],
  recommended_titles: [
    { title: "星潮之下", rationale: "对应核心意象" },
    { title: "无声潮汐", rationale: "突出封闭空间的压迫感" },
    { title: "第七码头", rationale: "强调旧案留下的异常编号" },
    { title: "退潮证词", rationale: "对应公平线索与证词冲突" }
  ],
  rolling_plan_markdown: "只规划当前故事弧。",
  review: { status: "passed", summary: "候选可以批准。", issues: [], signals: ["confirmed_decision_coverage:1/1"] },
  direction_path: "book/reviews/1/direction.md",
  constraints_path: "book/reviews/1/constraints.json",
  title_suggestions_path: "book/reviews/1/titles.json",
  rolling_plan_path: "book/reviews/1/plan.md",
  verification_path: "book/reviews/1/verification.json",
  profile_id: "main",
  model_snapshot: "model",
  review_model_snapshot: "review-model"
};

function setupState(withCandidate = false): SetupStateDocument {
  return {
    schema_version: 2,
    revision: withCandidate ? 3 : 2,
    phase: withCandidate ? "review_ready" : "discussing",
    approved: false,
    approved_at: null,
    approved_title: null,
    selected_title: withCandidate ? "星潮之下" : null,
    title_selection_source: null,
    migrated_from_schema_version: null,
    turn_count: 1,
    candidate_revision_counter: withCandidate ? 1 : 0,
    messages: [
      { id: "m1", turn: 1, role: "user", content: "我要公平线索。", created_at: "2026-07-13T00:00:00Z", profile_id: null, model_snapshot: null, migrated: false },
      { id: "m2", turn: 1, role: "assistant", content: "我会把公平线索作为全书方向的硬约束。", created_at: "2026-07-13T00:00:01Z", profile_id: "main", model_snapshot: "model", migrated: false }
    ],
    direction_draft: "# 全书方向\n\n公平线索。",
    discussion_summary: "已确认公平线索。",
    confirmed_decisions: ["线索必须公平"],
    superseded_decisions: [],
    unresolved_questions: [],
    assumptions: [],
    contradictions: [],
    question: withCandidate ? null : "退休档案员是否计入六名核心人物？",
    suggestions: withCandidate ? [] : [
      { id: "s1", label: "计入六人", message: "退休档案员计入六名核心人物，岛上共有六名旧案相关者。", rationale: "人物规模更紧凑，也更容易维持群像辨识度。", recommended: true },
      { id: "s2", label: "六人之外", message: "退休档案员不计入六名核心人物，岛上共有七名旧案相关者。", rationale: "关系网更复杂，但会增加前期认知负担。", recommended: false }
    ],
    readiness: withCandidate
      ? { status: "ready", reason: "方向已经足够具体。" }
      : { status: "continue", reason: "还需要确认一项创作原则。" },
    candidate: withCandidate ? candidate : null,
    direction_draft_version_path: "book/versions/direction.md",
    discussion_state_version_path: "book/versions/state.json",
    discussion_transcript_version_path: "book/versions/transcript.jsonl",
    last_context_snapshot_path: null,
    last_profile_id: "main",
    last_model_snapshot: "model"
  };
}

function reviewableSetupState(): SetupStateDocument {
  const state = setupState();
  state.selected_title = "星潮之下";
  state.question = null;
  state.suggestions = [];
  state.readiness = { status: "ready", reason: "方向与正式书名已经确认。" };
  return state;
}

describe("SetupConversation", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    window.sessionStorage.clear();
  });

  it("presents one question with model choices and a custom-answer option", async () => {
    const user = userEvent.setup();
    vi.spyOn(api, "setupState").mockResolvedValue(setupState());
    const turnSpy = vi.spyOn(api, "continueSetupDiscussion").mockResolvedValue(setupState());
    render(<SetupConversation projectId="project-1" onApproved={() => undefined} onExit={() => undefined} />);

    expect(await screen.findByRole("heading", { name: "退休档案员是否计入六名核心人物？" })).toBeInTheDocument();
    expect(screen.getByText("人物规模更紧凑，也更容易维持群像辨识度。")).toBeInTheDocument();
    expect(screen.getByText("推荐")).toBeInTheDocument();
    const input = screen.getByPlaceholderText("选择建议后可以继续编辑，或直接输入自己的回答...");
    await user.type(input, "已有补充");
    await user.click(screen.getByRole("button", { name: /计入六人/ }));

    expect(input).toHaveValue("退休档案员计入六名核心人物，岛上共有六名旧案相关者。");
    expect(turnSpy).not.toHaveBeenCalled();
    await waitFor(() => expect(input).toHaveFocus());
    await user.click(screen.getByRole("button", { name: /自己输入/ }));
    expect(input).toHaveValue("");
    expect(input).toHaveFocus();
  });

  it("uses the standard one-question flow for the final formal-title decision", async () => {
    const user = userEvent.setup();
    const titleQuestion = setupState();
    titleQuestion.selected_title = null;
    titleQuestion.question = "以下哪个书名最适合作为正式书名？";
    titleQuestion.suggestions = [
      { id: "title-1", label: "退潮前的十一分钟", message: "采用《退潮前的十一分钟》作为正式书名。", rationale: "对应核心倒计时意象。", recommended: true },
      { id: "title-2", label: "缺失的潮窗", message: "采用《缺失的潮窗》作为正式书名。", rationale: "突出封闭空间与证据缺口。", recommended: false }
    ];
    const readyState: SetupStateDocument = {
      ...titleQuestion,
      selected_title: "退潮前的十一分钟",
      question: null,
      suggestions: [],
      readiness: { status: "ready", reason: "方向与书名均已确认。" }
    };
    vi.spyOn(api, "setupState").mockResolvedValue(titleQuestion);
    const turnSpy = vi.spyOn(api, "continueSetupDiscussion").mockResolvedValue(readyState);
    render(<SetupConversation projectId="project-title" onApproved={() => undefined} onExit={() => undefined} />);

    expect(await screen.findByRole("heading", { name: titleQuestion.question ?? "" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /退潮前的十一分钟/ }));
    expect(screen.getByLabelText("你的意见")).toHaveValue("采用《退潮前的十一分钟》作为正式书名。");
    await user.click(screen.getByRole("button", { name: "发送本轮讨论" }));

    await waitFor(() => expect(turnSpy).toHaveBeenCalledWith("采用《退潮前的十一分钟》作为正式书名。"));
    expect(await screen.findByRole("button", { name: "准备审阅" })).toBeEnabled();
  });

  it("does not turn legacy topic suggestions into a choose-the-next-topic question", async () => {
    const legacyState = setupState();
    legacyState.question = null;
    vi.spyOn(api, "setupState").mockResolvedValue(legacyState);
    render(<SetupConversation projectId="project-1" onApproved={() => undefined} onExit={() => undefined} />);

    await screen.findByRole("textbox");
    expect(screen.queryByText("请选择下一步优先确认的方向")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /计入六人/ })).not.toBeInTheDocument();
  });

  it("clears the composer immediately after send", async () => {
    const user = userEvent.setup();
    let resolveTurn!: (state: SetupStateDocument) => void;
    const pendingTurn = new Promise<SetupStateDocument>((resolve) => { resolveTurn = resolve; });
    vi.spyOn(api, "setupState").mockResolvedValue(setupState());
    vi.spyOn(api, "continueSetupDiscussion").mockReturnValue(pendingTurn);
    render(<SetupConversation projectId="project-1" onApproved={() => undefined} onExit={() => undefined} />);

    const input = await screen.findByPlaceholderText("选择建议后可以继续编辑，或直接输入自己的回答...");
    await user.type(input, "我的回答");
    await user.click(screen.getByRole("button", { name: "发送本轮讨论" }));

    expect(input).toHaveValue("");
    expect(window.sessionStorage.getItem("novelpilot:book-direction-input:project-1")).toBeNull();
    resolveTurn(setupState());
    await waitFor(() => expect(api.continueSetupDiscussion).toHaveBeenCalledWith("我的回答"));
  });

  it("shows streaming progress without exposing the structured response body", async () => {
    const user = userEvent.setup();
    let resolveTurn!: (state: SetupStateDocument) => void;
    const pendingTurn = new Promise<SetupStateDocument>((resolve) => { resolveTurn = resolve; });
    vi.spyOn(api, "setupState").mockResolvedValue(setupState());
    vi.spyOn(api, "continueSetupDiscussion").mockReturnValue(pendingTurn);
    const props = {
      projectId: "project-1",
      onApproved: () => undefined,
      onExit: () => undefined
    };
    const { rerender } = render(<SetupConversation {...props} events={[]} />);

    const input = await screen.findByLabelText("你的意见");
    await user.type(input, "继续讨论");
    await user.click(screen.getByRole("button", { name: "发送本轮讨论" }));
    expect(screen.getByRole("status")).toHaveTextContent("正在连接模型流");

    const progressEvent: HarnessEvent = {
      seq: 1,
      event_id: "stream-progress-1",
      timestamp: "2026-07-14T00:00:00Z",
      project_id: "project-1",
      run_id: null,
      kind: "llm_stream_progress",
      loop_layer: "book",
      atomic_action: "continue_book_discussion",
      status: "delta",
      artifact_path: null,
      routing_decision: null,
      message: "Model response is streaming.",
      payload: { received_characters: 2048 }
    };
    rerender(<SetupConversation {...props} events={[progressEvent]} />);

    expect(screen.getByRole("status")).toHaveTextContent("已接收 2,048 个字符");
    expect(screen.queryByText(/\{"reply"/)).not.toBeInTheDocument();
    resolveTurn(setupState());
    await waitFor(() => expect(screen.queryByRole("status")).not.toBeInTheDocument());
  });

  it("restores the submitted text when sending fails", async () => {
    const user = userEvent.setup();
    vi.spyOn(api, "setupState").mockResolvedValue(setupState());
    vi.spyOn(api, "continueSetupDiscussion").mockRejectedValue(new Error("request failed"));
    render(<SetupConversation projectId="project-1" onApproved={() => undefined} onExit={() => undefined} />);

    const input = await screen.findByRole("textbox");
    await user.type(input, "retry this answer");
    await user.click(screen.getByRole("button", { name: "发送本轮讨论" }));

    await waitFor(() => expect(input).toHaveValue("retry this answer"));
    expect(window.sessionStorage.getItem("novelpilot:book-direction-input:project-1")).toBe("retry this answer");
  });

  it("switches the main stage to review after synthesis", async () => {
    const user = userEvent.setup();
    vi.spyOn(api, "setupState").mockResolvedValue(reviewableSetupState());
    vi.spyOn(api, "prepareSetupReview").mockResolvedValue(setupState(true));
    render(<SetupConversation projectId="project-1" onApproved={() => undefined} onExit={() => undefined} />);

    await user.click(await screen.findByRole("button", { name: "准备审阅" }));
    expect(await screen.findByRole("heading", { name: "确认全书方向" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Book Direction" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: "文稿" }));
    expect(screen.getByRole("heading", { name: "Book Direction" })).toBeInTheDocument();
    expect(screen.getByText("这是一份经过综合的候选方向。")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "查看详情" }));
    expect(screen.getByRole("dialog", { name: "方向详情" })).toBeInTheDocument();
    const rollingContract = screen.getByRole("heading", { name: "滚动故事弧契约" }).closest("section") as HTMLElement;
    expect(within(rollingContract).getByText("只规划当前故事弧。")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "关闭详情" }));
    expect(screen.getByRole("button", { name: "历史" })).toBeInTheDocument();
  });

  it("renders the discussion-confirmed title without another title picker", async () => {
    vi.spyOn(api, "setupState").mockResolvedValue(setupState(true));
    render(<SetupConversation projectId="project-1" onApproved={() => undefined} onExit={() => undefined} />);

    expect(await screen.findByText("《星潮之下》")).toBeInTheDocument();
    expect(screen.getByText("正式书名已经在逐问讨论的最后一步由你确认。")).toBeInTheDocument();
    expect(screen.queryByRole("radiogroup")).not.toBeInTheDocument();
    expect(screen.queryByPlaceholderText("输入你自己设计的正式书名")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "批准并采用《星潮之下》" })).toBeEnabled();
  });

  it("locks approval only while review feedback remains unsent", async () => {
    const user = userEvent.setup();
    vi.spyOn(api, "setupState").mockResolvedValue(setupState(true));
    render(<SetupConversation projectId="project-1" onApproved={() => undefined} onExit={() => undefined} />);

    const approve = await screen.findByRole("button", { name: "批准并采用《星潮之下》" });
    expect(approve).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "对当前方向不满意？继续修改" }));
    const feedback = screen.getByPlaceholderText("补充、纠正或否定当前候选；发送后将返回讨论阶段……");
    await user.type(feedback, "还要补充");
    await waitFor(() => expect(approve).toBeDisabled());
    await user.clear(feedback);
    await waitFor(() => expect(approve).toBeEnabled());
  });

  it("keeps the discussion-confirmed title after an explicit approval request fails", async () => {
    const user = userEvent.setup();
    vi.spyOn(api, "setupState").mockResolvedValue(setupState(true));
    const approveSpy = vi.spyOn(api, "approveSetup").mockRejectedValue(new Error("approval failed"));
    render(<SetupConversation projectId="project-1" onApproved={() => undefined} onExit={() => undefined} />);

    expect(approveSpy).not.toHaveBeenCalled();
    await user.click(await screen.findByRole("button", { name: "批准并采用《星潮之下》" }));

    await waitFor(() => expect(approveSpy).toHaveBeenCalledWith(1, "星潮之下"));
    expect(await screen.findByText(/approval failed/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "批准并采用《星潮之下》" })).toBeEnabled();
  });

  it("disables the explicit approval action while approval is pending", async () => {
    const user = userEvent.setup();
    let resolveApproval!: (state: SetupStateDocument) => void;
    const pendingApproval = new Promise<SetupStateDocument>((resolve) => { resolveApproval = resolve; });
    vi.spyOn(api, "setupState").mockResolvedValue(setupState(true));
    vi.spyOn(api, "approveSetup").mockReturnValue(pendingApproval);
    render(<SetupConversation projectId="project-1" onApproved={() => undefined} onExit={() => undefined} />);

    await user.click(await screen.findByRole("button", { name: "批准并采用《星潮之下》" }));

    expect(screen.getByRole("button", { name: "正在提交..." })).toBeDisabled();

    const approvedState = setupState(true);
    approvedState.approved = true;
    approvedState.phase = "approved";
    approvedState.approved_title = "星潮之下";
    resolveApproval(approvedState);
    expect(await screen.findByRole("heading", { name: "《星潮之下》" })).toBeInTheDocument();
  });

  it("keeps the full transcript primary while retaining the history dialog", async () => {
    const user = userEvent.setup();
    vi.spyOn(api, "setupState").mockResolvedValue(setupState());
    render(<SetupConversation projectId="project-1" onApproved={() => undefined} onExit={() => undefined} />);

    expect(await screen.findByRole("heading", { name: "全书共创" })).toBeInTheDocument();
    const conversationPanel = screen.getByRole("tabpanel", { name: "对话" });
    expect(within(conversationPanel).getByText("我要公平线索。")).toBeInTheDocument();
    expect(within(conversationPanel).getByText("我会把公平线索作为全书方向的硬约束。")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Book Direction" })).not.toBeInTheDocument();
    expect(screen.queryByRole("dialog", { name: "全书共创讨论记录" })).not.toBeInTheDocument();

    const historyButton = screen.getByRole("button", { name: "历史" });
    await user.click(historyButton);
    expect(screen.getByRole("dialog", { name: "全书共创讨论记录" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "关闭" }));
    await waitFor(() => expect(historyButton).toHaveFocus());
  });

  it("preserves unsent input while switching the document view and details sheet", async () => {
    const user = userEvent.setup();
    vi.spyOn(api, "setupState").mockResolvedValue(setupState());
    render(<SetupConversation projectId="project-1" onApproved={() => undefined} onExit={() => undefined} />);

    const input = await screen.findByLabelText("你的意见");
    await user.type(input, "不要丢失这段意见");
    await user.click(screen.getByRole("tab", { name: "文稿" }));
    expect(screen.getByRole("heading", { name: "Book Direction" })).toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: "对话" }));
    expect(input).toHaveValue("不要丢失这段意见");
    expect(screen.queryByRole("dialog", { name: "方向详情" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "查看详情" }));
    expect(screen.getByRole("dialog", { name: "方向详情" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "方向账本" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "已确认" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "待澄清" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "当前假设" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "矛盾" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "已取代" })).toBeInTheDocument();
    expect(input).toHaveValue("不要丢失这段意见");
  });

  it("shows a non-binding plan placeholder for a new project", async () => {
    const user = userEvent.setup();
    const emptyState = setupState();
    emptyState.turn_count = 0;
    emptyState.messages = [];
    emptyState.direction_draft = "";
    emptyState.question = null;
    emptyState.suggestions = [];
    vi.spyOn(api, "setupState").mockResolvedValue(emptyState);
    render(<SetupConversation projectId="project-new" onApproved={() => undefined} onExit={() => undefined} />);

    expect(await screen.findByRole("heading", { name: "全书共创" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Book Direction" })).not.toBeInTheDocument();
    expect(screen.getByLabelText("你的意见")).toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: "文稿" }));
    expect(screen.getByRole("heading", { name: "Book Direction" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "方向会在讨论中逐步成形" })).toBeInTheDocument();
    expect(screen.getByText(/不会强迫你填写固定模板/)).toBeInTheDocument();
    const stages = screen.getByRole("list", { name: "全书方向规划阶段" });
    expect(within(stages).getByText("探索").closest("li")).toHaveAttribute("aria-current", "step");
  });

  it("keeps ready-for-review advisory inside convergence until the user acts", async () => {
    const readyState = reviewableSetupState();
    vi.spyOn(api, "setupState").mockResolvedValue(readyState);
    render(<SetupConversation projectId="project-ready" onApproved={() => undefined} onExit={() => undefined} />);

    const stages = await screen.findByRole("list", { name: "全书方向规划阶段" });
    expect(within(stages).getByText("收敛").closest("li")).toHaveAttribute("aria-current", "step");
    expect(screen.getByText("方向已具备审阅条件，由你决定何时进入审阅")).toBeInTheDocument();
  });

  it("renders blocked review as the review stage with a return-to-discussion explanation", async () => {
    const user = userEvent.setup();
    const blockedState = setupState(true);
    blockedState.phase = "review_blocked";
    blockedState.candidate = {
      ...candidate,
      review: {
        status: "blocked",
        summary: "还缺少一个关键决定。",
        issues: [{ severity: "blocking", kind: "missing_decision", message: "结局代价未确认。", evidence: [], suggested_question: "请确认结局代价。" }],
        signals: []
      }
    };
    vi.spyOn(api, "setupState").mockResolvedValue(blockedState);
    const continueSpy = vi.spyOn(api, "prepareSetupReview").mockResolvedValue(blockedState);
    render(<SetupConversation projectId="project-blocked" onApproved={() => undefined} onExit={() => undefined} />);

    const stages = await screen.findByRole("list", { name: "全书方向规划阶段" });
    expect(within(stages).getByText("审阅").closest("li")).toHaveAttribute("aria-current", "step");
    expect(screen.getByText("候选仍有阻断问题，需要回到讨论修订")).toBeInTheDocument();
    expect(screen.queryByRole("radiogroup")).not.toBeInTheDocument();
    expect(screen.getByText("本轮自动修订已停止")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "继续修订" }));
    await waitFor(() => expect(continueSpy).toHaveBeenCalledOnce());
  });

  it("renders the approved execution handoff in the same planning shell", async () => {
    const approvedState = setupState(true);
    approvedState.phase = "approved";
    approvedState.approved = true;
    approvedState.approved_at = "2026-07-13T01:00:00Z";
    approvedState.approved_title = "星潮之下";
    approvedState.title_selection_source = "recommended";
    vi.spyOn(api, "setupState").mockResolvedValue(approvedState);
    render(<SetupConversation projectId="project-approved" onApproved={() => undefined} onExit={() => undefined} />);

    const stages = await screen.findByRole("list", { name: "全书方向规划阶段" });
    expect(within(stages).getByText("执行交接").closest("li")).toHaveAttribute("aria-current", "step");
    expect(screen.getByRole("heading", { name: "《星潮之下》" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "进入创作工作台" })).toBeInTheDocument();
  });

  it("summarizes plan and ledger changes after a successful turn", async () => {
    const user = userEvent.setup();
    const nextState = setupState();
    nextState.direction_draft = "# 全书方向\n\n公平线索得到强化。\n\n## 结局\n\n保留希望但必须付出代价。";
    nextState.confirmed_decisions = ["线索必须公平", "结局保留希望但付出代价"];
    vi.spyOn(api, "setupState").mockResolvedValue(setupState());
    vi.spyOn(api, "continueSetupDiscussion").mockResolvedValue(nextState);
    render(<SetupConversation projectId="project-1" onApproved={() => undefined} onExit={() => undefined} />);

    const input = await screen.findByLabelText("你的意见");
    await user.type(input, "结局要有希望，但必须付出代价");
    await user.click(screen.getByRole("button", { name: "发送本轮讨论" }));

    await user.click(screen.getByRole("tab", { name: "文稿" }));
    expect(await screen.findByText("本轮变更")).toBeInTheDocument();
    expect(screen.getByText(/更新章节：全书方向、结局/)).toBeInTheDocument();
    expect(screen.getByText(/已确认 \+1/)).toBeInTheDocument();
  });
});
