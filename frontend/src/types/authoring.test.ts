import { describe, expect, it } from "vitest";
import { authoringActivityLabel, decodeAuthoringEvent, decodeAuthoringProject, decodeModelSettings } from "./authoring";
import { modelSettings, readyModel } from "../test/authoring-fixtures";

const project = {
  project_id: "authoring-a",
  brief: "一个守钟人的承诺",
  title: "黎明钟声",
  status: "running",
  stage: "writing",
  target_chapters: 12,
  target_words: null,
  completed_chapters: 3,
  progress_percent: 25,
  failure_reason: null,
  latest_event_sequence: 9,
  profile_bindings: { default: "profile-a" },
  recent_activity: [{ sequence: 9, kind: "chapter_committed", payload: { chapter_number: 3 }, created_at: "2026-09-04T00:00:00Z" }]
};

describe("authoring wire decoders", () => {
  it("decodes safe settings, including incomplete models that need editing", () => {
    const incomplete = { ...readyModel, context_window: null, max_output_tokens: null, metadata_version: null, capability_status: "missing", has_api_key: false };
    expect(decodeModelSettings({ selected_profile_id: null, profiles: [incomplete] })?.profiles[0]).toEqual(incomplete);
    expect(decodeModelSettings(modelSettings)).toEqual(modelSettings);
    expect(decodeModelSettings({ ...modelSettings, profiles: [{ ...readyModel, api_key: "test-only-extra-secret" }] })?.profiles[0]).not.toHaveProperty("api_key");
  });

  it("rejects malformed settings instead of marking an invalid model as usable", () => {
    for (const patch of [
      { enabled: "yes" }, { has_api_key: 1 }, { request_options: [] }, { api_family: "unknown" },
      { context_window: 1.5 }, { max_output_tokens: 0 }, { metadata_version: 0 },
      { output_price_per_million: -1 }, { input_price_per_million: Infinity }, { cache_price_per_million: "0" }
    ]) {
      expect(decodeModelSettings({ selected_profile_id: null, profiles: [{ ...readyModel, ...patch }] })).toBeNull();
    }
    expect(decodeModelSettings({ ...modelSettings, selected_profile_id: 1 })).toBeNull();
    expect(decodeModelSettings({ ...modelSettings, profiles: [readyModel, readyModel] })).toBeNull();
  });

  it("decodes one shared project and event contract", () => {
    expect(decodeAuthoringProject(project)?.progress_percent).toBe(25);
    const event = decodeAuthoringEvent(JSON.stringify({
      project_id: "authoring-a",
      sequence: 9,
      kind: "chapter_committed",
      payload: { chapter_number: 3 },
      created_at: "2026-09-04T00:00:00Z"
    }));
    expect(event?.project_id).toBe("authoring-a");
    expect(event && authoringActivityLabel(event)).toBe("第 3 章已完成");
  });

  it("rejects malformed SSE and HTTP payloads", () => {
    expect(decodeAuthoringEvent("not-json")).toBeNull();
    expect(decodeAuthoringProject({ ...project, progress_percent: "25" })).toBeNull();
    expect(decodeAuthoringProject({ ...project, recent_activity: [{ sequence: "9" }] })).toBeNull();
    expect(decodeAuthoringEvent(JSON.stringify({
      project_id: "authoring-a",
      sequence: -1,
      kind: "chapter_committed",
      payload: {},
      created_at: "now"
    }))).toBeNull();
    expect(decodeAuthoringProject({ ...project, progress_percent: 101 })).toBeNull();
    expect(decodeAuthoringProject({ ...project, latest_event_sequence: 1.5 })).toBeNull();
    expect(decodeAuthoringProject({
      ...project,
      recent_activity: [
        { sequence: 9, kind: "one", payload: {}, created_at: "now" },
        { sequence: 8, kind: "two", payload: {}, created_at: "now" }
      ]
    })).toBeNull();
  });
});
