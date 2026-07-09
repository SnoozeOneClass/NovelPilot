from fastapi import APIRouter, HTTPException

from app.harness.run_control import has_active_runner
from app.schemas.readiness import ProjectReadiness
from app.storage.projects import get_active_project_path
from app.storage.readiness import build_project_readiness

router = APIRouter()


@router.get("", response_model=ProjectReadiness)
def get_readiness() -> ProjectReadiness:
    project_path = get_active_project_path()
    if project_path is None:
        raise HTTPException(status_code=404, detail="No active project.")
    return build_project_readiness(
        project_path,
        active_runner=has_active_runner(project_path),
    )
