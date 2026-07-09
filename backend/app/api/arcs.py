from pathlib import Path

from fastapi import APIRouter, HTTPException

from app.schemas.arcs import CurrentArcApprovalResponse, CurrentArcState
from app.schemas.events import HarnessEvent
from app.storage import arcs as arc_storage
from app.storage.events import append_event
from app.storage.projects import get_active_project_path, read_project_metadata

router = APIRouter()


def _active_project_or_404() -> Path:
    project_path = get_active_project_path()
    if project_path is None:
        raise HTTPException(status_code=404, detail="No active project.")
    return project_path


@router.get("/current", response_model=CurrentArcState | None)
def get_current_arc() -> CurrentArcState | None:
    project_path = _active_project_or_404()
    return arc_storage.read_current_arc_state(project_path)


@router.post("/current/approve", response_model=CurrentArcApprovalResponse)
def approve_current_arc() -> CurrentArcApprovalResponse:
    project_path = _active_project_or_404()
    try:
        arc = arc_storage.approve_current_arc(project_path)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    metadata = read_project_metadata(project_path)
    append_event(
        project_path,
        HarnessEvent(
            project_id=metadata.project_id,
            kind="story_arc_approved",
            loop_layer="story_arc",
            atomic_action="approve_current_arc",
            status="completed",
            artifact_path=arc.plan_path,
            routing_decision="continue",
            message=f"{arc.arc_id} approved for chapter writing.",
            payload={"arc_id": arc.arc_id},
        ),
    )
    return CurrentArcApprovalResponse(arc=arc, run_status=metadata.run_status)
