from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.experiments import ExperimentFixtureTransition


ArcReviewStatus = Literal["not_required", "awaiting_review", "approved"]
MIN_ARC_CHAPTER_COUNT = 1
MAX_ARC_CHAPTER_COUNT = 30


class StoryArcPlanProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_markdown: str = Field(min_length=1, max_length=30_000)
    target_chapter_count: int = Field(
        ge=MIN_ARC_CHAPTER_COUNT,
        le=MAX_ARC_CHAPTER_COUNT,
        strict=True,
    )


class CurrentArcApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_chapter_count: int = Field(
        ge=MIN_ARC_CHAPTER_COUNT,
        le=MAX_ARC_CHAPTER_COUNT,
        strict=True,
    )


class CurrentArcState(BaseModel):
    arc_id: str
    status: str = "planned"
    plan_path: str
    human_review: ArcReviewStatus = "not_required"
    approved_at: str | None = None
    recommended_target_chapter_count: int = Field(
        default=3,
        ge=MIN_ARC_CHAPTER_COUNT,
        le=MAX_ARC_CHAPTER_COUNT,
    )
    target_chapter_count: int = Field(
        default=3,
        ge=MIN_ARC_CHAPTER_COUNT,
        le=MAX_ARC_CHAPTER_COUNT,
    )
    completed_chapter_ids: list[str] = Field(default_factory=list)
    completed_at: str | None = None


class CurrentArcApprovalResponse(BaseModel):
    arc: CurrentArcState
    run_status: str
    fixture_transition: ExperimentFixtureTransition | None = None
