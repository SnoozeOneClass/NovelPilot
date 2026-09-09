import type { ModelSettingsDocument, ModelSettingsProfile } from "../types/authoring";

export const readyModel: ModelSettingsProfile = {
  profile_id: "model-a", display_name: "创作模型 A", model_id: "model-a-id",
  api_family: "openai_responses", base_url: "http://localhost:8317/v1", enabled: true,
  has_api_key: true, request_options: { max_tokens: 65536, reasoning: { effort: "high" } },
  capability_status: "ready", context_window: 1000000, max_output_tokens: 65536,
  input_price_per_million: 2, output_price_per_million: 4, cache_price_per_million: 0.5,
  metadata_version: 1
};

export const modelSettings: ModelSettingsDocument = {
  selected_profile_id: readyModel.profile_id,
  profiles: [readyModel, { ...readyModel, profile_id: "model-b", display_name: "创作模型 B", model_id: "model-b-id" }]
};
