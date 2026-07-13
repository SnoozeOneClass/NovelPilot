import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../../api/client";
import { ThemeProvider } from "../../app/theme";
import type { ProjectSummary } from "../../types/domain";
import { ProjectSelector } from "./ProjectSelector";

const project: ProjectSummary = {
  name: "project-1",
  title: "测试小说",
  path: "D:/output/project-1",
  metadata: {
    schema_version: 1,
    project_id: "project-1",
    title: "测试小说",
    operation_mode: "participatory",
    active_profile_id: null,
    active_arc_id: "arc-001",
    active_chapter_id: "chapter-002",
    run_status: "paused",
    created_at: "2026-07-13T00:00:00Z",
    updated_at: "2026-07-13T01:00:00Z"
  }
};

function renderSelector(onProjectOpened = vi.fn()) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <ThemeProvider><ProjectSelector onProjectOpened={onProjectOpened} /></ThemeProvider>
    </QueryClientProvider>
  );
  return onProjectOpened;
}

describe("ProjectSelector", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api, "listProjects").mockResolvedValue([project]);
    vi.spyOn(api, "activeProject").mockResolvedValue(null);
  });

  it("opens an existing project directly without changing its mode", async () => {
    const user = userEvent.setup();
    const opened = renderSelector();
    vi.spyOn(api, "openProject").mockResolvedValue(project);
    const modeMutation = vi.spyOn(api, "updateProjectMode");

    await user.click(await screen.findByRole("button", { name: /测试小说/ }));

    await waitFor(() => expect(opened).toHaveBeenCalledWith(project));
    expect(api.openProject).toHaveBeenCalledWith("project-1");
    expect(modeMutation).not.toHaveBeenCalled();
  });

  it("creates an untitled project from the mode dialog", async () => {
    const user = userEvent.setup();
    const opened = renderSelector();
    const create = vi.spyOn(api, "createProject").mockResolvedValue({ ...project, title: null });

    await user.click(screen.getByRole("button", { name: "新建小说" }));
    await user.click(screen.getByRole("button", { name: /创建并进入共创/ }));

    await waitFor(() => expect(opened).toHaveBeenCalled());
    expect(create).toHaveBeenCalledWith("full_auto");
  });
});
