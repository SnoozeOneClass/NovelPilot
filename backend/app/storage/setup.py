from __future__ import annotations

import json
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.schemas.setup import (
    BookDirectionCandidate,
    BookDirectionConstraints,
    BookDirectionReview,
    SetupApprovalRequest,
    SetupMessage,
    SetupReadinessSignal,
    SetupStateDocument,
    SetupSuggestion,
    TitleSelectionSource,
    missing_confirmed_decisions,
)
from app.schemas.events import HarnessEvent
from app.schemas.projects import ProjectMetadata
from app.storage.events import append_event as append_durable_event
from app.storage.file_lock import exclusive_file_lock
from app.storage.json_files import read_json, write_json
from app.storage.text_files import read_text_file
from app.storage.transactions import commit_file_transaction, recover_file_transactions

if TYPE_CHECKING:
    from app.harness.loops.book import BookDirectionSynthesis, BookDiscussionTurnResult


class SetupRevisionConflict(ValueError):
    pass


def enqueue_pending_setup_event(project_path: Path, event: HarnessEvent) -> None:
    with exclusive_file_lock(project_path / "book" / ".event-outbox.lock"):
        path = project_path / "book" / ".event-outbox" / f"{event.event_id}.json"
        write_json(path, event.model_dump(mode="json"))


def flush_pending_setup_events(project_path: Path) -> None:
    with exclusive_file_lock(project_path / "book" / ".event-outbox.lock"):
        outbox = project_path / "book" / ".event-outbox"
        if not outbox.exists():
            return
        for path in sorted(outbox.glob("*.json")):
            try:
                event = HarnessEvent.model_validate(read_json(path))
                append_durable_event(project_path, event)
                path.unlink()
            except (OSError, ValueError):
                return
        try:
            outbox.rmdir()
        except OSError:
            pass


def initialize_setup_state(project_path: Path) -> SetupStateDocument:
    with _setup_project_lock(project_path):
        recover_file_transactions(project_path)
        state = _initial_setup_state()
        commit_file_transaction(
            project_path,
            kind="setup-initialize",
            files=_initial_setup_files(state),
        )
        return state


def read_setup_state(project_path: Path) -> SetupStateDocument:
    with _setup_project_lock(project_path):
        recover_file_transactions(project_path)
        return _read_setup_state_unlocked(project_path)


def _read_setup_state_unlocked(project_path: Path) -> SetupStateDocument:
    data = read_json(_setup_path(project_path))
    if data is None:
        state = _initial_setup_state()
        commit_file_transaction(
            project_path,
            kind="setup-initialize",
            files=_initial_setup_files(state),
        )
        return state
    if not isinstance(data, dict):
        raise ValueError("Book setup state must be a JSON object.")
    if int(data.get("schema_version", 1)) < 2 or "questions" in data:
        return _migrate_legacy_setup(project_path, data)
    return SetupStateDocument.model_validate(data)


def write_discussion_context_snapshot(
    project_path: Path,
    *,
    turn: int,
    snapshot: dict[str, Any],
) -> str:
    with _setup_project_lock(project_path):
        recover_file_transactions(project_path)
        attempt_dir = _next_turn_attempt_dir(project_path, turn)
        relative_path = attempt_dir / "context_snapshot.json"
        write_json(project_path / relative_path, snapshot)
        return relative_path.as_posix()


def record_discussion_turn(
    project_path: Path,
    state: SetupStateDocument,
    *,
    user_message: str,
    result: BookDiscussionTurnResult,
    context_snapshot_path: str,
    profile_id: str,
) -> SetupStateDocument:
    with _setup_project_lock(project_path):
        recover_file_transactions(project_path)
        _assert_current_revision_unlocked(project_path, state)
        if state.approved:
            raise ValueError("Approved book direction cannot return to setup discussion.")

        updated = state.model_copy(deep=True)
        turn = updated.turn_count + 1
        now = datetime.now(UTC)
        updated.messages.extend(
            [
                SetupMessage(turn=turn, role="user", content=user_message, created_at=now),
                SetupMessage(
                    turn=turn,
                    role="assistant",
                    content=result.reply,
                    profile_id=profile_id,
                    model_snapshot=result.model_snapshot,
                ),
            ]
        )
        updated.turn_count = turn
        updated.revision += 1
        updated.phase = "discussing"
        updated.direction_draft = result.direction_draft
        updated.discussion_summary = result.discussion_summary
        updated.confirmed_decisions = _merge_confirmed_decisions(state, result)
        updated.superseded_decisions.extend(result.superseded_decisions)
        updated.unresolved_questions = result.unresolved_questions
        updated.assumptions = result.assumptions
        updated.contradictions = result.contradictions
        updated.selected_title = _validated_selected_title(
            state,
            result,
            user_message=user_message,
        )
        updated.title_selection_source = _title_selection_source(
            state,
            updated.selected_title,
            user_message=user_message,
        )
        updated.confirmed_decisions = _merge_title_decision(
            updated.confirmed_decisions,
            updated.selected_title,
        )
        updated.question = result.question
        updated.suggestions = result.suggestions
        updated.readiness = result.readiness
        updated.candidate = None
        updated.last_context_snapshot_path = context_snapshot_path
        updated.direction_draft_version_path = (
            Path(context_snapshot_path).parent / "direction_draft.md"
        ).as_posix()
        updated.discussion_state_version_path = (
            Path(context_snapshot_path).parent / "state.json"
        ).as_posix()
        updated.discussion_transcript_version_path = (
            Path(context_snapshot_path).parent / "transcript.jsonl"
        ).as_posix()
        updated.last_profile_id = profile_id
        updated.last_model_snapshot = result.model_snapshot
        if len(
            _json_document(
                {
                    "confirmed_decisions": updated.confirmed_decisions,
                    "unresolved_questions": updated.unresolved_questions,
                    "assumptions": updated.assumptions,
                    "contradictions": updated.contradictions,
                }
            )
        ) > 25_000:
            raise ValueError("Active book discussion decisions exceed their context budget.")

        attempt_dir = Path(context_snapshot_path).parent
        commit_file_transaction(
            project_path,
            kind=f"book-discussion-turn-{turn:04d}",
            files={
                (attempt_dir / "response.json").as_posix(): _json_document(
                    _discussion_response_payload(updated, result)
                ),
                (attempt_dir / "direction_draft.md").as_posix(): (
                    result.direction_draft.rstrip() + "\n"
                ),
                (attempt_dir / "transcript.jsonl").as_posix(): _transcript_content(
                    updated
                ),
                (attempt_dir / "state.json").as_posix(): _json_document(
                    updated.model_dump(mode="json")
                ),
                "book/direction_draft.md": result.direction_draft.rstrip() + "\n",
                "book/discussion/transcript.jsonl": _transcript_content(updated),
                "book/setup.json": _json_document(updated.model_dump(mode="json")),
            },
        )
        return updated


def record_agent_user_decision(
    project_path: Path,
    state: SetupStateDocument,
    *,
    payload: dict[str, Any],
    checkpoint_path: str,
    profile_id: str,
    model_snapshot: str,
) -> SetupStateDocument:
    """Persist one Agent-selected question without inventing a user message."""

    with _setup_project_lock(project_path):
        recover_file_transactions(project_path)
        _assert_current_revision_unlocked(project_path, state)
        question = str(payload.get("question", "")).strip()
        context = str(payload.get("context", "")).strip()
        raw_suggestions = payload.get("suggestions")
        if not question or not isinstance(raw_suggestions, list):
            raise ValueError("Book Agent user-decision checkpoint is incomplete.")
        suggestions = [
            SetupSuggestion(
                id=f"{payload.get('checkpoint_id', 'book-decision')}:{index + 1}",
                label=str(item.get("label", "")).strip(),
                message=str(item.get("message", "")).strip(),
                rationale=str(item.get("rationale", "")).strip(),
                recommended=item.get("recommended") is True,
            )
            for index, item in enumerate(raw_suggestions)
            if isinstance(item, dict)
        ]
        if not 2 <= len(suggestions) <= 3:
            raise ValueError("Book Agent user decision requires two or three suggestions.")

        updated = state.model_copy(deep=True)
        turn = updated.turn_count + 1
        updated.messages.append(
            SetupMessage(
                turn=turn,
                role="assistant",
                content=context or "当前候选还需要你确认一个关键决定。",
                profile_id=profile_id,
                model_snapshot=model_snapshot,
            )
        )
        updated.turn_count = turn
        updated.revision += 1
        updated.phase = "discussing"
        updated.candidate = None
        updated.question = question
        updated.suggestions = suggestions
        updated.readiness = SetupReadinessSignal(
            status="continue",
            reason=context or "审查需要一个明确的用户决定。",
        )
        updated.last_context_snapshot_path = checkpoint_path
        updated.last_profile_id = profile_id
        updated.last_model_snapshot = model_snapshot

        version_root = Path("book") / "discussion" / "versions" / (
            f"revision-{updated.revision:04d}"
        )
        updated.direction_draft_version_path = (
            version_root / "direction_draft.md"
        ).as_posix()
        updated.discussion_state_version_path = (version_root / "state.json").as_posix()
        updated.discussion_transcript_version_path = (
            version_root / "transcript.jsonl"
        ).as_posix()
        commit_file_transaction(
            project_path,
            kind=f"book-agent-user-decision-{turn:04d}",
            files={
                updated.direction_draft_version_path: updated.direction_draft.rstrip() + "\n",
                updated.discussion_state_version_path: _json_document(
                    updated.model_dump(mode="json")
                ),
                updated.discussion_transcript_version_path: _transcript_content(updated),
                "book/discussion/transcript.jsonl": _transcript_content(updated),
                "book/setup.json": _json_document(updated.model_dump(mode="json")),
            },
        )
        return updated


def write_review_context_snapshot(
    project_path: Path,
    *,
    candidate_revision: int,
    snapshot: dict[str, Any],
) -> str:
    with _setup_project_lock(project_path):
        recover_file_transactions(project_path)
        review_root = (
            project_path / "book" / "reviews" / f"review-{candidate_revision:04d}"
        )
        attempt = 1
        while (review_root / f"attempt-{attempt:03d}").exists():
            attempt += 1
        relative_path = (
            Path("book")
            / "reviews"
            / f"review-{candidate_revision:04d}"
            / f"attempt-{attempt:03d}"
            / "context_snapshot.json"
        )
        write_json(project_path / relative_path, snapshot)
        return relative_path.as_posix()


def save_book_direction_candidate(
    project_path: Path,
    state: SetupStateDocument,
    *,
    synthesis: BookDirectionSynthesis,
    review: BookDirectionReview,
    profile_id: str,
    review_model_snapshot: str,
    context_snapshot_path: str,
) -> SetupStateDocument:
    with _setup_project_lock(project_path):
        recover_file_transactions(project_path)
        _assert_current_revision_unlocked(project_path, state)
        if state.approved:
            raise ValueError("Book direction is already approved.")
        if not state.direction_draft.strip():
            raise ValueError("Book direction discussion has not produced a draft.")

        updated = state.model_copy(deep=True)
        revision = updated.candidate_revision_counter + 1
        review_root = Path("book") / "reviews" / f"review-{revision:04d}"
        direction_path = review_root / "candidate_direction.md"
        constraints_path = review_root / "candidate_constraints.json"
        title_suggestions_path = review_root / "candidate_titles.json"
        rolling_plan_path = review_root / "rolling_plan.md"
        verification_path = review_root / "verification.json"

        updated.candidate_revision_counter = revision
        updated.candidate = BookDirectionCandidate(
            revision=revision,
            direction_markdown=synthesis.direction_markdown,
            constraints=synthesis.constraints,
            confirmed_decision_coverage=synthesis.confirmed_decision_coverage,
            recommended_titles=synthesis.recommended_titles,
            rolling_plan_markdown=synthesis.rolling_plan_markdown,
            review=review,
            direction_path=direction_path.as_posix(),
            constraints_path=constraints_path.as_posix(),
            title_suggestions_path=title_suggestions_path.as_posix(),
            rolling_plan_path=rolling_plan_path.as_posix(),
            verification_path=verification_path.as_posix(),
            profile_id=profile_id,
            model_snapshot=synthesis.model_snapshot,
            review_model_snapshot=review_model_snapshot,
        )
        updated.phase = "review_ready" if review.commit_allowed else "review_blocked"
        updated.direction_draft = synthesis.direction_markdown
        updated.revision += 1
        updated.last_context_snapshot_path = context_snapshot_path
        updated.direction_draft_version_path = direction_path.as_posix()
        updated.discussion_state_version_path = (review_root / "state.json").as_posix()
        updated.discussion_transcript_version_path = (
            review_root / "transcript.jsonl"
        ).as_posix()
        updated.last_profile_id = profile_id
        updated.last_model_snapshot = review_model_snapshot

        commit_file_transaction(
            project_path,
            kind=f"book-direction-review-{revision:04d}",
            files={
                direction_path.as_posix(): synthesis.direction_markdown.rstrip() + "\n",
                constraints_path.as_posix(): _json_document(
                    {
                        "schema_version": 1,
                        "candidate": True,
                        "confirmed_decision_coverage": [
                            item.model_dump(mode="json")
                            for item in synthesis.confirmed_decision_coverage
                        ],
                        **synthesis.constraints.model_dump(mode="json"),
                    }
                ),
                title_suggestions_path.as_posix(): _json_document(
                    {
                        "schema_version": 1,
                        "candidate": True,
                        "recommended_titles": [
                            item.model_dump(mode="json")
                            for item in synthesis.recommended_titles
                        ],
                    }
                ),
                rolling_plan_path.as_posix(): (
                    synthesis.rolling_plan_markdown.rstrip() + "\n"
                ),
                verification_path.as_posix(): _json_document(
                    _verification_payload(review)
                ),
                "book/direction_draft.md": synthesis.direction_markdown.rstrip() + "\n",
                (review_root / "transcript.jsonl").as_posix(): _transcript_content(
                    updated
                ),
                (review_root / "state.json").as_posix(): _json_document(
                    updated.model_dump(mode="json")
                ),
                "book/discussion/transcript.jsonl": _transcript_content(updated),
                "book/setup.json": _json_document(updated.model_dump(mode="json")),
            },
        )
        return updated


def approve_setup(
    project_path: Path,
    request: SetupApprovalRequest,
) -> SetupStateDocument:
    with _setup_project_lock(project_path):
        with exclusive_file_lock(project_path / ".project.lock"):
            recover_file_transactions(project_path)
            state = _read_setup_state_unlocked(project_path)
            if state.approved:
                raise ValueError("Book direction is already approved.")
            candidate = state.candidate
            if candidate is None:
                raise ValueError(
                    "Book direction must be synthesized and reviewed before approval."
                )
            if candidate.revision != request.candidate_revision:
                raise ValueError("Book direction candidate is stale; review the latest candidate.")
            if not candidate.approval_allowed:
                raise ValueError("Book direction review has blocking issues.")
            if not state.selected_title:
                raise ValueError("Confirm the formal book title before approval.")
            if request.title != state.selected_title:
                raise ValueError("Approved title does not match the confirmed discussion title.")
            missing_decisions = _candidate_missing_confirmed_decisions(state, candidate)
            if missing_decisions:
                raise ValueError(
                    "Book direction candidate does not preserve every confirmed decision."
                )

            metadata_payload = read_json(project_path / "project.json")
            if metadata_payload is None:
                raise FileNotFoundError("Project metadata is missing.")
            metadata = ProjectMetadata.model_validate(metadata_payload)
            approved_at = datetime.now(UTC)
            metadata.title = request.title
            metadata.updated_at = approved_at

            updated = state.model_copy(deep=True)
            updated.approved = True
            updated.approved_at = approved_at
            updated.approved_title = request.title
            updated.title_selection_source = state.title_selection_source or "custom"
            updated.phase = "approved"
            updated.revision += 1
            updated.question = None
            updated.suggestions = []
            files = _approved_book_files(project_path, updated, candidate)
            files["project.json"] = _json_document(metadata.model_dump(mode="json"))
            files["book/discussion/transcript.jsonl"] = _transcript_content(updated)
            files["book/setup.json"] = _json_document(updated.model_dump(mode="json"))
            commit_file_transaction(
                project_path,
                kind=f"book-direction-approval-{candidate.revision:04d}",
                files=files,
            )
            return updated


def _setup_path(project_path: Path) -> Path:
    return project_path / "book" / "setup.json"


def _initial_setup_state() -> SetupStateDocument:
    version_root = Path("book") / "discussion" / "versions" / "revision-0001"
    return SetupStateDocument(
        direction_draft_version_path=(version_root / "direction_draft.md").as_posix(),
        discussion_state_version_path=(version_root / "state.json").as_posix(),
        discussion_transcript_version_path=(version_root / "transcript.jsonl").as_posix(),
    )


def _initial_setup_files(state: SetupStateDocument) -> dict[str, str | bytes]:
    assert state.direction_draft_version_path is not None
    assert state.discussion_state_version_path is not None
    assert state.discussion_transcript_version_path is not None
    state_document = _json_document(state.model_dump(mode="json"))
    return {
        state.direction_draft_version_path: "",
        state.discussion_state_version_path: state_document,
        state.discussion_transcript_version_path: "",
        "book/discussion/transcript.jsonl": "",
        "book/setup.json": state_document,
    }


def _setup_project_lock(project_path: Path) -> AbstractContextManager[None]:
    return exclusive_file_lock(project_path / "book" / ".setup.lock")


def _assert_current_revision_unlocked(
    project_path: Path,
    expected: SetupStateDocument,
) -> None:
    current = read_json(_setup_path(project_path), default={}) or {}
    current_revision = current.get("revision")
    if current_revision != expected.revision:
        raise SetupRevisionConflict(
            "Book discussion state changed while the model was working; discard the stale result."
        )


def _transcript_content(state: SetupStateDocument) -> str:
    lines = [message.model_dump_json() for message in state.messages]
    content = "\n".join(lines)
    if content:
        content += "\n"
    return content


def _json_document(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def _next_turn_attempt_dir(project_path: Path, turn: int) -> Path:
    turn_root = project_path / "book" / "discussion" / f"turn-{turn:04d}"
    attempt = 1
    while (turn_root / f"attempt-{attempt:03d}").exists():
        attempt += 1
    return (
        Path("book")
        / "discussion"
        / f"turn-{turn:04d}"
        / f"attempt-{attempt:03d}"
    )


def _discussion_response_payload(
    state: SetupStateDocument,
    result: BookDiscussionTurnResult,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "turn": state.turn_count,
        "reply": result.reply,
        "discussion_summary": result.discussion_summary,
        "confirmed_decisions": state.confirmed_decisions,
        "superseded_decisions": [
            item.model_dump(mode="json") for item in state.superseded_decisions
        ],
        "unresolved_questions": result.unresolved_questions,
        "assumptions": result.assumptions,
        "contradictions": result.contradictions,
        "selected_title": state.selected_title,
        "question": result.question,
        "suggestions": [item.model_dump(mode="json") for item in result.suggestions],
        "readiness": result.readiness.model_dump(mode="json"),
        "profile_id": state.last_profile_id,
        "model_snapshot": result.model_snapshot,
        "usage": result.usage,
    }


def _merge_confirmed_decisions(
    state: SetupStateDocument,
    result: BookDiscussionTurnResult,
) -> list[str]:
    existing = list(state.confirmed_decisions)
    superseded = {item.decision for item in result.superseded_decisions}
    unknown = [decision for decision in superseded if decision not in existing]
    if unknown:
        raise ValueError(
            "Model tried to supersede decisions that are not currently confirmed: "
            + "; ".join(unknown)
        )
    missing_replacements = [
        item.replacement
        for item in result.superseded_decisions
        if item.replacement and item.replacement not in result.confirmed_decisions
    ]
    if missing_replacements:
        raise ValueError(
            "Replacement decisions must appear in confirmed_decisions: "
            + "; ".join(missing_replacements)
        )
    merged = [decision for decision in existing if decision not in superseded]
    merged.extend(
        decision for decision in result.confirmed_decisions if decision not in superseded
    )
    return list(dict.fromkeys(merged))


def _validated_selected_title(
    state: SetupStateDocument,
    result: BookDiscussionTurnResult,
    *,
    user_message: str,
) -> str | None:
    selected = (result.selected_title or "").strip() or None
    if selected == state.selected_title:
        return selected
    if selected is None and state.selected_title:
        return state.selected_title
    if selected is None:
        return None
    if selected.casefold() not in user_message.casefold():
        raise ValueError(
            "Book Agent can set the formal title only from the user's explicit answer."
        )
    return selected


def _title_selection_source(
    state: SetupStateDocument,
    selected_title: str | None,
    *,
    user_message: str,
) -> TitleSelectionSource | None:
    if selected_title == state.selected_title:
        return state.title_selection_source
    if any(suggestion.message == user_message for suggestion in state.suggestions):
        return "recommended"
    return "custom"


def _merge_title_decision(decisions: list[str], selected_title: str | None) -> list[str]:
    without_previous_title = [
        decision for decision in decisions if not decision.startswith("正式书名：")
    ]
    if selected_title:
        without_previous_title.append(f"正式书名：《{selected_title}》")
    return without_previous_title


def _approved_book_files(
    project_path: Path,
    state: SetupStateDocument,
    candidate: BookDirectionCandidate,
) -> dict[str, str | bytes]:
    direction = candidate.direction_markdown.rstrip() + "\n"
    rolling_plan = candidate.rolling_plan_markdown.rstrip() + "\n"
    previous_state = read_json(project_path / "book" / "state.json", default={}) or {}
    previous_version = int(previous_state.get("version", 1))
    return {
        "book/direction.md": direction,
        "book/settings.md": direction,
        "book/outline.md": rolling_plan,
        "book/constraints.json": _json_document(
            {
                "schema_version": 1,
                "candidate": False,
                "approved_at": state.approved_at.isoformat() if state.approved_at else None,
                "source_candidate_revision": candidate.revision,
                "confirmed_decision_coverage": [
                    item.model_dump(mode="json")
                    for item in candidate.confirmed_decision_coverage
                ],
                **candidate.constraints.model_dump(mode="json"),
            }
        ),
        "book/state.json": _json_document(
            {
                "schema_version": 2,
                "version": previous_version + 1,
                "setup_approved": True,
                "approved_at": state.approved_at.isoformat() if state.approved_at else None,
                "title": state.approved_title,
                "title_selection_source": state.title_selection_source,
                "book_direction_version": candidate.revision,
                "approved_direction_path": "book/direction.md",
                "approved_constraints_path": "book/constraints.json",
                "rolling_plan_path": "book/outline.md",
                "confirmed_decisions": candidate.constraints.confirmed,
                "must_preserve": candidate.constraints.must_preserve,
                "must_avoid": candidate.constraints.must_avoid,
                "creative_freedoms": candidate.constraints.creative_freedoms,
                "open_decisions": candidate.constraints.open_decisions,
                "current_strategy": "rolling_story_arc_planning",
            }
        ),
    }


def _verification_payload(review: BookDirectionReview) -> dict[str, Any]:
    reasons = [issue.message for issue in review.issues if issue.severity == "blocking"]
    signals = [_verification_signal_payload(signal) for signal in review.signals]
    return {
        "schema_version": 1,
        "commit_allowed": review.commit_allowed,
        "routing_decision": (
            "await_user_approval" if review.commit_allowed else "continue_book_discussion"
        ),
        "reasons": reasons,
        "signals": signals,
        "summary": review.summary,
        "issues": [issue.model_dump(mode="json") for issue in review.issues],
    }


def _verification_signal_payload(signal: str) -> dict[str, Any]:
    name, separator, value = signal.partition(":")
    status = "observed"
    if separator and value in {"passed", "failed", "warning"}:
        status = value
    elif separator and "/" in value:
        numerator, _, denominator = value.partition("/")
        if numerator.isdigit() and denominator.isdigit():
            status = "passed" if numerator == denominator else "failed"
    return {"name": name, "status": status, "evidence": [signal]}


def _candidate_missing_confirmed_decisions(
    state: SetupStateDocument,
    candidate: BookDirectionCandidate,
) -> list[str]:
    return missing_confirmed_decisions(
        state.confirmed_decisions,
        constraints=candidate.constraints,
        coverage=candidate.confirmed_decision_coverage,
    )


def _migrate_legacy_setup(
    project_path: Path,
    legacy: dict[str, Any],
) -> SetupStateDocument:
    backup_path = project_path / "book" / "legacy" / "setup-v1.json"
    if not backup_path.exists():
        write_json(backup_path, legacy)

    questions = {
        str(item.get("id")): item
        for item in legacy.get("questions", [])
        if isinstance(item, dict) and item.get("id")
    }
    answers = [item for item in legacy.get("answers", []) if isinstance(item, dict)]
    messages: list[SetupMessage] = []
    confirmed: list[str] = []
    for answer in answers:
        question_id = str(answer.get("question_id", "legacy_question"))
        question = questions.get(question_id, {})
        prompt = str(question.get("prompt") or question.get("title") or question_id).strip()
        answer_text = str(answer.get("answer", "")).strip()
        if not answer_text:
            continue
        turn = len(messages) // 2 + 1
        messages.append(
            SetupMessage(
                turn=turn,
                role="assistant",
                content=prompt,
                profile_id=_optional_string(question.get("profile_id")),
                model_snapshot=_optional_string(question.get("model_snapshot")),
                migrated=True,
            )
        )
        messages.append(
            SetupMessage(
                turn=turn,
                role="user",
                content=answer_text,
                migrated=True,
            )
        )
        title = str(question.get("title") or question_id).strip()
        confirmed.append(f"{title}: {answer_text}")

    approved = bool(legacy.get("approved"))
    direction = _legacy_direction(project_path, questions, answers)
    state = SetupStateDocument(
        revision=1,
        phase="approved" if approved else "discussing",
        approved=approved,
        approved_at=legacy.get("approved_at"),
        migrated_from_schema_version=1,
        turn_count=len(messages) // 2,
        messages=messages,
        direction_draft=direction,
        discussion_summary=(
            "已从旧版固定问答导入。后续讨论应以这些历史决定为起点，继续开放澄清。"
        ),
        confirmed_decisions=confirmed,
        unresolved_questions=(
            [] if approved else ["旧版问答已导入，请确认是否还需要补充或修正全书方向。"]
        ),
        readiness=SetupReadinessSignal(
            status="ready" if approved else "continue",
            reason=(
                "旧版全书设定已经批准。"
                if approved
                else "旧版问答不再作为完成门槛，可以继续开放讨论。"
            ),
        ),
        direction_draft_version_path=(
            "book/legacy/open-discussion-migration/direction_draft.md"
        ),
        discussion_state_version_path=(
            "book/legacy/open-discussion-migration/state.json"
        ),
        discussion_transcript_version_path=(
            "book/legacy/open-discussion-migration/transcript.jsonl"
        ),
    )
    assert state.direction_draft_version_path is not None
    assert state.discussion_state_version_path is not None
    assert state.discussion_transcript_version_path is not None
    files: dict[str, str | bytes] = _legacy_artifact_backups(project_path)
    if approved:
        files.update(_legacy_approved_files(project_path, state, confirmed))
    files.update(
        {
            state.direction_draft_version_path: state.direction_draft.rstrip() + "\n",
            state.discussion_state_version_path: _json_document(
                state.model_dump(mode="json")
            ),
            state.discussion_transcript_version_path: _transcript_content(state),
            "book/direction_draft.md": state.direction_draft.rstrip() + "\n",
            "book/discussion/transcript.jsonl": _transcript_content(state),
            "book/setup.json": _json_document(state.model_dump(mode="json")),
        }
    )
    commit_file_transaction(
        project_path,
        kind="legacy-book-setup-migration",
        files=files,
    )
    return state


def _legacy_direction(
    project_path: Path,
    questions: dict[str, dict[str, Any]],
    answers: list[dict[str, Any]],
) -> str:
    settings_path = project_path / "book" / "settings.md"
    if settings_path.exists():
        settings = read_text_file(settings_path).strip()
        if settings and settings not in {"# Book Settings", "# Book Outline"}:
            return settings

    lines = ["# 迁移的全书方向", "", "> 以下内容来自旧版固定问答，尚未经过新版开放讨论综合。", ""]
    for answer in answers:
        question_id = str(answer.get("question_id", "legacy_question"))
        question = questions.get(question_id, {})
        answer_text = str(answer.get("answer", "")).strip()
        if not answer_text:
            continue
        title = str(question.get("title") or question_id).strip()
        lines.extend([f"## {title}", "", answer_text, ""])
    return "\n".join(lines).rstrip() + "\n"


def _legacy_artifact_backups(project_path: Path) -> dict[str, str | bytes]:
    backups: dict[str, str | bytes] = {}
    for relative_path in [
        "book/direction.md",
        "book/constraints.json",
        "book/settings.md",
        "book/outline.md",
        "book/state.json",
    ]:
        source = project_path / relative_path
        backup_relative = "book/legacy/pre-open-discussion-migration/" + Path(
            relative_path
        ).name
        if source.exists() and not (project_path / backup_relative).exists():
            backups[backup_relative] = source.read_bytes()
    return backups


def _legacy_approved_files(
    project_path: Path,
    state: SetupStateDocument,
    confirmed: list[str],
) -> dict[str, str | bytes]:
    direction = state.direction_draft.rstrip() + "\n"
    rolling_contract = (
        "# 滚动故事弧规划契约\n\n"
        "该项目从旧版全书设定迁移而来。后续只根据已批准方向与已提交正史规划当前故事弧。\n"
    )
    previous_state = read_json(project_path / "book" / "state.json", default={}) or {}
    return {
        "book/direction.md": direction,
        "book/settings.md": direction,
        "book/outline.md": rolling_contract,
        "book/constraints.json": _json_document(
            {
                "schema_version": 1,
                "candidate": False,
                "migration_source": "legacy_fixed_question_setup",
                **BookDirectionConstraints(confirmed=confirmed).model_dump(mode="json"),
            }
        ),
        "book/state.json": _json_document(
            {
                **previous_state,
                "schema_version": max(int(previous_state.get("schema_version", 1)), 2),
                "version": int(previous_state.get("version", 1)) + 1,
                "setup_approved": True,
                "approved_at": state.approved_at.isoformat() if state.approved_at else None,
                "approved_direction_path": "book/direction.md",
                "approved_constraints_path": "book/constraints.json",
                "rolling_plan_path": "book/outline.md",
                "current_strategy": previous_state.get(
                    "current_strategy", "rolling_story_arc_planning"
                ),
                "migration_source": "legacy_fixed_question_setup",
            }
        ),
    }


def _optional_string(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None
