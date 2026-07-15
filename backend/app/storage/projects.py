import json
import shutil
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from pathlib import Path

from app.core.config import ACTIVE_PROJECT_PATH, OUTPUT_DIR, ensure_runtime_dirs
from app.core.paths import resolve_project_path
from app.schemas.arcs import CurrentArcState
from app.schemas.events import HarnessEvent
from app.schemas.projects import (
    ActiveProjectDocument,
    AgentPolicy,
    CreateProjectRequest,
    OperationMode,
    ProjectMetadata,
    ProjectSummary,
)
from app.storage.file_lock import exclusive_file_lock
from app.storage.json_files import read_json, write_json
from app.storage.setup import initialize_setup_state
from app.storage.text_files import write_text_file
from app.storage.transactions import (
    TRANSACTION_ROOT,
    commit_file_transaction,
    recover_file_transactions,
)


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


def project_metadata_lock(project_path: Path) -> AbstractContextManager[None]:
    return exclusive_file_lock(project_path / ".project.lock")


def create_project(request: CreateProjectRequest) -> ProjectSummary:
    ensure_runtime_dirs()
    _ensure_active_project_can_change()
    metadata = ProjectMetadata(operation_mode=request.operation_mode)
    project_path = resolve_project_path(f"project-{metadata.project_id}")
    if project_path.exists():
        raise FileExistsError(f"Project already exists: {project_path.name}")

    creating_root = OUTPUT_DIR / ".creating"
    staging_path = creating_root / project_path.name
    creating_root.mkdir(parents=True, exist_ok=True)
    if staging_path.exists():
        raise FileExistsError(f"Project staging directory already exists: {staging_path.name}")

    try:
        staging_path.mkdir()
        for relative in ["book", "arcs", "chapters", "canon", "exports"]:
            (staging_path / relative).mkdir(parents=True, exist_ok=True)

        write_project_metadata(staging_path, metadata)
        write_text_file(staging_path / "events.jsonl", "")
        write_text_file(
            staging_path / "book" / "settings.md",
            "# Book Direction\n\nPending explicit user approval.\n",
        )
        write_text_file(
            staging_path / "book" / "outline.md",
            "# Rolling Story Arc Contract\n\nPending explicit user approval.\n",
        )
        write_json(
            staging_path / "book" / "state.json",
            {"schema_version": 2, "version": 1},
        )
        initialize_setup_state(staging_path)
        for relative, payload in INITIAL_CANON_FILES.items():
            write_json(staging_path / relative, payload)

        staging_path.replace(project_path)
    except BaseException:
        shutil.rmtree(staging_path, ignore_errors=True)
        _remove_empty_creating_root()
        raise
    _remove_empty_creating_root()

    set_active_project(project_path)
    return summarize_project(project_path)


def recover_project_transactions(project_path: Path) -> None:
    with exclusive_file_lock(project_path / "book" / ".setup.lock"):
        with project_metadata_lock(project_path):
            recover_file_transactions(project_path)


def recover_all_project_transactions() -> None:
    ensure_runtime_dirs()
    for entry in sorted(OUTPUT_DIR.iterdir(), key=lambda item: item.name.lower()):
        if not entry.is_dir() or not (entry / TRANSACTION_ROOT).exists():
            continue
        recover_project_transactions(entry)


def update_operation_mode(
    project_path: Path,
    operation_mode: OperationMode,
) -> ProjectSummary:
    # Setup transactions use the same recovery root. Keep the lock order aligned with
    # setup approval so mode changes cannot recover or overlap an in-flight setup commit.
    with exclusive_file_lock(project_path / "book" / ".setup.lock"):
        with project_metadata_lock(project_path):
            recover_file_transactions(project_path)
            metadata = read_project_metadata(project_path)
            if metadata.run_status in RUN_LOCK_STATUSES:
                raise ActiveProjectBusyError(
                    "Cannot change operation mode while the harness run is in progress."
                )
            if metadata.operation_mode == operation_mode:
                return summarize_project(project_path)

            files: dict[str, str | bytes] = {}
            previous_mode = metadata.operation_mode
            metadata.operation_mode = operation_mode
            metadata.updated_at = datetime.now(UTC)
            files["project.json"] = _json_document(metadata.model_dump(mode="json"))

            arc_state: dict[str, object] | None = None
            if metadata.active_arc_id is not None:
                arc_state_path = project_path / "arcs" / metadata.active_arc_id / "state.json"
                raw_arc_state = read_json(arc_state_path, default=None)
                if not isinstance(raw_arc_state, dict):
                    raise ValueError(
                        "Cannot change operation mode while the active story arc state is missing."
                    )
                validated_arc = CurrentArcState.model_validate(raw_arc_state)
                if validated_arc.arc_id != metadata.active_arc_id:
                    raise ValueError(
                        "Cannot change operation mode because the active story arc state "
                        "does not match project metadata."
                    )
                arc_state = raw_arc_state
                if validated_arc.human_review != "approved":
                    arc_state["human_review"] = "awaiting_review"
                    arc_state["approved_at"] = None
                    files[arc_state_path.relative_to(project_path).as_posix()] = _json_document(
                        arc_state
                    )

            pending_arc_review = (
                arc_state is not None
                and arc_state.get("human_review") == "awaiting_review"
            )
            event = HarnessEvent(
                project_id=metadata.project_id,
                kind="operation_mode_changed",
                loop_layer="system",
                atomic_action="update_operation_mode",
                status="completed",
                routing_decision="apply_at_next_safe_checkpoint",
                message=(
                    f"Operation mode changed from {previous_mode} to {operation_mode}."
                ),
                payload={
                    "previous_mode": previous_mode,
                    "operation_mode": operation_mode,
                    "run_status": metadata.run_status,
                    "active_arc_id": metadata.active_arc_id,
                    "pending_arc_review": pending_arc_review,
                },
            )
            event_path = Path("book") / ".event-outbox" / f"{event.event_id}.json"
            files[event_path.as_posix()] = _json_document(event.model_dump(mode="json"))

            commit_file_transaction(
                project_path,
                kind=f"operation-mode-{previous_mode}-to-{operation_mode}",
                files=files,
            )
            return summarize_project(project_path)


def update_agent_policy(project_path: Path, agent_policy: AgentPolicy) -> ProjectSummary:
    with project_metadata_lock(project_path):
        recover_file_transactions(project_path)
        metadata = read_project_metadata(project_path)
        if metadata.run_status in RUN_LOCK_STATUSES:
            raise ActiveProjectBusyError(
                "Cannot change Agent policy while the harness run is in progress."
            )
        if metadata.agent_policy == agent_policy:
            return summarize_project(project_path)

        metadata.agent_policy = agent_policy
        metadata.updated_at = datetime.now(UTC)
        event = HarnessEvent(
            project_id=metadata.project_id,
            kind="agent_policy_changed",
            loop_layer="system",
            atomic_action="update_agent_policy",
            status="completed",
            routing_decision="apply_at_next_candidate_run",
            message="Agent model bindings and execution limits were updated.",
            payload={
                "agent_policy": agent_policy.model_dump(mode="json"),
                "run_status": metadata.run_status,
            },
        )
        event_path = Path("book") / ".event-outbox" / f"{event.event_id}.json"
        commit_file_transaction(
            project_path,
            kind="agent-policy-update",
            files={
                "project.json": _json_document(metadata.model_dump(mode="json")),
                event_path.as_posix(): _json_document(event.model_dump(mode="json")),
            },
        )
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
        if (entry / TRANSACTION_ROOT).exists():
            recover_project_transactions(entry)
        projects.append(summarize_project(entry))
    return projects


def open_project(name: str) -> ProjectSummary:
    _ensure_active_project_can_change()
    project_path = resolve_project_path(name)
    if not metadata_path(project_path).exists():
        raise FileNotFoundError(f"Project not found: {name}")
    if (project_path / TRANSACTION_ROOT).exists():
        recover_project_transactions(project_path)
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
    if (project_path / TRANSACTION_ROOT).exists():
        recover_project_transactions(project_path)
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


def _json_document(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def _remove_empty_creating_root() -> None:
    try:
        (OUTPUT_DIR / ".creating").rmdir()
    except OSError:
        pass
