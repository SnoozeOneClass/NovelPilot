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

const secondProject: ProjectSummary = {
  ...project,
  name: "project-2",
  title: "第二本小说",
  path: "D:/output/project-2",
  metadata: {
    ...project.metadata,
    project_id: "project-2",
    title: "第二本小说",
    active_arc_id: null,
    active_chapter_id: null,
    updated_at: "2026-07-13T02:00:00Z"
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

  it("offers an in-page reconnect when the initial project request fails", async () => {
    const user = userEvent.setup();
    vi.mocked(api.listProjects)
      .mockRejectedValueOnce(new Error("本地服务尚未就绪。"))
      .mockResolvedValueOnce([project]);
    renderSelector();

    expect(await screen.findByText("本地服务尚未就绪。")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "重新连接" }));

    expect(await screen.findByRole("button", { name: /测试小说/ })).toBeInTheDocument();
    expect(api.listProjects).toHaveBeenCalledTimes(2);
  });

  it("selects and permanently deletes one project after confirmation", async () => {
    const user = userEvent.setup();
    vi.mocked(api.listProjects).mockResolvedValueOnce([project]).mockResolvedValue([]);
    const remove = vi.spyOn(api, "deleteProjects").mockResolvedValue({
      deleted: [{ project_id: "project-1", name: "project-1" }],
      active_project_closed: false
    });
    renderSelector();

    await user.click(await screen.findByRole("checkbox", { name: "选择《测试小说》" }));
    await user.click(screen.getByRole("button", { name: "删除选中（1）" }));

    expect(screen.getByRole("dialog", { name: "确认删除 1 本小说？" })).toHaveTextContent(
      "此操作不可撤销"
    );
    await user.click(screen.getByRole("button", { name: "永久删除" }));

    await waitFor(() => expect(remove).toHaveBeenCalledWith(["project-1"]));
    expect(await screen.findByText("已从本地 output 目录永久删除 1 本小说。")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /测试小说/ })).not.toBeInTheDocument();
  });

  it("selects every listed project for one batch deletion", async () => {
    const user = userEvent.setup();
    vi.mocked(api.listProjects).mockResolvedValue([project, secondProject]);
    const remove = vi.spyOn(api, "deleteProjects").mockResolvedValue({
      deleted: [
        { project_id: "project-1", name: "project-1" },
        { project_id: "project-2", name: "project-2" }
      ],
      active_project_closed: true
    });
    renderSelector();

    await screen.findByRole("button", { name: /第二本小说/ });
    await user.click(screen.getByRole("button", { name: "全选" }));

    expect(screen.getByRole("checkbox", { name: "选择《测试小说》" })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: "选择《第二本小说》" })).toBeChecked();
    await user.click(screen.getByRole("button", { name: "删除选中（2）" }));
    await user.click(screen.getByRole("button", { name: "永久删除" }));

    await waitFor(() => expect(remove).toHaveBeenCalledWith(["project-1", "project-2"]));
  });
});
