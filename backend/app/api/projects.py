from fastapi import APIRouter, HTTPException

from app.schemas.projects import CreateProjectRequest, OpenProjectRequest, ProjectSummary
from app.storage import projects as project_storage

router = APIRouter()


@router.get("", response_model=list[ProjectSummary])
def list_projects() -> list[ProjectSummary]:
    return project_storage.list_projects()


@router.post("", response_model=ProjectSummary)
def create_project(request: CreateProjectRequest) -> ProjectSummary:
    try:
        return project_storage.create_project(request)
    except project_storage.ActiveProjectBusyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (FileExistsError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/open", response_model=ProjectSummary)
def open_project(request: OpenProjectRequest) -> ProjectSummary:
    try:
        return project_storage.open_project(request.name)
    except project_storage.ActiveProjectBusyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/close")
def close_project() -> dict[str, bool]:
    try:
        project_storage.close_active_project()
    except project_storage.ActiveProjectBusyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"closed": True}


@router.get("/active", response_model=ProjectSummary | None)
def get_active_project() -> ProjectSummary | None:
    return project_storage.get_active_project()
