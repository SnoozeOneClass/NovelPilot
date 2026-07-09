from datetime import UTC, datetime
from pathlib import Path

from app.core.config import ACTIVE_PROJECT_PATH, OUTPUT_DIR, ensure_runtime_dirs
from app.core.paths import resolve_project_path
from app.schemas.projects import (
    ActiveProjectDocument,
    CreateProjectRequest,
    ProjectMetadata,
    ProjectSummary,
)
from app.storage.json_files import read_json, write_json
from app.storage.setup import initialize_setup_state
from app.storage.text_files import write_text_file


INITIAL_CANON_FILES = {
    "canon/characters.json": {"schema_version": 1, "version": 1, "items": {}},
    "canon/relationships.json": {"schema_version": 1, "version": 1, "items": {}},
    "canon/world_facts.json": {"schema_version": 1, "version": 1, "items": {}},
    "canon/foreshadowing.json": {"schema_version": 1, "version": 1, "items": {}},
}
RUN_LOCK_STATUSES = {"running", "pause_requested"}


class ActiveProjectBusyError(RuntimeError):
    pass


def metadata_path(project_path: Path) -> Path:
    return project_path / "project.json"


def read_project_metadata(project_path: Path) -> ProjectMetadata:
    data = read_json(metadata_path(project_path))
    if data is None:
        raise FileNotFoundError(f"Missing project metadata: {metadata_path(project_path)}")
    return ProjectMetadata.model_validate(data)


def write_project_metadata(project_path: Path, metadata: ProjectMetadata) -> None:
    metadata.updated_at = datetime.now(UTC)
    write_json(metadata_path(project_path), metadata.model_dump(mode="json"))


def create_project(request: CreateProjectRequest) -> ProjectSummary:
    ensure_runtime_dirs()
    _ensure_active_project_can_change()
    project_path = resolve_project_path(request.title)
    if project_path.exists():
        raise FileExistsError(f"Project already exists: {project_path.name}")

    metadata = ProjectMetadata(title=request.title, operation_mode=request.operation_mode)
    project_path.mkdir(parents=True)
    for relative in ["book", "arcs", "chapters", "canon", "exports"]:
        (project_path / relative).mkdir(parents=True, exist_ok=True)

    write_project_metadata(project_path, metadata)
    write_text_file(project_path / "events.jsonl", "")
    write_text_file(project_path / "book" / "settings.md", "# Book Settings\n")
    write_text_file(project_path / "book" / "outline.md", "# Book Outline\n")
    write_json(project_path / "book" / "state.json", {"schema_version": 1, "version": 1})
    initialize_setup_state(project_path)
    for relative, payload in INITIAL_CANON_FILES.items():
        write_json(project_path / relative, payload)

    set_active_project(project_path)
    return summarize_project(project_path)


def summarize_project(project_path: Path) -> ProjectSummary:
    metadata = read_project_metadata(project_path)
    return ProjectSummary(
        name=project_path.name,
        title=metadata.title,
        path=str(project_path),
        metadata=metadata,
    )


def list_projects() -> list[ProjectSummary]:
    ensure_runtime_dirs()
    projects: list[ProjectSummary] = []
    for entry in sorted(OUTPUT_DIR.iterdir(), key=lambda item: item.name.lower()):
        if not entry.is_dir() or not metadata_path(entry).exists():
            continue
        projects.append(summarize_project(entry))
    return projects


def open_project(name: str) -> ProjectSummary:
    _ensure_active_project_can_change()
    project_path = resolve_project_path(name)
    if not metadata_path(project_path).exists():
        raise FileNotFoundError(f"Project not found: {name}")
    set_active_project(project_path)
    return summarize_project(project_path)


def set_active_project(project_path: Path) -> None:
    document = ActiveProjectDocument(name=project_path.name, path=str(project_path))
    write_json(ACTIVE_PROJECT_PATH, document.model_dump(mode="json"))


def get_active_project_path() -> Path | None:
    data = read_json(ACTIVE_PROJECT_PATH)
    if data is None:
        return None
    document = ActiveProjectDocument.model_validate(data)
    project_path = Path(document.path).resolve()
    output_root = OUTPUT_DIR.resolve()
    try:
        project_path.relative_to(output_root)
    except ValueError:
        return None
    if not metadata_path(project_path).exists():
        return None
    return project_path


def get_active_project() -> ProjectSummary | None:
    project_path = get_active_project_path()
    if project_path is None:
        return None
    return summarize_project(project_path)


def close_active_project() -> None:
    _ensure_active_project_can_change()
    if ACTIVE_PROJECT_PATH.exists():
        ACTIVE_PROJECT_PATH.unlink()


def _ensure_active_project_can_change() -> None:
    project_path = get_active_project_path()
    if project_path is None:
        return

    metadata = read_project_metadata(project_path)
    if metadata.run_status in RUN_LOCK_STATUSES:
        raise ActiveProjectBusyError(
            "Cannot switch or close projects while the active harness run is in progress."
        )
