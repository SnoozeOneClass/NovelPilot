from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from fastapi import APIRouter, HTTPException

from app.core.paths import resolve_project_path
from app.harness.run_control import (
    active_project_transition_lock,
    begin_active_runner,
    end_active_runner,
)
from app.schemas.projects import (
    CreateProjectRequest,
    OpenProjectRequest,
    ProjectSummary,
    UpdateAgentPolicyRequest,
    UpdateOperationModeRequest,
)
from app.storage import projects as project_storage
from app.storage import profiles as profile_storage
from app.storage import setup as setup_storage

router = APIRouter()


@router.get("", response_model=list[ProjectSummary])
def list_projects() -> list[ProjectSummary]:
    return project_storage.list_projects()


@router.post("", response_model=ProjectSummary)
def create_project(request: CreateProjectRequest) -> ProjectSummary:
    try:
        with _active_project_change():
            return project_storage.create_project(request)
    except project_storage.ActiveProjectBusyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (FileExistsError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/open", response_model=ProjectSummary)
def open_project(request: OpenProjectRequest) -> ProjectSummary:
    try:
        target_path = resolve_project_path(request.name)
        with _active_project_change(target_path=target_path):
            return project_storage.open_project(request.name)
    except project_storage.ActiveProjectBusyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/close")
def close_project() -> dict[str, bool]:
    try:
        with _active_project_change():
            project_storage.close_active_project()
    except project_storage.ActiveProjectBusyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"closed": True}


@router.get("/active", response_model=ProjectSummary | None)
def get_active_project() -> ProjectSummary | None:
    return project_storage.get_active_project()


@router.patch("/active/mode", response_model=ProjectSummary)
def update_operation_mode(request: UpdateOperationModeRequest) -> ProjectSummary:
    # Storage commits the operation_mode_changed audit outbox record atomically with metadata.
    with active_project_transition_lock():
        project_path = project_storage.get_active_project_path()
        if project_path is None:
            raise HTTPException(status_code=404, detail="No active project.")
        if not begin_active_runner(project_path):
            raise HTTPException(
                status_code=409,
                detail=(
                    "A harness runner is active; request pause and wait for a safe checkpoint "
                    "before changing operation mode."
                ),
            )

    try:
        try:
            summary = project_storage.update_operation_mode(
                project_path,
                request.operation_mode,
            )
        except project_storage.ActiveProjectBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        try:
            setup_storage.flush_pending_setup_events(project_path)
        except (OSError, ValueError):
            # The mode transaction durably stored the event in the outbox. A later
            # setup read or mode request will retry the append without losing audit data.
            pass
        return summary
    finally:
        end_active_runner(project_path)


@router.patch("/active/agent-policy", response_model=ProjectSummary)
def update_agent_policy(request: UpdateAgentPolicyRequest) -> ProjectSummary:
    with active_project_transition_lock():
        project_path = project_storage.get_active_project_path()
        if project_path is None:
            raise HTTPException(status_code=404, detail="No active project.")
        if not begin_active_runner(project_path):
            raise HTTPException(
                status_code=409,
                detail="A harness runner is active; wait for a safe checkpoint.",
            )

    try:
        _validate_policy_profiles(request)
        try:
            return project_storage.update_agent_policy(project_path, request.agent_policy)
        except project_storage.ActiveProjectBusyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    finally:
        end_active_runner(project_path)


def _validate_policy_profiles(request: UpdateAgentPolicyRequest) -> None:
    bindings = {
        request.agent_policy.book_profile_id,
        request.agent_policy.story_arc_profile_id,
        request.agent_policy.chapter_profile_id,
        request.agent_policy.evaluator_profile_id,
    }
    for profile_id in sorted(item for item in bindings if item is not None):
        try:
            profile = profile_storage.get_profile(profile_id)
        except KeyError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Agent policy references unknown profile: {profile_id}",
            ) from exc
        if not profile.enabled:
            raise HTTPException(
                status_code=400,
                detail=f"Agent policy references disabled profile: {profile_id}",
            )
        try:
            profile_storage.require_harness_capabilities(profile)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc


@contextmanager
def _active_project_change(
    *,
    target_path: Path | None = None,
) -> Iterator[None]:
    with active_project_transition_lock():
        current_path = project_storage.get_active_project_path()
        lease_paths: list[Path] = []
        try:
            for path in [current_path, target_path]:
                if path is None or any(
                    existing.resolve() == path.resolve() for existing in lease_paths
                ):
                    continue
                if not begin_active_runner(path):
                    raise project_storage.ActiveProjectBusyError(
                        "Cannot change the active project while a harness runner is active."
                    )
                lease_paths.append(path)
            yield
        finally:
            for path in reversed(lease_paths):
                end_active_runner(path)
