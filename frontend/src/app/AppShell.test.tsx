import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ThemeProvider } from "./theme";
import { AppShell } from "./AppShell";
import type { ProjectSummary } from "../types/domain";

const project: ProjectSummary = {
  name: "project-1",
  title: "测试小说",
  path: "D:/output/project-1",
  metadata: {
    schema_version: 1,
    project_id: "project-1",
    title: "测试小说",
    operation_mode: "full_auto",
    active_profile_id: null,
    active_arc_id: null,
    active_chapter_id: null,
    run_status: "idle",
    created_at: "2026-07-13T00:00:00Z",
    updated_at: "2026-07-13T00:00:00Z"
  }
};

describe("AppShell", () => {
  it("keeps the experiment lab in primary navigation", async () => {
    const user = userEvent.setup();
    const navigate = vi.fn();
    render(
      <ThemeProvider>
        <AppShell
          project={project}
          location="workbench"
          profile={null}
          canRecover={false}
          runInFlight={false}
          onLocationChange={navigate}
          onRefresh={() => undefined}
          onRecover={() => undefined}
          onCloseProject={() => undefined}
        >
          <div>content</div>
        </AppShell>
      </ThemeProvider>
    );

    const primary = screen.getAllByRole("navigation", { name: "任务域" })[0];
    expect(primary).toHaveTextContent("共创工作台故事世界证据中心实验室");
    await user.click(screen.getAllByRole("button", { name: "实验室" })[0]);
    expect(navigate).toHaveBeenCalledWith("experiments");
  });
});
