from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Iterable
from typing import Literal

from app.agents.contracts import (
    BookDiscussionResult,
    BookDiscussionSuggestion,
    BookSupersededDecisionProposal,
)
from app.domain.book.contracts import (
    BookDiscussionState,
    BookSuggestion,
    BookSupersededDecision,
    BookTranscript,
    BookTranscriptMessage,
)

MAX_BOOK_DISCUSSION_TURNS = 10
_TITLE_DECISION_PREFIX = "正式书名：《"


class BookDiscussionBindingError(ValueError):
    """A model proposal or user answer cannot be bound to current Book facts."""


def bind_user_input(
    *,
    state: BookDiscussionState,
    transcript: BookTranscript,
    message: str,
    suggestion_id: str | None,
) -> tuple[BookDiscussionState, BookTranscript]:
    """Bind one user answer without inferring control meaning from presentation text."""

    normalized_message = message.strip()
    selected_title = state.selected_title
    selected_title_source = state.selected_title_source
    if suggestion_id is not None:
        suggestion = next((item for item in state.suggestions if item.id == suggestion_id), None)
        if suggestion is None:
            raise BookDiscussionBindingError("Book suggestion is stale or unknown.")
        if normalized_message != suggestion.message.strip():
            raise BookDiscussionBindingError(
                "Edited suggestion text must be submitted as a custom answer without its ID."
            )
        if suggestion.action == "select_title":
            assert suggestion.value is not None
            selected_title = suggestion.value.strip()
            selected_title_source = "recommended"

    confirmed = _merge_title_decision(state.confirmed_decisions, selected_title)
    updated_state = state.model_copy(
        update={
            "confirmed_decisions": confirmed,
            "selected_title": selected_title,
            "selected_title_source": selected_title_source,
            "question": None,
            "suggestions": [],
            "readiness_status": "awaiting_agent",
            "readiness_reason": "Waiting for the Book Agent to process the latest user answer.",
        }
    )
    return updated_state, _append_message(transcript, role="user", content=normalized_message)


def bind_agent_result(
    *,
    book_id: str,
    state: BookDiscussionState,
    transcript: BookTranscript,
    result: BookDiscussionResult,
) -> tuple[BookDiscussionState, BookTranscript]:
    """Convert a semantic Agent proposal into the next deterministic discussion state."""

    if state.readiness_status != "awaiting_agent":
        raise BookDiscussionBindingError("Book discussion is not awaiting an Agent result.")
    latest_user_message = _latest_user_message(transcript)
    turn = state.turn_count + 1
    selected_title = state.selected_title
    selected_title_source = state.selected_title_source
    proposed_title = _optional_text(result.newly_selected_title)
    if proposed_title is not None and proposed_title != selected_title:
        if not _contains_title_evidence(latest_user_message, proposed_title):
            raise BookDiscussionBindingError(
                "A changed Book title is not evidenced by the latest human message."
            )
        selected_title = proposed_title
        selected_title_source = "custom"

    confirmed = list(state.confirmed_decisions)
    superseded = list(state.superseded_decisions)
    contradictions = _deduplicate(result.contradictions)
    converges_at_boundary = bool(
        selected_title is not None and turn >= MAX_BOOK_DISCUSSION_TURNS
    )
    for proposal in result.superseded_decisions:
        confirmed, record, contradiction = _apply_supersession(
            confirmed=confirmed,
            proposal=proposal,
            latest_user_message=latest_user_message,
            turn=turn,
            allow_ambiguous=converges_at_boundary,
        )
        superseded.append(record)
        if contradiction is not None:
            contradictions.append(contradiction)

    confirmed = _deduplicate([*confirmed, *result.newly_confirmed_decisions])
    confirmed = _merge_title_decision(confirmed, selected_title)
    if result.readiness.status == "ready" and selected_title is None:
        raise BookDiscussionBindingError("A ready Book discussion requires a selected title.")

    readiness_status: Literal["continue", "ready"] = (
        "ready"
        if result.readiness.status == "ready" or converges_at_boundary
        else "continue"
    )
    if readiness_status == "ready":
        question = None
        suggestions: list[BookSuggestion] = []
        readiness_reason = (
            "The ten-turn discussion boundary was reached; remaining gaps are delegated "
            "to candidate evaluation."
            if converges_at_boundary and result.readiness.status != "ready"
            else result.readiness.reason
        )
    else:
        assert result.question is not None
        question = result.question.strip()
        suggestions = [
            _bind_suggestion(book_id=book_id, turn=turn, index=index, suggestion=suggestion)
            for index, suggestion in enumerate(result.suggestions, start=1)
        ]
        readiness_reason = result.readiness.reason

    updated_state = BookDiscussionState(
        turn_count=turn,
        direction_draft=result.direction_draft,
        discussion_summary=result.discussion_summary,
        confirmed_decisions=confirmed,
        superseded_decisions=superseded,
        unresolved_questions=_deduplicate(result.unresolved_questions),
        assumptions=_deduplicate(result.assumptions),
        contradictions=_deduplicate(contradictions),
        selected_title=selected_title,
        selected_title_source=selected_title_source,
        question=question,
        suggestions=suggestions,
        readiness_status=readiness_status,
        readiness_reason=readiness_reason,
    )
    assistant_content = result.reply.strip()
    if question is not None:
        assistant_content = f"{assistant_content}\n\n{question}"
    return updated_state, _append_message(
        transcript,
        role="assistant",
        content=assistant_content,
    )


def stable_suggestion_id(
    *,
    book_id: str,
    turn: int,
    index: int,
    label: str,
    message: str,
    formal_title: str | None,
) -> str:
    canonical = json.dumps(
        {
            "schema": "book-suggestion-identity-v1",
            "book_id": book_id,
            "turn": turn,
            "index": index,
            "label": label,
            "message": message,
            "formal_title": formal_title,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"book-suggestion-{hashlib.sha256(canonical).hexdigest()[:24]}"


def _bind_suggestion(
    *,
    book_id: str,
    turn: int,
    index: int,
    suggestion: BookDiscussionSuggestion,
) -> BookSuggestion:
    formal_title = _optional_text(suggestion.formal_title)
    return BookSuggestion(
        id=stable_suggestion_id(
            book_id=book_id,
            turn=turn,
            index=index,
            label=suggestion.label,
            message=suggestion.message,
            formal_title=formal_title,
        ),
        label=suggestion.label.strip(),
        message=suggestion.message.strip(),
        rationale=suggestion.rationale.strip(),
        recommended=suggestion.recommended,
        action="select_title" if formal_title is not None else "answer",
        value=formal_title,
    )


def _apply_supersession(
    *,
    confirmed: list[str],
    proposal: BookSupersededDecisionProposal,
    latest_user_message: str,
    turn: int,
    allow_ambiguous: bool,
) -> tuple[list[str], BookSupersededDecision, str | None]:
    if _normalized_text(proposal.user_evidence) not in _normalized_text(latest_user_message):
        raise BookDiscussionBindingError(
            "A superseded Book decision is not evidenced by the latest human message."
        )
    matches = _matching_decision_indexes(confirmed, proposal.prior_meaning)
    replacement = _optional_text(proposal.replacement)
    if len(matches) == 1:
        index = matches[0]
        decision = confirmed[index]
        updated = list(confirmed)
        if replacement is None:
            updated.pop(index)
        else:
            updated[index] = replacement
        return (
            _deduplicate(updated),
            BookSupersededDecision(
                turn=turn,
                decision=decision,
                replacement=replacement,
                reason=proposal.reason,
                user_evidence=proposal.user_evidence,
            ),
            None,
        )
    if not allow_ambiguous:
        raise BookDiscussionBindingError("Book superseded decision could not be resolved uniquely.")
    updated = list(confirmed)
    if replacement is not None:
        updated.append(replacement)
    contradiction = (
        "Evaluator reconciliation required for late supersession: "
        f"{proposal.prior_meaning} -> {replacement or '[removed]'}"
    )
    return (
        _deduplicate(updated),
        BookSupersededDecision(
            turn=turn,
            decision=proposal.prior_meaning,
            replacement=replacement,
            reason=proposal.reason,
            user_evidence=proposal.user_evidence,
        ),
        contradiction,
    )


def _matching_decision_indexes(decisions: list[str], meaning: str) -> list[int]:
    target = _normalized_text(meaning)
    exact = [index for index, item in enumerate(decisions) if _normalized_text(item) == target]
    if exact:
        return exact
    return [
        index
        for index, item in enumerate(decisions)
        if target in _normalized_text(item) or _normalized_text(item) in target
    ]


def _merge_title_decision(decisions: Iterable[str], selected_title: str | None) -> list[str]:
    without_title = [
        item for item in _deduplicate(decisions) if not item.startswith(_TITLE_DECISION_PREFIX)
    ]
    if selected_title is not None:
        without_title.append(f"{_TITLE_DECISION_PREFIX}{selected_title}》")
    return without_title


def _append_message(
    transcript: BookTranscript,
    *,
    role: Literal["user", "assistant"],
    content: str,
) -> BookTranscript:
    sequence = 1 if not transcript.messages else transcript.messages[-1].sequence + 1
    return transcript.model_copy(
        update={
            "messages": [
                *transcript.messages,
                BookTranscriptMessage(sequence=sequence, role=role, content=content),
            ]
        }
    )


def _latest_user_message(transcript: BookTranscript) -> str:
    for message in reversed(transcript.messages):
        if message.role == "user":
            return message.content
    raise BookDiscussionBindingError("Book transcript has no human message to bind.")


def _contains_title_evidence(message: str, title: str) -> bool:
    normalized_title = _normalized_text(title).strip("《》〈〉\"'“”‘’")
    return bool(normalized_title and normalized_title in _normalized_text(message))


def _normalized_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _deduplicate(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = value.strip()
        key = _normalized_text(normalized)
        if normalized and key not in seen:
            result.append(normalized)
            seen.add(key)
    return result
