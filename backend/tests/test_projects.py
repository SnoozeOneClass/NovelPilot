from uuid import uuid4

import pytest

from app.core import config as core_config
from app.core import paths as core_paths
from app.core.paths import resolve_project_path
from app.schemas.projects import ProjectMetadata
from app.schemas.projects import CreateProjectRequest
from app.storage.json_files import write_json
from app.storage import projects as project_storage
from app.storage.projects import create_project, get_active_project, open_project


def test_create_project_initializes_document_first_layout(tmp_path, monkeypatch) -> None:
    _isolate_project_runtime(tmp_path, monkeypatch)
    title = f"Test Novel {uuid4().hex}"

    summary = create_project(CreateProjectRequest(title=title, operation_mode="participatory"))
    project_path = resolve_project_path(summary.name)

    assert summary.title == title
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


def test_open_project_switches_single_active_project(tmp_path, monkeypatch) -> None:
    _isolate_project_runtime(tmp_path, monkeypatch)
    first = create_project(CreateProjectRequest(title="First Novel", operation_mode="full_auto"))
    second = create_project(CreateProjectRequest(title="Second Novel", operation_mode="participatory"))

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


@pytest.mark.parametrize("run_status", ["running", "pause_requested"])
def test_project_lifecycle_rejects_switch_create_or_close_during_run(
    tmp_path,
    monkeypatch,
    run_status: str,
) -> None:
    _isolate_project_runtime(tmp_path, monkeypatch)
    first = create_project(CreateProjectRequest(title="First Novel", operation_mode="full_auto"))
    second = create_project(CreateProjectRequest(title="Second Novel", operation_mode="full_auto"))
    open_project(first.name)
    first_path = resolve_project_path(first.name)
    _set_run_status(first_path, run_status)

    with pytest.raises(project_storage.ActiveProjectBusyError):
        open_project(second.name)
    with pytest.raises(project_storage.ActiveProjectBusyError):
        create_project(CreateProjectRequest(title="Third Novel", operation_mode="full_auto"))
    with pytest.raises(project_storage.ActiveProjectBusyError):
        project_storage.close_active_project()

    active = get_active_project()

    assert active is not None
    assert active.name == first.name
    assert not resolve_project_path("Third Novel").exists()


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
