import json
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi import HTTPException
from pydantic import SecretStr, ValidationError

from app.api import setup as setup_api
from app.harness.agents.domain_tools import BookDiscussionUpdateInput
from app.harness.agents.loop_runners import apply_book_direction_prechecks
from app.harness.agents.models import EvaluationRecord, EvaluationResult
from app.harness.loops import book as book_loop
from app.harness.loops.book import BookDirectionSynthesis, BookDiscussionTurnResult
from app.harness.run_control import begin_active_runner, end_active_runner
from app.llm.gateway import ChatChunk
from app.schemas.profiles import LlmProfile, LlmProfilesDocument
from app.schemas.projects import ProjectMetadata
from app.schemas.setup import (
    BookDirectionConstraints,
    BookDirectionReview,
    BookDirectionReviewIssue,
    BookTitleSuggestion,
    ConfirmedDecisionCoverage,
    SetupApprovalRequest,
    SetupMessage,
    SetupReadinessSignal,
    SetupStateDocument,
    SetupSuggestion,
    SetupTurnRequest,
    SupersededDecision,
)
from app.storage import transactions as file_transactions
from app.storage.events import read_events
from app.storage.json_files import read_json, write_json
from app.storage.projects import read_project_metadata
from app.storage.setup import (
    SetupRevisionConflict,
    approve_setup,
    initialize_setup_state,
    read_setup_state,
    record_discussion_turn,
    save_book_direction_candidate,
    write_discussion_context_snapshot,
    write_review_context_snapshot,
)


def test_setup_state_initializes_as_open_unapproved_discussion(tmp_path: Path) -> None:
    project_path = _make_project(tmp_path)

    state = initialize_setup_state(project_path)

    assert state.schema_version == 2
    assert state.phase == "discussing"
    assert state.approved is False
    assert state.turn_count == 0
    assert state.messages == []
    assert state.direction_draft == ""
    assert state.candidate is None
    assert (project_path / "book" / "discussion" / "transcript.jsonl").read_text(
        encoding="utf-8"
    ) == ""
    assert state.direction_draft_version_path is not None
    assert state.discussion_state_version_path is not None
    assert state.discussion_transcript_version_path is not None
    assert (project_path / state.direction_draft_version_path).exists()
    assert (project_path / state.discussion_state_version_path).exists()
    assert (project_path / state.discussion_transcript_version_path).exists()
    assert not (project_path / "book" / "direction.md").exists()


def test_setup_turn_request_rejects_blank_message() -> None:
    with pytest.raises(ValidationError, match="must not be blank"):
        SetupTurnRequest(message="   ")


def test_setup_approval_request_rejects_blank_title() -> None:
    with pytest.raises(ValidationError):
        SetupApprovalRequest(candidate_revision=1, title="   ")

    assert len(SetupTurnRequest(message="x" * 32_001).message) == 32_001


def test_discussion_context_uses_summary_and_recent_raw_messages_only() -> None:
    messages = [
        SetupMessage(
            id=f"message-{index:02d}",
            turn=(index // 2) + 1,
            role="user" if index % 2 == 0 else "assistant",
            content=f"raw-message-{index:02d}",
        )
        for index in range(14)
    ]
    state = SetupStateDocument(
        turn_count=7,
        messages=messages,
        direction_draft="# Direction\n\nCurrent complete candidate.",
        discussion_summary="The older discussion is represented here.",
        confirmed_decisions=["Keep the mystery fair."],
        unresolved_questions=["How costly is the ending?"],
    )

    assembly = book_loop.assemble_discussion_context(state, "Make the ending bittersweet.")
    recent_source = next(
        source
        for source in assembly.snapshot["sources"]
        if source["id"] == "recent-book-discussion"
    )

    assert recent_source["included_message_ids"] == [
        f"message-{index:02d}" for index in range(4, 14)
    ]
    assert assembly.snapshot["summarized"] == [
        f"message-{index:02d}" for index in range(4)
    ]
    assert "raw-message-00" not in assembly.prompt
    assert "raw-message-13" in assembly.prompt
    assert "The older discussion is represented here." in assembly.prompt
    assert "Make the ending bittersweet." in assembly.prompt
    assert assembly.snapshot["budget"]["total_character_budget"] is None
    assert assembly.snapshot["budget"]["total_character_count"] == len(assembly.prompt)
    injected = {item["id"]: item for item in assembly.snapshot["injected"]}
    assert injected["current_direction_draft"]["source_path"] == (
        "book/direction_draft.md"
    )
    assert len(injected["current_direction_draft"]["sha256"]) == 64
    assert "content" not in injected["current_direction_draft"]


def test_discussion_context_excludes_raw_message_that_exceeds_recent_budget() -> None:
    state = SetupStateDocument(
        turn_count=1,
        messages=[
            SetupMessage(
                id="oversized-message",
                turn=1,
                role="user",
                content="x" * (book_loop.RECENT_MESSAGE_CHARACTER_BUDGET + 1),
            )
        ],
        direction_draft="# Direction\n\nThe intent has already been integrated.",
        discussion_summary="The oversized input is represented by this compact summary.",
    )

    assembly = book_loop.assemble_discussion_context(state, "Continue from the summary.")

    recent_source = next(
        source
        for source in assembly.snapshot["sources"]
        if source["id"] == "recent-book-discussion"
    )
    assert recent_source["included_message_ids"] == []
    assert assembly.snapshot["summarized"] == ["oversized-message"]
    assert "x" * 100 not in assembly.prompt
    assert "The oversized input is represented" in assembly.prompt


def test_review_context_records_versioned_sources_hashes_and_total_budget() -> None:
    state = SetupStateDocument(
        revision=4,
        turn_count=3,
        direction_draft=_long_direction(),
        discussion_summary="A compact discussion summary.",
        confirmed_decisions=["Clues must remain fair"],
        direction_draft_version_path="book/discussion/turn-0003/attempt-001/direction_draft.md",
        discussion_state_version_path="book/discussion/turn-0003/attempt-001/state.json",
        discussion_transcript_version_path=(
            "book/discussion/turn-0003/attempt-001/transcript.jsonl"
        ),
    )

    snapshot = book_loop.build_review_context_snapshot(state)

    sources = {source["id"]: source for source in snapshot["sources"]}
    injected = {item["id"]: item for item in snapshot["injected"]}
    assert sources["book-direction-draft"]["resolved_version_path"] == (
        state.direction_draft_version_path
    )
    assert sources["book-discussion-state"]["resolved_version_path"] == (
        state.discussion_state_version_path
    )
    assert sources["book-discussion-transcript"]["resolved_version_path"] == (
        state.discussion_transcript_version_path
    )
    assert len(injected["complete_direction_draft"]["sha256"]) == 64
    assert "content" not in injected["complete_direction_draft"]
    assert snapshot["budget"]["total_character_budget"] is None
    assert snapshot["budget"]["total_character_count"] > len(state.direction_draft)


def test_book_discussion_tool_accepts_one_question_and_answer_options() -> None:
    result = BookDiscussionUpdateInput.model_validate(
        {
            "expected_revision": 0,
            "reply": "The emotional cost is now clear.",
            "direction_draft": _long_direction(),
            "discussion_summary": "A fair mystery with a costly hopeful ending.",
            "confirmed_decisions": ["Fair clues", "Costly hopeful ending"],
            "superseded_decisions": [],
            "unresolved_questions": ["The final relationship outcome"],
            "assumptions": ["The city remains politically stable"],
            "contradictions": [],
            "question": "Which relationship must carry the emotional cost?",
            "suggestions": [
                {
                    "id": "leave-open",
                    "label": "Leave it open",
                    "message": "Keep that relationship open.",
                },
                {
                    "id": "reconcile",
                    "label": "Reconcile",
                    "message": "Let them reconcile at a cost.",
                    "recommended": True,
                },
            ],
            "readiness": {"status": "continue", "reason": "One decision remains."},
        }
    )

    assert result.question == "Which relationship must carry the emotional cost?"
    assert len(result.suggestions) == 2
    assert result.suggestions[1].recommended is True


def test_setup_suggestion_keeps_legacy_payloads_readable() -> None:
    suggestion = SetupSuggestion.model_validate(
        {"id": "legacy", "label": "Legacy", "message": "Keep the old answer."}
    )

    assert suggestion.rationale == ""
    assert suggestion.recommended is False


def test_book_discussion_tool_rejects_model_attempt_to_approve() -> None:
    with pytest.raises(ValidationError):
        BookDiscussionUpdateInput.model_validate(
            {
                "expected_revision": 0,
                "reply": "Looks complete.",
                "direction_draft": _long_direction(),
                "discussion_summary": "Summary.",
                "confirmed_decisions": [],
                "superseded_decisions": [],
                "unresolved_questions": [],
                "assumptions": [],
                "contradictions": [],
                "question": None,
                "suggestions": [],
                "readiness": {"status": "approved", "reason": "Tried to approve."},
            }
        )


def test_review_blocks_candidate_without_confirmed_decision_coverage() -> None:
    state = SetupStateDocument(
        direction_draft=_long_direction(),
        confirmed_decisions=["Clues must remain fair"],
    )
    synthesis = replace(_synthesis(), confirmed_decision_coverage=[])
    evaluation = EvaluationRecord(
        candidate_artifact_id="book/candidate.json",
        candidate_revision=1,
        evaluator_profile_id="main",
        evaluator_model_snapshot="review-model",
        evaluator_provider_snapshot="openai-compatible",
        rubric_version="book-direction-v1",
        result=EvaluationResult(
            schema_version=1,
            outcome="pass",
            contract_satisfied=True,
            summary="The candidate is otherwise valid.",
            issues=[],
            signals=[],
            repair_brief=None,
            upstream_blocker=None,
        ),
    )

    reviewed = apply_book_direction_prechecks(state, synthesis, evaluation)

    assert reviewed.result.outcome == "local_repair"
    assert any(
        issue.category == "confirmed_decision_coverage"
        for issue in reviewed.result.issues
    )


def test_review_blocks_coverage_that_is_not_preserved_by_candidate() -> None:
    state = SetupStateDocument(
        direction_draft=_long_direction(),
        confirmed_decisions=["Clues must remain fair"],
    )
    synthesis = replace(
        _synthesis(),
        constraints=_synthesis().constraints.model_copy(update={"confirmed": []}),
        confirmed_decision_coverage=[
            ConfirmedDecisionCoverage(
                decision="Clues must remain fair",
                candidate_evidence="text that is absent from the candidate",
            )
        ],
    )
    evaluation = EvaluationRecord(
        candidate_artifact_id="book/candidate.json",
        candidate_revision=1,
        evaluator_profile_id="main",
        evaluator_model_snapshot="review-model",
        evaluator_provider_snapshot="openai-compatible",
        rubric_version="book-direction-v1",
        result=EvaluationResult(
            schema_version=1,
            outcome="pass",
            contract_satisfied=True,
            summary="Passed by semantic review.",
            issues=[],
            signals=[],
            repair_brief=None,
            upstream_blocker=None,
        ),
    )

    reviewed = apply_book_direction_prechecks(state, synthesis, evaluation)

    assert reviewed.result.outcome == "local_repair"
    assert reviewed.result.contract_satisfied is False


def test_review_accepts_non_verbatim_evidence_for_preserved_decision() -> None:
    state = SetupStateDocument(
        direction_draft=_long_direction(),
        confirmed_decisions=["Clues must remain fair"],
    )
    synthesis = replace(
        _synthesis(),
        confirmed_decision_coverage=[
            ConfirmedDecisionCoverage(
                decision="Clues must remain fair",
                candidate_evidence=(
                    "The fair-clue section explains the visible clue contract."
                ),
            )
        ],
    )
    evaluation = EvaluationRecord(
        candidate_artifact_id="book/candidate.json",
        candidate_revision=1,
        evaluator_profile_id="main",
        evaluator_model_snapshot="review-model",
        evaluator_provider_snapshot="openai-compatible",
        rubric_version="book-direction-v1",
        result=EvaluationResult(
            schema_version=1,
            outcome="pass",
            contract_satisfied=True,
            summary="Passed by semantic review.",
            issues=[],
            signals=[],
            repair_brief=None,
            upstream_blocker=None,
        ),
    )

    reviewed = apply_book_direction_prechecks(state, synthesis, evaluation)

    assert reviewed.result.outcome == "pass"
    assert reviewed.result.contract_satisfied is True


def test_record_discussion_turn_persists_trace_without_committing_book_direction(
    tmp_path: Path,
) -> None:
    project_path = _make_project(tmp_path)
    state = initialize_setup_state(project_path)
    context_path = write_discussion_context_snapshot(
        project_path,
        turn=1,
        snapshot={"sources": ["book/setup.json"]},
    )

    updated = record_discussion_turn(
        project_path,
        state,
        user_message="A fair mystery with personal consequences.",
        result=_turn_result(),
        context_snapshot_path=context_path,
        profile_id="main",
    )

    transcript = (project_path / "book" / "discussion" / "transcript.jsonl").read_text(
        encoding="utf-8"
    )
    attempt_dir = project_path / Path(context_path).parent
    assert updated.turn_count == 1
    assert updated.revision == 2
    assert updated.question == "Which relationship must carry the emotional cost?"
    assert [message.role for message in updated.messages] == ["user", "assistant"]
    assert "A fair mystery" in transcript
    assert (attempt_dir / "response.json").exists()
    assert (attempt_dir / "state.json").exists()
    assert (attempt_dir / "transcript.jsonl").exists()
    assert updated.discussion_state_version_path == (
        Path(context_path).parent / "state.json"
    ).as_posix()
    assert updated.discussion_transcript_version_path == (
        Path(context_path).parent / "transcript.jsonl"
    ).as_posix()
    assert (attempt_dir / "direction_draft.md").read_text(encoding="utf-8").rstrip() == (
        _long_direction()
    )
    assert not (project_path / "book" / "direction.md").exists()
    assert not (project_path / "book" / "constraints.json").exists()


def test_review_context_retries_preserve_each_attempt(tmp_path: Path) -> None:
    project_path = _make_project(tmp_path)

    first_path = write_review_context_snapshot(
        project_path,
        candidate_revision=1,
        snapshot={"attempt": 1},
    )
    second_path = write_review_context_snapshot(
        project_path,
        candidate_revision=1,
        snapshot={"attempt": 2},
    )

    assert first_path == "book/reviews/review-0001/attempt-001/context_snapshot.json"
    assert second_path == "book/reviews/review-0001/attempt-002/context_snapshot.json"
    assert read_json(project_path / first_path) == {"attempt": 1}
    assert read_json(project_path / second_path) == {"attempt": 2}


def test_blocking_review_persists_candidate_but_keeps_approval_locked(
    tmp_path: Path,
) -> None:
    project_path, state = _project_with_discussion(tmp_path)
    context_path = write_review_context_snapshot(
        project_path,
        candidate_revision=1,
        snapshot={"sources": ["book/direction_draft.md"]},
    )
    review = BookDirectionReview(
        status="blocked",
        summary="A confirmed decision was changed.",
        issues=[
            BookDirectionReviewIssue(
                severity="blocking",
                kind="contradiction",
                message="The candidate changes the confirmed ending.",
                evidence=["confirmed: hopeful", "candidate: tragic"],
                suggested_question="Which ending should be authoritative?",
            )
        ],
        signals=["confirmed_decision_coverage:1/2"],
    )

    updated = save_book_direction_candidate(
        project_path,
        state,
        synthesis=_synthesis(),
        review=review,
        profile_id="main",
        review_model_snapshot="review-model",
        context_snapshot_path=context_path,
    )

    assert updated.phase == "review_blocked"
    assert updated.candidate is not None
    assert updated.candidate.approval_allowed is False
    assert len(updated.candidate.recommended_titles) == 3
    assert (project_path / updated.candidate.title_suggestions_path).exists()
    assert (project_path / updated.candidate.verification_path).exists()
    verification = read_json(project_path / updated.candidate.verification_path)
    coverage_signal = next(
        signal
        for signal in verification["signals"]
        if signal["name"] == "confirmed_decision_coverage"
    )
    assert coverage_signal["status"] == "failed"
    with pytest.raises(ValueError, match="blocking issues"):
        approve_setup(
            project_path,
            SetupApprovalRequest(candidate_revision=1, title="Harbor of Trust"),
        )
    assert not (project_path / "book" / "direction.md").exists()


def test_explicit_approval_requires_latest_revision_and_commits_exact_candidate(
    tmp_path: Path,
) -> None:
    project_path, state = _project_with_discussion(tmp_path)
    context_path = write_review_context_snapshot(
        project_path,
        candidate_revision=1,
        snapshot={"sources": ["book/direction_draft.md"]},
    )
    synthesis = _synthesis()
    state = save_book_direction_candidate(
        project_path,
        state,
        synthesis=synthesis,
        review=_passing_review(),
        profile_id="main",
        review_model_snapshot="review-model",
        context_snapshot_path=context_path,
    )

    with pytest.raises(ValueError, match="stale"):
        approve_setup(
            project_path,
            SetupApprovalRequest(candidate_revision=99, title="Harbor of Trust"),
        )
    assert not (project_path / "book" / "direction.md").exists()

    approved = approve_setup(
        project_path,
        SetupApprovalRequest(candidate_revision=1, title="Harbor of Trust"),
    )
    constraints = read_json(project_path / "book" / "constraints.json")
    book_state = read_json(project_path / "book" / "state.json")
    metadata = read_json(project_path / "project.json")

    assert approved.approved is True
    assert approved.phase == "approved"
    assert approved.approved_title == "Harbor of Trust"
    assert approved.title_selection_source == "recommended"
    assert (project_path / "book" / "direction.md").read_text(encoding="utf-8") == (
        synthesis.direction_markdown.rstrip() + "\n"
    )
    assert (project_path / "book" / "settings.md").read_text(encoding="utf-8") == (
        synthesis.direction_markdown.rstrip() + "\n"
    )
    assert (project_path / "book" / "outline.md").read_text(encoding="utf-8") == (
        synthesis.rolling_plan_markdown.rstrip() + "\n"
    )
    assert constraints["candidate"] is False
    assert constraints["source_candidate_revision"] == 1
    assert constraints["must_preserve"] == synthesis.constraints.must_preserve
    assert book_state["book_direction_version"] == 1
    assert book_state["title"] == "Harbor of Trust"
    assert metadata["title"] == "Harbor of Trust"
    assert book_state["current_strategy"] == "rolling_story_arc_planning"


def test_explicit_approval_accepts_custom_title(tmp_path: Path) -> None:
    project_path, state = _project_with_discussion(tmp_path)
    context_path = write_review_context_snapshot(
        project_path,
        candidate_revision=1,
        snapshot={"sources": ["book/direction_draft.md"]},
    )
    state = save_book_direction_candidate(
        project_path,
        state,
        synthesis=_synthesis(),
        review=_passing_review(),
        profile_id="main",
        review_model_snapshot="review-model",
        context_snapshot_path=context_path,
    )

    approved = approve_setup(
        project_path,
        SetupApprovalRequest(candidate_revision=1, title="Saltwater Testimony"),
    )

    assert approved.approved_title == "Saltwater Testimony"
    assert approved.title_selection_source == "custom"
    assert read_json(project_path / "project.json")["title"] == "Saltwater Testimony"


def test_approval_rechecks_confirmed_decisions_even_if_review_claims_passed(
    tmp_path: Path,
) -> None:
    project_path, state = _project_with_discussion(tmp_path)
    context_path = write_review_context_snapshot(
        project_path,
        candidate_revision=1,
        snapshot={"sources": ["book/direction_draft.md"]},
    )
    synthesis = _synthesis()
    forged = replace(
        synthesis,
        constraints=synthesis.constraints.model_copy(update={"confirmed": []}),
    )
    reviewed = save_book_direction_candidate(
        project_path,
        state,
        synthesis=forged,
        review=_passing_review(),
        profile_id="main",
        review_model_snapshot="review-model",
        context_snapshot_path=context_path,
    )

    with pytest.raises(ValueError, match="does not preserve every confirmed decision"):
        approve_setup(
            project_path,
            SetupApprovalRequest(
                candidate_revision=reviewed.candidate_revision_counter,
                title="Harbor of Trust",
            ),
        )

    assert not (project_path / "book" / "direction.md").exists()


def test_new_discussion_turn_invalidates_candidate_without_deleting_review_bundle(
    tmp_path: Path,
) -> None:
    project_path, state = _project_with_discussion(tmp_path)
    context_path = write_review_context_snapshot(
        project_path,
        candidate_revision=1,
        snapshot={"sources": ["book/direction_draft.md"]},
    )
    state = save_book_direction_candidate(
        project_path,
        state,
        synthesis=_synthesis(),
        review=_passing_review(),
        profile_id="main",
        review_model_snapshot="review-model",
        context_snapshot_path=context_path,
    )
    assert state.candidate is not None
    archived_verification = project_path / state.candidate.verification_path
    next_context = write_discussion_context_snapshot(
        project_path,
        turn=state.turn_count + 1,
        snapshot={"sources": ["book/setup.json"]},
    )

    updated = record_discussion_turn(
        project_path,
        state,
        user_message="Change the ending cost.",
        result=_turn_result(reply="The ending cost is now unresolved."),
        context_snapshot_path=next_context,
        profile_id="main",
    )

    assert updated.phase == "discussing"
    assert updated.candidate is None
    assert updated.candidate_revision_counter == 1
    assert archived_verification.exists()
    with pytest.raises(ValueError, match="synthesized and reviewed"):
        approve_setup(
            project_path,
            SetupApprovalRequest(candidate_revision=1, title="Harbor of Trust"),
        )


def test_discussion_preserves_confirmed_decisions_until_user_supersedes_them(
    tmp_path: Path,
) -> None:
    project_path, state = _project_with_discussion(tmp_path)
    next_context = write_discussion_context_snapshot(
        project_path,
        turn=2,
        snapshot={"sources": ["book/setup.json"]},
    )
    additive_result = replace(
        _turn_result(),
        confirmed_decisions=["The ending remains hopeful"],
    )

    state = record_discussion_turn(
        project_path,
        state,
        user_message="The ending remains hopeful.",
        result=additive_result,
        context_snapshot_path=next_context,
        profile_id="main",
    )

    assert state.confirmed_decisions == [
        "Clues must remain fair",
        "Trust is the central change",
        "The ending remains hopeful",
    ]

    replacement_context = write_discussion_context_snapshot(
        project_path,
        turn=3,
        snapshot={"sources": ["book/setup.json"]},
    )
    replacement = "Clues may be deliberately misleading when character motive supports it"
    superseding_result = replace(
        _turn_result(),
        confirmed_decisions=[replacement],
        superseded_decisions=[
            SupersededDecision(
                turn=3,
                decision="Clues must remain fair",
                replacement=replacement,
                reason="The user explicitly changed the clue contract.",
                user_evidence="Change the clue rule",
            )
        ],
    )

    updated = record_discussion_turn(
        project_path,
        state,
        user_message="Change the clue rule; misleading clues are allowed when motives support it.",
        result=superseding_result,
        context_snapshot_path=replacement_context,
        profile_id="main",
    )

    assert "Clues must remain fair" not in updated.confirmed_decisions
    assert replacement in updated.confirmed_decisions
    assert updated.superseded_decisions[-1].decision == "Clues must remain fair"


def test_stale_discussion_result_cannot_overwrite_newer_revision(tmp_path: Path) -> None:
    project_path, stale_state = _project_with_discussion(tmp_path)
    current_payload = stale_state.model_dump(mode="json")
    current_payload["revision"] = stale_state.revision + 1
    write_json(project_path / "book" / "setup.json", current_payload)
    context_path = write_discussion_context_snapshot(
        project_path,
        turn=2,
        snapshot={"sources": ["book/setup.json"]},
    )

    with pytest.raises(SetupRevisionConflict, match="stale result"):
        record_discussion_turn(
            project_path,
            stale_state,
            user_message="This result was generated from stale state.",
            result=_turn_result(),
            context_snapshot_path=context_path,
            profile_id="main",
        )

    assert read_setup_state(project_path).revision == stale_state.revision + 1


def test_approval_transaction_rolls_back_partial_formal_artifacts(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_path, state = _project_with_discussion(tmp_path)
    context_path = write_review_context_snapshot(
        project_path,
        candidate_revision=1,
        snapshot={"sources": ["book/direction_draft.md"]},
    )
    state = save_book_direction_candidate(
        project_path,
        state,
        synthesis=_synthesis(),
        review=_passing_review(),
        profile_id="main",
        review_model_snapshot="review-model",
        context_snapshot_path=context_path,
    )
    original_promote = file_transactions._promote_staged_file
    promotion_count = 0

    def fail_third_promotion(staged: Path, target: Path, transaction_id: str) -> None:
        nonlocal promotion_count
        promotion_count += 1
        if promotion_count == 3:
            raise OSError("injected approval failure")
        original_promote(staged, target, transaction_id)

    monkeypatch.setattr(file_transactions, "_promote_staged_file", fail_third_promotion)

    with pytest.raises(OSError, match="injected approval failure"):
        approve_setup(
            project_path,
            SetupApprovalRequest(candidate_revision=1, title="Harbor of Trust"),
        )

    reloaded = read_setup_state(project_path)
    metadata = read_json(project_path / "project.json")
    assert reloaded.approved is False
    assert reloaded.candidate is not None
    assert not (project_path / "book" / "direction.md").exists()
    assert not (project_path / "book" / "constraints.json").exists()
    assert metadata["title"] == "Novel"


def test_setup_approval_rejects_active_runner_before_committing_title(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_path, state = _project_with_discussion(tmp_path)
    context_path = write_review_context_snapshot(
        project_path,
        candidate_revision=1,
        snapshot={"sources": ["book/direction_draft.md"]},
    )
    save_book_direction_candidate(
        project_path,
        state,
        synthesis=_synthesis(),
        review=_passing_review(),
        profile_id="main",
        review_model_snapshot="review-model",
        context_snapshot_path=context_path,
    )
    monkeypatch.setattr(setup_api, "get_active_project_path", lambda: project_path)

    assert begin_active_runner(project_path) is True
    try:
        with pytest.raises(HTTPException) as caught:
            setup_api.approve_setup(
                SetupApprovalRequest(candidate_revision=1, title="Harbor of Trust")
            )
    finally:
        end_active_runner(project_path)

    assert caught.value.status_code == 409
    assert read_project_metadata(project_path).title == "Novel"
    assert read_setup_state(project_path).approved is False
    assert not (project_path / "book" / "direction.md").exists()


def test_unapproved_legacy_fixed_questions_migrate_to_open_discussion(tmp_path: Path) -> None:
    project_path = _make_project(tmp_path)
    legacy = _legacy_setup(approved=False)
    write_json(project_path / "book" / "setup.json", legacy)

    state = read_setup_state(project_path)

    assert state.schema_version == 2
    assert state.migrated_from_schema_version == 1
    assert state.approved is False
    assert state.phase == "discussing"
    assert len(state.messages) == 4
    assert all(message.migrated for message in state.messages)
    assert "Tense mystery" in state.direction_draft
    assert state.unresolved_questions
    assert read_json(project_path / "book" / "legacy" / "setup-v1.json") == legacy


def test_legacy_migration_numbers_only_nonblank_answer_turns(tmp_path: Path) -> None:
    project_path = _make_project(tmp_path)
    legacy = _legacy_setup(approved=False)
    answers = legacy["answers"]
    assert isinstance(answers, list)
    first = answers[0]
    assert isinstance(first, dict)
    first["answer"] = ""
    write_json(project_path / "book" / "setup.json", legacy)

    state = read_setup_state(project_path)

    assert state.turn_count == 1
    assert [message.turn for message in state.messages] == [1, 1]


def test_approved_legacy_project_remains_runnable_after_migration(tmp_path: Path) -> None:
    project_path = _make_project(tmp_path)
    write_json(project_path / "book" / "setup.json", _legacy_setup(approved=True))
    write_json(project_path / "book" / "state.json", {"schema_version": 1, "version": 1})
    (project_path / "book" / "direction.md").write_text(
        "# Stale interrupted migration direction\n",
        encoding="utf-8",
    )
    write_json(
        project_path / "book" / "constraints.json",
        {"candidate": False, "confirmed": ["stale constraint"]},
    )
    (project_path / "book" / "outline.md").write_text(
        "# Stale full-book roadmap\n",
        encoding="utf-8",
    )

    state = read_setup_state(project_path)
    book_state = read_json(project_path / "book" / "state.json")

    assert state.approved is True
    assert state.phase == "approved"
    assert (project_path / "book" / "direction.md").exists()
    assert (project_path / "book" / "constraints.json").exists()
    assert (project_path / "book" / "outline.md").exists()
    assert book_state["setup_approved"] is True
    assert book_state["migration_source"] == "legacy_fixed_question_setup"
    assert "Stale interrupted" not in (project_path / "book" / "direction.md").read_text(
        encoding="utf-8"
    )
    assert "stale constraint" not in json.dumps(
        read_json(project_path / "book" / "constraints.json")
    )
    assert (project_path / "book/legacy/pre-open-discussion-migration/direction.md").exists()
    assert (project_path / "book/legacy/pre-open-discussion-migration/constraints.json").exists()
    assert (project_path / "book/legacy/pre-open-discussion-migration/outline.md").exists()


def test_setup_api_failure_is_fail_closed_and_redacts_provider_secrets(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_path = _make_project(tmp_path)
    initial = initialize_setup_state(project_path)
    profile = _profile(api_key="secret-key", base_url="https://api.example.com/v1")
    monkeypatch.setattr(setup_api, "get_active_project_path", lambda: project_path)
    monkeypatch.setattr(setup_api, "get_active_profile", lambda: profile)

    def fail_discussion(*_args, **_kwargs):
        raise RuntimeError("provider echoed secret-key at https://api.example.com/v1")

    monkeypatch.setattr(setup_api, "continue_book_discussion", fail_discussion)

    with pytest.raises(HTTPException) as exc:
        setup_api.continue_setup_discussion(SetupTurnRequest(message="Start the discussion."))

    reloaded = read_setup_state(project_path)
    events = read_events(project_path)
    rendered = json.dumps([event.model_dump(mode="json") for event in events], ensure_ascii=False)
    assert exc.value.status_code == 502
    assert reloaded.revision == initial.revision
    assert reloaded.messages == []
    assert events[-1].kind == "book_discussion_turn_failed"
    assert "secret-key" not in rendered
    assert "https://api.example.com/v1" not in rendered
    assert "[redacted]" in rendered


def test_setup_agent_event_callback_persists_safe_agent_evidence(
    tmp_path: Path,
) -> None:
    project_path = _make_project(tmp_path)
    metadata = ProjectMetadata.model_validate(read_json(project_path / "project.json"))
    callback = setup_api._setup_agent_event_callback(
        project_path,
        metadata,
        action="continue_book_discussion",
    )

    callback(
        {
            "kind": "agent_activation_completed",
            "activation_id": "activation-1",
            "candidate_run_id": "run-1",
            "outcome": "candidate",
            "evidence_paths": [
                "book/candidates/direction-1.json",
                "book/agent/a/activation-1/telemetry.json",
            ],
            "raw_arguments": {"api_key": "secret"},
        }
    )

    event = read_events(project_path)[-1]
    assert event.kind == "agent_activation_completed"
    assert event.artifact_path == "book/agent/a/activation-1/telemetry.json"
    assert event.payload["evidence_paths"] == [
        "book/candidates/direction-1.json",
        "book/agent/a/activation-1/telemetry.json",
    ]
    assert "raw_arguments" not in event.payload
    assert "secret" not in str(event.payload)


def test_setup_stream_progress_counts_tool_argument_deltas(tmp_path: Path) -> None:
    project_path = _make_project(tmp_path)
    metadata = ProjectMetadata.model_validate(read_json(project_path / "project.json"))
    callback = setup_api._setup_stream_callback(
        project_path,
        metadata,
        action="synthesize_book_direction",
    )

    callback(
        ChatChunk(
            event_type="tool_argument_delta",
            arguments_delta='{"direction_markdown":"开始',
            provider_snapshot="openai-compatible",
        )
    )

    event = read_events(project_path)[-1]
    assert event.kind == "llm_stream_progress"
    assert event.payload["received_characters"] == len(
        '{"direction_markdown":"开始'
    )
    assert "direction_markdown" not in str(event.payload)


def test_setup_stream_progress_coalesces_small_provider_fragments(tmp_path: Path) -> None:
    project_path = _make_project(tmp_path)
    metadata = ProjectMetadata.model_validate(read_json(project_path / "project.json"))
    callback = setup_api._setup_stream_callback(
        project_path,
        metadata,
        action="synthesize_book_direction",
    )

    for _ in range(100):
        callback(
            ChatChunk(
                event_type="tool_argument_delta",
                arguments_delta="x",
                provider_snapshot="openai-compatible",
            )
        )
    callback(
        ChatChunk(
            event_type="tool_call_stop",
            provider_snapshot="openai-compatible",
        )
    )

    progress_events = [
        event for event in read_events(project_path) if event.kind == "llm_stream_progress"
    ]
    assert [event.payload["received_characters"] for event in progress_events] == [1, 100]


def test_setup_api_review_failure_preserves_attempt_and_never_unlocks_approval(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_path, initial = _project_with_discussion(tmp_path)
    profile = _profile(api_key="secret-key", base_url="https://api.example.com/v1")
    monkeypatch.setattr(setup_api, "get_active_project_path", lambda: project_path)
    monkeypatch.setattr(setup_api, "get_active_profile", lambda: profile)

    def fail_synthesis(*_args, **_kwargs):
        raise RuntimeError("provider echoed secret-key at https://api.example.com/v1")

    monkeypatch.setattr(setup_api, "synthesize_book_direction", fail_synthesis)

    with pytest.raises(HTTPException) as exc:
        setup_api.prepare_setup_review()

    failed_state = read_setup_state(project_path)
    failed_events = read_events(project_path)
    failed_rendered = json.dumps(
        [event.model_dump(mode="json") for event in failed_events],
        ensure_ascii=False,
    )
    first_context = (
        project_path
        / "book/reviews/review-0001/attempt-001/context_snapshot.json"
    )
    assert exc.value.status_code == 502
    assert failed_state.revision == initial.revision
    assert failed_state.candidate is None
    assert failed_state.candidate_revision_counter == 0
    assert first_context.exists()
    assert failed_events[-1].kind == "book_direction_review_failed"
    assert "secret-key" not in failed_rendered
    assert "https://api.example.com/v1" not in failed_rendered

    monkeypatch.setattr(
        setup_api,
        "synthesize_book_direction",
        lambda *_args, **_kwargs: _synthesis(),
    )
    monkeypatch.setattr(
        setup_api,
        "review_book_direction",
        lambda *_args: (_passing_review(), "review-model", {}),
    )
    reviewed = setup_api.prepare_setup_review()

    assert reviewed.candidate is not None
    assert reviewed.candidate.revision == 1
    assert reviewed.last_context_snapshot_path == (
        "book/reviews/review-0001/attempt-002/context_snapshot.json"
    )
    assert first_context.exists()


def test_setup_api_runs_discussion_review_and_explicit_approval(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_path = _make_project(tmp_path)
    initialize_setup_state(project_path)
    profile = _profile()
    monkeypatch.setattr(setup_api, "get_active_project_path", lambda: project_path)
    monkeypatch.setattr(setup_api, "get_active_profile", lambda: profile)

    def fake_discussion(
        _profile,
        _state,
        _message,
        _assembly,
        on_text_delta,
        **_kwargs,
    ):
        on_text_delta(
            ChatChunk(
                text_delta='{"reply":',
                provider_snapshot="openai-compatible",
            )
        )
        on_text_delta(
            ChatChunk(
                text_delta="{",
                provider_snapshot="openai-compatible",
            )
        )
        return _turn_result()

    monkeypatch.setattr(
        setup_api,
        "continue_book_discussion",
        fake_discussion,
    )
    monkeypatch.setattr(
        setup_api,
        "synthesize_book_direction",
        lambda *_args, **_kwargs: _synthesis(),
    )
    monkeypatch.setattr(
        setup_api,
        "review_book_direction",
        lambda *_args: (_passing_review(), "review-model", {"total_tokens": 20}),
    )

    discussed = setup_api.continue_setup_discussion(
        SetupTurnRequest(message="A fair mystery with personal consequences.")
    )
    reviewed = setup_api.prepare_setup_review()
    assert discussed.approved is False
    assert reviewed.candidate is not None
    assert reviewed.candidate.approval_allowed is True
    assert not (project_path / "book" / "direction.md").exists()

    with pytest.raises(HTTPException) as duplicate_review:
        setup_api.prepare_setup_review()
    assert duplicate_review.value.status_code == 409

    approved = setup_api.approve_setup(
        SetupApprovalRequest(
            candidate_revision=reviewed.candidate.revision,
            title="Harbor of Trust",
        )
    )
    event_kinds = [event.kind for event in read_events(project_path)]
    assert approved.approved is True
    assert "book_discussion_context_assembled" in event_kinds
    assert "llm_stream_progress" in event_kinds
    assert "book_direction_candidate_reviewed" in event_kinds
    assert "book_loop_approved" in event_kinds
    assert event_kinds.count("approved_book_artifact_written") == 4
    progress_events = [
        event for event in read_events(project_path) if event.kind == "llm_stream_progress"
    ]
    assert [event.payload["received_characters"] for event in progress_events] == [9]
    assert all("text_delta" not in event.payload for event in progress_events)

    with pytest.raises(HTTPException) as duplicate_approval:
        setup_api.approve_setup(
            SetupApprovalRequest(candidate_revision=1, title="Harbor of Trust")
        )
    assert duplicate_approval.value.status_code == 409
    assert len(read_events(project_path)) == len(event_kinds)


def test_setup_api_rejects_title_containing_configured_profile_secret(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_path, state = _project_with_discussion(tmp_path)
    reviewed = save_book_direction_candidate(
        project_path,
        state,
        synthesis=_synthesis(),
        review=_passing_review(),
        profile_id="main",
        review_model_snapshot="review-model",
        context_snapshot_path="book/reviews/review-0001/attempt-001/context_snapshot.json",
    )
    profile = _profile(api_key="secret-key", base_url="https://api.example.com/v1")
    monkeypatch.setattr(setup_api, "get_active_project_path", lambda: project_path)
    monkeypatch.setattr(
        setup_api,
        "load_profiles",
        lambda: LlmProfilesDocument(active_profile_id=profile.id, profiles=[profile]),
    )

    with pytest.raises(HTTPException) as exc:
        setup_api.approve_setup(
            SetupApprovalRequest(
                candidate_revision=reviewed.candidate_revision_counter,
                title="secret-key",
            )
        )

    assert exc.value.status_code == 409
    assert read_setup_state(project_path).approved is False
    assert ProjectMetadata.model_validate(read_json(project_path / "project.json")).title == "Novel"
    rendered = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in project_path.rglob("*")
        if path.is_file()
    )
    assert "secret-key" not in rendered


def test_setup_api_queues_events_when_durable_append_temporarily_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_path = _make_project(tmp_path)
    initialize_setup_state(project_path)
    profile = _profile()
    real_append_event = setup_api.append_event
    monkeypatch.setattr(setup_api, "get_active_project_path", lambda: project_path)
    monkeypatch.setattr(setup_api, "get_active_profile", lambda: profile)
    monkeypatch.setattr(
        setup_api,
        "continue_book_discussion",
        lambda *_args, **_kwargs: _turn_result(),
    )
    monkeypatch.setattr(
        setup_api,
        "append_event",
        lambda *_args: (_ for _ in ()).throw(OSError("temporary event failure")),
    )

    state = setup_api.continue_setup_discussion(
        SetupTurnRequest(message="A fair mystery with personal consequences.")
    )
    outbox = project_path / "book" / ".event-outbox"

    assert state.turn_count == 1
    assert list(outbox.glob("*.json"))

    monkeypatch.setattr(setup_api, "append_event", real_append_event)
    setup_api.get_setup_state()

    assert not outbox.exists()
    assert any(event.kind == "book_discussion_turn_completed" for event in read_events(project_path))


def test_setup_api_redacts_secrets_from_user_discussion_before_storage(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_path = _make_project(tmp_path)
    initialize_setup_state(project_path)
    profile = _profile(api_key="secret-key", base_url="https://api.example.com/v1")
    captured_messages: list[str] = []
    monkeypatch.setattr(setup_api, "get_active_project_path", lambda: project_path)
    monkeypatch.setattr(setup_api, "get_active_profile", lambda: profile)

    def fake_discussion(
        _profile,
        _state,
        message,
        _assembly,
        _on_text_delta,
        **_kwargs,
    ):
        captured_messages.append(message)
        return _turn_result()

    monkeypatch.setattr(setup_api, "continue_book_discussion", fake_discussion)

    state = setup_api.continue_setup_discussion(
        SetupTurnRequest(
            message="Do not store secret-key or https://api.example.com/v1 in this project."
        )
    )
    persisted = json.dumps(state.model_dump(mode="json"), ensure_ascii=False)
    transcript = (project_path / "book" / "discussion" / "transcript.jsonl").read_text(
        encoding="utf-8"
    )

    assert captured_messages == ["Do not store [redacted] or [redacted] in this project."]
    assert "secret-key" not in persisted + transcript
    assert "https://api.example.com/v1" not in persisted + transcript


def _make_project(tmp_path: Path) -> Path:
    project_path = tmp_path / "novel"
    (project_path / "book").mkdir(parents=True, exist_ok=True)
    write_json(
        project_path / "project.json",
        ProjectMetadata(title="Novel").model_dump(mode="json"),
    )
    (project_path / "events.jsonl").write_text("", encoding="utf-8")
    return project_path


def _project_with_discussion(tmp_path: Path) -> tuple[Path, SetupStateDocument]:
    project_path = _make_project(tmp_path)
    state = initialize_setup_state(project_path)
    context_path = write_discussion_context_snapshot(
        project_path,
        turn=1,
        snapshot={"sources": ["book/setup.json"]},
    )
    state = record_discussion_turn(
        project_path,
        state,
        user_message="A fair mystery with personal consequences.",
        result=_turn_result(),
        context_snapshot_path=context_path,
        profile_id="main",
    )
    return project_path, state


def _profile(
    *,
    api_key: str = "secret",
    base_url: str = "https://provider.invalid/v1",
) -> LlmProfile:
    return LlmProfile(
        id="main",
        name="Main",
        protocol="openai-compatible",
        base_url=base_url,
        api_key=SecretStr(api_key),
        model="story-model",
    )


def _turn_result(*, reply: str = "The next decision should identify the relationship cost.") -> BookDiscussionTurnResult:
    return BookDiscussionTurnResult(
        reply=reply,
        direction_draft=_long_direction(),
        discussion_summary="A fair mystery about trust in a grounded coastal city.",
        confirmed_decisions=["Clues must remain fair", "Trust is the central change"],
        superseded_decisions=[],
        unresolved_questions=["Which relationship bears the final cost?"],
        assumptions=["The final cost will be personal"],
        contradictions=[],
        question="Which relationship must carry the emotional cost?",
        suggestions=[
            SetupSuggestion(
                id="turn-0001-suggestion-1",
                label="Mentor",
                message="Let the mentor relationship carry the cost.",
            ),
            SetupSuggestion(
                id="turn-0001-suggestion-2",
                label="Sibling",
                message="Let the sibling relationship carry the cost.",
            ),
        ],
        readiness=SetupReadinessSignal(
            status="continue",
            reason="The final relationship cost still needs one decision.",
        ),
        model_snapshot="story-model",
        provider_snapshot="openai-compatible",
        usage={"total_tokens": 100},
    )


def _synthesis() -> BookDirectionSynthesis:
    return BookDirectionSynthesis(
        direction_markdown=_long_direction(),
        constraints=BookDirectionConstraints(
            confirmed=[
                "Clues must remain fair",
                "Trust is the central change",
                "The central mystery uses fair clues.",
            ],
            must_preserve=["Every major reveal changes a relationship."],
            must_avoid=["No arbitrary supernatural solution."],
            creative_freedoms=["The current arc may choose its own local antagonist."],
            open_decisions=["The exact final relationship cost remains open."],
        ),
        confirmed_decision_coverage=[
            ConfirmedDecisionCoverage(
                decision="Clues must remain fair",
                candidate_evidence="visible clues",
            ),
            ConfirmedDecisionCoverage(
                decision="Trust is the central change",
                candidate_evidence="earned trust",
            ),
        ],
        recommended_titles=[
            BookTitleSuggestion(
                title="Harbor of Trust",
                rationale="Connects the coastal setting to the protagonist's emotional arc.",
            ),
            BookTitleSuggestion(
                title="The Fair-Clue Harbor",
                rationale="Signals the mystery's promise of visible, earned clues.",
            ),
            BookTitleSuggestion(
                title="What the Tide Reveals",
                rationale="Links each revelation to the story's coastal atmosphere.",
            ),
        ],
        rolling_plan_markdown=_long_rolling_contract(),
        model_snapshot="story-model",
        provider_snapshot="openai-compatible",
        usage={"total_tokens": 200},
    )


def _passing_review() -> BookDirectionReview:
    return BookDirectionReview(
        status="passed",
        summary="The candidate preserves confirmed decisions and leaves open decisions explicit.",
        issues=[
            BookDirectionReviewIssue(
                severity="warning",
                kind="open_decision",
                message="The final relationship cost remains intentionally open.",
                evidence=["constraints.open_decisions"],
            )
        ],
        signals=["confirmed_decisions_preserved:passed", "rolling_scope:passed"],
    )


def _long_direction() -> str:
    return (
        "# Book Direction\n\n"
        "The novel is a grounded coastal-city mystery about earned trust. Every reveal must be "
        "supported by visible clues and must alter a meaningful relationship, so plot knowledge "
        "and emotional consequence advance together. The protagonist begins strategically capable "
        "but isolated, and gains agency by learning which alliances deserve trust. Victories should "
        "carry durable personal costs without making hope feel false. Speculative technology stays "
        "limited, socially consequential, and unable to erase prior choices. The exact final cost, "
        "local antagonists, and later arc routes remain open for rolling planning from committed canon."
    )


def _long_rolling_contract() -> str:
    return (
        "# Rolling Story Arc Contract\n\n"
        "Plan only the current story arc from the approved direction and committed canon. Give the "
        "arc one concrete mystery advance, one relationship change, and one test of earned trust. "
        "After its chapters commit, reconcile observations and state patches, then plan the next arc "
        "from that new canon. Return to the book loop only when a proposed route conflicts with an "
        "approved constraint or requires changing a highest-level user decision."
    )


def _legacy_setup(*, approved: bool) -> dict[str, object]:
    return {
        "schema_version": 1,
        "approved": approved,
        "approved_at": "2026-01-01T00:00:00Z" if approved else None,
        "questions": [
            {"id": "genre_promise", "title": "Genre", "prompt": "What is the promise?"},
            {
                "id": "protagonist_direction",
                "title": "Protagonist",
                "prompt": "How should the protagonist change?",
            },
        ],
        "answers": [
            {"question_id": "genre_promise", "answer": "Tense mystery"},
            {"question_id": "protagonist_direction", "answer": "Learn earned trust"},
        ],
    }
