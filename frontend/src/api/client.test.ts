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
