import { describe, expect, it } from "vitest";
import type { SetupStateDocument } from "../../types/domain";
import {
  deriveSetupPlanningStage,
  latestSetupExchange,
  parseDirectionDocument,
  summarizeSetupChanges
} from "./setup-planning";

function setupState(overrides: Partial<SetupStateDocument> = {}): SetupStateDocument {
  return {
    schema_version: 2,
    revision: 1,
    phase: "discussing",
    approved: false,
    approved_at: null,
    approved_title: null,
    title_selection_source: null,
    migrated_from_schema_version: null,
    turn_count: 0,
    candidate_revision_counter: 0,
    messages: [],
    direction_draft: "",
    discussion_summary: "",
    confirmed_decisions: [],
    superseded_decisions: [],
    unresolved_questions: [],
    assumptions: [],
    contradictions: [],
    question: null,
    suggestions: [],
    readiness: { status: "continue", reason: "继续讨论。" },
    candidate: null,
    direction_draft_version_path: null,
    discussion_state_version_path: null,
    discussion_transcript_version_path: null,
    last_context_snapshot_path: null,
    last_profile_id: null,
    last_model_snapshot: null,
    ...overrides
  };
}

describe("setup planning utilities", () => {
  it("derives presentation stages without creating a second state machine", () => {
    expect(deriveSetupPlanningStage(setupState())).toBe("exploration");
    expect(deriveSetupPlanningStage(setupState({ direction_draft: "# 方向" }))).toBe("convergence");
    expect(deriveSetupPlanningStage(setupState({ candidate: {} as SetupStateDocument["candidate"] }))).toBe("review");
    expect(deriveSetupPlanningStage(setupState({ approved: true }))).toBe("approved");
  });

  it("parses a continuous document with heading levels and stable duplicate anchors", () => {
    const sections = parseDirectionDocument("开场说明\r\n\r\n# 方向\n正文\n## 人物\n甲\n## 人物\n乙");

    expect(sections.map(({ title, level, anchor }) => ({ title, level, anchor }))).toEqual([
      { title: "开篇说明", level: 1, anchor: "direction-开篇说明" },
      { title: "方向", level: 1, anchor: "direction-方向" },
      { title: "人物", level: 2, anchor: "direction-人物" },
      { title: "人物", level: 2, anchor: "direction-人物-2" }
    ]);
  });

  it("summarizes changed sections and ledger count deltas", () => {
    const previous = setupState({
      direction_draft: "# 方向\n旧方向\n## 人物\n甲",
      confirmed_decisions: ["公平线索"]
    });
    const next = setupState({
      direction_draft: "# 方向\n新方向\n## 人物\n甲\n## 结局\n保留希望",
      confirmed_decisions: ["公平线索", "付出代价"],
      unresolved_questions: ["谁承担代价？"]
    });

    expect(summarizeSetupChanges(previous, next)).toEqual({
      changedSections: ["方向", "结局"],
      ledgerDeltas: [
        { label: "已确认", delta: 1 },
        { label: "待澄清", delta: 1 }
      ],
      directionUpdated: true
    });
  });

  it("finds only the latest user and assistant messages", () => {
    const messages: SetupStateDocument["messages"] = [
      { id: "1", turn: 1, role: "user", content: "旧问题", created_at: "", profile_id: null, model_snapshot: null, migrated: false },
      { id: "2", turn: 1, role: "assistant", content: "旧回答", created_at: "", profile_id: null, model_snapshot: null, migrated: false },
      { id: "3", turn: 2, role: "user", content: "新问题", created_at: "", profile_id: null, model_snapshot: null, migrated: false }
    ];

    const latest = latestSetupExchange(messages);
    expect(latest.user?.content).toBe("新问题");
    expect(latest.assistant?.content).toBe("旧回答");
  });
});
