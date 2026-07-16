from pathlib import Path

import pytest
from fastapi import HTTPException

from app.api import readiness as readiness_api
from app.core import config as core_config
from app.core import paths as core_paths
from app.harness.loops.book import BookDirectionSynthesis
from app.harness.run_control import begin_active_runner, end_active_runner
from app.schemas.events import HarnessEvent
from app.schemas.profiles import LlmProfileUpsert
from app.schemas.projects import CreateProjectRequest
from app.schemas.setup import (
    BookDirectionConstraints,
    BookDirectionReview,
    BookDirectionReviewIssue,
    BookTitleSuggestion,
    ConfirmedDecisionCoverage,
    SetupApprovalRequest,
    SetupReadinessSignal,
)
from app.storage import profiles as profile_storage
from app.storage import projects as project_storage
from app.storage import setup as setup_storage
from app.storage.events import append_event
from app.storage.json_files import write_json


def test_readiness_requires_active_project(tmp_path: Path, monkeypatch) -> None:
    _isolate_runtime_paths(tmp_path, monkeypatch)

    with pytest.raises(HTTPException) as exc:
        readiness_api.get_readiness()

    assert exc.value.status_code == 404
    assert exc.value.detail == "No active project."


def test_readiness_blocks_run_without_setup_and_profile(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _isolate_runtime_paths(tmp_path, monkeypatch)
    project_storage.create_project(CreateProjectRequest(operation_mode="full_auto"))

    readiness = readiness_api.get_readiness()
    by_id = {gate.id: gate for gate in readiness.gates}

    assert readiness.status == "pending"
    assert readiness.can_start_run is False
    assert by_id["book_setup"].status == "pending"
    assert by_id["active_llm_profile"].status == "pending"
    assert by_id["run_control"].status == "passed"
    assert readiness.next_action.id == "configure_llm_profile"
    assert readiness.next_action.command == "POST /api/profiles"
    assert readiness.next_action.requires_user is True


def test_readiness_allows_run_when_required_gates_pass(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _isolate_runtime_paths(tmp_path, monkeypatch)
    project = project_storage.create_project(
        CreateProjectRequest(operation_mode="full_auto")
    )
    project_path = Path(project.path)
    _approve_setup(project_path)
    profile_storage.upsert_profile(
        LlmProfileUpsert(
            id="main",
            name="Main Provider",
            protocol="openai-compatible",
            base_url="https://api.example.com/v1",
            api_key="secret-key",
            model="example-model",
        )
    )

    readiness = readiness_api.get_readiness()
    by_id = {gate.id: gate for gate in readiness.gates}

    assert readiness.status == "passed"
    assert readiness.can_start_run is True
    assert by_id["book_setup"].status == "passed"
    assert by_id["active_llm_profile"].status == "passed"
    assert by_id["completion_evidence"].required is False
    assert by_id["completion_evidence"].status == "pending"
    assert readiness.next_action.id == "start_run"
    assert readiness.next_action.command == "POST /api/runs/start"
    assert readiness.next_action.can_auto_continue is True


def test_readiness_fails_closed_when_approved_setup_has_no_title(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _isolate_runtime_paths(tmp_path, monkeypatch)
    project = project_storage.create_project(
        CreateProjectRequest(operation_mode="full_auto")
    )
    project_path = Path(project.path)
    _approve_setup(project_path)
    metadata = project_storage.read_project_metadata(project_path)
    metadata.title = None
    project_storage.write_project_metadata(project_path, metadata)

    readiness = readiness_api.get_readiness()
    book_setup = next(gate for gate in readiness.gates if gate.id == "book_setup")

    assert readiness.can_start_run is False
    assert book_setup.status == "failed"
    assert "project.json:title" in book_setup.evidence


def test_readiness_recommends_review_when_discussion_draft_is_ready(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _isolate_runtime_paths(tmp_path, monkeypatch)
    project = project_storage.create_project(
        CreateProjectRequest(operation_mode="full_auto")
    )
    project_path = Path(project.path)
    state = setup_storage.read_setup_state(project_path)
    state.direction_draft = _direction()
    state.selected_title = "Readiness Fixture"
    state.readiness = SetupReadinessSignal(status="ready", reason="Ready for review.")
    write_json(project_path / "book" / "setup.json", state.model_dump(mode="json"))
    _create_profile()

    readiness = readiness_api.get_readiness()

    assert readiness.next_action.id == "review_book_direction"
    assert readiness.next_action.command == "POST /api/setup/prepare-review"
    assert readiness.next_action.requires_user is True


def test_readiness_recommends_explicit_approval_for_reviewed_candidate(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _isolate_runtime_paths(tmp_path, monkeypatch)
    project = project_storage.create_project(
        CreateProjectRequest(operation_mode="full_auto")
    )
    project_path = Path(project.path)
    _prepare_candidate(project_path)

    readiness = readiness_api.get_readiness()

    assert readiness.next_action.id == "approve_book_direction"
    assert readiness.next_action.command == "POST /api/setup/approve"
    assert readiness.next_action.requires_user is True
    assert "candidate_revision:1" in readiness.next_action.evidence


def test_readiness_routes_blocked_candidate_back_to_discussion(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _isolate_runtime_paths(tmp_path, monkeypatch)
    project = project_storage.create_project(
        CreateProjectRequest(operation_mode="full_auto")
    )
    project_path = Path(project.path)
    _prepare_candidate(project_path, blocked=True)
    _create_profile()

    readiness = readiness_api.get_readiness()

    assert readiness.next_action.id == "review_book_direction"
    assert readiness.next_action.command == "POST /api/setup/prepare-review"
    assert "candidate_review:blocked" in readiness.next_action.evidence
    assert "The candidate contradicts a confirmed decision." in readiness.next_action.evidence


def test_readiness_recommends_arc_approval_in_participatory_mode(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _isolate_runtime_paths(tmp_path, monkeypatch)
    project = project_storage.create_project(
        CreateProjectRequest(operation_mode="participatory")
    )
    project_path = Path(project.path)
    _approve_setup(project_path)
    _create_profile()
    metadata = project_storage.read_project_metadata(project_path)
    metadata.operation_mode = "participatory"
    metadata.active_arc_id = "arc-001"
    metadata.run_status = "waiting_for_user"
    project_storage.write_project_metadata(project_path, metadata)
    arc_path = project_path / "arcs" / "arc-001"
    arc_path.mkdir(parents=True)
    write_json(
        arc_path / "state.json",
        {
            "arc_id": "arc-001",
            "status": "planned",
            "plan_path": "arcs/arc-001/plan.md",
            "human_review": "awaiting_review",
        },
    )

    readiness = readiness_api.get_readiness()

    assert readiness.status == "passed"
    assert readiness.can_start_run is True
    assert readiness.next_action.id == "approve_story_arc"
    assert readiness.next_action.command == "POST /api/arcs/current/approve"
    assert readiness.next_action.requires_user is True
    assert "arcs/arc-001/plan.md" in readiness.next_action.evidence

    metadata.operation_mode = "full_auto"
    project_storage.write_project_metadata(project_path, metadata)
    after_mode_change = readiness_api.get_readiness()

    assert after_mode_change.next_action.id == "approve_story_arc"
    assert after_mode_change.next_action.requires_user is True


def test_readiness_recommends_retry_for_rejected_state_patch(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _isolate_runtime_paths(tmp_path, monkeypatch)
    project = project_storage.create_project(
        CreateProjectRequest(operation_mode="full_auto")
    )
    project_path = Path(project.path)
    _approve_setup(project_path)
    _create_profile()
    metadata = project_storage.read_project_metadata(project_path)
    metadata.active_chapter_id = "chapter-001"
    metadata.run_status = "waiting_for_user"
    project_storage.write_project_metadata(project_path, metadata)
    chapter_path = project_path / "chapters" / "chapter-001"
    chapter_path.mkdir(parents=True)
    write_json(
        chapter_path / "state_patch_rejection.json",
        {
            "schema": "failed",
            "versions": "passed",
            "evidence": "passed",
            "conflicts": "passed",
            "reasons": ["Candidate patch conflicts with committed canon."],
        },
    )

    readiness = readiness_api.get_readiness()

    assert readiness.next_action.id == "retry_current_chapter"
    assert readiness.next_action.command == "POST /api/runs/retry-current-chapter"
    assert readiness.next_action.requires_user is True
    assert readiness.next_action.evidence[0] == "state_patch"


def test_readiness_recommends_explicit_retry_for_generic_failed_runs(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _isolate_runtime_paths(tmp_path, monkeypatch)
    project = project_storage.create_project(
        CreateProjectRequest(operation_mode="full_auto")
    )
    project_path = Path(project.path)
    _approve_setup(project_path)
    _create_profile()
    metadata = project_storage.read_project_metadata(project_path)
    metadata.run_status = "failed"
    project_storage.write_project_metadata(project_path, metadata)
    append_event(
        project_path,
        HarnessEvent(
            project_id=metadata.project_id,
            kind="run_failed",
            atomic_action="advance_to_next_checkpoint",
            status="failed",
            message="Harness run failed: provider timeout.",
        ),
    )

    readiness = readiness_api.get_readiness()

    assert readiness.next_action.id == "retry_failed_run"
    assert readiness.next_action.command == "POST /api/runs/resume"
    assert readiness.next_action.requires_user is True
    assert readiness.next_action.evidence == [
        "run_failed",
        "advance_to_next_checkpoint",
        "Harness run failed: provider timeout.",
    ]


@pytest.mark.parametrize(
    "failure_kind",
    ["agent_activation_failed", "agent_evaluation_failed"],
)
def test_readiness_recommends_reconnect_for_provider_failure(
    tmp_path: Path,
    monkeypatch,
    failure_kind: str,
) -> None:
    _isolate_runtime_paths(tmp_path, monkeypatch)
    project = project_storage.create_project(
        CreateProjectRequest(operation_mode="full_auto")
    )
    project_path = Path(project.path)
    _approve_setup(project_path)
    _create_profile()
    metadata = project_storage.read_project_metadata(project_path)
    metadata.run_status = "failed"
    project_storage.write_project_metadata(project_path, metadata)
    append_event(
        project_path,
        HarnessEvent(
            project_id=metadata.project_id,
            run_id="run-1",
            kind=failure_kind,
            loop_layer="chapter",
            atomic_action="run_chapter_agent",
            status="failed",
            artifact_path="chapters/chapter-001/agent/a/failed/failure.json",
            message="Bounded Loop Agent activation failed closed.",
            payload={
                "category": "transport_provider",
                "code": "provider_retry_exhausted",
            },
        ),
    )
    append_event(
        project_path,
        HarnessEvent(
            project_id=metadata.project_id,
            run_id="run-1",
            kind="run_failed",
            atomic_action="advance_to_next_checkpoint",
            status="failed",
            message="Harness run failed: provider unavailable.",
        ),
    )

    readiness = readiness_api.get_readiness()

    assert readiness.next_action.id == "retry_provider_connection"
    assert readiness.next_action.command == "POST /api/runs/resume"
    assert readiness.next_action.requires_user is True
    assert readiness.next_action.can_auto_continue is False
    assert "chapters/chapter-001/agent/a/failed/failure.json" in readiness.next_action.evidence


@pytest.mark.parametrize(
    ("category", "code"),
    [
        ("malformed_model_output", "tool_schema_repair_exhausted"),
        ("local_semantic", "semantic_revision_exhausted"),
    ],
)
def test_readiness_recommends_new_bounded_retry_for_retryable_agent_failure(
    tmp_path: Path,
    monkeypatch,
    category: str,
    code: str,
) -> None:
    _isolate_runtime_paths(tmp_path, monkeypatch)
    project = project_storage.create_project(
        CreateProjectRequest(operation_mode="full_auto")
    )
    project_path = Path(project.path)
    _approve_setup(project_path)
    _create_profile()
    metadata = project_storage.read_project_metadata(project_path)
    metadata.run_status = "failed"
    project_storage.write_project_metadata(project_path, metadata)
    append_event(
        project_path,
        HarnessEvent(
            project_id=metadata.project_id,
            run_id="run-retryable",
            kind="agent_activation_failed",
            loop_layer="chapter",
            atomic_action="run_chapter_agent",
            status="failed",
            message="The bounded automatic repair budget was exhausted.",
            payload={"category": category, "code": code},
        ),
    )
    append_event(
        project_path,
        HarnessEvent(
            project_id=metadata.project_id,
            run_id="run-retryable",
            kind="run_failed",
            atomic_action="advance_to_next_checkpoint",
            status="failed",
            message="Harness run failed after bounded repair.",
        ),
    )

    readiness = readiness_api.get_readiness()

    assert readiness.next_action.id == "retry_failed_run"
    assert readiness.next_action.command == "POST /api/runs/resume"
    assert readiness.next_action.requires_user is True


@pytest.mark.parametrize(
    ("category", "code"),
    [
        ("unsupported_capability", "tool_calling_unavailable"),
        ("harness_conflict", "stale_candidate_revision"),
        ("cross_loop_semantic", "unsupported_owner_route"),
        ("needs_user", "explicit_decision_required"),
        ("exhausted", "agent_turn_limit_exhausted"),
        ("cancelled", "activation_cancelled"),
    ],
)
def test_readiness_keeps_non_retryable_failure_inspection_only(
    tmp_path: Path,
    monkeypatch,
    category: str,
    code: str,
) -> None:
    _isolate_runtime_paths(tmp_path, monkeypatch)
    project = project_storage.create_project(
        CreateProjectRequest(operation_mode="full_auto")
    )
    project_path = Path(project.path)
    _approve_setup(project_path)
    _create_profile()
    metadata = project_storage.read_project_metadata(project_path)
    metadata.run_status = "failed"
    project_storage.write_project_metadata(project_path, metadata)
    append_event(
        project_path,
        HarnessEvent(
            project_id=metadata.project_id,
            run_id="run-2",
            kind="agent_activation_failed",
            loop_layer="chapter",
            atomic_action="run_chapter_agent",
            status="failed",
            message="Non-retryable Agent failure preserved for inspection.",
            payload={"category": category, "code": code},
        ),
    )
    append_event(
        project_path,
        HarnessEvent(
            project_id=metadata.project_id,
            run_id="run-2",
            kind="run_failed",
            atomic_action="advance_to_next_checkpoint",
            status="failed",
            message="Harness run failed: stale candidate revision.",
        ),
    )

    readiness = readiness_api.get_readiness()

    assert readiness.next_action.id == "inspect_failure"
    assert readiness.next_action.command is None
    assert readiness.next_action.requires_user is True


def test_readiness_recommends_recovering_stale_run_lock(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _isolate_runtime_paths(tmp_path, monkeypatch)
    project = project_storage.create_project(
        CreateProjectRequest(operation_mode="full_auto")
    )
    project_path = Path(project.path)
    _approve_setup(project_path)
    _create_profile()
    metadata = project_storage.read_project_metadata(project_path)
    metadata.run_status = "running"
    project_storage.write_project_metadata(project_path, metadata)

    readiness = readiness_api.get_readiness()
    by_id = {gate.id: gate for gate in readiness.gates}

    assert readiness.status == "pending"
    assert readiness.can_start_run is False
    assert by_id["run_control"].status == "pending"
    assert readiness.next_action.id == "recover_stale_run"
    assert readiness.next_action.command == "POST /api/runs/recover-stale"
    assert readiness.next_action.requires_user is True
    assert readiness.next_action.evidence == ["running", "no_active_runner"]


def test_readiness_waits_when_runner_is_active(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _isolate_runtime_paths(tmp_path, monkeypatch)
    project = project_storage.create_project(
        CreateProjectRequest(operation_mode="full_auto")
    )
    project_path = Path(project.path)
    _approve_setup(project_path)
    _create_profile()
    metadata = project_storage.read_project_metadata(project_path)
    metadata.run_status = "running"
    project_storage.write_project_metadata(project_path, metadata)

    assert begin_active_runner(project_path) is True
    try:
        readiness = readiness_api.get_readiness()
    finally:
        end_active_runner(project_path)

    assert readiness.next_action.id == "wait_for_safe_checkpoint"


def test_readiness_fails_when_approved_setup_artifact_is_missing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _isolate_runtime_paths(tmp_path, monkeypatch)
    project = project_storage.create_project(
        CreateProjectRequest(operation_mode="full_auto")
    )
    project_path = Path(project.path)
    _approve_setup(project_path)
    (project_path / "book" / "settings.md").unlink()

    readiness = readiness_api.get_readiness()
    by_id = {gate.id: gate for gate in readiness.gates}

    assert readiness.status == "failed"
    assert readiness.can_start_run is False
    assert by_id["book_setup"].status == "failed"
    assert "book/settings.md" in by_id["book_setup"].evidence


def _approve_setup(project_path: Path) -> None:
    state = _prepare_candidate(project_path)
    assert state.candidate is not None
    setup_storage.approve_setup(
        project_path,
        SetupApprovalRequest(
            candidate_revision=state.candidate.revision,
            title="Readiness Fixture",
        ),
    )


def _prepare_candidate(project_path: Path, *, blocked: bool = False):
    state = setup_storage.read_setup_state(project_path)
    state.direction_draft = _direction()
    state.selected_title = "Readiness Fixture"
    state.title_selection_source = "custom"
    title_decision = "正式书名：《Readiness Fixture》"
    state.confirmed_decisions = [title_decision]
    state.readiness = SetupReadinessSignal(
        status="ready",
        reason="Direction and formal title are ready for review.",
    )
    context_path = setup_storage.write_review_context_snapshot(
        project_path,
        candidate_revision=state.candidate_revision_counter + 1,
        snapshot={"sources": ["book/direction_draft.md"]},
    )
    return setup_storage.save_book_direction_candidate(
        project_path,
        state,
        synthesis=BookDirectionSynthesis(
            direction_markdown=_direction(),
            constraints=BookDirectionConstraints(
                confirmed=["Use fair clues.", title_decision],
                must_preserve=["Reveals change relationships."],
                must_avoid=["No arbitrary solution."],
                creative_freedoms=["Plan only the current arc."],
                open_decisions=[],
            ),
            confirmed_decision_coverage=[
                ConfirmedDecisionCoverage(
                    decision=title_decision,
                    candidate_evidence="Readiness Fixture",
                )
            ],
            recommended_titles=[
                BookTitleSuggestion(title="Readiness Fixture", rationale="Primary option."),
                BookTitleSuggestion(title="Ready Arc", rationale="Arc-focused option."),
                BookTitleSuggestion(title="Prepared Story", rationale="Harness-focused option."),
            ],
            rolling_plan_markdown=_rolling_contract(),
            model_snapshot="fixture-model",
            provider_snapshot="openai-compatible",
            usage={},
        ),
        review=(
            BookDirectionReview(
                status="blocked",
                summary="Candidate must return to discussion.",
                issues=[
                    BookDirectionReviewIssue(
                        severity="blocking",
                        kind="contradiction",
                        message="The candidate contradicts a confirmed decision.",
                        evidence=["confirmed direction"],
                    )
                ],
                signals=[],
            )
            if blocked
            else BookDirectionReview(
                status="passed",
                summary="Candidate is usable.",
                issues=[],
                signals=["rolling_scope:passed"],
            )
        ),
        profile_id="main",
        review_model_snapshot="fixture-model",
        context_snapshot_path=context_path,
    )


def _direction() -> str:
    return (
        "# Book Direction\n\nA grounded mystery about earned trust. Every reveal uses fair clues "
        "and changes a relationship. The protagonist gains agency through difficult alliances, "
        "while every victory carries a visible personal cost. Later antagonists and the exact ending "
        "remain open so each story arc can be planned from committed canon without rewriting these "
        "stable promises."
    )


def _rolling_contract() -> str:
    return (
        "# Rolling Contract\n\nPlan only the current story arc from approved direction and committed "
        "canon. After its chapters commit, reconcile observations and state patches before choosing "
        "the next arc. Return to book discussion only if a route requires changing an approved "
        "highest-level decision."
    )


def _create_profile() -> None:
    profile_storage.upsert_profile(
        LlmProfileUpsert(
            id="main",
            name="Main Provider",
            protocol="openai-compatible",
            base_url="https://api.example.com/v1",
            api_key="secret-key",
            model="example-model",
        )
    )


def _isolate_runtime_paths(tmp_path: Path, monkeypatch) -> None:
    config_dir = tmp_path / "config"
    output_dir = tmp_path / "output"
    active_project_path = config_dir / "active-project.local.json"
    llm_profiles_path = config_dir / "llm-profiles.local.json"

    monkeypatch.setattr(core_config, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(core_config, "OUTPUT_DIR", output_dir)
    monkeypatch.setattr(core_config, "ACTIVE_PROJECT_PATH", active_project_path)
    monkeypatch.setattr(core_config, "LLM_PROFILES_PATH", llm_profiles_path)
    monkeypatch.setattr(core_paths, "OUTPUT_DIR", output_dir)
    monkeypatch.setattr(project_storage, "OUTPUT_DIR", output_dir)
    monkeypatch.setattr(project_storage, "ACTIVE_PROJECT_PATH", active_project_path)
    monkeypatch.setattr(profile_storage, "LLM_PROFILES_PATH", llm_profiles_path)
