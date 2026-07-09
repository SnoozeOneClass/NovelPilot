from pathlib import Path

import pytest
from fastapi import HTTPException

from app.api import readiness as readiness_api
from app.core import config as core_config
from app.core import paths as core_paths
from app.harness.run_control import begin_active_runner, end_active_runner
from app.schemas.events import HarnessEvent
from app.schemas.profiles import LlmProfileUpsert
from app.schemas.projects import CreateProjectRequest, ProjectMetadata
from app.schemas.setup import SetupAnswerRequest
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
    project_storage.create_project(CreateProjectRequest(title="Novel", operation_mode="full_auto"))

    readiness = readiness_api.get_readiness()
    by_id = {gate.id: gate for gate in readiness.gates}

    assert readiness.status == "pending"
    assert readiness.can_start_run is False
    assert by_id["book_setup"].status == "pending"
    assert by_id["active_llm_profile"].status == "pending"
    assert by_id["run_control"].status == "passed"
    assert readiness.next_action.id == "answer_book_setup"
    assert readiness.next_action.command == "POST /api/setup/answers"
    assert readiness.next_action.requires_user is True


def test_readiness_allows_run_when_required_gates_pass(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _isolate_runtime_paths(tmp_path, monkeypatch)
    project = project_storage.create_project(
        CreateProjectRequest(title="Ready Novel", operation_mode="full_auto")
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


def test_readiness_recommends_setup_approval_when_required_answers_exist(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _isolate_runtime_paths(tmp_path, monkeypatch)
    project = project_storage.create_project(
        CreateProjectRequest(title="Answered Novel", operation_mode="full_auto")
    )
    project_path = Path(project.path)
    state = setup_storage.read_setup_state(project_path)
    for question in state.questions:
        setup_storage.answer_setup_question(
            project_path,
            SetupAnswerRequest(question_id=question.id, answer=f"{question.title} answer"),
        )

    readiness = readiness_api.get_readiness()

    assert readiness.next_action.id == "approve_book_setup"
    assert readiness.next_action.command == "POST /api/setup/approve"
    assert readiness.next_action.requires_user is True


def test_readiness_recommends_arc_approval_in_participatory_mode(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _isolate_runtime_paths(tmp_path, monkeypatch)
    project = project_storage.create_project(
        CreateProjectRequest(title="Review Novel", operation_mode="participatory")
    )
    project_path = Path(project.path)
    _approve_setup(project_path)
    _create_profile()
    metadata = ProjectMetadata.model_validate(project.metadata.model_dump(mode="json"))
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


def test_readiness_recommends_retry_for_rejected_state_patch(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _isolate_runtime_paths(tmp_path, monkeypatch)
    project = project_storage.create_project(
        CreateProjectRequest(title="Retry Novel", operation_mode="full_auto")
    )
    project_path = Path(project.path)
    _approve_setup(project_path)
    _create_profile()
    metadata = ProjectMetadata.model_validate(project.metadata.model_dump(mode="json"))
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


def test_readiness_recommends_failure_inspection_for_failed_runs(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _isolate_runtime_paths(tmp_path, monkeypatch)
    project = project_storage.create_project(
        CreateProjectRequest(title="Failed Novel", operation_mode="full_auto")
    )
    project_path = Path(project.path)
    _approve_setup(project_path)
    _create_profile()
    metadata = ProjectMetadata.model_validate(project.metadata.model_dump(mode="json"))
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

    assert readiness.next_action.id == "inspect_failure"
    assert readiness.next_action.requires_user is True
    assert readiness.next_action.evidence == [
        "run_failed",
        "advance_to_next_checkpoint",
        "Harness run failed: provider timeout.",
    ]


def test_readiness_recommends_recovering_stale_run_lock(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _isolate_runtime_paths(tmp_path, monkeypatch)
    project = project_storage.create_project(
        CreateProjectRequest(title="Stale Novel", operation_mode="full_auto")
    )
    project_path = Path(project.path)
    _approve_setup(project_path)
    _create_profile()
    metadata = ProjectMetadata.model_validate(project.metadata.model_dump(mode="json"))
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
        CreateProjectRequest(title="Active Novel", operation_mode="full_auto")
    )
    project_path = Path(project.path)
    _approve_setup(project_path)
    _create_profile()
    metadata = ProjectMetadata.model_validate(project.metadata.model_dump(mode="json"))
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
        CreateProjectRequest(title="Corrupt Novel", operation_mode="full_auto")
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
    state = setup_storage.read_setup_state(project_path)
    for question in state.questions:
        setup_storage.answer_setup_question(
            project_path,
            SetupAnswerRequest(question_id=question.id, answer=f"{question.title} answer"),
        )
    setup_storage.approve_setup(project_path)


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
