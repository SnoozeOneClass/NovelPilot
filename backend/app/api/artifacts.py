from fastapi import APIRouter, HTTPException, Query

from app.core.paths import resolve_artifact_path
from app.schemas.artifacts import ArtifactSummary
from app.storage.artifacts import (
    is_internal_artifact_path,
    list_project_artifacts,
    summarize_project_artifacts,
)
from app.storage.projects import get_active_project_path
from app.storage.text_files import read_text_file

router = APIRouter()


@router.get("")
def list_artifacts() -> list[str]:
    project_path = get_active_project_path()
    if project_path is None:
        raise HTTPException(status_code=404, detail="No active project.")
    return list_project_artifacts(project_path)


@router.get("/summary", response_model=list[ArtifactSummary])
def list_artifact_summaries() -> list[ArtifactSummary]:
    project_path = get_active_project_path()
    if project_path is None:
        raise HTTPException(status_code=404, detail="No active project.")
    return summarize_project_artifacts(project_path)


@router.get("/content")
def read_artifact_content(path: str = Query(min_length=1)) -> dict[str, str]:
    project_path = get_active_project_path()
    if project_path is None:
        raise HTTPException(status_code=404, detail="No active project.")
    try:
        artifact_path = resolve_artifact_path(project_path, path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if (
        not artifact_path.exists()
        or not artifact_path.is_file()
        or is_internal_artifact_path(artifact_path)
    ):
        raise HTTPException(status_code=404, detail="Artifact not found.")
    return {
        "path": artifact_path.relative_to(project_path).as_posix(),
        "content": read_text_file(artifact_path),
    }
