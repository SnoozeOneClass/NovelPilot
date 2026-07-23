from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.agents.contracts import (
    BookDiscussionContinue,
    BookDiscussionReady,
    BookDiscussionResult,
    BookDiscussionSuggestion,
    BookSupersededDecisionProposal,
)
from app.domain.book.contracts import BookDiscussionState, BookTranscript, BookTranscriptMessage
from app.domain.book.discussion import (
    BookDiscussionBindingError,
    bind_agent_result,
    bind_user_input,
)


def _awaiting_state(**updates: object) -> BookDiscussionState:
    state = BookDiscussionState(
        turn_count=0,
        direction_draft="A memory mystery.",
        discussion_summary="Initial brief received.",
        readiness_status="awaiting_agent",
        readiness_reason="Waiting for the Book Agent.",
    )
    return state.model_copy(update=updates)


def _transcript(message: str = "Write a memory mystery.") -> BookTranscript:
    return BookTranscript(
        messages=[BookTranscriptMessage(sequence=1, role="user", content=message)]
    )


def _continue_result(
    *,
    question: str = "Should the witness know that her memory was changed?",
    selected_title: str | None = None,
) -> BookDiscussionResult:
    return BookDiscussionResult(
        reply="This decision controls the reader's information advantage.",
        direction_draft="A witness investigates the editing of her own memory.",
        discussion_summary="The story is a memory mystery centered on one witness.",
        newly_confirmed_decisions=["The witness drives the investigation."],
        newly_selected_title=selected_title,
        readiness=BookDiscussionContinue(
            status="continue",
            reason="The witness knowledge boundary is unresolved.",
            question=question,
            suggestions=[
                BookDiscussionSuggestion(
                    label="She knows",
                    message="She knows from the opening that her memory was changed.",
                    recommended=True,
                ),
                BookDiscussionSuggestion(
                    label="She discovers it",
                    message="She discovers the memory edit at the first major reversal.",
                ),
            ],
        ),
    )


def test_continuing_turn_binds_stable_suggestion_ids_and_retains_creator_brief() -> None:
    state = _awaiting_state()
    transcript = _transcript()

    first_state, first_transcript = bind_agent_result(
        book_id="book-a",
        state=state,
        transcript=transcript,
        result=_continue_result(),
    )
    second_state, _ = bind_agent_result(
        book_id="book-a",
        state=state,
        transcript=transcript,
        result=_continue_result(),
    )

    assert first_state.readiness_status == "continue"
    assert len(first_state.suggestions) == 2
    assert [item.id for item in first_state.suggestions] == [
        item.id for item in second_state.suggestions
    ]
    assert all(item.action == "answer" and item.value is None for item in first_state.suggestions)
    assert first_transcript.messages[0].content == "Write a memory mystery."
    assert first_transcript.messages[-1].content.endswith(first_state.question or "")


def test_model_contract_does_not_turn_punctuation_or_option_kind_into_control_protocol() -> None:
    payload = _continue_result().model_dump(mode="json")
    payload["reply"] = "Could this reveal strengthen the midpoint? It can, if foreshadowed."
    payload["readiness"]["question"] = "Choose the strongest midpoint reveal"
    payload["readiness"]["suggestions"][0]["formal_title"] = "Echo Testimony"
    result = BookDiscussionResult.model_validate(payload)

    assert isinstance(result.readiness, BookDiscussionContinue)
    assert result.readiness.question == "Choose the strongest midpoint reveal"
    assert result.readiness.suggestions[0].formal_title == "Echo Testimony"
    assert result.readiness.suggestions[1].formal_title is None


def test_continue_shape_exposes_the_actual_minimum_option_count() -> None:
    payload = _continue_result().model_dump(mode="json")
    payload["readiness"]["suggestions"] = payload["readiness"]["suggestions"][:1]
    with pytest.raises(ValidationError):
        BookDiscussionResult.model_validate(payload)


def test_stale_or_edited_suggestion_changes_no_state() -> None:
    state, transcript = bind_agent_result(
        book_id="book-a",
        state=_awaiting_state(),
        transcript=_transcript(),
        result=_continue_result(),
    )

    with pytest.raises(BookDiscussionBindingError, match="stale"):
        bind_user_input(
            state=state,
            transcript=transcript,
            message=state.suggestions[0].message,
            suggestion_id="old-id",
        )
    with pytest.raises(BookDiscussionBindingError, match="Edited"):
        bind_user_input(
            state=state,
            transcript=transcript,
            message=state.suggestions[0].message + " edited",
            suggestion_id=state.suggestions[0].id,
        )
    assert state.selected_title is None
    assert len(transcript.messages) == 2


def test_title_suggestion_is_control_bound_and_model_omission_cannot_clear_it() -> None:
    title_turn = BookDiscussionResult(
        reply="The whole-book direction is now coherent; only the formal title remains.",
        direction_draft="A witness investigates the editing of her own memory.",
        discussion_summary="The Book direction has converged.",
        readiness=BookDiscussionContinue(
            status="continue",
            reason="A title is required.",
            question="Which formal title should this novel use?",
            suggestions=[
                BookDiscussionSuggestion(
                    label="Echo Testimony",
                    message="Use Echo Testimony as the formal title.",
                    formal_title="Echo Testimony",
                    recommended=True,
                ),
                BookDiscussionSuggestion(
                    label="The Second Memory",
                    message="Use The Second Memory as the formal title.",
                    formal_title="The Second Memory",
                ),
            ],
        ),
    )
    state, transcript = bind_agent_result(
        book_id="book-a",
        state=_awaiting_state(),
        transcript=_transcript(),
        result=title_turn,
    )
    selected = state.suggestions[0]
    state, transcript = bind_user_input(
        state=state,
        transcript=transcript,
        message=selected.message,
        suggestion_id=selected.id,
    )
    assert state.selected_title == "Echo Testimony"
    assert state.selected_title_source == "recommended"

    ready_result = BookDiscussionResult(
        reply="The direction and formal title are ready for synthesis.",
        direction_draft="A witness investigates the editing of her own memory.",
        discussion_summary="The Book direction and title are confirmed.",
        newly_selected_title=None,
        readiness=BookDiscussionReady(status="ready", reason="All Book decisions converged."),
    )
    state, _ = bind_agent_result(
        book_id="book-a",
        state=state,
        transcript=transcript,
        result=ready_result,
    )
    assert state.readiness_status == "ready"
    assert state.selected_title == "Echo Testimony"
    assert state.confirmed_decisions[-1] == "正式书名：《Echo Testimony》"


def test_custom_title_requires_latest_human_evidence() -> None:
    result = _continue_result(selected_title="Unspoken Title")
    with pytest.raises(BookDiscussionBindingError, match="not evidenced"):
        bind_agent_result(
            book_id="book-a",
            state=_awaiting_state(),
            transcript=_transcript("I want the title Echo Testimony."),
            result=result,
        )


def test_turn_ten_converges_and_sends_ambiguous_supersession_to_evaluator() -> None:
    state = _awaiting_state(
        turn_count=9,
        confirmed_decisions=["The witness hides the archive.", "The witness hides the key."],
        selected_title="Echo Testimony",
        selected_title_source="custom",
    )
    result = _continue_result()
    result = result.model_copy(
        update={
            "superseded_decisions": [
                BookSupersededDecisionProposal(
                    prior_meaning="The witness hides",
                    replacement="The witness reveals the archive.",
                    reason="The latest answer changes her strategy.",
                )
            ]
        }
    )
    updated, _ = bind_agent_result(
        book_id="book-a",
        state=state,
        transcript=_transcript("Have the witness reveal it instead."),
        result=result,
    )

    assert updated.turn_count == 10
    assert updated.readiness_status == "ready"
    assert updated.question is None and updated.suggestions == []
    assert any("Evaluator reconciliation required" in item for item in updated.contradictions)


def test_ready_result_without_any_confirmed_title_is_rejected() -> None:
    result = BookDiscussionResult(
        reply="The direction is ready.",
        direction_draft="A complete direction.",
        discussion_summary="All direction decisions converged.",
        readiness=BookDiscussionReady(status="ready", reason="Direction converged."),
    )
    with pytest.raises(BookDiscussionBindingError, match="selected title"):
        bind_agent_result(
            book_id="book-a",
            state=_awaiting_state(),
            transcript=_transcript(),
            result=result,
        )
