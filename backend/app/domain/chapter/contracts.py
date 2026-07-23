from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

ChapterComponent = Literal[
    "plan",
    "draft",
    "observations",
    "repair_prose",
    "repair_observations",
]
ChapterReviewDecision = Literal[
    "pass",
    "local_repair",
    "escalate_to_arc",
    "escalate_to_book",
    "needs_user",
]


class CreateChapterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    book_id: str
    arc_id: str
    expected_book_baseline_id: str
    expected_arc_baseline_id: str
    expected_canon_baseline_id: str


class CreateChapterResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    chapter_id: str
    workspace_id: str
    book_ordinal: int = Field(ge=1)
    arc_ordinal: int = Field(ge=1)
    workspace_lock_version: int = Field(ge=1)


class RebaseStaleChapterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    book_id: str
    arc_id: str
    chapter_id: str
    expected_workspace_lock_version: int = Field(ge=1)
    expected_book_baseline_id: str
    expected_arc_baseline_id: str
    expected_chapter_baseline_id: str | None
    expected_canon_baseline_id: str


class RebaseStaleChapterResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    chapter_id: str
    workspace_lock_version: int = Field(ge=1)
    base_chapter_baseline_id: str | None
    book_baseline_id: str
    arc_baseline_id: str
    canon_baseline_id: str


class ApplyChapterTaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    chapter_id: str
    task_id: str
    attempt_id: str
    expected_workspace_lock_version: int = Field(ge=1)


class ApplyChapterTaskResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    chapter_id: str
    task_id: str
    component: ChapterComponent
    delivery: Literal["applied", "discarded_stale"]
    workspace_lock_version: int = Field(ge=1)


class SubmitChapterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    chapter_id: str
    expected_workspace_lock_version: int = Field(ge=1)


class SubmitChapterResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    chapter_id: str
    submission_id: str
    content_fingerprint: str


class RecordChapterReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    chapter_id: str
    submission_id: str
    evaluator_task_id: str
    evaluator_attempt_id: str
    rubric_id: str
    rubric_version: int = Field(ge=1)
    deterministic_precheck: dict[str, object]


class RecordChapterReviewResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    chapter_id: str
    submission_id: str
    review_id: str
    decision: ChapterReviewDecision


class CommitChapterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    chapter_id: str
    submission_id: str
    review_id: str
    expected_current_chapter_baseline_id: str | None = None
    expected_canon_baseline_id: str


class CommitChapterResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    chapter_id: str
    chapter_baseline_id: str
    chapter_baseline_version: int = Field(ge=1)
    canon_before_id: str
    canon_after_id: str
    canon_changed: bool
    arc_completed: bool


class ChapterTextView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    chapter_id: str
    chapter_title: str
    prose: str

    @field_validator("chapter_title", "prose")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Committed Chapter title and prose must be non-blank.")
        return value
