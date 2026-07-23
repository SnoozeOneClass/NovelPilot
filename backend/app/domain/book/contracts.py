from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class BookSuggestion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    label: str
    message: str
    rationale: str = ""
    recommended: bool = False
    action: Literal["answer", "select_title"] = "answer"
    value: str | None = None

    @model_validator(mode="after")
    def _action_value(self) -> BookSuggestion:
        if self.action == "select_title" and not (self.value or "").strip():
            raise ValueError("A title suggestion requires a title value.")
        if self.action == "answer" and self.value is not None:
            raise ValueError("An ordinary suggestion cannot carry a control value.")
        return self


class BookSupersededDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    turn: int = Field(ge=1)
    decision: str
    replacement: str | None
    reason: str
    user_evidence: str


class BookDiscussionState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_id: Literal["book-discussion-state-v1"] = "book-discussion-state-v1"
    turn_count: int = Field(ge=0)
    direction_draft: str
    discussion_summary: str
    confirmed_decisions: list[str] = Field(default_factory=list)
    superseded_decisions: list[BookSupersededDecision] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    contradictions: list[str] = Field(default_factory=list)
    selected_title: str | None = None
    selected_title_source: Literal["recommended", "custom"] | None = None
    question: str | None = None
    suggestions: list[BookSuggestion] = Field(default_factory=list)
    readiness_status: Literal["awaiting_agent", "continue", "ready"]
    readiness_reason: str

    @model_validator(mode="after")
    def _discussion_state_boundary(self) -> BookDiscussionState:
        if (self.selected_title is None) != (self.selected_title_source is None):
            raise ValueError("A selected title and its source must be persisted together.")
        if self.readiness_status == "awaiting_agent":
            if self.question is not None or self.suggestions:
                raise ValueError("An awaiting-agent state cannot expose stale suggestions.")
            return self
        if self.readiness_status == "ready":
            if self.selected_title is None:
                raise ValueError("A ready Book discussion requires a selected title.")
            if self.question is not None or self.suggestions:
                raise ValueError("A ready Book discussion cannot expose another question.")
            return self
        if self.question is None or not 2 <= len(self.suggestions) <= 3:
            raise ValueError("A continuing Book discussion requires one question and 2-3 options.")
        return self


class BookTranscriptMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sequence: int = Field(ge=1)
    role: Literal["user", "assistant"]
    content: str

    @field_validator("content")
    @classmethod
    def _message_non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Book transcript messages must be non-blank.")
        return value


class BookTranscript(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_id: Literal["book-transcript-v1"] = "book-transcript-v1"
    messages: list[BookTranscriptMessage]


class RecordBookUserInputRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    book_id: str
    expected_workspace_lock_version: int = Field(ge=1)
    message: str
    suggestion_id: str | None = None

    @field_validator("message")
    @classmethod
    def _input_non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Book discussion input must be non-blank.")
        return value.strip()


class RecordBookUserInputResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    book_id: str
    workspace_lock_version: int
    selected_title: str | None


class ApplyBookDiscussionTaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    book_id: str
    task_id: str
    attempt_id: str
    expected_workspace_lock_version: int = Field(ge=1)


class ApplyBookDiscussionTaskResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    book_id: str
    task_id: str
    delivery: Literal["applied", "discarded_stale"]
    workspace_lock_version: int = Field(ge=1)
    readiness_status: Literal["continue", "ready"]
    selected_title: str | None


class ApplyBookCandidateTaskRequest(ApplyBookDiscussionTaskRequest):
    pass


class ApplyBookCandidateTaskResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    book_id: str
    task_id: str
    delivery: Literal["applied", "discarded_stale"]
    workspace_lock_version: int = Field(ge=1)


class CompletionContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    minimum_chapter_count: int = Field(ge=1)
    maximum_chapter_count: int = Field(ge=1)
    completion_requirements: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _ordered_range(self) -> CompletionContract:
        if self.maximum_chapter_count < self.minimum_chapter_count:
            raise ValueError("maximum_chapter_count must be >= minimum_chapter_count")
        return self


class BookCandidatePack(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    direction: str
    constraints: dict[str, object]
    selected_title: str
    rolling_plan: dict[str, object]
    completion_contract: CompletionContract

    @field_validator("direction")
    @classmethod
    def _direction_non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Book direction must be non-blank")
        return value

    @field_validator("selected_title")
    @classmethod
    def _title_non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Book selected title must be non-blank")
        return value.strip()


BookRepairComponent = Literal[
    "direction",
    "constraints",
    "rolling_plan",
    "completion_contract",
]


class BookRepairContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    authorized_components: list[BookRepairComponent] = Field(min_length=1)
    issue_summary: str

    @field_validator("authorized_components")
    @classmethod
    def _unique_components(
        cls,
        value: list[BookRepairComponent],
    ) -> list[BookRepairComponent]:
        if len(value) != len(set(value)):
            raise ValueError("Book repair components must be unique")
        return value

    @field_validator("issue_summary")
    @classmethod
    def _issue_non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Book repair issue summary must be non-blank")
        return value


class BookEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    decision: Literal["pass", "local_repair", "needs_user"]
    summary: str
    findings: list[dict[str, object]] = Field(default_factory=list)
    repair_contract: BookRepairContract | None = None

    @field_validator("summary")
    @classmethod
    def _summary_non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Evaluation summary must be non-blank")
        return value

    @model_validator(mode="after")
    def _repair_shape(self) -> BookEvaluation:
        if (self.decision == "local_repair") != (self.repair_contract is not None):
            raise ValueError("Only local_repair carries a repair_contract")
        return self


class ApplyBookCandidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    book_id: str
    expected_workspace_lock_version: int = Field(ge=1)
    candidate: BookCandidatePack
    selected_title_source: Literal["recommended", "custom"] = "custom"


class ApplyBookCandidateResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    book_id: str
    workspace_id: str
    workspace_lock_version: int


class SubmitBookRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    book_id: str
    expected_workspace_lock_version: int = Field(ge=1)


class SubmitBookResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    book_id: str
    submission_id: str
    content_fingerprint: str


class RecordBookReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    book_id: str
    submission_id: str
    evaluator_task_id: str
    evaluator_attempt_id: str
    rubric_id: str
    rubric_version: int = Field(ge=1)
    deterministic_precheck: dict[str, object]


class RecordBookReviewResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    book_id: str
    submission_id: str
    review_id: str
    decision: Literal["pass", "local_repair", "needs_user"]


class ApproveBookRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    book_id: str
    submission_id: str
    review_id: str
    expected_current_baseline_id: str | None = None


class ApproveBookResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    book_id: str
    baseline_id: str
    baseline_version: int
    approved_title: str
