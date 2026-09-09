import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ThemeProvider } from "./app/theme";
import { AuthoringApp } from "./AuthoringApp";
import type { AuthoringProject } from "./types/authoring";
import { modelSettings, readyModel } from "./test/authoring-fixtures";

const api = vi.hoisted(() => ({
  listProjects: vi.fn(), getProject: vi.fn(), createProject: vi.fn(), startOrResume: vi.fn(),
  pause: vi.fn(), cancel: vi.fn(), updateProfiles: vi.fn(), profiles: vi.fn(),
  modelSettings: vi.fn(), saveModelProfile: vi.fn(), validateModelProfile: vi.fn(), selectDefaultProfile: vi.fn(), manuscript: vi.fn(),
  eventStreamUrl: vi.fn((id: string, after: number) => `/events/${id}?after=${after}`),
  exportUrl: vi.fn((id: string, format: string) => `/export/${id}.${format}`)
}));
vi.mock("./api/authoring-client", () => ({ authoringApi: api }));

class FakeEventSource {
  static instances: FakeEventSource[] = [];
  addEventListener = vi.fn();
  close = vi.fn();
  constructor(public readonly url: string) { FakeEventSource.instances.push(this); }
}

const running: AuthoringProject = {
  project_id: "authoring-a", brief: "一个守钟人的承诺", title: "黎明钟声", status: "running",
  stage: "writing", target_chapters: 12, target_words: null, completed_chapters: 3,
  progress_percent: 25, failure_reason: null, latest_event_sequence: 9,
  profile_bindings: {}, recent_activity: [{ sequence: 9, kind: "chapter_committed", payload: { chapter_number: 3 }, created_at: "2026-09-04T00:00:00Z" }]
};

function renderApp() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return { ...render(<QueryClientProvider client={client}><ThemeProvider><AuthoringApp /></ThemeProvider></QueryClientProvider>), client };
}

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  vi.stubGlobal("EventSource", FakeEventSource);
  FakeEventSource.instances = [];
  api.listProjects.mockResolvedValue([]);
  api.profiles.mockResolvedValue([]);
  api.modelSettings.mockResolvedValue(modelSettings);
  api.getProject.mockResolvedValue(running);
  api.createProject.mockResolvedValue({ ...running, status: "ready", completed_chapters: 0 });
  api.startOrResume.mockResolvedValue({ accepted: true, ownership: "started_here", project: running });
});

describe("AuthoringApp beginner flow", () => {
  it("preserves service default and explicit choices when ready models load or reorder", async () => {
    const models = ["first", "second"].map((id) => ({
      ...readyModel, profile_id: id, display_name: id, model_id: id
    }));
    api.modelSettings.mockResolvedValue({ selected_profile_id: "second", profiles: models });
    const { client } = renderApp();
    fireEvent.click(screen.getByRole("button", { name: "设置篇幅和模型（可选）" }));
    await screen.findAllByRole("option", { name: "first · first" });
    expect(screen.getByLabelText("创作模型")).toHaveValue("");

    fireEvent.change(screen.getByLabelText("创作模型"), { target: { value: "second" } });
    api.modelSettings.mockResolvedValue({ selected_profile_id: "second", profiles: [...models].reverse() });
    await act(() => client.invalidateQueries({ queryKey: ["authoring", "model-settings"] }));
    expect(screen.getByLabelText("创作模型")).toHaveValue("second");
    fireEvent.change(screen.getByLabelText("创作模型"), { target: { value: "" } });
    fireEvent.change(screen.getByLabelText("你的故事想法"), { target: { value: running.brief } });
    fireEvent.click(screen.getByRole("button", { name: "开始自动创作" }));
    await waitFor(() => expect(api.createProject).toHaveBeenCalledWith({ brief: running.brief }));
  });

  it("retains the created project when startup fails and retries only its run endpoint", async () => {
    const ready = { ...running, status: "ready", completed_chapters: 0 };
    api.getProject.mockResolvedValue(ready);
    api.startOrResume.mockRejectedValueOnce(new Error("启动暂时不可用，请重试"));
    renderApp();
    fireEvent.change(screen.getByLabelText("你的故事想法"), { target: { value: running.brief } });
    fireEvent.click(screen.getByRole("button", { name: "开始自动创作" }));
    expect(await screen.findByText(/启动暂时不可用/)).toBeInTheDocument();
    expect(localStorage.getItem("novelpilot.authoring.project-id")).toBe("authoring-a");
    expect(screen.queryByLabelText("你的故事想法")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "继续创作" }));
    await waitFor(() => expect(api.startOrResume).toHaveBeenCalledTimes(2));
    expect(api.startOrResume.mock.calls).toEqual([["authoring-a"], ["authoring-a"]]);
    expect(api.createProject).toHaveBeenCalledOnce();
    expect(await screen.findByRole("button", { name: "暂停" })).toBeInTheDocument();
  });

  it("keeps the idea and optional length when creation fails", async () => {
    api.createProject.mockRejectedValueOnce(new Error("暂时无法创建作品"));
    renderApp();
    fireEvent.change(screen.getByLabelText("你的故事想法"), { target: { value: running.brief } });
    fireEvent.click(screen.getByRole("button", { name: "设置篇幅和模型（可选）" }));
    fireEvent.change(screen.getByLabelText("目标数量"), { target: { value: "3" } });
    fireEvent.click(screen.getByRole("button", { name: "开始自动创作" }));
    expect(await screen.findByText("暂时无法创建作品")).toBeInTheDocument();
    expect(screen.getByLabelText("你的故事想法")).toHaveValue(running.brief);
    expect(screen.getByLabelText("目标数量")).toHaveValue(3);
    expect(api.startOrResume).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "检查模型设置" }));
    expect(await screen.findByRole("heading", { name: "设置自动创作使用的模型" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "返回创作" }));
    expect(screen.getByLabelText("你的故事想法")).toHaveValue(running.brief);
    expect(screen.getByLabelText("目标数量")).toHaveValue(3);
  });

  it("starts automatic writing from one idea without expert workflow choices", async () => {
    renderApp();
    expect(await screen.findByText("说出一个想法，剩下的交给我们。")).toBeInTheDocument();
    expect(screen.queryByText(/短篇|长篇|Planner|Arc|Checkpoint|Steering|逐章/)).not.toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("你的故事想法"), { target: { value: "一个守钟人的承诺" } });
    fireEvent.click(screen.getByRole("button", { name: "开始自动创作" }));
    await waitFor(() => expect(api.createProject).toHaveBeenCalledWith({ brief: "一个守钟人的承诺" }));
    await waitFor(() => expect(api.startOrResume).toHaveBeenCalledWith("authoring-a"));
  });

  it("submits an explicit model choice while leaving optional role assignments empty", async () => {
    renderApp();
    fireEvent.click(screen.getByRole("button", { name: "设置篇幅和模型（可选）" }));
    await screen.findAllByRole("option", { name: "创作模型 B · model-b-id" });
    fireEvent.change(screen.getByLabelText("你的故事想法"), { target: { value: running.brief } });
    fireEvent.change(screen.getByLabelText("创作模型"), { target: { value: "model-b" } });
    fireEvent.click(screen.getByRole("button", { name: "开始自动创作" }));
    await waitFor(() => expect(api.createProject).toHaveBeenCalledWith({ brief: running.brief, default_profile_id: "model-b" }));
  });

  it("shows only progress, safe controls, activity and downloads", async () => {
    localStorage.setItem("novelpilot.authoring.project-id", "authoring-a");
    renderApp();
    expect(await screen.findByText("3 / 12 章已完成")).toBeInTheDocument();
    expect(screen.getByText("第 3 章已完成")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "暂停" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "下载 Markdown" })).toHaveAttribute("href", "/export/authoring-a.markdown");
    expect(screen.getByRole("link", { name: "下载 TXT" })).toHaveAttribute("href", "/export/authoring-a.txt");
    expect(screen.queryByText(/批准|共创|反馈层级/)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "调整本作品模型" })).not.toBeInTheDocument();
    expect(screen.getByText("正文创作")).toBeInTheDocument();
  });

  it("keeps paused-work assignments and chapters when changing one model", async () => {
    const paused: AuthoringProject = { ...running, status: "paused", profile_bindings: {
      default: "model-a", architect: "model-a", writer: "model-a", editor: "model-b", arbiter: "model-a", fallback: "backup", "fallback:writer": "writer-backup"
    } };
    api.getProject.mockResolvedValue(paused);
    api.modelSettings.mockResolvedValue({ ...modelSettings, profiles: [
      ...modelSettings.profiles, { ...readyModel, profile_id: "backup", display_name: "备用服务" },
      { ...readyModel, profile_id: "writer-backup", display_name: "写作备用服务" }
    ] });
    api.updateProfiles.mockResolvedValue({ ...paused, profile_bindings: { ...paused.profile_bindings, default: "model-b" } });
    localStorage.setItem("novelpilot.authoring.project-id", paused.project_id);
    renderApp();
    fireEvent.click(await screen.findByRole("button", { name: "调整本作品模型" }));
    fireEvent.change(screen.getByLabelText("本作品创作模型"), { target: { value: "model-b" } });
    fireEvent.click(screen.getByRole("button", { name: "保存本作品模型" }));
    await waitFor(() => expect(api.updateProfiles).toHaveBeenCalledWith(paused.project_id, {
      default_profile_id: "model-b", architect_profile_id: "model-a", writer_profile_id: "model-a",
      editor_profile_id: "model-b", arbiter_profile_id: "model-a", fallback_profile_id: "backup",
      architect_fallback_profile_id: null, writer_fallback_profile_id: "writer-backup",
      editor_fallback_profile_id: null, arbiter_fallback_profile_id: null
    }));
    expect(screen.getByText("3 / 12 章已完成")).toBeInTheDocument();
    expect(screen.getByText("正文写作的备用模型：写作备用服务")).toBeInTheDocument();
    expect(screen.getByText("正文创作")).toBeInTheDocument();
    expect(localStorage.getItem("novelpilot.authoring.project-id")).toBe(paused.project_id);
    expect(api.createProject).not.toHaveBeenCalled();
  });

  it("honors a server state conflict instead of reporting a successful model switch", async () => {
    const paused: AuthoringProject = { ...running, status: "failure_paused", failure_reason: "模型设置当前不可用，请检查设置后继续。" };
    api.getProject.mockResolvedValue(paused);
    api.updateProfiles.mockRejectedValueOnce(new Error("创作已在运行，请先暂停"));
    localStorage.setItem("novelpilot.authoring.project-id", paused.project_id);
    renderApp();
    fireEvent.click(await screen.findByRole("button", { name: "调整本作品模型" }));
    fireEvent.change(screen.getByLabelText("本作品创作模型"), { target: { value: "model-b" } });
    api.getProject.mockResolvedValue(running);
    fireEvent.click(screen.getByRole("button", { name: "保存本作品模型" }));
    await waitFor(() => expect(api.updateProfiles).toHaveBeenCalledOnce());
    expect(await screen.findByRole("button", { name: "暂停" })).toBeInTheDocument();
    expect(screen.getByText("创作已在运行，请先暂停")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "保存本作品模型" })).not.toBeInTheDocument();
    expect(api.startOrResume).not.toHaveBeenCalled();
  });

  it("previews the completed TXT snapshot, retries failures and keeps terminal content stable", async () => {
    const completed: AuthoringProject = { ...running, status: "completed", stage: "complete", completed_chapters: 12, progress_percent: 100 };
    let rejectPreview!: (reason: Error) => void;
    api.manuscript.mockReturnValueOnce(new Promise((_, reject) => { rejectPreview = reject; }));
    api.getProject.mockResolvedValue(completed);
    localStorage.setItem("novelpilot.authoring.project-id", completed.project_id);
    const { client, unmount } = renderApp();
    expect(await screen.findByText("正在读取成稿…")).toBeInTheDocument();
    await act(async () => rejectPreview(new Error("成稿暂时无法读取")));
    expect(await screen.findByText("成稿暂时无法读取")).toBeInTheDocument();
    const text = "黎明钟声\n\n第一章 承诺\n钟声响起。\n\n第十二章 黎明\n他终于归来。\n";
    api.manuscript.mockResolvedValue({ text, etag: '"stable-snapshot"' });
    fireEvent.click(screen.getByRole("button", { name: "重新读取成稿" }));
    expect((await screen.findByLabelText("成稿正文")).textContent).toBe(text);
    expect(screen.getByRole("link", { name: "下载 TXT" })).toHaveAttribute("href", "/export/authoring-a.txt");
    expect(screen.getByText("成稿已完成")).toBeInTheDocument();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    await act(() => client.invalidateQueries({ queryKey: ["authoring", "project", completed.project_id] }));
    expect(api.manuscript).toHaveBeenCalledTimes(2);
    expect(screen.getByLabelText("成稿正文").textContent).toBe(text);
    unmount();
    renderApp();
    expect((await screen.findByLabelText("成稿正文")).textContent).toBe(text);
    expect(api.manuscript).toHaveBeenCalledTimes(3);
  });

  it("projects updated stages even when progress is paused and activity has not changed", async () => {
    const preparing: AuthoringProject = { ...running, status: "paused", stage: "preparing" };
    api.getProject.mockResolvedValue(preparing);
    localStorage.setItem("novelpilot.authoring.project-id", preparing.project_id);
    const { client } = renderApp();
    expect(await screen.findByText("准备故事")).toBeInTheDocument();
    api.getProject.mockResolvedValue({ ...preparing, status: "failure_paused", stage: "finishing" });
    await act(() => client.invalidateQueries({ queryKey: ["authoring", "project", preparing.project_id] }));
    expect(await screen.findByText("整理成稿")).toBeInTheDocument();
    expect(screen.getByText("遇到问题")).toBeInTheDocument();
    expect(screen.getByText("3 / 12 章已完成")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "继续创作" })).toBeInTheDocument();
  });

  it("starts SSE after the HTTP cursor and closes it on navigation", async () => {
    localStorage.setItem("novelpilot.authoring.project-id", "authoring-a");
    renderApp();
    expect(await screen.findByText("3 / 12 章已完成")).toBeInTheDocument();
    await waitFor(() => expect(FakeEventSource.instances).toHaveLength(1));
    expect(api.eventStreamUrl).toHaveBeenCalledWith("authoring-a", 9);

    fireEvent.click(screen.getByRole("button", { name: "全部作品" }));
    expect(FakeEventSource.instances[0]?.close).toHaveBeenCalledOnce();
  });
});
