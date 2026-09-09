import { afterEach, describe, expect, it, vi } from "vitest";
import { authoringApi } from "./authoring-client";
import { modelSettings, readyModel } from "../test/authoring-fixtures";

const project = {
  project_id: "authoring-a", brief: "idea", title: null, status: "ready", stage: "preparing",
  target_chapters: 120, target_words: null, completed_chapters: 0, progress_percent: 0,
  failure_reason: null, latest_event_sequence: 1, profile_bindings: {}, recent_activity: []
};

afterEach(() => vi.unstubAllGlobals());

describe("authoringApi", () => {
  it("uses the isolated authoring endpoints and validates responses", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(project), {
      status: 201, headers: { "Content-Type": "application/json" }
    }));
    vi.stubGlobal("fetch", fetchMock);

    await authoringApi.createProject({ brief: "idea" });

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/authoring/projects",
      expect.objectContaining({ method: "POST", body: JSON.stringify({ brief: "idea" }) })
    );
    expect(authoringApi.exportUrl("a/b", "txt")).toBe("/api/authoring/projects/a%2Fb/export?format=txt");
  });

  it("fails closed when the server contract is malformed", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ project_id: 1 }), { status: 200 })));
    await expect(authoringApi.getProject("bad")).rejects.toThrow("无法识别");
  });

  it("reads the shared API error envelope", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({
      error: { code: "request_validation_failed", message: "请填写故事想法" }
    }), { status: 422 })));
    await expect(authoringApi.createProject({ brief: "" })).rejects.toThrow("请填写故事想法");
  });

  it("uses the frozen settings endpoints, with a write-only key and no client validation evidence", async () => {
    const fetchMock = vi.fn().mockImplementation(() => Promise.resolve(new Response(JSON.stringify({
      ...modelSettings, profiles: [{ ...readyModel, api_key: "unexpected-response-key" }]
    }), { status: 200 })));
    vi.stubGlobal("fetch", fetchMock);
    const safe = await authoringApi.modelSettings();
    expect(safe.profiles[0]).not.toHaveProperty("api_key");
    const input = {
      display_name: readyModel.display_name, api_family: readyModel.api_family, base_url: readyModel.base_url,
      model_id: readyModel.model_id, enabled: true, context_window: 1000000, max_output_tokens: 65536,
      request_options: readyModel.request_options, input_price_per_million: 2,
      output_price_per_million: 4, cache_price_per_million: 0.5, api_key: "test-only-write-key"
    };
    await authoringApi.saveModelProfile("model/a", input);
    await authoringApi.validateModelProfile("model/a");
    await authoringApi.selectDefaultProfile("model/a");
    expect(fetchMock.mock.calls[0]![0]).toBe("/api/authoring/model-settings");
    expect(fetchMock.mock.calls[1]).toEqual(["/api/authoring/model-settings/profiles/model%2Fa", expect.objectContaining({ method: "PUT", body: JSON.stringify(input) })]);
    expect(fetchMock.mock.calls[2]).toEqual(["/api/authoring/model-settings/profiles/model%2Fa/validate", expect.objectContaining({ method: "POST", body: undefined })]);
    expect(fetchMock.mock.calls[3]).toEqual(["/api/authoring/model-settings/default", expect.objectContaining({ method: "PUT", body: JSON.stringify({ profile_id: "model/a" }) })]);
  });

  it("redacts a submitted key from a settings error and propagates safe validation failures", async () => {
    const testKey = "test-only-private-value";
    const input = {
      display_name: "test", api_family: readyModel.api_family, base_url: readyModel.base_url, model_id: readyModel.model_id,
      enabled: true, api_key: testKey, context_window: 1000000, max_output_tokens: 65536, request_options: {},
      input_price_per_million: 0, output_price_per_million: 0, cache_price_per_million: 0
    };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(new Response(JSON.stringify({
      error: { message: `配置错误 ${testKey}` }
    }), { status: 422 })).mockResolvedValueOnce(new Response(JSON.stringify({
      error: { message: "设置已被修改，请重新验证" }
    }), { status: 409 })));
    await expect(authoringApi.saveModelProfile("model-a", input)).rejects.toThrow("配置错误 [已隐藏密钥]");
    await expect(authoringApi.validateModelProfile("model-a")).rejects.toThrow("设置已被修改，请重新验证");
  });

  it("reads manuscript bytes and ETag from the exact TXT download endpoint", async () => {
    const text = "书名\n\n第一章\n  正文保留空格。\n<script>只是文本</script>\n";
    const etag = '"snapshot-sha"';
    const fetchMock = vi.fn().mockImplementation(() => Promise.resolve(new Response(text, { headers: { ETag: etag } })));
    vi.stubGlobal("fetch", fetchMock);
    const controller = new AbortController();
    const result = await authoringApi.manuscript("a/b", controller.signal);
    expect(fetchMock).toHaveBeenCalledWith(authoringApi.exportUrl("a/b", "txt"), { signal: controller.signal });
    expect(result).toEqual({ text, etag });
    const download = await fetch(authoringApi.exportUrl("a/b", "txt"));
    expect(result.text).toBe(await download.text());
    expect(result.etag).toBe(download.headers.get("ETag"));
  });

  it("surfaces manuscript HTTP errors instead of showing an error document as prose", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({
      error: { message: "暂时无法读取成稿" }
    }), { status: 503 })));
    await expect(authoringApi.manuscript("project-a")).rejects.toThrow("暂时无法读取成稿");
  });
});
