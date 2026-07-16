from pathlib import Path

import pytest

from app.harness.agents.models import EvaluationRecord, EvaluationResult
from app.harness.loops.book import BookDirectionSynthesis
from app.harness.orchestrator import HarnessOrchestrator, HarnessRunContext
from app.schemas.book_revisions import BookRevisionApprovalRequest
from app.schemas.projects import ProjectMetadata
from app.schemas.setup import (
    BookDirectionConstraints,
    BookDirectionReview,
    BookTitleSuggestion,
    ConfirmedDecisionCoverage,
    SetupStateDocument,
)
from app.storage.book_revisions import (
    BookRevisionConflict,
    approve_book_revision,
    read_pending_book_revision,
    save_book_revision_candidate,
)
from app.storage.json_files import read_json, write_json
from app.storage.events import read_events
from app.storage.readiness import build_project_readiness


def test_full_auto_book_revision_stays_candidate_until_explicit_approval(
    tmp_path: Path,
) -> None:
    project_path = _approved_project(tmp_path, operation_mode="full_auto")
    original_direction = (project_path / "book" / "direction.md").read_text(
        encoding="utf-8"
    )

    pending = save_book_revision_candidate(
        project_path,
        route_id="route-1234567890",
        base_book_version=4,
        source_loop="chapter",
        source_artifact="chapters/chapter-003/agent-candidate.json",
        source_candidate_run_id="chapter-run-1",
        summary="The future reveal cannot satisfy the current Book contract.",
        contract_field="ending.reveal",
        committed_evidence_locator="book/direction.md",
        impossibility_reason="The approved reveal contradicts committed chapter evidence.",
        synthesis=_synthesis(),
        evaluation=_passing_evaluation(),
        review=_passing_review(),
        profile_id="profile-1",
    )

    assert pending.status == "awaiting_approval"
    assert read_pending_book_revision(project_path) == pending
    assert (project_path / "book" / "direction.md").read_text(
        encoding="utf-8"
    ) == original_direction
    assert read_json(project_path / "book" / "state.json")["version"] == 4
    readiness = build_project_readiness(project_path)
    assert readiness.can_start_run is False
    assert readiness.next_action.id == "approve_book_revision"
    assert readiness.next_action.requires_user is True
    HarnessOrchestrator(
        HarnessRunContext(project_path=project_path, run_id="run-approval-guard")
    ).advance_to_next_checkpoint()
    assert (project_path / "book" / "direction.md").read_text(
        encoding="utf-8"
    ) == original_direction

    approved = approve_book_revision(
        project_path,
        BookRevisionApprovalRequest(
            revision_id=pending.revision_id,
            expected_base_book_version=4,
        ),
    )

    assert approved.status == "approved"
    assert approved.downstream_status == "pending"
    assert read_pending_book_revision(project_path) is None
    assert (project_path / "book" / "direction.md").read_text(
        encoding="utf-8"
    ) == "# Revised future direction\n\nKeep committed history unchanged.\n"
    book_state = read_json(project_path / "book" / "state.json")
    assert book_state["version"] == 5
    assert book_state["source_book_revision_id"] == pending.revision_id
    setup_state = SetupStateDocument.model_validate(
        read_json(project_path / "book" / "setup.json")
    )
    assert setup_state.approved is True
    assert setup_state.approved_title == "Original title"
    after_approval = build_project_readiness(project_path)
    assert after_approval.next_action.id == "resume_run"
    assert pending.revision_id in after_approval.next_action.evidence


def test_book_revision_approval_rejects_stale_book_contract_without_overwrite(
    tmp_path: Path,
) -> None:
    project_path = _approved_project(tmp_path, operation_mode="participatory")
    pending = save_book_revision_candidate(
        project_path,
        route_id="route-stale123",
        base_book_version=4,
        source_loop="story_arc",
        source_artifact="arcs/arc-002/agent-candidate.json",
        source_candidate_run_id="arc-run-2",
        summary="Upper contract is stale.",
        contract_field="arc_constraints.deadline",
        committed_evidence_locator="book/outline.md",
        impossibility_reason="The deadline cannot coexist with committed chronology.",
        synthesis=_synthesis(),
        evaluation=_passing_evaluation(),
        review=_passing_review(),
        profile_id="profile-1",
    )
    metadata = ProjectMetadata.model_validate(read_json(project_path / "project.json"))
    metadata.run_status = "running"
    write_json(project_path / "project.json", metadata.model_dump(mode="json"))
    with pytest.raises(BookRevisionConflict, match="stopped Harness checkpoint"):
        approve_book_revision(
            project_path,
            BookRevisionApprovalRequest(
                revision_id=pending.revision_id,
                expected_base_book_version=4,
            ),
        )
    metadata.run_status = "waiting_for_user"
    write_json(project_path / "project.json", metadata.model_dump(mode="json"))
    state = read_json(project_path / "book" / "state.json")
    state["version"] = 5
    write_json(project_path / "book" / "state.json", state)

    with pytest.raises(BookRevisionConflict, match="changed after"):
        approve_book_revision(
            project_path,
            BookRevisionApprovalRequest(
                revision_id=pending.revision_id,
                expected_base_book_version=4,
            ),
        )

    assert (project_path / "book" / "direction.md").read_text(
        encoding="utf-8"
    ) == "# Original direction\n"
    assert read_pending_book_revision(project_path) is not None


def test_pending_route_reuses_already_saved_book_revision_after_restart(
    tmp_path: Path,
) -> None:
    project_path = _approved_project(tmp_path, operation_mode="full_auto")
    pending = save_book_revision_candidate(
        project_path,
        route_id="route-restart1",
        base_book_version=4,
        source_loop="chapter",
        source_artifact="chapters/chapter-003/agent-candidate.json",
        source_candidate_run_id="chapter-run-1",
        summary="A future Book revision is required.",
        contract_field="ending.reveal",
        committed_evidence_locator="book/direction.md",
        impossibility_reason="The approved reveal conflicts with committed evidence.",
        synthesis=_synthesis(),
        evaluation=_passing_evaluation(),
        review=_passing_review(),
        profile_id="profile-1",
    )
    pending_route_path = (
        project_path / "book" / "harness" / "pending-cross-loop-route.json"
    )
    write_json(
        pending_route_path,
        {
            "schema_version": 1,
            "route_id": pending.route_id,
            "loop_layer": "chapter",
            "action": "run_chapter_agent",
            "proposal": {"target_owner": "book"},
            "source_artifact": pending.source_artifact,
        },
    )
    metadata = ProjectMetadata.model_validate(read_json(project_path / "project.json"))

    handled = HarnessOrchestrator(
        HarnessRunContext(project_path=project_path, run_id="run-restart")
    )._process_pending_cross_loop_route(metadata)

    assert handled is True
    assert not pending_route_path.exists()
    assert read_pending_book_revision(project_path) == pending
    assert read_json(project_path / "project.json")["run_status"] == "waiting_for_user"
    assert read_events(project_path)[-1].kind == "cross_loop_route_recovered"


def _approved_project(tmp_path: Path, *, operation_mode: str) -> Path:
    project_path = tmp_path / "project"
    (project_path / "book").mkdir(parents=True)
    (project_path / "arcs" / "arc-001").mkdir(parents=True)
    metadata = ProjectMetadata(
        project_id="project-1",
        title="Original title",
        operation_mode=operation_mode,
        active_profile_id="profile-1",
        active_arc_id="arc-001",
        run_status="waiting_for_user",
    )
    write_json(project_path / "project.json", metadata.model_dump(mode="json"))
    write_json(
        project_path / "book" / "setup.json",
        SetupStateDocument(
            phase="approved",
            approved=True,
            approved_title="Original title",
            direction_draft="# Original direction",
            confirmed_decisions=["Committed history is immutable."],
        ).model_dump(mode="json"),
    )
    (project_path / "book" / "direction.md").write_text(
        "# Original direction\n", encoding="utf-8"
    )
    (project_path / "book" / "settings.md").write_text(
        "# Original direction\n", encoding="utf-8"
    )
    (project_path / "book" / "outline.md").write_text(
        "# Original rolling plan\n", encoding="utf-8"
    )
    write_json(
        project_path / "book" / "constraints.json",
        {"schema_version": 1, "candidate": False},
    )
    write_json(
        project_path / "book" / "state.json",
        {
            "schema_version": 2,
            "version": 4,
            "book_direction_version": 2,
            "setup_approved": True,
            "title": "Original title",
            "confirmed_decisions": ["Committed history is immutable."],
        },
    )
    return project_path


def _synthesis() -> BookDirectionSynthesis:
    return BookDirectionSynthesis(
        direction_markdown=(
            "# Revised future direction\n\nKeep committed history unchanged."
        ),
        constraints=BookDirectionConstraints(
            confirmed=["Committed history is immutable."],
            must_preserve=["Every completed chapter and canon fact."],
            must_avoid=["Retconning committed evidence."],
            creative_freedoms=["Revise future reveal mechanics."],
            open_decisions=[],
        ),
        confirmed_decision_coverage=[
            ConfirmedDecisionCoverage(
                decision="Committed history is immutable.",
                candidate_evidence="The revision explicitly preserves committed history.",
            )
        ],
        recommended_titles=[
            BookTitleSuggestion(title=f"Title {index}", rationale="Schema-compatible title.")
            for index in range(1, 4)
        ],
        rolling_plan_markdown="# Revised rolling plan\n\nOnly future arcs change.",
        model_snapshot="model-1",
        provider_snapshot="openai-compatible",
        usage={},
    )


def _passing_review() -> BookDirectionReview:
    return BookDirectionReview(
        status="passed",
        summary="The future-only revision is internally consistent.",
        issues=[],
        signals=["contract_preservation=passed"],
    )


def _passing_evaluation() -> EvaluationRecord:
    return EvaluationRecord(
        candidate_artifact_id=(
            "book/agent/a/activation-1/candidates/book-direction.json"
        ),
        candidate_revision=3,
        evaluator_profile_id="profile-1",
        evaluator_model_snapshot="model-1",
        evaluator_provider_snapshot="openai-compatible",
        rubric_version="book-direction-v1",
        result=EvaluationResult(
            schema_version=1,
            outcome="pass",
            contract_satisfied=True,
            summary="The candidate is safe to present for approval.",
            issues=[],
            signals=[],
            repair_brief=None,
            upstream_blocker=None,
        ),
    )
