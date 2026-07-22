from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from app.core.paths import resolve_artifact_path
from app.llm.gateway import ChatChunk
from app.storage.json_files import read_json


PublicDraftEventEmitter = Callable[[str, dict[str, object]], None]


@dataclass
class _TopLevelContentDecoder:
    """Incrementally decode one top-level JSON string without exposing raw input."""

    depth: int = 0
    root_started: bool = False
    expectation: Literal["key", "colon", "value", "comma"] = "key"
    current_key: str | None = None
    in_string: bool = False
    string_kind: Literal["key", "content", "other"] = "other"
    string_buffer: list[str] = field(default_factory=list)
    escaped: bool = False
    unicode_digits: str | None = None
    pending_high_surrogate: int | None = None
    in_primitive: bool = False
    failed: bool = False
    content_started: bool = False
    content_finished: bool = False

    def feed(self, fragment: str) -> str:
        if self.failed or self.content_finished:
            return ""
        output: list[str] = []
        for char in fragment:
            if self.failed or self.content_finished:
                break
            if self.in_string:
                self._consume_string_char(char, output)
            else:
                self._consume_structural_char(char)
        return "".join(output)

    def _consume_string_char(self, char: str, output: list[str]) -> None:
        if self.unicode_digits is not None:
            if char.lower() not in "0123456789abcdef":
                self.failed = True
                return
            self.unicode_digits += char
            if len(self.unicode_digits) == 4:
                value = int(self.unicode_digits, 16)
                self.unicode_digits = None
                self.escaped = False
                self._append_codepoint(value, output)
            return
        if self.escaped:
            if char == "u":
                self.unicode_digits = ""
                return
            decoded = {
                '"': '"',
                "\\": "\\",
                "/": "/",
                "b": "\b",
                "f": "\f",
                "n": "\n",
                "r": "\r",
                "t": "\t",
            }.get(char)
            if decoded is None:
                self.failed = True
                return
            self.escaped = False
            self._append_string_value(decoded, output)
            return
        if char == "\\":
            self.escaped = True
            return
        if char == '"':
            self.in_string = False
            self._finish_string()
            return
        if ord(char) < 0x20:
            self.failed = True
            return
        self._append_string_value(char, output)

    def _append_codepoint(self, value: int, output: list[str]) -> None:
        if 0xD800 <= value <= 0xDBFF:
            if self.pending_high_surrogate is not None:
                self.failed = True
                return
            self.pending_high_surrogate = value
            return
        if 0xDC00 <= value <= 0xDFFF:
            if self.pending_high_surrogate is None:
                self.failed = True
                return
            high = self.pending_high_surrogate
            self.pending_high_surrogate = None
            value = 0x10000 + ((high - 0xD800) << 10) + (value - 0xDC00)
        elif self.pending_high_surrogate is not None:
            self.failed = True
            return
        self._append_string_value(chr(value), output)

    def _append_string_value(self, value: str, output: list[str]) -> None:
        if self.string_kind == "key":
            self.string_buffer.append(value)
        elif self.string_kind == "content":
            output.append(value)

    def _finish_string(self) -> None:
        if self.pending_high_surrogate is not None or self.escaped:
            self.failed = True
            return
        if self.string_kind == "key":
            self.current_key = "".join(self.string_buffer)
            self.string_buffer.clear()
            self.expectation = "colon"
        elif self.string_kind == "content":
            self.content_finished = True
            self.expectation = "comma"
        elif self.depth == 1 and self.expectation == "value":
            self.expectation = "comma"

    def _consume_structural_char(self, char: str) -> None:
        if self.in_primitive:
            if self.depth == 1 and char in ",}":
                self.in_primitive = False
                self.expectation = "comma"
                self._consume_structural_char(char)
            return
        if not self.root_started:
            if char.isspace():
                return
            if char != "{":
                self.failed = True
                return
            self.root_started = True
            self.depth = 1
            self.expectation = "key"
            return
        if self.depth > 1:
            if char == '"':
                self._start_string("other")
            elif char in "[{":
                self.depth += 1
            elif char in "]}":
                self.depth -= 1
                if self.depth == 1:
                    self.expectation = "comma"
            return
        if char.isspace():
            return
        if self.expectation == "key":
            if char == '"':
                self._start_string("key")
            elif char == "}":
                self.depth = 0
            else:
                self.failed = True
            return
        if self.expectation == "colon":
            if char == ":":
                self.expectation = "value"
            else:
                self.failed = True
            return
        if self.expectation == "value":
            if char == '"':
                kind: Literal["content", "other"] = (
                    "content" if self.current_key == "content" else "other"
                )
                self._start_string(kind)
                self.content_started = kind == "content"
            elif char in "[{":
                self.depth += 1
            elif char in "-0123456789tfn":
                self.in_primitive = True
            else:
                self.failed = True
            return
        if self.expectation == "comma":
            if char == ",":
                self.current_key = None
                self.expectation = "key"
            elif char == "}":
                self.depth = 0
            else:
                self.failed = True

    def _start_string(self, kind: Literal["key", "content", "other"]) -> None:
        self.in_string = True
        self.string_kind = kind
        self.escaped = False
        self.unicode_digits = None
        self.pending_high_surrogate = None
        if kind == "key":
            self.string_buffer.clear()


@dataclass
class _DraftToolStream:
    chapter_id: str
    stream_id: str
    tool_call_id: str
    tool_index: int | None
    decoder: _TopLevelContentDecoder = field(default_factory=_TopLevelContentDecoder)
    pending_text: list[str] = field(default_factory=list)
    pending_characters: int = 0
    resolved: bool = False


class ChapterDraftStreamProjector:
    """Project only ``write_chapter_draft.content`` into safe Harness events."""

    def __init__(
        self,
        *,
        chapter_id: str,
        emit: PublicDraftEventEmitter,
        project_path: Path | None = None,
        minimum_emit_chars: int = 384,
    ) -> None:
        if minimum_emit_chars < 1:
            raise ValueError("minimum_emit_chars must be positive.")
        self._chapter_id = chapter_id
        self._emit = emit
        self._project_path = project_path
        self._minimum_emit_chars = minimum_emit_chars
        self._by_call_id: dict[str, _DraftToolStream] = {}
        self._by_index: dict[int, _DraftToolStream] = {}

    def observe(self, chunk: ChatChunk) -> None:
        if chunk.event_type == "tool_call_start":
            self._start(chunk)
            return
        stream = self._stream_for(chunk)
        if stream is None:
            return
        if chunk.event_type == "tool_argument_delta" and chunk.arguments_delta:
            decoded = stream.decoder.feed(chunk.arguments_delta)
            if decoded:
                stream.pending_text.append(decoded)
                stream.pending_characters += len(decoded)
            if stream.pending_characters >= self._minimum_emit_chars:
                self._flush(stream)
        elif chunk.event_type == "tool_call_stop":
            self._flush(stream)
            if stream.decoder.failed:
                self._discard(stream, "public_projection_invalid_json")

    def observe_agent_event(self, event: dict[str, Any]) -> None:
        if event.get("kind") == "agent_transport_retry":
            self.discard_open("provider_stream_retry")
            return
        if (
            event.get("kind") != "agent_tool_result"
            or event.get("tool_name") != "write_chapter_draft"
        ):
            return
        call_id = event.get("tool_call_id")
        if not isinstance(call_id, str):
            return
        stream = self._by_call_id.get(call_id)
        if stream is None or stream.resolved:
            return
        if event.get("status") != "ok":
            error_code = event.get("error_code")
            self._discard(
                stream,
                error_code if isinstance(error_code, str) else "draft_tool_rejected",
            )
            return
        paths = event.get("artifact_paths")
        artifact_path = next(
            (
                path
                for path in paths
                if isinstance(path, str) and path.endswith("/draft.md")
            ),
            None,
        ) if isinstance(paths, list) else None
        if artifact_path is None:
            self._discard(stream, "draft_artifact_missing")
            return
        characters, draft_revision = self._artifact_metadata(artifact_path)
        stream.resolved = True
        self._emit(
            "chapter_draft_stream_committed",
            {
                "chapter_id": self._chapter_id,
                "stream_id": stream.stream_id,
                "tool_call_id": stream.tool_call_id,
                "artifact_path": artifact_path,
                "characters": characters,
                "draft_revision": draft_revision,
            },
        )

    def discard_open(self, error_code: str) -> None:
        seen: set[str] = set()
        for stream in self._by_call_id.values():
            if stream.stream_id in seen:
                continue
            seen.add(stream.stream_id)
            self._flush(stream)
            self._discard(stream, error_code)

    def _start(self, chunk: ChatChunk) -> None:
        if chunk.tool_name != "write_chapter_draft":
            return
        call_id = chunk.tool_call_id or (
            f"tool-index-{chunk.tool_index}" if chunk.tool_index is not None else uuid4().hex
        )
        existing = self._by_call_id.get(call_id)
        if existing is not None and not existing.resolved:
            return
        stream = _DraftToolStream(
            chapter_id=self._chapter_id,
            stream_id=f"chapter-draft-{uuid4().hex}",
            tool_call_id=call_id,
            tool_index=chunk.tool_index,
        )
        self._by_call_id[call_id] = stream
        if chunk.tool_index is not None:
            self._by_index[chunk.tool_index] = stream
        self._emit(
            "chapter_draft_stream_started",
            {
                "chapter_id": self._chapter_id,
                "stream_id": stream.stream_id,
                "tool_call_id": call_id,
            },
        )

    def _stream_for(self, chunk: ChatChunk) -> _DraftToolStream | None:
        stream = (
            self._by_call_id.get(chunk.tool_call_id)
            if chunk.tool_call_id is not None
            else None
        )
        if stream is None and chunk.tool_index is not None:
            stream = self._by_index.get(chunk.tool_index)
        if stream is not None and chunk.tool_call_id and chunk.tool_call_id not in self._by_call_id:
            self._by_call_id[chunk.tool_call_id] = stream
        return stream

    def _flush(self, stream: _DraftToolStream) -> None:
        if not stream.pending_text or stream.resolved:
            return
        text_delta = "".join(stream.pending_text)
        stream.pending_text.clear()
        stream.pending_characters = 0
        self._emit(
            "chapter_draft_delta",
            {
                "chapter_id": self._chapter_id,
                "stream_id": stream.stream_id,
                "text_delta": text_delta,
            },
        )

    def _discard(self, stream: _DraftToolStream, error_code: str) -> None:
        if stream.resolved:
            return
        stream.resolved = True
        self._emit(
            "chapter_draft_stream_discarded",
            {
                "chapter_id": self._chapter_id,
                "stream_id": stream.stream_id,
                "tool_call_id": stream.tool_call_id,
                "error_code": error_code,
            },
        )

    def _artifact_metadata(self, artifact_path: str) -> tuple[int, int]:
        if self._project_path is None:
            return 0, 0
        try:
            draft_path = resolve_artifact_path(self._project_path, artifact_path)
            content = draft_path.read_text(encoding="utf-8").rstrip("\n")
            workspace = read_json(draft_path.parent / "workspace.json", default={})
            revision = (
                int(workspace.get("draft_revision", 0))
                if isinstance(workspace, dict)
                else 0
            )
        except (OSError, ValueError, TypeError):
            return 0, 0
        return len(content), revision
