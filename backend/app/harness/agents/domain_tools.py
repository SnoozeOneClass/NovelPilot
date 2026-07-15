import json
import re
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal, TypeVar, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.harness.agents.persistence import activation_relative, json_document
from app.harness.agents.registry import (
    ToolExecutionContext,
    ToolExecutionPlan,
    ToolHandlerError,
    ToolRegistry,
    ToolSpec,
)
from app.harness.agents.shared_tools import register_shared_tools
from app.schemas.artifacts import CandidateObservations
from app.schemas.patches import (
    CandidatePatchOperation,
    CandidateStatePatch,
    PatchEvidence,
)
from app.schemas.setup import (
    BookDirectionConstraints,
    BookTitleSuggestion,
    ConfirmedDecisionCoverage,
    SetupReadinessSignal,
    SetupSuggestion,
    SupersededDecision,
)


_META_PRIORITY_QUESTION_FRAGMENTS = (
    "which issue should we discuss",
    "which topic should we discuss",
    "what should we discuss first",
    "which direction should we confirm",
    "先讨论哪个",
    "先确认哪个",
    "先聊哪个",
)


class BookDiscussionUpdateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=0)
    reply: str = Field(min_length=1, max_length=20_000)
    direction_draft: str = Field(min_length=1, max_length=100_000)
    discussion_summary: str = Field(min_length=1, max_length=20_000)
    confirmed_decisions: list[str] = Field(max_length=200)
    superseded_decisions: list[SupersededDecision] = Field(max_length=200)
    unresolved_questions: list[str] = Field(max_length=100)
    assumptions: list[str] = Field(max_length=100)
    contradictions: list[str] = Field(max_length=100)
    selected_title: str | None = Field(default=None, max_length=200)
    question: str | None = Field(default=None, max_length=600)
    suggestions: list[SetupSuggestion] = Field(max_length=3)
    readiness: SetupReadinessSignal

    @model_validator(mode="before")
    @classmethod
    def validate_raw_suggestion_types(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        suggestions = value.get("suggestions")
        if not isinstance(suggestions, list):
            return value
        for suggestion in suggestions:
            if not isinstance(suggestion, dict):
                continue
            if "recommended" in suggestion and not isinstance(
                suggestion.get("recommended"), bool
            ):
                raise ValueError("Book discussion suggestion.recommended must be a boolean.")
        return value

    @model_validator(mode="after")
    def validate_next_decision(self) -> "BookDiscussionUpdateInput":
        if self.readiness.status == "continue":
            if self.question is None:
                raise ValueError("A continuing Book discussion requires one next question.")
            question = self.question.strip()
            if question.count("?") + question.count("？") != 1 or not question.endswith(
                ("?", "？")
            ):
                raise ValueError("Book discussion must ask exactly one concrete question.")
            if not 2 <= len(self.suggestions) <= 3:
                raise ValueError("Book discussion requires two or three suggestions.")
            if "?" in self.reply or "？" in self.reply:
                raise ValueError("Book discussion reply must not contain another question.")
            normalized_question = question.casefold()
            if any(
                fragment in normalized_question
                for fragment in _META_PRIORITY_QUESTION_FRAGMENTS
            ):
                raise ValueError(
                    "Book discussion must choose the next concrete decision instead of "
                    "delegating topic prioritization to the user."
                )
            for suggestion in self.suggestions:
                if not suggestion.label.strip():
                    raise ValueError("Book discussion suggestion.label must not be blank.")
                if not suggestion.message.strip():
                    raise ValueError("Book discussion suggestion.message must not be blank.")
            labels = [item.label.strip().casefold() for item in self.suggestions]
            messages = [item.message.strip().casefold() for item in self.suggestions]
            if len(labels) != len(set(labels)) or len(messages) != len(set(messages)):
                raise ValueError("Book discussion suggestions must be unique.")
            if sum(item.recommended for item in self.suggestions) != 1:
                raise ValueError(
                    "Book discussion must recommend exactly one answer option."
                )
        elif self.question is not None or self.suggestions:
            raise ValueError("A review-ready Book direction cannot ask another question.")
        elif not (self.selected_title or "").strip():
            raise ValueError(
                "A review-ready Book direction requires the user-confirmed formal title."
            )
        return self


class BookDirectionCandidateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=0)
    candidate_revision: int = Field(ge=1)
    direction_markdown: str = Field(min_length=1, max_length=100_000)
    constraints: BookDirectionConstraints
    confirmed_decision_coverage: list[ConfirmedDecisionCoverage] = Field(max_length=500)
    recommended_titles: list[BookTitleSuggestion] = Field(min_length=3, max_length=5)
    rolling_plan_markdown: str = Field(min_length=1, max_length=50_000)

    @model_validator(mode="after")
    def validate_titles(self) -> "BookDirectionCandidateInput":
        titles = [item.title.casefold() for item in self.recommended_titles]
        if len(titles) != len(set(titles)):
            raise ValueError("Recommended titles must be unique.")
        return self


class StoryArcCandidateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=0)
    intent: Literal["create", "revise"]
    arc_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    plan_markdown: str = Field(min_length=1, max_length=50_000)
    target_chapter_count: int = Field(ge=1, le=30, strict=True)
    change_summary: str = Field(min_length=1, max_length=8_000)


class ChapterPlanCandidateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chapter_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    expected_revision: int = Field(ge=0)
    plan_revision: int = Field(ge=1)
    plan_markdown: str = Field(min_length=1, max_length=50_000)


class WriteChapterDraftInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chapter_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    expected_revision: int = Field(ge=0)
    plan_revision: int = Field(ge=1)
    draft_revision: int = Field(ge=1)
    mode: Literal["write", "append"] = "write"
    content: str = Field(min_length=1, max_length=500_000)


class EditChapterDraftInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chapter_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    expected_revision: int = Field(ge=0)
    draft_revision: int = Field(ge=1)
    next_draft_revision: int = Field(ge=2)
    anchor: str = Field(min_length=1, max_length=20_000)
    replacement: str = Field(max_length=100_000)

    @model_validator(mode="after")
    def validate_revision_delta(self) -> "EditChapterDraftInput":
        if self.next_draft_revision != self.draft_revision + 1:
            raise ValueError("Targeted edit must advance the draft revision by one.")
        return self


class InspectChapterConsistencyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chapter_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    expected_revision: int = Field(ge=0)
    draft_revision: int = Field(ge=1)


class SubmitChapterCandidateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chapter_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    expected_revision: int = Field(ge=0)
    candidate_revision: int = Field(ge=1)
    plan_revision: int = Field(ge=1)
    draft_revision: int = Field(ge=1)
    summary: str = Field(min_length=1, max_length=8_000)
    observations: CandidateObservations
    state_patch: CandidateStatePatch


class ChapterObservationInput(BaseModel):
    """Strict model-facing observation without an open-ended JSON object."""

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, max_length=4_000)
    evidence_quote: str = Field(min_length=1, max_length=4_000)


class ChapterPatchValueFieldInput(BaseModel):
    """One canon field encoded as JSON so the surrounding Tool schema stays closed."""

    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=256)
    json_value: str = Field(
        min_length=1,
        max_length=20_000,
        description=(
            "The canon field value. Plain text is stored as a string; valid JSON literals such "
            "as true, 3, or [\"clue-a\"] preserve their JSON type."
        ),
    )


class ChapterPatchOperationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    op: Literal["upsert", "delete", "append"]
    target_file: Literal[
        "canon/characters.json",
        "canon/relationships.json",
        "canon/world_facts.json",
        "canon/foreshadowing.json",
    ]
    target_id: str = Field(min_length=1, max_length=256)
    expected_version: int = Field(ge=1)
    value_fields: list[ChapterPatchValueFieldInput] = Field(max_length=100)
    evidence_quotes: list[str] = Field(min_length=1, max_length=20)
    rationale: str = Field(min_length=1, max_length=8_000)


class ChapterCandidateObservationsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    events: list[ChapterObservationInput] = Field(max_length=100)
    character_changes: list[ChapterObservationInput] = Field(max_length=100)
    relationship_changes: list[ChapterObservationInput] = Field(max_length=100)
    world_fact_candidates: list[ChapterObservationInput] = Field(max_length=100)
    foreshadowing_candidates: list[ChapterObservationInput] = Field(max_length=100)
    requires_commit: bool


class ChapterCandidateStatePatchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operations: list[ChapterPatchOperationInput] = Field(max_length=100)


class SubmitChapterCandidateToolInput(BaseModel):
    """Closed provider schema converted to the durable chapter candidate models."""

    model_config = ConfigDict(extra="forbid")

    chapter_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    expected_revision: int = Field(ge=0)
    candidate_revision: int = Field(ge=1)
    plan_revision: int = Field(ge=1)
    draft_revision: int = Field(ge=1)
    summary: str = Field(min_length=1, max_length=8_000)
    observations: ChapterCandidateObservationsInput
    state_patch: ChapterCandidateStatePatchInput


class ChapterPatchEvidenceRepairItemInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_index: int = Field(ge=0)
    evidence_quotes: list[str] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def validate_quotes(self) -> "ChapterPatchEvidenceRepairItemInput":
        if any(not quote.strip() for quote in self.evidence_quotes):
            raise ValueError("Patch evidence quotes must not be blank.")
        if len(self.evidence_quotes) != len(set(self.evidence_quotes)):
            raise ValueError("Patch evidence quotes must be unique per operation.")
        return self


class SubmitChapterPatchEvidenceRepairInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chapter_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    expected_revision: int = Field(ge=0)
    repairs: list[ChapterPatchEvidenceRepairItemInput] = Field(
        min_length=1,
        max_length=100,
    )


def build_default_tool_registry() -> ToolRegistry:
    registry = ToolRegistry()
    register_shared_tools(registry)
    register_domain_tools(registry)
    return registry


def register_domain_tools(registry: ToolRegistry) -> None:
    registry.register(
        ToolSpec(
            name="submit_book_discussion_update",
            version=1,
            description=(
                "Submit the complete Book discussion state update and the single next "
                "decision, or mark the direction ready for review."
            ),
            input_model=BookDiscussionUpdateInput,
            allowed_roles=frozenset({"book"}),
            allowed_phases=frozenset({"discussion"}),
            handler=_submit_book_discussion_update,
            read_only=False,
            terminal=True,
        )
    )
    registry.register(
        ToolSpec(
            name="submit_book_direction_candidate",
            version=1,
            description="Submit one versioned Book Direction candidate for evaluation.",
            input_model=BookDirectionCandidateInput,
            allowed_roles=frozenset({"book"}),
            allowed_phases=frozenset({"direction"}),
            handler=_submit_book_direction_candidate,
            read_only=False,
            terminal=True,
        )
    )
    registry.register(
        ToolSpec(
            name="submit_story_arc_candidate",
            version=1,
            description="Submit a create or revise Story Arc candidate for evaluation.",
            input_model=StoryArcCandidateInput,
            allowed_roles=frozenset({"story_arc"}),
            allowed_phases=frozenset({"planning", "revision"}),
            handler=_submit_story_arc_candidate,
            read_only=False,
            terminal=True,
        )
    )
    registry.register(
        ToolSpec(
            name="plan_chapter_candidate",
            version=1,
            description="Write the versioned candidate plan for the owned chapter.",
            input_model=ChapterPlanCandidateInput,
            allowed_roles=frozenset({"chapter"}),
            allowed_phases=frozenset({"chapter"}),
            handler=_plan_chapter_candidate,
            read_only=False,
        )
    )
    registry.register(
        ToolSpec(
            name="write_chapter_draft",
            version=1,
            description=(
                "Write or append visible prose to the quarantined candidate draft. "
                "This Tool never promotes prose."
            ),
            input_model=WriteChapterDraftInput,
            allowed_roles=frozenset({"chapter"}),
            allowed_phases=frozenset({"chapter"}),
            handler=_write_chapter_draft,
            read_only=False,
        )
    )
    registry.register(
        ToolSpec(
            name="edit_chapter_draft",
            version=1,
            description="Replace one exact anchor in the quarantined chapter draft.",
            input_model=EditChapterDraftInput,
            allowed_roles=frozenset({"chapter"}),
            allowed_phases=frozenset({"chapter"}),
            handler=_edit_chapter_draft,
            read_only=False,
        )
    )
    registry.register(
        ToolSpec(
            name="inspect_chapter_consistency",
            version=1,
            description=(
                "Record deterministic draft evidence without making a semantic verdict."
            ),
            input_model=InspectChapterConsistencyInput,
            allowed_roles=frozenset({"chapter"}),
            allowed_phases=frozenset({"chapter"}),
            handler=_inspect_chapter_consistency,
            read_only=False,
        )
    )
    registry.register(
        ToolSpec(
            name="submit_chapter_candidate",
            version=1,
            description=(
                "Bind the quarantined plan, draft, observations, and state patch into one "
                "candidate. Harness evaluation and promotion remain separate."
            ),
            input_model=SubmitChapterCandidateToolInput,
            allowed_roles=frozenset({"chapter"}),
            allowed_phases=frozenset({"chapter"}),
            handler=_submit_chapter_candidate,
            read_only=False,
            terminal=True,
        )
    )
    registry.register(
        ToolSpec(
            name="submit_chapter_patch_evidence_repair",
            version=1,
            description=(
                "Replace only rejected state-patch evidence quotes with exact substrings from "
                "the immutable final chapter. Patch operations and values cannot be changed."
            ),
            input_model=SubmitChapterPatchEvidenceRepairInput,
            allowed_roles=frozenset({"chapter"}),
            allowed_phases=frozenset({"state_patch_repair"}),
            handler=_submit_chapter_patch_evidence_repair,
            read_only=False,
            terminal=True,
        )
    )


def _submit_book_discussion_update(
    context: ToolExecutionContext, arguments: BaseModel
) -> ToolExecutionPlan:
    request = _typed(arguments, BookDiscussionUpdateInput)
    _expect_revision(context, request.expected_revision)
    relative = _candidate_relative(context, "discussion-update.json")
    return _terminal_candidate_plan(
        context,
        request,
        relative,
        checkpoint=f"book-discussion:{request.expected_revision + 1}",
        summary="Book discussion update submitted.",
    )


def _submit_book_direction_candidate(
    context: ToolExecutionContext, arguments: BaseModel
) -> ToolExecutionPlan:
    request = _typed(arguments, BookDirectionCandidateInput)
    _expect_revision(context, request.expected_revision)
    relative = _candidate_relative(context, "book-direction.json")
    return _terminal_candidate_plan(
        context,
        request,
        relative,
        checkpoint=f"book-direction:{request.candidate_revision}",
        summary="Book Direction candidate submitted for evaluation.",
    )


def _submit_story_arc_candidate(
    context: ToolExecutionContext, arguments: BaseModel
) -> ToolExecutionPlan:
    request = _typed(arguments, StoryArcCandidateInput)
    _expect_revision(context, request.expected_revision)
    if context.identity.scope_id != request.arc_id:
        raise ToolHandlerError(
            "arc_ownership_mismatch",
            "Story Arc Tool can only write the Agent's owned arc.",
            recoverable=False,
        )
    relative = _candidate_relative(context, "story-arc.json")
    return _terminal_candidate_plan(
        context,
        request,
        relative,
        checkpoint=f"story-arc:{request.arc_id}:{request.expected_revision + 1}",
        summary="Story Arc candidate submitted for evaluation.",
    )


def _plan_chapter_candidate(
    context: ToolExecutionContext, arguments: BaseModel
) -> ToolExecutionPlan:
    request = _typed(arguments, ChapterPlanCandidateInput)
    _expect_chapter(context, request.chapter_id, request.expected_revision)
    root = _candidate_root(context)
    plan_path = root / "plan.md"
    state_path = root / "workspace.json"
    state = _workspace_state(context)
    current = int(state.get("plan_revision", 0))
    if request.plan_revision != current + 1:
        raise ToolHandlerError(
            "stale_plan_revision",
            f"Expected plan revision {current + 1}, got {request.plan_revision}.",
            recoverable=True,
            allowed_actions=["reload_chapter_workspace", "retry:plan_chapter_candidate"],
        )
    state.update(
        {
            "schema_version": 1,
            "chapter_id": request.chapter_id,
            "expected_revision": request.expected_revision,
            "plan_revision": request.plan_revision,
        }
    )
    return ToolExecutionPlan(
        content={"plan_revision": request.plan_revision, "path": plan_path.as_posix()},
        files={
            plan_path.as_posix(): request.plan_markdown.rstrip() + "\n",
            state_path.as_posix(): json_document(state),
        },
        artifact_paths=[plan_path.as_posix(), state_path.as_posix()],
        allowed_actions=["write_chapter_draft"],
    )


def _write_chapter_draft(
    context: ToolExecutionContext, arguments: BaseModel
) -> ToolExecutionPlan:
    request = _typed(arguments, WriteChapterDraftInput)
    _expect_chapter(context, request.chapter_id, request.expected_revision)
    root = _candidate_root(context)
    draft_path = root / "draft.md"
    state_path = root / "workspace.json"
    state = _workspace_state(context)
    if int(state.get("plan_revision", 0)) != request.plan_revision:
        raise ToolHandlerError(
            "stale_plan_revision",
            "Draft does not reference the current candidate plan revision.",
            recoverable=True,
            allowed_actions=["reload_chapter_workspace"],
        )
    current_revision = int(state.get("draft_revision", 0))
    if request.draft_revision != current_revision + 1:
        raise ToolHandlerError(
            "stale_draft_revision",
            f"Expected draft revision {current_revision + 1}, got {request.draft_revision}.",
            recoverable=True,
            allowed_actions=["reload_chapter_workspace"],
        )
    existing = _read_optional_text(context.project_path / draft_path)
    if request.mode == "append" and not existing:
        raise ToolHandlerError(
            "draft_append_without_base",
            "Cannot append before a candidate draft has been written.",
            recoverable=True,
            allowed_actions=["retry:write_chapter_draft"],
        )
    content = (
        existing.rstrip() + "\n\n" + request.content.strip()
        if request.mode == "append"
        else request.content.strip()
    )
    state["draft_revision"] = request.draft_revision
    state["draft_sha256"] = sha256(content.encode("utf-8")).hexdigest()
    return ToolExecutionPlan(
        content={
            "draft_revision": request.draft_revision,
            "characters": len(content),
            "path": draft_path.as_posix(),
        },
        files={
            draft_path.as_posix(): content.rstrip() + "\n",
            state_path.as_posix(): json_document(state),
        },
        artifact_paths=[draft_path.as_posix(), state_path.as_posix()],
        allowed_actions=["write_chapter_draft", "edit_chapter_draft", "inspect_chapter_consistency"],
    )


def _edit_chapter_draft(
    context: ToolExecutionContext, arguments: BaseModel
) -> ToolExecutionPlan:
    request = _typed(arguments, EditChapterDraftInput)
    _expect_chapter(context, request.chapter_id, request.expected_revision)
    root = _candidate_root(context)
    draft_path = root / "draft.md"
    state_path = root / "workspace.json"
    state = _workspace_state(context)
    if int(state.get("draft_revision", 0)) != request.draft_revision:
        raise ToolHandlerError(
            "stale_draft_revision",
            "Targeted edit references a stale candidate draft.",
            recoverable=True,
            allowed_actions=["reload_chapter_workspace"],
        )
    draft = _read_required_text(
        context.project_path / draft_path,
        code="candidate_draft_missing",
    )
    occurrences = draft.count(request.anchor)
    if occurrences != 1:
        raise ToolHandlerError(
            "edit_anchor_not_unique",
            f"Targeted edit anchor matched {occurrences} locations; exactly one is required.",
            recoverable=True,
            allowed_actions=["inspect_chapter_consistency", "retry:edit_chapter_draft"],
        )
    updated = draft.replace(request.anchor, request.replacement, 1)
    state["draft_revision"] = request.next_draft_revision
    state["draft_sha256"] = sha256(updated.encode("utf-8")).hexdigest()
    return ToolExecutionPlan(
        content={
            "draft_revision": request.next_draft_revision,
            "characters": len(updated),
            "path": draft_path.as_posix(),
        },
        files={
            draft_path.as_posix(): updated.rstrip() + "\n",
            state_path.as_posix(): json_document(state),
        },
        artifact_paths=[draft_path.as_posix(), state_path.as_posix()],
        allowed_actions=["inspect_chapter_consistency", "submit_chapter_candidate"],
    )


def _inspect_chapter_consistency(
    context: ToolExecutionContext, arguments: BaseModel
) -> ToolExecutionPlan:
    request = _typed(arguments, InspectChapterConsistencyInput)
    _expect_chapter(context, request.chapter_id, request.expected_revision)
    root = _candidate_root(context)
    draft_path = root / "draft.md"
    state = _workspace_state(context)
    if int(state.get("draft_revision", 0)) != request.draft_revision:
        raise ToolHandlerError(
            "stale_draft_revision",
            "Consistency inspection references a stale draft revision.",
            recoverable=True,
            allowed_actions=["reload_chapter_workspace"],
        )
    draft = _read_required_text(
        context.project_path / draft_path,
        code="candidate_draft_missing",
    )
    evidence = {
        "schema_version": 1,
        "chapter_id": request.chapter_id,
        "draft_revision": request.draft_revision,
        "draft_sha256": sha256(draft.encode("utf-8")).hexdigest(),
        "characters": len(draft),
        "paragraphs": len([item for item in draft.split("\n\n") if item.strip()]),
        "empty": not bool(draft.strip()),
        "semantic_verdict": None,
    }
    relative = root / "consistency.json"
    return ToolExecutionPlan(
        content=evidence,
        files={relative.as_posix(): json_document(evidence)},
        artifact_paths=[relative.as_posix()],
        allowed_actions=["edit_chapter_draft", "submit_chapter_candidate"],
    )


def _submit_chapter_candidate(
    context: ToolExecutionContext, arguments: BaseModel
) -> ToolExecutionPlan:
    tool_request = _typed(arguments, SubmitChapterCandidateToolInput)
    request = _normalize_chapter_submission(context, tool_request)
    _expect_chapter(context, request.chapter_id, request.expected_revision)
    root = _candidate_root(context)
    state = _workspace_state(context)
    if int(state.get("plan_revision", 0)) != request.plan_revision:
        raise ToolHandlerError(
            "stale_plan_revision",
            "Chapter submission references a stale plan revision.",
            recoverable=True,
            allowed_actions=["reload_chapter_workspace"],
        )
    if int(state.get("draft_revision", 0)) != request.draft_revision:
        raise ToolHandlerError(
            "stale_draft_revision",
            "Chapter submission references a stale draft revision.",
            recoverable=True,
            allowed_actions=["reload_chapter_workspace"],
        )
    plan_path = root / "plan.md"
    draft_path = root / "draft.md"
    _read_required_text(context.project_path / plan_path, code="candidate_plan_missing")
    draft = _read_required_text(
        context.project_path / draft_path,
        code="candidate_draft_missing",
    )
    observations_path = root / "obs.json"
    patch_path = root / "patch.json"
    manifest_path = root / "manifest.json"
    payload = {
        "schema_version": 1,
        "status": "candidate",
        "chapter_id": request.chapter_id,
        "expected_revision": request.expected_revision,
        "candidate_revision": request.candidate_revision,
        "plan_revision": request.plan_revision,
        "draft_revision": request.draft_revision,
        "summary": request.summary,
        "observations": request.observations.model_dump(mode="json"),
        "state_patch": request.state_patch.model_dump(mode="json"),
        "plan_path": plan_path.as_posix(),
        "draft_path": draft_path.as_posix(),
        "draft_sha256": sha256(draft.encode("utf-8")).hexdigest(),
        "observations_path": observations_path.as_posix(),
        "state_patch_path": patch_path.as_posix(),
        "promotable": False,
    }
    return ToolExecutionPlan(
        content={
            "summary": "Chapter candidate submitted for evaluation.",
            "candidate_revision": request.candidate_revision,
            "manifest_path": manifest_path.as_posix(),
            "promotable": False,
        },
        files={
            observations_path.as_posix(): json_document(
                request.observations.model_dump(mode="json")
            ),
            patch_path.as_posix(): json_document(request.state_patch.model_dump(mode="json")),
            manifest_path.as_posix(): json_document(payload),
        },
        checkpoint_id=f"chapter:{request.chapter_id}:{request.candidate_revision}",
        artifact_paths=[
            manifest_path.as_posix(),
            plan_path.as_posix(),
            draft_path.as_posix(),
            observations_path.as_posix(),
            patch_path.as_posix(),
        ],
        allowed_actions=["evaluate_chapter_candidate"],
    )


def _normalize_chapter_submission(
    context: ToolExecutionContext,
    request: SubmitChapterCandidateToolInput,
) -> SubmitChapterCandidateInput:
    root = _candidate_root(context)

    def observations(
        values: list[ChapterObservationInput],
    ) -> list[dict[str, str]]:
        return [item.model_dump(mode="json") for item in values]

    normalized_observations = CandidateObservations(
        based_on=(root / "draft.md").as_posix(),
        events=observations(request.observations.events),
        character_changes=observations(request.observations.character_changes),
        relationship_changes=observations(request.observations.relationship_changes),
        world_fact_candidates=observations(
            request.observations.world_fact_candidates
        ),
        foreshadowing_candidates=observations(
            request.observations.foreshadowing_candidates
        ),
        requires_commit=request.observations.requires_commit,
    )
    final_path = f"chapters/{request.chapter_id}/final.md"
    observations_path = f"chapters/{request.chapter_id}/observations.json"
    operations = [
        _normalize_patch_operation(item, final_path=final_path)
        for item in request.state_patch.operations
    ]
    normalized_patch = CandidateStatePatch(
        based_on={
            "chapter_final": final_path,
            "observations": observations_path,
        },
        operations=operations,
    )
    return SubmitChapterCandidateInput(
        chapter_id=request.chapter_id,
        expected_revision=request.expected_revision,
        candidate_revision=request.candidate_revision,
        plan_revision=request.plan_revision,
        draft_revision=request.draft_revision,
        summary=request.summary,
        observations=normalized_observations,
        state_patch=normalized_patch,
    )


def _normalize_patch_operation(
    operation: ChapterPatchOperationInput,
    *,
    final_path: str,
) -> CandidatePatchOperation:
    value: dict[str, Any] = {}
    for field in operation.value_fields:
        if field.key in value:
            raise ToolHandlerError(
                "duplicate_patch_value_field",
                f"Patch value field is duplicated: {field.key}",
                recoverable=True,
                allowed_actions=["retry:submit_chapter_candidate"],
            )
        try:
            value[field.key] = json.loads(field.json_value)
        except json.JSONDecodeError:
            value[field.key] = field.json_value
    return CandidatePatchOperation(
        op=operation.op,
        target_file=operation.target_file,
        target_id=operation.target_id,
        expected_version=operation.expected_version,
        value=value,
        evidence=[
            PatchEvidence(file=final_path, quote=quote)
            for quote in operation.evidence_quotes
        ],
        rationale=operation.rationale,
    )


def _submit_chapter_patch_evidence_repair(
    context: ToolExecutionContext,
    arguments: BaseModel,
) -> ToolExecutionPlan:
    request = _typed(arguments, SubmitChapterPatchEvidenceRepairInput)
    _expect_chapter(context, request.chapter_id, request.expected_revision)
    chapter_root = Path("chapters") / request.chapter_id
    patch_path = context.project_path / chapter_root / "candidate_state_patch.json"
    rejection_path = context.project_path / chapter_root / "state_patch_rejection.json"
    final_relative = (chapter_root / "final.md").as_posix()
    patch = CandidateStatePatch.model_validate(
        json.loads(_read_required_text(patch_path, code="candidate_state_patch_missing"))
    )
    rejection = json.loads(
        _read_required_text(rejection_path, code="state_patch_rejection_missing")
    )
    reasons = rejection.get("reasons") if isinstance(rejection, dict) else None
    rejected_indexes = {
        int(match.group(1))
        for reason in reasons or []
        if isinstance(reason, str)
        and (match := re.match(r"Operation (\d+) evidence ", reason)) is not None
    }
    provided_indexes = {item.operation_index for item in request.repairs}
    if not rejected_indexes or provided_indexes != rejected_indexes:
        raise ToolHandlerError(
            "incomplete_patch_evidence_repair",
            "Evidence repair must cover exactly the rejected operation indexes.",
            recoverable=True,
            content={"required_operation_indexes": sorted(rejected_indexes)},
            allowed_actions=["retry:submit_chapter_patch_evidence_repair"],
        )
    if any(index >= len(patch.operations) for index in provided_indexes):
        raise ToolHandlerError(
            "invalid_patch_operation_index",
            "Evidence repair references an operation that does not exist.",
            recoverable=True,
            content={"operation_count": len(patch.operations)},
            allowed_actions=["retry:submit_chapter_patch_evidence_repair"],
        )
    final_text = _read_required_text(
        context.project_path / final_relative,
        code="final_chapter_missing",
    )
    invalid_quotes = [
        {"operation_index": item.operation_index, "quote": quote}
        for item in request.repairs
        for quote in item.evidence_quotes
        if quote not in final_text
    ]
    if invalid_quotes:
        raise ToolHandlerError(
            "evidence_quote_not_in_final",
            "Every repaired evidence quote must be an exact substring of final.md.",
            recoverable=True,
            content={"invalid_quotes": invalid_quotes},
            allowed_actions=["retry:submit_chapter_patch_evidence_repair"],
        )
    repairs = {item.operation_index: item for item in request.repairs}
    operations = [
        operation.model_copy(
            update={
                "evidence": [
                    PatchEvidence(file=final_relative, quote=quote)
                    for quote in repairs[index].evidence_quotes
                ]
            }
        )
        if index in repairs
        else operation
        for index, operation in enumerate(patch.operations)
    ]
    repaired_patch = patch.model_copy(update={"operations": operations})
    relative = _candidate_relative(context, "state-patch-evidence-repair.json")
    return ToolExecutionPlan(
        content={
            "summary": "Rejected state-patch evidence quotes were repaired.",
            "candidate_path": relative.as_posix(),
            "repaired_operation_indexes": sorted(rejected_indexes),
            "promotable": False,
        },
        files={
            relative.as_posix(): json_document(
                repaired_patch.model_dump(mode="json")
            )
        },
        checkpoint_id=(
            f"chapter-patch:{request.chapter_id}:{request.expected_revision + 1}"
        ),
        artifact_paths=[relative.as_posix()],
        allowed_actions=["validate_state_patch"],
    )


def _terminal_candidate_plan(
    context: ToolExecutionContext,
    request: BaseModel,
    relative: Path,
    *,
    checkpoint: str,
    summary: str,
) -> ToolExecutionPlan:
    return ToolExecutionPlan(
        content={
            "summary": summary,
            "candidate_path": relative.as_posix(),
            "promotable": False,
        },
        files={relative.as_posix(): json_document(request.model_dump(mode="json"))},
        checkpoint_id=checkpoint,
        artifact_paths=[relative.as_posix()],
        allowed_actions=["evaluate_candidate"],
    )


def _expect_revision(context: ToolExecutionContext, supplied: int) -> None:
    if context.expected_revision != supplied:
        raise ToolHandlerError(
            "stale_candidate_revision",
            f"Expected Harness revision {context.expected_revision}, got {supplied}.",
            recoverable=True,
            content={
                "expected_revision": context.expected_revision,
                "received_revision": supplied,
            },
            allowed_actions=["retry_with_expected_revision"],
        )


def _expect_chapter(
    context: ToolExecutionContext,
    chapter_id: str,
    expected_revision: int,
) -> None:
    _expect_revision(context, expected_revision)
    if context.identity.scope_id != chapter_id:
        raise ToolHandlerError(
            "chapter_ownership_mismatch",
            "Chapter Tool can only mutate the Agent's owned chapter candidate.",
            recoverable=False,
        )


def _candidate_root(context: ToolExecutionContext) -> Path:
    return activation_relative(context.identity, context.activation_id) / "c"


def _candidate_relative(context: ToolExecutionContext, filename: str) -> Path:
    return _candidate_root(context) / filename


def _workspace_state(context: ToolExecutionContext) -> dict[str, Any]:
    path = context.project_path / _candidate_root(context) / "workspace.json"
    if not path.is_file():
        return {"schema_version": 1}
    import json

    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ToolHandlerError(
            "invalid_chapter_workspace",
            "Candidate chapter workspace is not a JSON object.",
            recoverable=False,
        )
    return cast(dict[str, Any], payload)


def _read_optional_text(path: Path) -> str:
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise ToolHandlerError(
            "candidate_artifact_unreadable",
            f"Candidate artifact could not be read: {path.name}",
            recoverable=False,
        ) from exc


def _read_required_text(path: Path, *, code: str) -> str:
    value = _read_optional_text(path)
    if not value.strip():
        raise ToolHandlerError(
            code,
            f"Required candidate artifact is missing: {path.name}",
            recoverable=True,
            allowed_actions=["recreate_candidate_artifact"],
        )
    return value


ModelT = TypeVar("ModelT", bound=BaseModel)


def _typed(value: BaseModel, expected: type[ModelT]) -> ModelT:
    if not isinstance(value, expected):
        raise TypeError(f"Expected {expected.__name__}, got {type(value).__name__}.")
    return value
