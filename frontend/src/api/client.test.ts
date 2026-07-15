import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "./client";

describe("experiment fixture API errors", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("surfaces both the freeze conflict and its actionable eligibility issue", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            detail: {
              message: "当前项目还没有到达可冻结的实验检查点。",
              issues: [
                {
                  code: "current_arc_not_approved",
                  message: "请先明确批准当前故事弧计划。"
                }
              ]
            }
          }),
          {
            status: 409,
            headers: { "Content-Type": "application/json" }
          }
        )
      )
    );

    await expect(api.freezeExperimentFixture()).rejects.toThrow(
      "当前项目还没有到达可冻结的实验检查点。 请先明确批准当前故事弧计划。"
    );
  });
});

describe("Agent policy API", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("sends the complete bounded policy to the active project endpoint", async () => {
    const policy = {
      schema_version: 1,
      book_profile_id: null,
      story_arc_profile_id: "arc-model",
      chapter_profile_id: null,
      evaluator_profile_id: "judge-model",
      book_max_turns: 20,
      story_arc_max_turns: 20,
      chapter_max_turns: 30,
      tool_schema_repair_limit: 2,
      semantic_revision_limit: 2,
      transport_retry_limit: 3
    };
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      name: "project-1",
      title: "Novel",
      path: "D:/output/project-1",
      metadata: {
        schema_version: 1,
        project_id: "project-1",
        title: "Novel",
        operation_mode: "participatory",
        active_profile_id: "main",
        agent_policy: policy,
        active_arc_id: null,
        active_chapter_id: null,
        run_status: "paused",
        created_at: "2026-07-14T00:00:00Z",
        updated_at: "2026-07-14T00:00:00Z"
      }
    }), { status: 200, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);

    await api.updateAgentPolicy(policy);

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/projects/active/agent-policy",
      expect.objectContaining({ method: "PATCH", body: JSON.stringify({ agent_policy: policy }) })
    );
  });
});

describe("Book revision approval API", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("sends both the revision identity and evaluated base version", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({}), {
        status: 200,
        headers: { "Content-Type": "application/json" }
      })
    );
    vi.stubGlobal("fetch", fetchMock);

    await api.approveBookRevision("revision-0003-abcd1234", 7);

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/book-revisions/approve",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          revision_id: "revision-0003-abcd1234",
          expected_base_book_version: 7
        })
      })
    );
  });
});
