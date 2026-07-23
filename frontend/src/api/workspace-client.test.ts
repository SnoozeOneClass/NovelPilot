import { afterEach, describe, expect, it, vi } from "vitest";
import { workspaceApi } from "./workspace-client";

afterEach(() => vi.unstubAllGlobals());

describe("workspaceApi", () => {
  it("keeps project reads side-effect free", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ project: { project_id: "project-a" } }), {
        status: 200,
        headers: { "Content-Type": "application/json" }
      })
    );
    vi.stubGlobal("fetch", fetchMock);

    await workspaceApi.getProject("project-a");

    expect(fetchMock).toHaveBeenCalledWith("/api/projects/project-a", undefined);
  });

  it("sends explicit run commands with the idempotency key", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ replayed: false, receipt_id: "r1", state: {} }), {
        status: 200,
        headers: { "Content-Type": "application/json" }
      })
    );
    vi.stubGlobal("fetch", fetchMock);

    await workspaceApi.runControl("project-a", "start", 4, "start-key");

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/projects/project-a/run/start",
      expect.objectContaining({
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": "start-key"
        },
        body: JSON.stringify({ expected_lock_version: 4 })
      })
    );
  });

  it("uses one error envelope without echoing request content", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({ error: { code: "command_failed", message: "任务已失败" } }),
          { status: 409, headers: { "Content-Type": "application/json" } }
        )
      )
    );

    await expect(workspaceApi.getProject("project-a")).rejects.toThrow("任务已失败");
  });
});
