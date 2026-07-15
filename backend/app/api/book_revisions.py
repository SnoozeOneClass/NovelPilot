from pathlib import Path

from fastapi import APIRouter, HTTPException

from app.schemas.book_revisions import BookRevisionApprovalRequest, BookRevisionState
from app.schemas.events import HarnessEvent
from app.storage import book_revisions as book_revision_storage
from app.storage.events import append_event
from app.storage.projects import get_active_project_path, read_project_metadata


router = APIRouter()


@router.get("/pending", response_model=BookRevisionState | None)
def get_pending_book_revision() -> BookRevisionState | None:
    return book_revision_storage.read_pending_book_revision(_active_project_or_404())


@router.post("/approve", response_model=BookRevisionState)
def approve_book_revision(
    request: BookRevisionApprovalRequest,
) -> BookRevisionState:
    project_path = _active_project_or_404()
    try:
        state = book_revision_storage.approve_book_revision(project_path, request)
    except book_revision_storage.BookRevisionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    metadata = read_project_metadata(project_path)
    append_event(
        project_path,
        HarnessEvent(
            project_id=metadata.project_id,
            kind="book_revision_approved",
            loop_layer="book",
            atomic_action="approve_book_revision",
            status="completed",
            artifact_path=state.candidate.direction_path,
            routing_decision=(
                "revise_active_story_arc"
                if state.downstream_status == "pending"
                else "continue"
            ),
            message=(
                "User explicitly approved the evaluated Book revision; future contract "
                "artifacts were promoted."
            ),
            payload={
                "revision_id": state.revision_id,
                "base_book_version": state.base_book_version,
                "target_book_version": state.target_book_version,
                "evidence_paths": [
                    state.candidate.direction_path,
                    state.review_path,
                    state.verification_path,
                    f"book/revisions/{state.revision_id}/state.json",
                ],
            },
        ),
    )
    return state


def _active_project_or_404() -> Path:
    project_path = get_active_project_path()
    if project_path is None:
        raise HTTPException(status_code=404, detail="No active project.")
    return project_path
