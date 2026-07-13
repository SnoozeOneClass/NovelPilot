import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../../api/client";
import type { BookDirectionCandidate, SetupStateDocument } from "../../types/domain";
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
  recommended_titles: [{ title: "星潮之下", rationale: "对应核心意象" }],
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
    title_selection_source: null,
    migrated_from_schema_version: null,
    turn_count: 1,
    candidate_revision_counter: withCandidate ? 1 : 0,
    messages: [{ id: "m1", turn: 1, role: "user", content: "我要公平线索。", created_at: "2026-07-13T00:00:00Z", profile_id: null, model_snapshot: null, migrated: false }],
    direction_draft: "# 全书方向\n\n公平线索。",
    discussion_summary: "已确认公平线索。",
    confirmed_decisions: ["线索必须公平"],
    superseded_decisions: [],
    unresolved_questions: [],
    assumptions: [],
    contradictions: [],
    suggestions: [{ id: "s1", label: "补充禁区", message: "不要使用临时设定解围。" }],
    readiness: { status: "ready", reason: "方向已经足够具体。" },
    candidate: withCandidate ? candidate : null,
    direction_draft_version_path: "book/versions/direction.md",
    discussion_state_version_path: "book/versions/state.json",
    discussion_transcript_version_path: "book/versions/transcript.jsonl",
    last_context_snapshot_path: null,
    last_profile_id: "main",
    last_model_snapshot: "model"
  };
}

describe("SetupConversation", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    window.sessionStorage.clear();
  });

  it("appends a suggested direction without replacing an unsent draft", async () => {
    const user = userEvent.setup();
    vi.spyOn(api, "setupState").mockResolvedValue(setupState());
    render(<SetupConversation projectId="project-1" onApproved={() => undefined} onExit={() => undefined} />);

    const input = await screen.findByPlaceholderText("继续描述、纠正、否定或提出新的方向...");
    await user.type(input, "已有补充");
    await user.click(screen.getByRole("button", { name: "补充禁区" }));

    expect(input).toHaveValue("已有补充\n\n不要使用临时设定解围。");
  });

  it("switches the main stage to review after synthesis", async () => {
    const user = userEvent.setup();
    vi.spyOn(api, "setupState").mockResolvedValue(setupState());
    vi.spyOn(api, "prepareSetupReview").mockResolvedValue(setupState(true));
    render(<SetupConversation projectId="project-1" onApproved={() => undefined} onExit={() => undefined} />);

    await user.click(await screen.findByRole("button", { name: "整理并审阅" }));
    expect(await screen.findByRole("heading", { name: "确认方向与正式书名" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "讨论记录" })).toBeInTheDocument();
  });

  it("locks approval while review feedback remains unsent", async () => {
    const user = userEvent.setup();
    vi.spyOn(api, "setupState").mockResolvedValue(setupState(true));
    render(<SetupConversation projectId="project-1" onApproved={() => undefined} onExit={() => undefined} />);

    await user.click(await screen.findByRole("button", { name: /星潮之下/ }));
    const approve = screen.getByRole("button", { name: "批准候选 v1" });
    expect(approve).toBeEnabled();
    await user.type(screen.getByPlaceholderText("补充、纠正或否定当前候选..."), "还要补充");
    await waitFor(() => expect(approve).toBeDisabled());
  });
});
