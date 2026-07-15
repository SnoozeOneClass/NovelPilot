import { describe, expect, it } from "vitest";
import type { HarnessEvent } from "../../types/domain";
import { latestChapterDraft, reduceChapterDraftStreams } from "./chapter-draft-stream";

function event(
  eventId: string,
  kind: string,
  payload: Record<string, unknown>
): HarnessEvent {
  return {
    seq: Number(eventId.replace(/\D/g, "")) || null,
    event_id: eventId,
    timestamp: "2026-07-15T00:00:00Z",
    project_id: "project-1",
    run_id: "run-1",
    kind,
    loop_layer: "chapter",
    atomic_action: "run_chapter_agent",
    status: kind.endsWith("discarded") ? "failed" : "started",
    artifact_path: null,
    routing_decision: null,
    message: kind,
    payload
  };
}

describe("chapter draft stream reducer", () => {
  it("replays deltas once and reconciles a committed candidate", () => {
    const start = event("event-1", "chapter_draft_stream_started", {
      chapter_id: "chapter-001",
      stream_id: "stream-1",
      tool_call_id: "call-1"
    });
    const delta = event("event-2", "chapter_draft_delta", {
      chapter_id: "chapter-001",
      stream_id: "stream-1",
      text_delta: "潮水退去。"
    });
    const commit = event("event-3", "chapter_draft_stream_committed", {
      chapter_id: "chapter-001",
      stream_id: "stream-1",
      tool_call_id: "call-1",
      artifact_path: "chapters/chapter-001/agent/a/1/draft.md",
      characters: 6,
      draft_revision: 1
    });

    const state = reduceChapterDraftStreams([start, delta, delta, commit]);
    expect(state).toHaveLength(1);
    expect(state[0]).toMatchObject({
      chapterId: "chapter-001",
      text: "潮水退去。",
      status: "candidate",
      artifactPath: "chapters/chapter-001/agent/a/1/draft.md"
    });
    expect(latestChapterDraft(state)?.streamId).toBe("stream-1");
  });

  it("keeps failed partial prose explicitly uncommitted", () => {
    const state = reduceChapterDraftStreams([
      event("event-1", "chapter_draft_stream_started", {
        chapter_id: "chapter-001",
        stream_id: "stream-1",
        tool_call_id: "call-1"
      }),
      event("event-2", "chapter_draft_delta", {
        chapter_id: "chapter-001",
        stream_id: "stream-1",
        text_delta: "未完成正文"
      }),
      event("event-3", "chapter_draft_stream_discarded", {
        chapter_id: "chapter-001",
        stream_id: "stream-1",
        tool_call_id: "call-1",
        error_code: "draft_tool_rejected"
      })
    ]);

    expect(state[0]).toMatchObject({
      text: "未完成正文",
      status: "discarded",
      errorCode: "draft_tool_rejected"
    });
  });

  it("ignores malformed and unrelated event payloads", () => {
    expect(reduceChapterDraftStreams([
      event("event-1", "chapter_draft_delta", {
        chapter_id: "chapter-001",
        stream_id: 1,
        text_delta: "private"
      }),
      event("event-2", "agent_tool_result", { state_patch: "private" })
    ])).toEqual([]);
  });
});
