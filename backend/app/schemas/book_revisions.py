from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.setup import BookDirectionCandidate


BookRevisionSourceLoop = Literal["story_arc", "chapter"]
BookRevisionStatus = Literal["awaiting_approval", "approved"]
BookRevisionDownstreamStatus = Literal["pending", "not_required", "completed"]


class BookRevisionState(BaseModel):
    schema_version: int = 1
    revision_id: str
    route_id: str
    status: BookRevisionStatus = "awaiting_approval"
    downstream_status: BookRevisionDownstreamStatus = "pending"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    approved_at: datetime | None = None
    base_book_version: int = Field(ge=1)
    target_book_version: int = Field(ge=2)
    source_loop: BookRevisionSourceLoop
    source_artifact: str
    source_candidate_run_id: str | None = None
    summary: str
    contract_field: str
    committed_evidence_locator: str
    impossibility_reason: str
    candidate: BookDirectionCandidate
    evaluation_id: str
    evaluation_path: str
    review_path: str
    verification_path: str
    downstream_artifact_paths: list[str] = Field(default_factory=list)


class BookRevisionApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision_id: str = Field(min_length=1, max_length=200)
    expected_base_book_version: int = Field(ge=1)
