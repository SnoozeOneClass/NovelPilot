import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { api } from "../../api/client";
import { ThemeProvider } from "../../app/theme";
import type { LlmProfilesDocument, ProjectSummary } from "../../types/domain";
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

  it("configures verified per-loop models and bounded Agent budgets", async () => {
    const user = userEvent.setup();
    const verifiedProfiles: LlmProfilesDocument = {
      schema_version: 1,
      active_profile_id: "main",
      profiles: [{
        id: "main",
        name: "Main",
        protocol: "openai-compatible",
        base_url: "https://api.example.com/v1",
        model: "story-model",
        request_options: {},
        enabled: true,
        has_api_key: true,
        capability_test: {
          schema_version: 1,
          checked_at: "2026-07-14T00:00:00Z",
          profile_fingerprint: "fingerprint",
          tool_calling: { ok: true, message: "supported" },
          structured_output: { ok: true, message: "supported" },
          ready_for_harness: true
        }
      }]
    };
    vi.spyOn(api, "profiles").mockResolvedValue(verifiedProfiles);
    const changed = renderSettings();
    const nextProject = {
      ...project,
      metadata: {
        ...project.metadata,
        agent_policy: {
          schema_version: 1,
          book_profile_id: "main",
          story_arc_profile_id: null,
          chapter_profile_id: null,
          evaluator_profile_id: null,
          book_max_turns: 20,
          story_arc_max_turns: 20,
          chapter_max_turns: 30,
          tool_schema_repair_limit: 2,
          semantic_revision_limit: 3,
          transport_retry_limit: 3
        }
      }
    };
    const updatePolicy = vi.spyOn(api, "updateAgentPolicy").mockResolvedValue(nextProject);

    await user.click(screen.getByRole("button", { name: /LLM Profile/ }));
    await user.selectOptions(await screen.findByLabelText("全书 Loop 模型"), "main");
    await user.clear(screen.getByLabelText("语义自动修订"));
    await user.type(screen.getByLabelText("语义自动修订"), "3");
    await user.click(screen.getByRole("button", { name: "保存 Agent 配置" }));

    await waitFor(() => expect(updatePolicy).toHaveBeenCalledWith(expect.objectContaining({
      book_profile_id: "main",
      book_max_turns: 20,
      chapter_max_turns: 30,
      semantic_revision_limit: 3
    })));
    expect(changed).toHaveBeenCalledWith(nextProject);
  });
});
