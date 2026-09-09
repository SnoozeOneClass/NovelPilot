import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ThemeProvider } from "../../app/theme";
import { modelSettings, readyModel } from "../../test/authoring-fixtures";
import type { ModelSettingsDocument } from "../../types/authoring";
import { ModelSettings } from "./ModelSettings";

const api = vi.hoisted(() => ({ saveModelProfile: vi.fn(), validateModelProfile: vi.fn(), selectDefaultProfile: vi.fn() }));
vi.mock("../../api/authoring-client", () => ({ authoringApi: api }));

function SettingsHarness({ initial = modelSettings }: { initial?: ModelSettingsDocument }) {
  const [document, setDocument] = useState(initial);
  return <ThemeProvider><ModelSettings document={document} loading={false} error={null}
    onRetry={vi.fn()} onChanged={setDocument} onBack={vi.fn()} /></ThemeProvider>;
}

function modelCard(name = readyModel.display_name) {
  return within(screen.getByRole("article", { name: `模型 ${name}` }));
}

function editFirstModel() {
  fireEvent.click(modelCard().getByRole("button", { name: "编辑" }));
}

beforeEach(() => {
  vi.resetAllMocks();
  localStorage.clear();
  api.saveModelProfile.mockResolvedValue(modelSettings);
  api.validateModelProfile.mockResolvedValue(modelSettings);
  api.selectDefaultProfile.mockResolvedValue(modelSettings);
});

describe("model settings", () => {
  it("loads editable values with an empty key and preserves the stored key on a blank edit", async () => {
    render(<SettingsHarness />);
    editFirstModel();
    expect(screen.getByLabelText("服务地址")).toHaveValue(readyModel.base_url);
    expect(screen.getByLabelText("模型名称")).toHaveValue(readyModel.model_id);
    expect(screen.getByLabelText("API 密钥")).toHaveValue("");
    expect(screen.getByText("密钥已保存。这里不会显示原值，留空保存可继续使用。")).toBeInTheDocument();
    expect(screen.getByLabelText("上下文窗口（token）")).toHaveValue(1000000);
    expect(screen.getByLabelText("最大输出 token 数")).toHaveValue(65536);
    fireEvent.click(screen.getByRole("button", { name: "保存模型设置" }));
    await waitFor(() => expect(api.saveModelProfile).toHaveBeenCalledOnce());
    const body = api.saveModelProfile.mock.calls[0]![1];
    expect(body).not.toHaveProperty("api_key");
    expect(body).toMatchObject({
      request_options: readyModel.request_options, context_window: 1000000, max_output_tokens: 65536,
      input_price_per_million: 2, output_price_per_million: 4, cache_price_per_million: 0.5
    });
    expect(screen.queryByLabelText("API 密钥")).not.toBeInTheDocument();
    editFirstModel();
    expect(screen.getByLabelText("API 密钥")).toHaveValue("");
  });

  it("keeps a changed key write-only and requires successful validation before choosing a default", async () => {
    const missing = { selected_profile_id: null, profiles: [{ ...readyModel, capability_status: "missing" as const }] };
    api.saveModelProfile.mockResolvedValue(missing);
    api.validateModelProfile.mockRejectedValueOnce(new Error("无法连接模型服务，请检查服务地址后重试。"))
      .mockResolvedValueOnce({ ...missing, profiles: [readyModel] });
    api.selectDefaultProfile.mockResolvedValue({ selected_profile_id: readyModel.profile_id, profiles: [readyModel] });
    render(<SettingsHarness initial={{ selected_profile_id: null, profiles: [readyModel] }} />);
    editFirstModel();
    const testKey = "test-only-replacement-credential";
    fireEvent.change(screen.getByLabelText("API 密钥"), { target: { value: testKey } });
    fireEvent.click(screen.getByRole("button", { name: "保存模型设置" }));
    expect(await screen.findByText("尚未验证")).toBeInTheDocument();
    expect(api.saveModelProfile.mock.calls[0]![1]).toHaveProperty("api_key", testKey);
    expect(document.body.textContent).not.toContain(testKey);
    expect(Object.values(localStorage).join(" ")).not.toContain(testKey);
    expect(modelCard().getByRole("button", { name: "设为默认" })).toBeDisabled();
    fireEvent.click(modelCard().getByRole("button", { name: "验证连接与能力" }));
    expect(await screen.findByText("无法连接模型服务，请检查服务地址后重试。")).toBeInTheDocument();
    expect(modelCard().getByRole("button", { name: "设为默认" })).toBeDisabled();
    fireEvent.click(modelCard().getByRole("button", { name: "验证连接与能力" }));
    await waitFor(() => expect(modelCard().getByRole("button", { name: "设为默认" })).toBeEnabled());
    fireEvent.click(modelCard().getByRole("button", { name: "设为默认" }));
    expect(await screen.findByText("默认模型已更新，新作品会使用此设置。")).toBeInTheDocument();
    expect(api.validateModelProfile.mock.calls).toEqual([[readyModel.profile_id], [readyModel.profile_id]]);
    expect(api.selectDefaultProfile).toHaveBeenCalledWith(readyModel.profile_id);
    editFirstModel();
    expect(screen.getByLabelText("API 密钥")).toHaveValue("");
  });

  it("rejects invalid JSON and mismatched output limits without sending or echoing input", async () => {
    render(<SettingsHarness />);
    editFirstModel();
    const invalid = '{"example":"test-only-hidden-value",';
    fireEvent.change(screen.getByLabelText("请求选项（JSON）"), { target: { value: invalid } });
    fireEvent.click(screen.getByRole("button", { name: "保存模型设置" }));
    expect(screen.getByRole("alert")).toHaveTextContent("请求选项需要有效的 JSON 对象");
    expect(screen.getByRole("alert")).not.toHaveTextContent("test-only-hidden-value");
    expect(api.saveModelProfile).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText("请求选项（JSON）"), { target: { value: '{"max_tokens":2048,"reasoning":{"effort":"high"}}' } });
    fireEvent.click(screen.getByRole("button", { name: "保存模型设置" }));
    expect(screen.getByRole("alert")).toHaveTextContent("max_tokens 必须与最大输出 token 数一致");
    expect(api.saveModelProfile).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText("最大输出 token 数"), { target: { value: "2048" } });
    fireEvent.click(screen.getByRole("button", { name: "保存模型设置" }));
    await waitFor(() => expect(api.saveModelProfile).toHaveBeenCalledOnce());
    expect(api.saveModelProfile.mock.calls[0]![1]).toMatchObject({ max_output_tokens: 2048, request_options: { max_tokens: 2048, reasoning: { effort: "high" } } });
  });

  it("refreshes saved settings after a validation conflict before offering default selection", async () => {
    api.validateModelProfile.mockRejectedValueOnce(new Error("模型设置已在验证期间更改，请重新验证。"));
    const current: ModelSettingsDocument = { selected_profile_id: null, profiles: [{
      ...readyModel, model_id: "changed-while-validating", capability_status: "missing"
    }] };
    function ConflictHarness() {
      const [document, setDocument] = useState<ModelSettingsDocument>({
        selected_profile_id: null, profiles: [readyModel]
      });
      return <ThemeProvider><ModelSettings document={document} loading={false} error={null}
        onRetry={() => setDocument(current)} onChanged={setDocument} onBack={vi.fn()} /></ThemeProvider>;
    }
    render(<ConflictHarness />);
    expect(modelCard().getByRole("button", { name: "设为默认" })).toBeEnabled();
    fireEvent.click(modelCard().getByRole("button", { name: "验证连接与能力" }));
    expect(await screen.findByText("模型设置已在验证期间更改，请重新验证。")).toBeInTheDocument();
    expect(await screen.findByText("changed-while-validating")).toBeInTheDocument();
    expect(modelCard().getByText("尚未验证")).toBeInTheDocument();
    expect(modelCard().getByRole("button", { name: "设为默认" })).toBeDisabled();
  });

  it("keeps entered settings after a save failure and disables actions during a capability probe", async () => {
    api.saveModelProfile.mockRejectedValueOnce(new Error("请求选项不被支持，请检查设置。"));
    let finishProbe!: (document: ModelSettingsDocument) => void;
    api.validateModelProfile.mockReturnValueOnce(new Promise((resolve) => { finishProbe = resolve; }));
    render(<SettingsHarness />);
    editFirstModel();
    fireEvent.change(screen.getByLabelText("模型名称"), { target: { value: "new-model" } });
    fireEvent.click(screen.getByRole("button", { name: "保存模型设置" }));
    expect(await screen.findByText("请求选项不被支持，请检查设置。")).toBeInTheDocument();
    expect(screen.getByLabelText("模型名称")).toHaveValue("new-model");
    fireEvent.click(screen.getByRole("button", { name: "取消编辑" }));
    fireEvent.click(modelCard().getByRole("button", { name: "验证连接与能力" }));
    expect(modelCard().getByRole("button", { name: "正在处理…" })).toBeDisabled();
    expect(modelCard().getByRole("button", { name: "编辑" })).toBeDisabled();
    await act(() => finishProbe(modelSettings));
    expect(modelCard().getByRole("button", { name: "编辑" })).toBeEnabled();
  });

  it("adds a model and shows disabled, stale and missing settings with actionable editing", async () => {
    const initial: ModelSettingsDocument = { selected_profile_id: "removed", profiles: [
      { ...readyModel, enabled: false },
      { ...readyModel, profile_id: "stale", display_name: "需要复验", capability_status: "stale" },
      { ...readyModel, profile_id: "incomplete", display_name: "未填容量", context_window: null }
    ] };
    render(<SettingsHarness initial={initial} />);
    expect(screen.getByText(/模型不存在/)).toBeInTheDocument();
    expect(screen.getByText("已停用")).toBeInTheDocument();
    expect(screen.getByText("配置已变更，需重新验证")).toBeInTheDocument();
    expect(screen.getByText("待补充容量设置")).toBeInTheDocument();
    expect(modelCard().getByRole("button", { name: "设为默认" })).toBeDisabled();
    expect(modelCard().getByRole("button", { name: "编辑" })).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: "添加模型" }));
    fireEvent.change(screen.getByLabelText("配置标识"), { target: { value: "new-model" } });
    fireEvent.change(screen.getByLabelText("显示名称"), { target: { value: "新模型" } });
    fireEvent.change(screen.getByLabelText("连接方式"), { target: { value: "anthropic_messages" } });
    fireEvent.change(screen.getByLabelText("服务地址"), { target: { value: "https://model.example.test" } });
    fireEvent.change(screen.getByLabelText("模型名称"), { target: { value: "opaque-model-id" } });
    fireEvent.change(screen.getByLabelText("API 密钥"), { target: { value: "test-only-new-key" } });
    fireEvent.change(screen.getByLabelText("上下文窗口（token）"), { target: { value: "200000" } });
    fireEvent.change(screen.getByLabelText("最大输出 token 数"), { target: { value: "16000" } });
    fireEvent.click(screen.getByRole("button", { name: "保存模型设置" }));
    await waitFor(() => expect(api.saveModelProfile).toHaveBeenCalledWith("new-model", expect.objectContaining({
      api_family: "anthropic_messages", model_id: "opaque-model-id", request_options: { max_tokens: 16000 },
      context_window: 200000, max_output_tokens: 16000, enabled: true, api_key: "test-only-new-key"
    })));
  });
});
