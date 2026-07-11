from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator, model_validator


SetupPhase = Literal["discussing", "review_ready", "review_blocked", "approved"]
SetupReadinessStatus = Literal["continue", "ready"]
SetupReviewSeverity = Literal["warning", "blocking"]
TitleSelectionSource = Literal["recommended", "custom"]


class SetupMessage(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    turn: int = Field(ge=1)
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    profile_id: str | None = None
    model_snapshot: str | None = None
    migrated: bool = False


class SetupSuggestion(BaseModel):
    id: str
    label: str
    message: str


class SetupReadinessSignal(BaseModel):
    status: SetupReadinessStatus = "continue"
    reason: str = "等待用户开始全书方向讨论。"


class SupersededDecision(BaseModel):
    turn: int = Field(ge=1)
    decision: str
    replacement: str | None = None
    reason: str
    user_evidence: str


class ConfirmedDecisionCoverage(BaseModel):
    decision: str
    candidate_evidence: str


class BookDirectionConstraints(BaseModel):
    confirmed: list[str] = Field(default_factory=list)
    must_preserve: list[str] = Field(default_factory=list)
    must_avoid: list[str] = Field(default_factory=list)
    creative_freedoms: list[str] = Field(default_factory=list)
    open_decisions: list[str] = Field(default_factory=list)


class BookDirectionReviewIssue(BaseModel):
    severity: SetupReviewSeverity
    kind: str
    message: str
    evidence: list[str] = Field(default_factory=list)
    suggested_question: str | None = None


class BookDirectionReview(BaseModel):
    status: Literal["passed", "blocked"]
    summary: str
    issues: list[BookDirectionReviewIssue] = Field(default_factory=list)
    signals: list[str] = Field(default_factory=list)

    @property
    def commit_allowed(self) -> bool:
        return self.status == "passed" and not any(
            issue.severity == "blocking" for issue in self.issues
        )


class BookTitleSuggestion(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    rationale: str = Field(min_length=1, max_length=1_000)

    @field_validator("title", "rationale")
    @classmethod
    def strip_non_blank_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Title suggestions must not contain blank text.")
        return stripped


class BookDirectionCandidate(BaseModel):
    revision: int = Field(ge=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    direction_markdown: str
    constraints: BookDirectionConstraints
    confirmed_decision_coverage: list[ConfirmedDecisionCoverage] = Field(
        default_factory=list
    )
    recommended_titles: list[BookTitleSuggestion] = Field(min_length=3, max_length=5)
    rolling_plan_markdown: str
    review: BookDirectionReview
    direction_path: str
    constraints_path: str
    title_suggestions_path: str
    rolling_plan_path: str
    verification_path: str
    profile_id: str
    model_snapshot: str
    review_model_snapshot: str

    @model_validator(mode="after")
    def recommended_titles_must_be_unique(self) -> "BookDirectionCandidate":
        normalized = [item.title.casefold() for item in self.recommended_titles]
        if len(normalized) != len(set(normalized)):
            raise ValueError("Recommended book titles must be unique.")
        return self

    @property
    def approval_allowed(self) -> bool:
        return self.review.commit_allowed


class SetupStateDocument(BaseModel):
    schema_version: int = 2
    revision: int = Field(default=1, ge=1)
    phase: SetupPhase = "discussing"
    approved: bool = False
    approved_at: datetime | None = None
    approved_title: str | None = None
    title_selection_source: TitleSelectionSource | None = None
    migrated_from_schema_version: int | None = None
    turn_count: int = Field(default=0, ge=0)
    candidate_revision_counter: int = Field(default=0, ge=0)
    messages: list[SetupMessage] = Field(default_factory=list)
    direction_draft: str = ""
    discussion_summary: str = ""
    confirmed_decisions: list[str] = Field(default_factory=list)
    superseded_decisions: list[SupersededDecision] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    contradictions: list[str] = Field(default_factory=list)
    suggestions: list[SetupSuggestion] = Field(default_factory=list)
    readiness: SetupReadinessSignal = Field(default_factory=SetupReadinessSignal)
    candidate: BookDirectionCandidate | None = None
    direction_draft_version_path: str | None = None
    discussion_state_version_path: str | None = None
    discussion_transcript_version_path: str | None = None
    last_context_snapshot_path: str | None = None
    last_profile_id: str | None = None
    last_model_snapshot: str | None = None


class SetupTurnRequest(BaseModel):
    message: str = Field(min_length=1, max_length=32_000)

    @field_validator("message")
    @classmethod
    def message_must_not_be_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Book discussion message must not be blank.")
        return stripped


class SetupApprovalRequest(BaseModel):
    candidate_revision: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=200)

    @field_validator("title")
    @classmethod
    def title_must_not_be_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Approved book title must not be blank.")
        return stripped
