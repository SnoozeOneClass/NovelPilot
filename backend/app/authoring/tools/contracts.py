from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ToolInputBase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EmptyInput(ToolInputBase):
    pass


class ReadChapterInput(ToolInputBase):
    chapter_number: int = Field(ge=1)


class SaveBookInput(ToolInputBase):
    title: str = Field(min_length=1, max_length=200)


class SaveFoundationInput(ToolInputBase):
    premise: str = Field(min_length=1)
    compass: str = Field(min_length=1)
    characters: list[dict[str, str]] = Field(min_length=1)
    world: dict[str, str]
    outline: list[str] = Field(min_length=1)
    planned_through: int = Field(ge=1)


class AuditFoundationInput(ToolInputBase):
    passed: bool
    issues: list[str] = Field(default_factory=list)


class ReviseOutlineInput(ToolInputBase):
    planned_through: int = Field(ge=1)
    outline_extension: list[str] = Field(min_length=1)


class ResolveOutlineFeedbackInput(ToolInputBase):
    resolutions: list[str] = Field(min_length=1)


class CompleteBookInput(ToolInputBase):
    audit_passed: bool


class PlanChapterInput(ToolInputBase):
    chapter_number: int = Field(ge=1)
    plan: str = Field(min_length=1)


class DraftChapterInput(ToolInputBase):
    chapter_number: int = Field(ge=1)
    content: str = Field(min_length=1)


class EditChapterInput(DraftChapterInput):
    pass


class CheckConsistencyInput(ToolInputBase):
    chapter_number: int = Field(ge=1)
    passed: bool
    issues: list[str] = Field(default_factory=list)


class ChapterFactsInput(ToolInputBase):
    summary: str = Field(min_length=1)
    character_changes: dict[str, str] = Field(default_factory=dict)
    timeline_changes: list[str] = Field(default_factory=list)
    relationship_changes: list[str] = Field(default_factory=list)
    open_threads: list[str] = Field(default_factory=list)
    resolved_threads: list[str] = Field(default_factory=list)
    state_changes: dict[str, str] = Field(default_factory=dict)
    context_seen: bool | None = None


class CommitChapterInput(ToolInputBase):
    chapter_number: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1)
    facts: ChapterFactsInput


class ReviewDimensions(ToolInputBase):
    causality: int = Field(ge=1, le=5)
    character: int = Field(ge=1, le=5)
    pacing: int = Field(ge=1, le=5)
    continuity: int = Field(ge=1, le=5)
    stakes: int = Field(ge=1, le=5)
    prose: int = Field(ge=1, le=5)
    payoff: int = Field(ge=1, le=5)


class SaveReviewInput(ToolInputBase):
    boundary: int = Field(ge=1)
    verdict: Literal["accept", "polish", "rewrite"]
    dimensions: ReviewDimensions
    evidence: list[str]
    chapters: list[int] = Field(default_factory=list)

    @field_validator("evidence")
    @classmethod
    def short_evidence(cls, value: list[str]) -> list[str]:
        if any(not item.strip() or len(item) > 240 for item in value):
            raise ValueError("review evidence must contain non-empty excerpts up to 240 characters")
        return value

    @model_validator(mode="after")
    def verdict_targets(self) -> SaveReviewInput:
        if self.verdict == "accept" and self.chapters:
            raise ValueError("accept verdict cannot enqueue chapter rewrites")
        if self.verdict != "accept" and not self.chapters:
            raise ValueError("polish/rewrite verdict requires chapter targets")
        return self


class SaveSummaryInput(ToolInputBase):
    boundary: int = Field(ge=1)
    summary: str = Field(min_length=1)
    character_state: dict[str, str] = Field(default_factory=dict)


type ToolInput = (
    EmptyInput
    | ReadChapterInput
    | SaveBookInput
    | SaveFoundationInput
    | AuditFoundationInput
    | ReviseOutlineInput
    | ResolveOutlineFeedbackInput
    | CompleteBookInput
    | PlanChapterInput
    | DraftChapterInput
    | EditChapterInput
    | CheckConsistencyInput
    | CommitChapterInput
    | SaveReviewInput
    | SaveSummaryInput
)

TOOL_INPUT_MODELS: dict[str, type[ToolInputBase]] = {
    "novel_context": EmptyInput,
    "read_chapter": ReadChapterInput,
    "save_book": SaveBookInput,
    "save_foundation": SaveFoundationInput,
    "audit_foundation": AuditFoundationInput,
    "revise_outline": ReviseOutlineInput,
    "resolve_outline_feedback": ResolveOutlineFeedbackInput,
    "complete_book": CompleteBookInput,
    "plan_chapter": PlanChapterInput,
    "draft_chapter": DraftChapterInput,
    "edit_chapter": EditChapterInput,
    "check_consistency": CheckConsistencyInput,
    "commit_chapter": CommitChapterInput,
    "save_review": SaveReviewInput,
    "save_arc_summary": SaveSummaryInput,
    "save_volume_summary": SaveSummaryInput,
}


def validate_tool_input(tool_name: str, value: ToolInput | dict[str, Any] | None) -> ToolInputBase:
    try:
        model = TOOL_INPUT_MODELS[tool_name]
    except KeyError as error:
        raise ValueError(f"unknown Tool {tool_name}") from error
    if isinstance(value, BaseModel):
        return model.model_validate(value.model_dump(mode="python"))
    return model.model_validate(value or {})
