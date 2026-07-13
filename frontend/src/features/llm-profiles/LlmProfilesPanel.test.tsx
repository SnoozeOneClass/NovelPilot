import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../../api/client";
import type { LlmProfilesDocument, LlmProfilePublic } from "../../types/domain";
import { LlmProfilesPanel } from "./LlmProfilesPanel";

const emptyProfiles: LlmProfilesDocument = {
  schema_version: 1,
  active_profile_id: null,
  profiles: []
};

const savedProfile: LlmProfilePublic = {
  id: "main",
  name: "Main",
  protocol: "openai-compatible",
  base_url: "https://api.example.com/v1",
  model: "story-model",
  request_options: { reasoning_effort: "high" },
  enabled: true,
  has_api_key: true
};

async function fillRequiredFields(user: ReturnType<typeof userEvent.setup>) {
  await user.type(screen.getByLabelText("Profile ID"), "main");
  await user.type(screen.getByLabelText("显示名称"), "Main");
  await user.type(screen.getByLabelText("模型名"), "story-model");
  await user.type(screen.getByLabelText("API Key"), "secret");
}

describe("LlmProfilesPanel", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api, "profiles").mockResolvedValue(emptyProfiles);
  });

  it("rejects request options that are not a JSON object", async () => {
    const user = userEvent.setup();
    const upsert = vi.spyOn(api, "upsertProfile");
    render(<LlmProfilesPanel />);
    await fillRequiredFields(user);

    const options = screen.getByLabelText("额外请求参数（JSON）");
    fireEvent.change(options, { target: { value: "[]" } });
    await user.click(screen.getByRole("button", { name: "保存配置" }));

    expect(await screen.findByText("请求参数必须是 JSON 对象。")).toBeInTheDocument();
    expect(upsert).not.toHaveBeenCalled();
  });

  it("sends arbitrary nested request options to the profile API", async () => {
    const user = userEvent.setup();
    const upsert = vi.spyOn(api, "upsertProfile").mockResolvedValue(savedProfile);
    vi.spyOn(api, "profiles").mockResolvedValueOnce(emptyProfiles).mockResolvedValue({
      ...emptyProfiles,
      active_profile_id: "main",
      profiles: [savedProfile]
    });
    render(<LlmProfilesPanel />);
    await fillRequiredFields(user);

    const options = screen.getByLabelText("额外请求参数（JSON）");
    fireEvent.change(options, {
      target: {
        value: '{"reasoning_effort":"high","provider_extension":{"novel":true}}'
      }
    });
    await user.click(screen.getByRole("button", { name: "保存配置" }));

    await waitFor(() => expect(upsert).toHaveBeenCalledWith(expect.objectContaining({
      id: "main",
      request_options: {
        reasoning_effort: "high",
        provider_extension: { novel: true }
      }
    })));
  });
});
