import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { api } from "../../api/client";
import { ThemeProvider } from "../../app/theme";
import type { ProjectSummary } from "../../types/domain";
import { SettingsView } from "./SettingsView";

const project: ProjectSummary = {
  name: "project-1",
  title: "测试小说",
  path: "D:/output/project-1",
  metadata: { schema_version: 1, project_id: "project-1", title: "测试小说", operation_mode: "participatory", active_profile_id: null, active_arc_id: null, active_chapter_id: null, run_status: "paused", created_at: "2026-07-13T00:00:00Z", updated_at: "2026-07-13T00:00:00Z" }
};

function renderSettings(value = project, onProjectChanged = vi.fn()) {
  render(<ThemeProvider><SettingsView project={value} onProjectChanged={onProjectChanged} onProfilesChanged={vi.fn()} /></ThemeProvider>);
  return onProjectChanged;
}

describe("SettingsView", () => {
  it("locks operation mode while the harness is running", () => {
    renderSettings({ ...project, metadata: { ...project.metadata, run_status: "running" } });
    expect(screen.getByRole("radio", { name: /全自动模式/ })).toBeDisabled();
    expect(screen.getByRole("radio", { name: /参与模式/ })).toBeDisabled();
  });

  it("updates a stopped project's operation mode through the existing API", async () => {
    const user = userEvent.setup();
    const next = { ...project, metadata: { ...project.metadata, operation_mode: "full_auto" as const } };
    const changed = renderSettings(project);
    vi.spyOn(api, "updateProjectMode").mockResolvedValue(next);

    await user.click(screen.getByRole("radio", { name: /全自动模式/ }));

    await waitFor(() => expect(changed).toHaveBeenCalledWith(next));
    expect(api.updateProjectMode).toHaveBeenCalledWith("full_auto");
  });

  it("changes the stored theme preference", async () => {
    const user = userEvent.setup();
    renderSettings();
    await user.click(screen.getByRole("button", { name: /界面主题/ }));
    await user.click(screen.getByRole("radio", { name: /暗色/ }));
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(window.localStorage.getItem("novelpilot.theme")).toBe("dark");
  });
});
