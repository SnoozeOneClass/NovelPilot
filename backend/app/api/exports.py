from fastapi import APIRouter, HTTPException

from app.schemas.events import HarnessEvent
from app.storage.events import append_event
from app.storage.export import export_manuscript
from app.storage.projects import get_active_project_path, read_project_metadata

router = APIRouter()


@router.post("/manuscript")
def export_current_manuscript() -> dict[str, str]:
    project_path = get_active_project_path()
    if project_path is None:
        raise HTTPException(status_code=404, detail="No active project.")
    metadata = read_project_metadata(project_path)
    manuscript_path = export_manuscript(project_path)
    artifact_path = str(manuscript_path.relative_to(project_path)).replace("\\", "/")
    append_event(
        project_path,
        HarnessEvent(
            project_id=metadata.project_id,
            kind="export_completed",
            loop_layer="system",
            status="completed",
            artifact_path=artifact_path,
            message="Manuscript export completed.",
        ),
    )
    return {"artifact_path": artifact_path}
