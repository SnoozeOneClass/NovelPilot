import type { HarnessEvent } from "../../types/domain";

export type ChapterDraftStatus = "streaming" | "candidate" | "discarded";

export interface ChapterDraftStreamState {
  chapterId: string;
  streamId: string;
  toolCallId: string | null;
  text: string;
  status: ChapterDraftStatus;
  artifactPath: string | null;
  characters: number;
  draftRevision: number;
  errorCode: string | null;
  lastEventId: string;
}

type PublicDraftEvent =
  | { kind: "started"; chapterId: string; streamId: string; toolCallId: string }
  | { kind: "delta"; chapterId: string; streamId: string; textDelta: string }
  | {
      kind: "committed";
      chapterId: string;
      streamId: string;
      toolCallId: string;
      artifactPath: string;
      characters: number;
      draftRevision: number;
    }
  | {
      kind: "discarded";
      chapterId: string;
      streamId: string;
      toolCallId: string;
      errorCode: string;
    };

function stringField(payload: Record<string, unknown>, key: string): string | null {
  const value = payload[key];
  return typeof value === "string" && value.length > 0 ? value : null;
}

function numberField(payload: Record<string, unknown>, key: string): number | null {
  const value = payload[key];
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;
}

export function decodePublicDraftEvent(event: HarnessEvent): PublicDraftEvent | null {
  const chapterId = stringField(event.payload, "chapter_id");
  const streamId = stringField(event.payload, "stream_id");
  if (!chapterId || !streamId) return null;

  switch (event.kind) {
    case "chapter_draft_stream_started": {
      const toolCallId = stringField(event.payload, "tool_call_id");
      return toolCallId ? { kind: "started", chapterId, streamId, toolCallId } : null;
    }
    case "chapter_draft_delta": {
      const textDelta = stringField(event.payload, "text_delta");
      return textDelta ? { kind: "delta", chapterId, streamId, textDelta } : null;
    }
    case "chapter_draft_stream_committed": {
      const toolCallId = stringField(event.payload, "tool_call_id");
      const artifactPath = stringField(event.payload, "artifact_path");
      const characters = numberField(event.payload, "characters");
      const draftRevision = numberField(event.payload, "draft_revision");
      return toolCallId && artifactPath && characters !== null && draftRevision !== null
        ? {
            kind: "committed",
            chapterId,
            streamId,
            toolCallId,
            artifactPath,
            characters,
            draftRevision
          }
        : null;
    }
    case "chapter_draft_stream_discarded": {
      const toolCallId = stringField(event.payload, "tool_call_id");
      const errorCode = stringField(event.payload, "error_code");
      return toolCallId && errorCode
        ? { kind: "discarded", chapterId, streamId, toolCallId, errorCode }
        : null;
    }
    default:
      return null;
  }
}

export function reduceChapterDraftStreams(events: HarnessEvent[]): ChapterDraftStreamState[] {
  const streams = new Map<string, ChapterDraftStreamState>();
  const seenEvents = new Set<string>();

  for (const event of events) {
    if (seenEvents.has(event.event_id)) continue;
    seenEvents.add(event.event_id);
    const decoded = decodePublicDraftEvent(event);
    if (!decoded) continue;

    if (decoded.kind === "started") {
      streams.set(decoded.streamId, {
        chapterId: decoded.chapterId,
        streamId: decoded.streamId,
        toolCallId: decoded.toolCallId,
        text: "",
        status: "streaming",
        artifactPath: null,
        characters: 0,
        draftRevision: 0,
        errorCode: null,
        lastEventId: event.event_id
      });
      continue;
    }

    const current = streams.get(decoded.streamId);
    if (!current || current.chapterId !== decoded.chapterId) continue;
    switch (decoded.kind) {
      case "delta":
        if (current.status === "streaming") {
          current.text += decoded.textDelta;
          current.characters = current.text.length;
          current.lastEventId = event.event_id;
        }
        break;
      case "committed":
        current.status = "candidate";
        current.toolCallId = decoded.toolCallId;
        current.artifactPath = decoded.artifactPath;
        current.characters = decoded.characters;
        current.draftRevision = decoded.draftRevision;
        current.errorCode = null;
        current.lastEventId = event.event_id;
        break;
      case "discarded":
        current.status = "discarded";
        current.toolCallId = decoded.toolCallId;
        current.errorCode = decoded.errorCode;
        current.lastEventId = event.event_id;
        break;
    }
  }

  return [...streams.values()];
}

export function latestChapterDraft(
  streams: ChapterDraftStreamState[]
): ChapterDraftStreamState | null {
  return streams.at(-1) ?? null;
}
