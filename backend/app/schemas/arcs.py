from typing import Literal

from pydantic import BaseModel


ArcReviewStatus = Literal["not_required", "awaiting_review", "approved"]


class CurrentArcState(BaseModel):
    arc_id: str
    status: str = "planned"
    plan_path: str
    human_review: ArcReviewStatus = "not_required"
    approved_at: str | None = None
    target_chapter_count: int = 3
    completed_chapter_ids: list[str] = []
    completed_at: str | None = None


class CurrentArcApprovalResponse(BaseModel):
    arc: CurrentArcState
    run_status: str
