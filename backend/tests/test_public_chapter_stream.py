from pathlib import Path

from app.harness.agents.public_stream import ChapterDraftStreamProjector
from app.llm.gateway import ChatChunk


def _chunk(event_type: str, **updates: object) -> ChatChunk:
    return ChatChunk(
        event_type=event_type,
        provider_snapshot="test-provider",
        **updates,
    )


def test_projector_streams_only_decoded_write_chapter_content() -> None:
    events: list[tuple[str, dict[str, object]]] = []
    projector = ChapterDraftStreamProjector(
        chapter_id="chapter-001",
        emit=lambda kind, payload: events.append((kind, payload)),
        minimum_emit_chars=4,
    )
    arguments = (
        '{"state_patch":"DO-NOT-LEAK","content":"第一行\\n第二行：'
        '\\"潮声\\"与反斜线\\\\。","chapter_id":"chapter-001"}'
    )

    projector.observe(
        _chunk(
            "tool_call_start",
            tool_call_id="call-1",
            tool_name="write_chapter_draft",
            tool_index=0,
        )
    )
    for fragment in [arguments[:7], arguments[7:19], arguments[19:31], *arguments[31:]]:
        projector.observe(
            _chunk(
                "tool_argument_delta",
                tool_call_id="call-1",
                tool_name="write_chapter_draft",
                tool_index=0,
                arguments_delta=fragment,
            )
        )
    projector.observe(
        _chunk(
            "tool_call_stop",
            tool_call_id="call-1",
            tool_name="write_chapter_draft",
            tool_index=0,
        )
    )

    assert events[0][0] == "chapter_draft_stream_started"
    prose = "".join(
        str(payload["text_delta"])
        for kind, payload in events
        if kind == "chapter_draft_delta"
    )
    assert prose == '第一行\n第二行："潮声"与反斜线\\。'
    serialized_payloads = repr(events)
    assert "DO-NOT-LEAK" not in serialized_payloads
    assert "state_patch" not in serialized_payloads
    assert "chapter_id\\\"" not in serialized_payloads


def test_projector_handles_unicode_escape_split_across_chunks() -> None:
    events: list[tuple[str, dict[str, object]]] = []
    projector = ChapterDraftStreamProjector(
        chapter_id="chapter-001",
        emit=lambda kind, payload: events.append((kind, payload)),
        minimum_emit_chars=1,
    )
    projector.observe(
        _chunk(
            "tool_call_start",
            tool_call_id="call-unicode",
            tool_name="write_chapter_draft",
            tool_index=0,
        )
    )
    for fragment in ('{"content":"A\\u', "6f", '6eB"}'):
        projector.observe(
            _chunk(
                "tool_argument_delta",
                tool_call_id="call-unicode",
                tool_name="write_chapter_draft",
                tool_index=0,
                arguments_delta=fragment,
            )
        )
    projector.observe(
        _chunk(
            "tool_call_stop",
            tool_call_id="call-unicode",
            tool_name="write_chapter_draft",
            tool_index=0,
        )
    )

    assert "".join(
        str(payload["text_delta"])
        for kind, payload in events
        if kind == "chapter_draft_delta"
    ) == "A潮B"


def test_projector_ignores_every_other_tool() -> None:
    events: list[tuple[str, dict[str, object]]] = []
    projector = ChapterDraftStreamProjector(
        chapter_id="chapter-001",
        emit=lambda kind, payload: events.append((kind, payload)),
    )

    for chunk in (
        _chunk(
            "tool_call_start",
            tool_call_id="call-secret",
            tool_name="submit_chapter_candidate",
            tool_index=1,
        ),
        _chunk(
            "tool_argument_delta",
            tool_call_id="call-secret",
            tool_name="submit_chapter_candidate",
            tool_index=1,
            arguments_delta='{"content":"private","state_patch":"secret"}',
        ),
        _chunk(
            "tool_call_stop",
            tool_call_id="call-secret",
            tool_name="submit_chapter_candidate",
            tool_index=1,
        ),
    ):
        projector.observe(chunk)

    assert events == []


def test_projector_reconciles_success_and_discards_failed_tool(tmp_path: Path) -> None:
    events: list[tuple[str, dict[str, object]]] = []
    projector = ChapterDraftStreamProjector(
        chapter_id="chapter-001",
        emit=lambda kind, payload: events.append((kind, payload)),
        project_path=tmp_path,
        minimum_emit_chars=1,
    )
    draft_path = Path("chapters/chapter-001/agent/a/activation/candidates/run/draft.md")
    absolute_draft_path = tmp_path / draft_path
    absolute_draft_path.parent.mkdir(parents=True)
    absolute_draft_path.write_text("最终候选正文\n", encoding="utf-8")

    for call_id in ("call-ok", "call-failed"):
        projector.observe(
            _chunk(
                "tool_call_start",
                tool_call_id=call_id,
                tool_name="write_chapter_draft",
                tool_index=0,
            )
        )
        projector.observe(
            _chunk(
                "tool_argument_delta",
                tool_call_id=call_id,
                tool_name="write_chapter_draft",
                tool_index=0,
                arguments_delta='{"content":"草稿"}',
            )
        )
        projector.observe(
            _chunk(
                "tool_call_stop",
                tool_call_id=call_id,
                tool_name="write_chapter_draft",
                tool_index=0,
            )
        )

    projector.observe_agent_event(
        {
            "kind": "agent_tool_result",
            "tool_name": "write_chapter_draft",
            "tool_call_id": "call-ok",
            "status": "ok",
            "artifact_paths": [draft_path.as_posix()],
        }
    )
    projector.observe_agent_event(
        {
            "kind": "agent_tool_result",
            "tool_name": "write_chapter_draft",
            "tool_call_id": "call-failed",
            "status": "error",
            "error_code": "stale_draft_revision",
            "artifact_paths": [],
        }
    )

    committed = next(payload for kind, payload in events if kind.endswith("committed"))
    assert committed["artifact_path"] == draft_path.as_posix()
    assert committed["characters"] == len("最终候选正文")
    discarded = next(payload for kind, payload in events if kind.endswith("discarded"))
    assert discarded["error_code"] == "stale_draft_revision"
    assert "草稿" not in repr(committed)
