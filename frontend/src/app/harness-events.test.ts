import { mergeHarnessEvent, refreshTargetsForEvent } from "./harness-events";
import { harnessEventEvidencePaths, type HarnessEvent } from "../types/domain";
import { describe, expect, it } from "vitest";

const event: HarnessEvent = {
  seq: 1,
  event_id: "event-1",
  timestamp: "2026-07-13T00:00:00Z",
  project_id: "project-1",
  run_id: "run-1",
  kind: "run_started",
  loop_layer: "system",
  atomic_action: null,
  status: "started",
  artifact_path: null,
  routing_decision: null,
  message: "started",
  payload: {}
};

describe("mergeHarnessEvent", () => {
  it("deduplicates replayed events by event id", () => {
    expect(mergeHarnessEvent([event], { ...event })).toEqual([event]);
  });

  it("appends a new event", () => {
    expect(mergeHarnessEvent([], event)).toEqual([event]);
  });
});

describe("refreshTargetsForEvent", () => {
  it("does not refetch server state for streaming text deltas", () => {
    expect(refreshTargetsForEvent({ ...event, kind: "llm_output_delta", status: "delta" })).toEqual([]);
    expect(refreshTargetsForEvent({ ...event, kind: "llm_stream_progress", status: "delta" })).toEqual([]);
  });

  it("refreshes only chapter artifacts and canon for a committed patch", () => {
    expect(refreshTargetsForEvent({
      ...event,
      kind: "state_patch_committed",
      loop_layer: "chapter",
      atomic_action: "commit_state_patch",
      status: "completed",
      artifact_path: "chapters/chapter-001/committed_state_patch.json"
    })).toEqual(["project", "readiness", "artifacts", "canon"]);
  });

  it("refreshes current arc state for story arc events", () => {
    expect(refreshTargetsForEvent({ ...event, kind: "story_arc_planned", loop_layer: "story_arc" })).toContain("arc");
  });
});

describe("harnessEventEvidencePaths", () => {
  it("normalizes the primary artifact and Agent evidence paths once", () => {
    expect(harnessEventEvidencePaths({
      ...event,
      artifact_path: "book/agent/a/a1/failure.json",
      payload: {
        evidence_paths: [
          "book/agent/a/a1/failure.json",
          "book/agent/a/a1/telemetry.json"
        ]
      }
    })).toEqual([
      "book/agent/a/a1/failure.json",
      "book/agent/a/a1/telemetry.json"
    ]);
  });
});
