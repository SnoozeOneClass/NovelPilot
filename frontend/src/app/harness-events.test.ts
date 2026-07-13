import { mergeHarnessEvent } from "./harness-events";
import type { HarnessEvent } from "../types/domain";
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
