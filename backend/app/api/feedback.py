from fastapi import APIRouter, HTTPException

from app.schemas.events import HarnessEvent, UserFeedbackRequest
from app.storage.events import append_event
from app.storage.projects import (
    ProjectReadOnlyError,
    ensure_creative_mutation_allowed,
    get_active_project_path,
    read_project_metadata,
)

router = APIRouter()


@router.post("")
def submit_feedback(request: UserFeedbackRequest) -> dict[str, bool]:
    project_path = get_active_project_path()
    if project_path is None:
        raise HTTPException(status_code=404, detail="No active project.")
    try:
        ensure_creative_mutation_allowed(project_path)
    except ProjectReadOnlyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    metadata = read_project_metadata(project_path)
    append_event(
        project_path,
        HarnessEvent(
            project_id=metadata.project_id,
            kind="user_feedback",
            loop_layer="system",
            status="completed",
            message="User feedback recorded for the next safe checkpoint.",
            payload={"feedback": request.message},
        ),
    )
    return {"recorded": True}

