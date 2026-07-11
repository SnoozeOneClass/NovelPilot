from threading import Event, Thread

import pytest
from fastapi import HTTPException

from app.api import profiles as profile_api
from app.api import projects as project_api
from app.core import config as core_config
from app.core import paths as core_paths
from app.core.paths import resolve_project_path
from app.harness.run_control import begin_active_runner, end_active_runner
from app.schemas.projects import (
    CreateProjectRequest,
    OpenProjectRequest,
    ProjectMetadata,
    UpdateOperationModeRequest,
)
from app.storage import projects as project_storage
from app.storage import setup as setup_storage
from app.storage import transactions
from app.storage.events import read_events
from app.storage.json_files import read_json, write_json
from app.storage.projects import create_project, get_active_project, open_project


def test_create_project_initializes_document_first_layout(tmp_path, monkeypatch) -> None:
    _isolate_project_runtime(tmp_path, monkeypatch)

    summary = create_project(CreateProjectRequest(operation_mode="participatory"))
    project_path = resolve_project_path(summary.name)

    assert summary.title is None
    assert summary.name == f"project-{summary.metadata.project_id}"
    assert summary.metadata.operation_mode == "participatory"
    assert (project_path / "project.json").exists()
    assert (project_path / "events.jsonl").exists()
    assert (project_path / "book" / "settings.md").exists()
    assert (project_path / "book" / "outline.md").exists()
    assert (project_path / "book" / "state.json").exists()
    assert (project_path / "canon" / "characters.json").exists()
    assert (project_path / "canon" / "relationships.json").exists()
    assert (project_path / "canon" / "world_facts.json").exists()
    assert (project_path / "canon" / "foreshadowing.json").exists()
    assert not (project_storage.OUTPUT_DIR / ".creating").exists()


def test_create_project_does_not_publish_partial_initialization(tmp_path, monkeypatch) -> None:
    _isolate_project_runtime(tmp_path, monkeypatch)

    def fail_setup_initialization(_project_path) -> None:
        raise OSError("injected setup initialization failure")

    monkeypatch.setattr(
        project_storage,
        "initialize_setup_state",
        fail_setup_initialization,
    )

    with pytest.raises(OSError, match="injected setup initialization failure"):
        create_project(CreateProjectRequest(operation_mode="full_auto"))

    assert project_storage.list_projects() == []
    assert project_storage.get_active_project() is None
    assert not (project_storage.OUTPUT_DIR / ".creating").exists()


def test_project_load_boundary_recovers_interrupted_transaction(
    tmp_path,
    monkeypatch,
) -> None:
    _isolate_project_runtime(tmp_path, monkeypatch)
    project = create_project(CreateProjectRequest(operation_mode="full_auto"))
    project_path = resolve_project_path(project.name)
    original_book_state = (project_path / "book" / "state.json").read_text(encoding="utf-8")
    changed_metadata = project.metadata.model_copy(deep=True)
    changed_metadata.title = "Partially Promoted"
    original_promote = transactions._promote_staged_file
    promotion_count = 0

    def stop_on_second_promotion(staged, target, transaction_id: str) -> None:
        nonlocal promotion_count
        promotion_count += 1
        if promotion_count == 2:
            raise SystemExit("simulated process stop")
        original_promote(staged, target, transaction_id)

    monkeypatch.setattr(transactions, "_promote_staged_file", stop_on_second_promotion)

    with pytest.raises(SystemExit, match="simulated process stop"):
        transactions.commit_file_transaction(
            project_path,
            kind="test-project-load-recovery",
            files={
                "project.json": changed_metadata.model_dump_json(indent=2) + "\n",
                "book/state.json": '{"schema_version": 2, "version": 999}\n',
            },
        )

    assert project_storage.read_project_metadata(project_path).title == "Partially Promoted"

    recovered = project_storage.get_active_project()

    assert recovered is not None
    assert recovered.title is None
    assert (project_path / "book" / "state.json").read_text(
        encoding="utf-8"
    ) == original_book_state
    assert not (project_path / transactions.TRANSACTION_ROOT).exists()


def test_open_project_switches_single_active_project(tmp_path, monkeypatch) -> None:
    _isolate_project_runtime(tmp_path, monkeypatch)
    first = create_project(CreateProjectRequest(operation_mode="full_auto"))
    second = create_project(CreateProjectRequest(operation_mode="participatory"))

    opened_first = open_project(first.name)
    active_first = get_active_project()
    opened_second = open_project(second.name)
    active_second = get_active_project()

    assert opened_first.name == first.name
    assert active_first is not None
    assert active_first.name == first.name
    assert opened_second.name == second.name
    assert active_second is not None
    assert active_second.name == second.name
    assert active_second.metadata.project_id == second.metadata.project_id
    assert active_second.metadata.project_id != first.metadata.project_id


def test_reopen_project_restores_content_progress_and_mode(tmp_path, monkeypatch) -> None:
    _isolate_project_runtime(tmp_path, monkeypatch)
    project = create_project(CreateProjectRequest(operation_mode="participatory"))
    project_path = resolve_project_path(project.name)
    metadata = project_storage.read_project_metadata(project_path)
    metadata.active_arc_id = "arc-003"
    metadata.active_chapter_id = "chapter-007"
    metadata.run_status = "waiting_for_user"
    project_storage.write_project_metadata(project_path, metadata)
    write_json(
        project_path / "arcs" / "arc-003" / "state.json",
        {
            "arc_id": "arc-003",
            "plan_path": "arcs/arc-003/plan.md",
            "human_review": "awaiting_review",
        },
    )
    chapter_path = project_path / "chapters" / "chapter-007" / "draft.md"
    chapter_path.parent.mkdir(parents=True)
    chapter_path.write_text("persisted chapter content", encoding="utf-8")

    create_project(CreateProjectRequest(operation_mode="full_auto"))
    reopened = open_project(project.name)

    assert reopened.metadata.operation_mode == "participatory"
    assert reopened.metadata.active_arc_id == "arc-003"
    assert reopened.metadata.active_chapter_id == "chapter-007"
    assert reopened.metadata.run_status == "waiting_for_user"
    assert chapter_path.read_text(encoding="utf-8") == "persisted chapter content"
    assert read_json(project_path / "arcs" / "arc-003" / "state.json")[
        "human_review"
    ] == "awaiting_review"


@pytest.mark.parametrize("run_status", ["running", "pause_requested"])
def test_project_lifecycle_rejects_switch_create_or_close_during_run(
    tmp_path,
    monkeypatch,
    run_status: str,
) -> None:
    _isolate_project_runtime(tmp_path, monkeypatch)
    first = create_project(CreateProjectRequest(operation_mode="full_auto"))
    second = create_project(CreateProjectRequest(operation_mode="full_auto"))
    open_project(first.name)
    first_path = resolve_project_path(first.name)
    _set_run_status(first_path, run_status)

    with pytest.raises(project_storage.ActiveProjectBusyError):
        open_project(second.name)
    with pytest.raises(project_storage.ActiveProjectBusyError):
        create_project(CreateProjectRequest(operation_mode="full_auto"))
    with pytest.raises(project_storage.ActiveProjectBusyError):
        project_storage.close_active_project()

    active = get_active_project()

    assert active is not None
    assert active.name == first.name
    assert len(project_storage.list_projects()) == 2


def test_project_lifecycle_rejects_active_runner_before_status_transition(
    tmp_path,
    monkeypatch,
) -> None:
    _isolate_project_runtime(tmp_path, monkeypatch)
    first = create_project(CreateProjectRequest(operation_mode="full_auto"))
    second = create_project(CreateProjectRequest(operation_mode="participatory"))
    open_project(first.name)
    first_path = resolve_project_path(first.name)
    assert begin_active_runner(first_path) is True
    try:
        with pytest.raises(HTTPException) as open_error:
            project_api.open_project(OpenProjectRequest(name=second.name))
        with pytest.raises(HTTPException) as create_error:
            project_api.create_project(CreateProjectRequest(operation_mode="full_auto"))
        with pytest.raises(HTTPException) as close_error:
            project_api.close_project()
    finally:
        end_active_runner(first_path)

    assert open_error.value.status_code == 400
    assert create_error.value.status_code == 400
    assert close_error.value.status_code == 400
    active = get_active_project()
    assert active is not None
    assert active.name == first.name
    assert len(project_storage.list_projects()) == 2


def test_multiple_untitled_projects_have_unique_stable_directories(
    tmp_path,
    monkeypatch,
) -> None:
    _isolate_project_runtime(tmp_path, monkeypatch)

    first = create_project(CreateProjectRequest(operation_mode="full_auto"))
    second = create_project(CreateProjectRequest(operation_mode="full_auto"))

    assert first.title is None
    assert second.title is None
    assert first.name != second.name
    assert first.path != second.path
    assert resolve_project_path(first.name).exists()
    assert resolve_project_path(second.name).exists()


def test_mode_change_marks_existing_unapproved_arc_for_review_and_keeps_directory(
    tmp_path,
    monkeypatch,
) -> None:
    _isolate_project_runtime(tmp_path, monkeypatch)
    project = create_project(CreateProjectRequest(operation_mode="full_auto"))
    project_path = resolve_project_path(project.name)
    metadata = project_storage.read_project_metadata(project_path)
    metadata.active_arc_id = "arc-001"
    project_storage.write_project_metadata(project_path, metadata)
    write_json(
        project_path / "arcs" / "arc-001" / "state.json",
        {
            "arc_id": "arc-001",
            "plan_path": "arcs/arc-001/plan.md",
            "human_review": "not_required",
        },
    )

    updated = project_api.update_operation_mode(
        UpdateOperationModeRequest(operation_mode="participatory")
    )

    assert updated.name == project.name
    assert updated.path == project.path
    assert updated.metadata.operation_mode == "participatory"
    arc_state = read_json(project_path / "arcs" / "arc-001" / "state.json")
    assert arc_state["human_review"] == "awaiting_review"
    event = read_events(project_path)[-1]
    assert event.kind == "operation_mode_changed"
    assert event.payload["previous_mode"] == "full_auto"
    assert event.payload["operation_mode"] == "participatory"
    assert event.payload["pending_arc_review"] is True


def test_mode_change_to_full_auto_preserves_pending_arc_gate(
    tmp_path,
    monkeypatch,
) -> None:
    _isolate_project_runtime(tmp_path, monkeypatch)
    project = create_project(CreateProjectRequest(operation_mode="participatory"))
    project_path = resolve_project_path(project.name)
    metadata = project_storage.read_project_metadata(project_path)
    metadata.active_arc_id = "arc-001"
    metadata.run_status = "waiting_for_user"
    project_storage.write_project_metadata(project_path, metadata)
    write_json(
        project_path / "arcs" / "arc-001" / "state.json",
        {
            "arc_id": "arc-001",
            "plan_path": "arcs/arc-001/plan.md",
            "human_review": "awaiting_review",
        },
    )

    updated = project_api.update_operation_mode(
        UpdateOperationModeRequest(operation_mode="full_auto")
    )

    assert updated.metadata.operation_mode == "full_auto"
    assert updated.metadata.run_status == "waiting_for_user"
    arc_state = read_json(project_path / "arcs" / "arc-001" / "state.json")
    assert arc_state["human_review"] == "awaiting_review"


@pytest.mark.parametrize("run_status", ["running", "pause_requested"])
def test_mode_change_rejects_run_lock(tmp_path, monkeypatch, run_status: str) -> None:
    _isolate_project_runtime(tmp_path, monkeypatch)
    project = create_project(CreateProjectRequest(operation_mode="full_auto"))
    project_path = resolve_project_path(project.name)
    _set_run_status(project_path, run_status)

    with pytest.raises(HTTPException) as caught:
        project_api.update_operation_mode(
            UpdateOperationModeRequest(operation_mode="participatory")
        )

    assert caught.value.status_code == 409
    assert project_storage.read_project_metadata(project_path).operation_mode == "full_auto"


def test_mode_change_rejects_active_runner_before_status_transition(
    tmp_path,
    monkeypatch,
) -> None:
    _isolate_project_runtime(tmp_path, monkeypatch)
    project = create_project(CreateProjectRequest(operation_mode="full_auto"))
    project_path = resolve_project_path(project.name)
    assert begin_active_runner(project_path) is True
    try:
        with pytest.raises(HTTPException) as caught:
            project_api.update_operation_mode(
                UpdateOperationModeRequest(operation_mode="participatory")
            )
    finally:
        end_active_runner(project_path)

    assert caught.value.status_code == 409
    assert project_storage.read_project_metadata(project_path).operation_mode == "full_auto"


def test_mode_change_fails_closed_with_malformed_arc_state(
    tmp_path,
    monkeypatch,
) -> None:
    _isolate_project_runtime(tmp_path, monkeypatch)
    project = create_project(CreateProjectRequest(operation_mode="full_auto"))
    project_path = resolve_project_path(project.name)
    metadata = project_storage.read_project_metadata(project_path)
    metadata.active_arc_id = "arc-001"
    project_storage.write_project_metadata(project_path, metadata)
    write_json(
        project_path / "arcs" / "arc-001" / "state.json",
        {"human_review": "not_required"},
    )

    with pytest.raises(HTTPException) as caught:
        project_api.update_operation_mode(
            UpdateOperationModeRequest(operation_mode="participatory")
        )

    assert caught.value.status_code == 409
    assert project_storage.read_project_metadata(project_path).operation_mode == "full_auto"
    assert not any(event.kind == "operation_mode_changed" for event in read_events(project_path))


def test_participatory_to_full_auto_fails_closed_when_active_arc_state_is_missing(
    tmp_path,
    monkeypatch,
) -> None:
    _isolate_project_runtime(tmp_path, monkeypatch)
    project = create_project(CreateProjectRequest(operation_mode="participatory"))
    project_path = resolve_project_path(project.name)
    metadata = project_storage.read_project_metadata(project_path)
    metadata.active_arc_id = "arc-001"
    project_storage.write_project_metadata(project_path, metadata)

    with pytest.raises(HTTPException) as caught:
        project_api.update_operation_mode(
            UpdateOperationModeRequest(operation_mode="full_auto")
        )

    assert caught.value.status_code == 409
    assert project_storage.read_project_metadata(project_path).operation_mode == "participatory"


def test_participatory_to_full_auto_persists_unapproved_arc_gate(
    tmp_path,
    monkeypatch,
) -> None:
    _isolate_project_runtime(tmp_path, monkeypatch)
    project = create_project(CreateProjectRequest(operation_mode="participatory"))
    project_path = resolve_project_path(project.name)
    metadata = project_storage.read_project_metadata(project_path)
    metadata.active_arc_id = "arc-001"
    project_storage.write_project_metadata(project_path, metadata)
    write_json(
        project_path / "arcs" / "arc-001" / "state.json",
        {
            "arc_id": "arc-001",
            "plan_path": "arcs/arc-001/plan.md",
            "human_review": "not_required",
        },
    )

    updated = project_api.update_operation_mode(
        UpdateOperationModeRequest(operation_mode="full_auto")
    )

    arc_state = read_json(project_path / "arcs" / "arc-001" / "state.json")
    assert updated.metadata.operation_mode == "full_auto"
    assert arc_state["human_review"] == "awaiting_review"
    assert read_events(project_path)[-1].payload["pending_arc_review"] is True


def test_mode_change_keeps_audit_event_in_outbox_until_append_recovers(
    tmp_path,
    monkeypatch,
) -> None:
    _isolate_project_runtime(tmp_path, monkeypatch)
    project = create_project(CreateProjectRequest(operation_mode="full_auto"))
    project_path = resolve_project_path(project.name)
    original_append = setup_storage.append_durable_event

    def fail_append(*_args, **_kwargs) -> None:
        raise OSError("injected event append failure")

    monkeypatch.setattr(setup_storage, "append_durable_event", fail_append)
    updated = project_api.update_operation_mode(
        UpdateOperationModeRequest(operation_mode="participatory")
    )

    assert updated.metadata.operation_mode == "participatory"
    assert not any(event.kind == "operation_mode_changed" for event in read_events(project_path))
    assert list((project_path / "book" / ".event-outbox").glob("*.json"))

    monkeypatch.setattr(setup_storage, "append_durable_event", original_append)
    setup_storage.flush_pending_setup_events(project_path)

    assert read_events(project_path)[-1].kind == "operation_mode_changed"
    assert not (project_path / "book" / ".event-outbox").exists()


def test_profile_sync_and_mode_change_preserve_each_others_metadata(
    tmp_path,
    monkeypatch,
) -> None:
    _isolate_project_runtime(tmp_path, monkeypatch)
    project = create_project(CreateProjectRequest(operation_mode="full_auto"))
    project_path = resolve_project_path(project.name)
    profile_write_entered = Event()
    allow_profile_write = Event()
    original_write = profile_api.write_project_metadata
    errors: list[BaseException] = []

    def blocking_profile_write(path, metadata) -> None:
        profile_write_entered.set()
        if not allow_profile_write.wait(timeout=5):
            raise TimeoutError("profile write was not released")
        original_write(path, metadata)

    def sync_profile() -> None:
        try:
            profile_api._sync_active_project_profile("main")
        except BaseException as exc:  # pragma: no cover - asserted through errors
            errors.append(exc)

    def switch_mode() -> None:
        try:
            project_api.update_operation_mode(
                UpdateOperationModeRequest(operation_mode="participatory")
            )
        except BaseException as exc:  # pragma: no cover - asserted through errors
            errors.append(exc)

    monkeypatch.setattr(profile_api, "write_project_metadata", blocking_profile_write)
    profile_thread = Thread(target=sync_profile)
    profile_thread.start()
    assert profile_write_entered.wait(timeout=5)

    mode_thread = Thread(target=switch_mode)
    mode_thread.start()
    mode_thread.join(timeout=0.1)
    assert mode_thread.is_alive()

    allow_profile_write.set()
    profile_thread.join(timeout=5)
    mode_thread.join(timeout=5)

    assert not errors
    assert not profile_thread.is_alive()
    assert not mode_thread.is_alive()
    metadata = project_storage.read_project_metadata(project_path)
    assert metadata.active_profile_id == "main"
    assert metadata.operation_mode == "participatory"


def test_open_project_rejects_missing_project(tmp_path, monkeypatch) -> None:
    _isolate_project_runtime(tmp_path, monkeypatch)

    with pytest.raises(FileNotFoundError):
        open_project("Missing Novel")


def test_active_project_pointer_must_stay_under_output(tmp_path, monkeypatch) -> None:
    _isolate_project_runtime(tmp_path, monkeypatch)
    outside_project = tmp_path / "outside-project"
    outside_project.mkdir()
    write_json(
        outside_project / "project.json",
        ProjectMetadata(title="Outside").model_dump(mode="json"),
    )
    write_json(
        project_storage.ACTIVE_PROJECT_PATH,
        {"name": "outside-project", "path": str(outside_project)},
    )

    assert project_storage.get_active_project_path() is None
    assert get_active_project() is None


@pytest.mark.parametrize("name", ["..", "../escape", "bad/name"])
def test_resolve_project_path_rejects_unsafe_names(name: str) -> None:
    with pytest.raises(ValueError):
        resolve_project_path(name)


def _isolate_project_runtime(tmp_path, monkeypatch) -> None:
    config_dir = tmp_path / "config"
    output_dir = tmp_path / "output"
    active_project_path = config_dir / "active-project.local.json"
    monkeypatch.setattr(core_config, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(core_config, "OUTPUT_DIR", output_dir)
    monkeypatch.setattr(core_config, "ACTIVE_PROJECT_PATH", active_project_path)
    monkeypatch.setattr(core_paths, "OUTPUT_DIR", output_dir)
    monkeypatch.setattr(project_storage, "OUTPUT_DIR", output_dir)
    monkeypatch.setattr(project_storage, "ACTIVE_PROJECT_PATH", active_project_path)


def _set_run_status(project_path, run_status: str) -> None:
    metadata = project_storage.read_project_metadata(project_path)
    metadata.run_status = run_status
    project_storage.write_project_metadata(project_path, metadata)
