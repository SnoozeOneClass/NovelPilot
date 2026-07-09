from typing import Literal

from pydantic import BaseModel, Field


GateStatus = Literal["passed", "pending", "failed"]
LiteraryReviewDecision = Literal["approved", "rejected"]


class CompletionGate(BaseModel):
    id: str
    status: GateStatus
    message: str
    evidence: list[str] = Field(default_factory=list)


class ProjectCompletionAudit(BaseModel):
    status: GateStatus
    gates: list[CompletionGate] = Field(default_factory=list)


class LiteraryReviewRequest(BaseModel):
    decision: LiteraryReviewDecision
    reviewer: str = Field(default="manual reviewer", min_length=1)
    chapter_assessment: str = Field(min_length=1)
    state_patch_assessment: str = Field(min_length=1)
    notes: str = ""


class LiteraryReviewRecord(BaseModel):
    schema_version: int = 1
    decision: LiteraryReviewDecision
    reviewer: str
    reviewed_at: str
    chapter_assessment: str
    state_patch_assessment: str
    notes: str = ""
    smoke_report: str
    reviewed_artifacts: dict[str, str]
    literary_review_json: str
    literary_review_markdown: str
