from fastapi import APIRouter, HTTPException

from app.schemas.completion import (
    LiteraryReviewRecord,
    LiteraryReviewRequest,
    ProjectCompletionAudit,
)
from app.storage.completion import audit_project_completion, record_literary_review
from app.storage.projects import (
    ProjectReadOnlyError,
    ensure_creative_mutation_allowed,
    get_active_project_path,
)

router = APIRouter()


@router.get("/audit", response_model=ProjectCompletionAudit)
def get_completion_audit() -> ProjectCompletionAudit:
    project_path = get_active_project_path()
    if project_path is None:
        raise HTTPException(status_code=404, detail="No active project.")
    return audit_project_completion(project_path)


@router.post("/literary-review", response_model=LiteraryReviewRecord)
def create_literary_review(request: LiteraryReviewRequest) -> LiteraryReviewRecord:
    project_path = get_active_project_path()
    if project_path is None:
        raise HTTPException(status_code=404, detail="No active project.")
    try:
        ensure_creative_mutation_allowed(project_path)
    except ProjectReadOnlyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    try:
        return record_literary_review(project_path, request)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
