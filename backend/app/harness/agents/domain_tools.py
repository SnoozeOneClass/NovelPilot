import json
import re
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal, TypeVar, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.harness.agents.persistence import activation_relative, json_document
from app.harness.agents.repair_workspace import (
    add_book_item,
    add_chapter_observation,
    add_state_patch_operation,
    book_item_update_value,
    delete_structured_item,
    edit_text_component,
    ensure_repair_workspace,
    finalize_repair_workspace,
    repair_workspace_relative,
    replace_text_component,
    set_story_arc_chapter_count,
    update_structured_item,
    workspace_document,
    workspace_public_view,
)
from app.harness.agents.models import (
    AgentRole,
    BookCandidateSnapshot,
    CandidateSnapshot,
    ChapterCandidateSnapshot,
    StoryArcCandidateSnapshot,
)
from app.harness.agents.evidence_matching import resolve_verbatim_evidence_quote
from app.harness.agents.rubrics import changed_components, component_fingerprints
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
from app.storage.patches import read_canon_versions


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


class WriteChapterObservationsInput(BaseModel):
    """Persist semantic observations separately from the terminal submission."""

    model_config = ConfigDict(extra="forbid")

    chapter_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    expected_revision: int = Field(ge=0)
    draft_revision: int = Field(ge=1)
    observations: ChapterCandidateObservationsInput


class WriteChapterStatePatchInput(BaseModel):
    """Persist the candidate canon delta separately from the terminal submission."""

    model_config = ConfigDict(extra="forbid")

    chapter_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    expected_revision: int = Field(ge=0)
    draft_revision: int = Field(ge=1)
    state_patch: ChapterCandidateStatePatchInput


class SubmitChapterCandidateToolInput(BaseModel):
    """Short terminal reference to components already stored in the workspace."""

    model_config = ConfigDict(extra="forbid")

    chapter_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    expected_revision: int = Field(ge=0)
    candidate_revision: int = Field(ge=1)
    plan_revision: int = Field(ge=1)
    draft_revision: int = Field(ge=1)
    summary: str = Field(min_length=1, max_length=8_000)


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


class OpenCandidateRepairInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReplaceCandidateTextInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    component: Literal["direction", "rolling_plan", "plan", "change_summary", "draft"]
    content: str = Field(min_length=1, max_length=500_000)


class EditCandidateTextInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    component: Literal["plan", "draft"]
    anchor: str = Field(min_length=1, max_length=20_000)
    replacement: str = Field(max_length=100_000)


class SetStoryArcChapterCountInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_chapter_count: int = Field(ge=1, le=30, strict=True)


class AddBookRepairItemInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    collection: Literal[
        "constraints.must_avoid",
        "constraints.creative_freedoms",
        "constraints.open_decisions",
        "confirmed_decision_coverage",
        "recommended_titles",
    ]
    primary: str = Field(min_length=1, max_length=4_000)
    secondary: str | None = Field(max_length=8_000)


class UpdateBookRepairItemInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: str = Field(min_length=1, max_length=128)
    primary: str = Field(min_length=1, max_length=4_000)
    secondary: str | None = Field(max_length=8_000)


class AddChapterObservationRepairInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    collection: Literal[
        "events",
        "character_changes",
        "relationship_changes",
        "world_fact_candidates",
        "foreshadowing_candidates",
    ]
    summary: str = Field(min_length=1, max_length=4_000)
    evidence_quote: str = Field(min_length=1, max_length=4_000)


class UpdateChapterObservationRepairInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: str = Field(min_length=1, max_length=128)
    summary: str = Field(min_length=1, max_length=4_000)
    evidence_quote: str = Field(min_length=1, max_length=4_000)


class AddStatePatchOperationRepairInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: ChapterPatchOperationInput


class UpdateStatePatchOperationRepairInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: str = Field(min_length=1, max_length=128)
    operation: ChapterPatchOperationInput


class DeleteCandidateRepairItemInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: str = Field(min_length=1, max_length=128)


class SubmitCandidateRepairInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, max_length=8_000)


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
            name="write_chapter_observations",
            version=1,
            description=(
                "Write the semantic observations for the current quarantined draft. "
                "Harness assigns durable item identifiers during final assembly."
            ),
            input_model=WriteChapterObservationsInput,
            allowed_roles=frozenset({"chapter"}),
            allowed_phases=frozenset({"chapter"}),
            handler=_write_chapter_observations,
            read_only=False,
        )
    )
    registry.register(
        ToolSpec(
            name="write_chapter_state_patch",
            version=1,
            description=(
                "Write the semantic canon delta for the current quarantined draft. "
                "This Tool never commits canon."
            ),
            input_model=WriteChapterStatePatchInput,
            allowed_roles=frozenset({"chapter"}),
            allowed_phases=frozenset({"chapter"}),
            handler=_write_chapter_state_patch,
            read_only=False,
        )
    )
    registry.register(
        ToolSpec(
            name="submit_chapter_candidate",
            version=1,
            description=(
                "Ask Harness to assemble the stored plan, draft, observations, and state patch "
                "into one candidate. Harness evaluation and promotion remain separate."
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
    repair_phases = frozenset({"direction", "planning", "revision", "chapter"})
    repair_roles: frozenset[AgentRole] = frozenset(
        {"book", "story_arc", "chapter"}
    )
    registry.register(
        ToolSpec(
            name="open_candidate_repair",
            version=1,
            description=(
                "Open the Harness-owned repair workspace and return stable IDs for "
                "structured candidate items. Call this before structured updates."
            ),
            input_model=OpenCandidateRepairInput,
            allowed_roles=repair_roles,
            allowed_phases=repair_phases,
            handler=_open_candidate_repair,
            read_only=False,
        )
    )
    registry.register(
        ToolSpec(
            name="replace_candidate_text",
            version=1,
            description=(
                "Replace one authorized semantic text artifact in the repair workspace. "
                "Unchanged candidate artifacts remain Harness-owned."
            ),
            input_model=ReplaceCandidateTextInput,
            allowed_roles=repair_roles,
            allowed_phases=repair_phases,
            handler=_replace_candidate_text,
            read_only=False,
        )
    )
    registry.register(
        ToolSpec(
            name="edit_candidate_text",
            version=1,
            description=(
                "Optionally replace one exact anchor in an authorized Chapter plan or "
                "draft. Use replace_candidate_text if the anchor is unsuitable."
            ),
            input_model=EditCandidateTextInput,
            allowed_roles=frozenset({"chapter"}),
            allowed_phases=frozenset({"chapter"}),
            handler=_edit_candidate_text,
            read_only=False,
        )
    )
    registry.register(
        ToolSpec(
            name="set_story_arc_chapter_count",
            version=1,
            description="Set the authorized Story Arc target chapter count.",
            input_model=SetStoryArcChapterCountInput,
            allowed_roles=frozenset({"story_arc"}),
            allowed_phases=frozenset({"planning", "revision"}),
            handler=_set_story_arc_chapter_count,
            read_only=False,
        )
    )
    registry.register(
        ToolSpec(
            name="add_book_repair_item",
            version=1,
            description=(
                "Add one authorized Book constraint, coverage item, or title. The Harness "
                "assigns its stable ID. secondary is null for constraints."
            ),
            input_model=AddBookRepairItemInput,
            allowed_roles=frozenset({"book"}),
            allowed_phases=frozenset({"direction"}),
            handler=_add_book_repair_item,
            read_only=False,
        )
    )
    registry.register(
        ToolSpec(
            name="update_book_repair_item",
            version=1,
            description=(
                "Update one Book item by its Harness stable ID. Preserve confirmed user "
                "decisions; secondary is null only for constraint items."
            ),
            input_model=UpdateBookRepairItemInput,
            allowed_roles=frozenset({"book"}),
            allowed_phases=frozenset({"direction"}),
            handler=_update_book_repair_item,
            read_only=False,
        )
    )
    registry.register(
        ToolSpec(
            name="add_chapter_observation_repair",
            version=1,
            description=(
                "Add one Chapter observation; the Harness assigns its stable ID."
            ),
            input_model=AddChapterObservationRepairInput,
            allowed_roles=frozenset({"chapter"}),
            allowed_phases=frozenset({"chapter"}),
            handler=_add_chapter_observation_repair,
            read_only=False,
        )
    )
    registry.register(
        ToolSpec(
            name="update_chapter_observation_repair",
            version=1,
            description="Update one Chapter observation by its Harness stable ID.",
            input_model=UpdateChapterObservationRepairInput,
            allowed_roles=frozenset({"chapter"}),
            allowed_phases=frozenset({"chapter"}),
            handler=_update_chapter_observation_repair,
            read_only=False,
        )
    )
    registry.register(
        ToolSpec(
            name="add_state_patch_operation_repair",
            version=1,
            description=(
                "Add one Chapter canon operation; the Harness assigns its stable ID and "
                "retains normal evidence/version/conflict validation."
            ),
            input_model=AddStatePatchOperationRepairInput,
            allowed_roles=frozenset({"chapter"}),
            allowed_phases=frozenset({"chapter"}),
            handler=_add_state_patch_operation_repair,
            read_only=False,
        )
    )
    registry.register(
        ToolSpec(
            name="update_state_patch_operation_repair",
            version=1,
            description="Update one Chapter canon operation by its Harness stable ID.",
            input_model=UpdateStatePatchOperationRepairInput,
            allowed_roles=frozenset({"chapter"}),
            allowed_phases=frozenset({"chapter"}),
            handler=_update_state_patch_operation_repair,
            read_only=False,
        )
    )
    registry.register(
        ToolSpec(
            name="delete_candidate_repair_item",
            version=1,
            description="Delete one authorized structured candidate item by stable ID.",
            input_model=DeleteCandidateRepairItemInput,
            allowed_roles=repair_roles,
            allowed_phases=repair_phases,
            handler=_delete_candidate_repair_item,
            read_only=False,
        )
    )
    registry.register(
        ToolSpec(
            name="submit_candidate_repair",
            version=1,
            description=(
                "Finalize the Harness-owned repair workspace. Submit only a short summary; "
                "the Harness preserves and assembles the complete candidate."
            ),
            input_model=SubmitCandidateRepairInput,
            allowed_roles=repair_roles,
            allowed_phases=repair_phases,
            handler=_submit_candidate_repair,
            read_only=False,
            terminal=True,
        )
    )


def _open_candidate_repair(
    context: ToolExecutionContext, arguments: BaseModel
) -> ToolExecutionPlan:
    _typed(arguments, OpenCandidateRepairInput)
    workspace = ensure_repair_workspace(context)
    relative = repair_workspace_relative(context)
    return ToolExecutionPlan(
        content=workspace_public_view(workspace),
        files={relative.as_posix(): workspace_document(workspace)},
        artifact_paths=[relative.as_posix()],
        allowed_actions=_repair_allowed_actions(context),
    )


def _replace_candidate_text(
    context: ToolExecutionContext, arguments: BaseModel
) -> ToolExecutionPlan:
    request = _typed(arguments, ReplaceCandidateTextInput)
    workspace = replace_text_component(
        context,
        ensure_repair_workspace(context),
        component=request.component,
        content=request.content,
    )
    return _repair_workspace_plan(context, workspace)


def _edit_candidate_text(
    context: ToolExecutionContext, arguments: BaseModel
) -> ToolExecutionPlan:
    request = _typed(arguments, EditCandidateTextInput)
    workspace = edit_text_component(
        context,
        ensure_repair_workspace(context),
        component=request.component,
        anchor=request.anchor,
        replacement=request.replacement,
    )
    return _repair_workspace_plan(context, workspace)


def _set_story_arc_chapter_count(
    context: ToolExecutionContext, arguments: BaseModel
) -> ToolExecutionPlan:
    request = _typed(arguments, SetStoryArcChapterCountInput)
    workspace = set_story_arc_chapter_count(
        context,
        ensure_repair_workspace(context),
        request.target_chapter_count,
    )
    return _repair_workspace_plan(context, workspace)


def _add_book_repair_item(
    context: ToolExecutionContext, arguments: BaseModel
) -> ToolExecutionPlan:
    request = _typed(arguments, AddBookRepairItemInput)
    workspace, item_id = add_book_item(
        context,
        ensure_repair_workspace(context),
        collection=request.collection,
        primary=request.primary,
        secondary=request.secondary,
    )
    return _repair_workspace_plan(context, workspace, item_id=item_id)


def _update_book_repair_item(
    context: ToolExecutionContext, arguments: BaseModel
) -> ToolExecutionPlan:
    request = _typed(arguments, UpdateBookRepairItemInput)
    workspace = ensure_repair_workspace(context)
    value = book_item_update_value(
        workspace,
        item_id=request.item_id,
        primary=request.primary,
        secondary=request.secondary,
    )
    workspace = update_structured_item(
        context,
        workspace,
        item_id=request.item_id,
        value=value,
    )
    return _repair_workspace_plan(context, workspace, item_id=request.item_id)


def _add_chapter_observation_repair(
    context: ToolExecutionContext, arguments: BaseModel
) -> ToolExecutionPlan:
    request = _typed(arguments, AddChapterObservationRepairInput)
    workspace, item_id = add_chapter_observation(
        context,
        ensure_repair_workspace(context),
        collection=request.collection,
        summary=request.summary,
        evidence_quote=request.evidence_quote,
    )
    return _repair_workspace_plan(context, workspace, item_id=item_id)


def _update_chapter_observation_repair(
    context: ToolExecutionContext, arguments: BaseModel
) -> ToolExecutionPlan:
    request = _typed(arguments, UpdateChapterObservationRepairInput)
    workspace = update_structured_item(
        context,
        ensure_repair_workspace(context),
        item_id=request.item_id,
        value={
            "summary": request.summary.strip(),
            "evidence_quote": request.evidence_quote,
        },
    )
    return _repair_workspace_plan(context, workspace, item_id=request.item_id)


def _add_state_patch_operation_repair(
    context: ToolExecutionContext, arguments: BaseModel
) -> ToolExecutionPlan:
    request = _typed(arguments, AddStatePatchOperationRepairInput)
    chapter_id = context.identity.scope_id
    if chapter_id is None:
        raise ToolHandlerError(
            "chapter_ownership_mismatch",
            "Chapter repair activation is missing its owned chapter.",
            recoverable=False,
        )
    operation = _semantic_patch_operation(
        request.operation,
        final_path=f"chapters/{chapter_id}/final.md",
    )
    workspace, item_id = add_state_patch_operation(
        context,
        ensure_repair_workspace(context),
        operation=operation,
    )
    return _repair_workspace_plan(context, workspace, item_id=item_id)


def _update_state_patch_operation_repair(
    context: ToolExecutionContext, arguments: BaseModel
) -> ToolExecutionPlan:
    request = _typed(arguments, UpdateStatePatchOperationRepairInput)
    chapter_id = context.identity.scope_id
    if chapter_id is None:
        raise ToolHandlerError(
            "chapter_ownership_mismatch",
            "Chapter repair activation is missing its owned chapter.",
            recoverable=False,
        )
    operation = _semantic_patch_operation(
        request.operation,
        final_path=f"chapters/{chapter_id}/final.md",
    )
    workspace = update_structured_item(
        context,
        ensure_repair_workspace(context),
        item_id=request.item_id,
        value=operation,
    )
    return _repair_workspace_plan(context, workspace, item_id=request.item_id)


def _delete_candidate_repair_item(
    context: ToolExecutionContext, arguments: BaseModel
) -> ToolExecutionPlan:
    request = _typed(arguments, DeleteCandidateRepairItemInput)
    workspace = delete_structured_item(
        context,
        ensure_repair_workspace(context),
        item_id=request.item_id,
    )
    return _repair_workspace_plan(context, workspace, item_id=request.item_id)


def _submit_candidate_repair(
    context: ToolExecutionContext, arguments: BaseModel
) -> ToolExecutionPlan:
    request = _typed(arguments, SubmitCandidateRepairInput)
    workspace = ensure_repair_workspace(context)
    _, files, candidate_path, artifact_paths = finalize_repair_workspace(
        context,
        workspace,
        summary=request.summary,
    )
    contract = context.repair_contract
    if contract is None:
        raise ToolHandlerError(
            "repair_contract_missing",
            "Candidate repair finalization requires a pending contract.",
            recoverable=False,
        )
    return ToolExecutionPlan(
        content={
            "summary": "Candidate repair finalized for independent evaluation.",
            "candidate_path": candidate_path,
            "candidate_revision": contract.next_candidate_revision,
            "promotable": False,
        },
        files=files,
        checkpoint_id=(
            f"{context.identity.role}:{context.identity.scope_id or 'book'}:"
            f"repair:{contract.next_candidate_revision}"
        ),
        artifact_paths=artifact_paths,
        allowed_actions=["evaluate_candidate"],
    )


def _repair_workspace_plan(
    context: ToolExecutionContext,
    workspace: Any,
    *,
    item_id: str | None = None,
) -> ToolExecutionPlan:
    relative = repair_workspace_relative(context)
    content: dict[str, Any] = {
        "workspace_id": workspace.workspace_id,
        "mutation_count": len(workspace.mutations),
    }
    if item_id is not None:
        content["item_id"] = item_id
    return ToolExecutionPlan(
        content=content,
        files={relative.as_posix(): workspace_document(workspace)},
        artifact_paths=[relative.as_posix()],
        allowed_actions=_repair_allowed_actions(context),
    )


def _repair_allowed_actions(context: ToolExecutionContext) -> list[str]:
    actions = ["submit_candidate_repair"]
    contract = context.repair_contract
    if contract is None:
        return actions
    for component in contract.allowed_components:
        if component in {"direction", "rolling_plan", "plan", "change_summary", "draft"}:
            actions.append("replace_candidate_text")
        elif component in {"constraints", "confirmed_decision_coverage", "recommended_titles"}:
            actions.extend(["add_book_repair_item", "update_book_repair_item"])
        elif component == "observations":
            actions.extend(
                ["add_chapter_observation_repair", "update_chapter_observation_repair"]
            )
        elif component == "state_patch":
            actions.extend(
                ["add_state_patch_operation_repair", "update_state_patch_operation_repair"]
            )
    if any(
        component
        in {
            "constraints",
            "confirmed_decision_coverage",
            "recommended_titles",
            "observations",
            "state_patch",
        }
        for component in contract.allowed_components
    ):
        actions.append("delete_candidate_repair_item")
    return list(dict.fromkeys(actions))


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
    if context.expected_candidate_revision is None:
        raise ToolHandlerError(
            "missing_expected_candidate_revision",
            "Book Direction activation is missing its Harness candidate revision target.",
            recoverable=False,
        )
    if request.candidate_revision != context.expected_candidate_revision:
        raise ToolHandlerError(
            "stale_candidate_revision",
            (
                "Book Direction submission must use review candidate revision "
                f"{context.expected_candidate_revision}."
            ),
            recoverable=True,
            content={
                "expected_candidate_revision": context.expected_candidate_revision,
                "received_candidate_revision": request.candidate_revision,
            },
            allowed_actions=["retry:submit_book_direction_candidate"],
        )
    _enforce_repair_scope(
        context,
        BookCandidateSnapshot(
            direction=request.direction_markdown,
            constraints=request.constraints.model_dump(mode="json"),
            confirmed_decision_coverage=[
                item.model_dump(mode="json")
                for item in request.confirmed_decision_coverage
            ],
            recommended_titles=[
                item.model_dump(mode="json") for item in request.recommended_titles
            ],
            rolling_plan=request.rolling_plan_markdown,
        ),
    )
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
    _enforce_repair_scope(
        context,
        StoryArcCandidateSnapshot(
            plan=request.plan_markdown,
            target_chapter_count=request.target_chapter_count,
            change_summary=request.change_summary,
        ),
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
            "canon_versions": read_canon_versions(context.project_path),
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
        allowed_actions=[
            "inspect_chapter_consistency",
            "write_chapter_observations",
            "write_chapter_state_patch",
        ],
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
        allowed_actions=[
            "edit_chapter_draft",
            "write_chapter_observations",
            "write_chapter_state_patch",
        ],
    )


def _write_chapter_observations(
    context: ToolExecutionContext, arguments: BaseModel
) -> ToolExecutionPlan:
    request = _typed(arguments, WriteChapterObservationsInput)
    _expect_chapter(context, request.chapter_id, request.expected_revision)
    root = _candidate_root(context)
    state_path = root / "workspace.json"
    component_path = root / "observations-input.json"
    state = _workspace_state(context)
    _expect_current_draft_revision(state, request.draft_revision)
    payload = request.observations.model_dump(mode="json")
    state["observations_draft_revision"] = request.draft_revision
    state["observations_sha256"] = _json_payload_sha256(payload)
    observation_count = sum(
        len(payload[name])
        for name in (
            "events",
            "character_changes",
            "relationship_changes",
            "world_fact_candidates",
            "foreshadowing_candidates",
        )
    )
    return ToolExecutionPlan(
        content={
            "draft_revision": request.draft_revision,
            "observation_count": observation_count,
            "path": component_path.as_posix(),
        },
        files={
            component_path.as_posix(): json_document(payload),
            state_path.as_posix(): json_document(state),
        },
        artifact_paths=[component_path.as_posix(), state_path.as_posix()],
        allowed_actions=["write_chapter_state_patch", "submit_chapter_candidate"],
    )


def _write_chapter_state_patch(
    context: ToolExecutionContext, arguments: BaseModel
) -> ToolExecutionPlan:
    request = _typed(arguments, WriteChapterStatePatchInput)
    _expect_chapter(context, request.chapter_id, request.expected_revision)
    root = _candidate_root(context)
    state_path = root / "workspace.json"
    component_path = root / "state-patch-input.json"
    state = _workspace_state(context)
    _expect_current_draft_revision(state, request.draft_revision)
    payload = request.state_patch.model_dump(mode="json")
    state["state_patch_draft_revision"] = request.draft_revision
    state["state_patch_sha256"] = _json_payload_sha256(payload)
    return ToolExecutionPlan(
        content={
            "draft_revision": request.draft_revision,
            "operation_count": len(request.state_patch.operations),
            "path": component_path.as_posix(),
        },
        files={
            component_path.as_posix(): json_document(payload),
            state_path.as_posix(): json_document(state),
        },
        artifact_paths=[component_path.as_posix(), state_path.as_posix()],
        allowed_actions=["write_chapter_observations", "submit_chapter_candidate"],
    )


def _submit_chapter_candidate(
    context: ToolExecutionContext, arguments: BaseModel
) -> ToolExecutionPlan:
    tool_request = _typed(arguments, SubmitChapterCandidateToolInput)
    _expect_chapter(context, tool_request.chapter_id, tool_request.expected_revision)
    expected_candidate_revision = (
        context.repair_contract.next_candidate_revision
        if context.repair_contract is not None
        else 1
    )
    if tool_request.candidate_revision != expected_candidate_revision:
        raise ToolHandlerError(
            "stale_candidate_revision",
            (
                "Chapter submission must use logical candidate revision "
                f"{expected_candidate_revision}."
            ),
            recoverable=True,
            allowed_actions=["retry:submit_chapter_candidate"],
        )
    root = _candidate_root(context)
    state = _workspace_state(context)
    if int(state.get("plan_revision", 0)) != tool_request.plan_revision:
        raise ToolHandlerError(
            "stale_plan_revision",
            "Chapter submission references a stale plan revision.",
            recoverable=True,
            allowed_actions=["reload_chapter_workspace"],
        )
    if int(state.get("draft_revision", 0)) != tool_request.draft_revision:
        raise ToolHandlerError(
            "stale_draft_revision",
            "Chapter submission references a stale draft revision.",
            recoverable=True,
            allowed_actions=["reload_chapter_workspace"],
        )
    _expect_component_revision(
        state,
        component="observations",
        draft_revision=tool_request.draft_revision,
        write_action="write_chapter_observations",
    )
    _expect_component_revision(
        state,
        component="state_patch",
        draft_revision=tool_request.draft_revision,
        write_action="write_chapter_state_patch",
    )
    request = _normalize_chapter_submission(context, tool_request)
    plan_path = root / "plan.md"
    draft_path = root / "draft.md"
    plan = _read_required_text(
        context.project_path / plan_path,
        code="candidate_plan_missing",
    )
    draft = _read_required_text(
        context.project_path / draft_path,
        code="candidate_draft_missing",
    )
    normalized_operations = []
    rejected_evidence = []
    for operation_index, operation in enumerate(request.state_patch.operations):
        normalized_evidence = []
        rejected_indexes = []
        for evidence_index, evidence in enumerate(operation.evidence):
            resolved_quote = resolve_verbatim_evidence_quote(draft, evidence.quote)
            if resolved_quote is None:
                rejected_indexes.append(evidence_index)
                normalized_evidence.append(evidence)
            else:
                normalized_evidence.append(
                    evidence.model_copy(update={"quote": resolved_quote})
                )
        normalized_operations.append(
            operation.model_copy(update={"evidence": normalized_evidence})
        )
        if rejected_indexes:
            rejected_evidence.append(
                {
                    "operation_index": operation_index,
                    "evidence_indexes": rejected_indexes,
                }
            )
    if rejected_evidence:
        raise ToolHandlerError(
            "candidate_patch_evidence_not_verbatim",
            (
                "State-patch evidence must quote exact substrings from the current "
                "candidate draft. Correct only the rejected evidence indexes and resubmit."
            ),
            recoverable=True,
            content={"rejected_evidence": rejected_evidence},
            artifact_paths=[plan_path.as_posix(), draft_path.as_posix()],
            allowed_actions=[
                "write_chapter_state_patch",
            ],
        )
    request = request.model_copy(
        update={
            "state_patch": request.state_patch.model_copy(
                update={"operations": normalized_operations}
            )
        }
    )
    _enforce_repair_scope(
        context,
        ChapterCandidateSnapshot(
            plan=plan,
            draft=draft,
            observations=request.observations.model_dump(mode="json"),
            state_patch=request.state_patch.model_dump(mode="json"),
        ),
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
        "canon_versions": _required_canon_version_snapshot(state=state),
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


def _enforce_repair_scope(
    context: ToolExecutionContext,
    candidate: CandidateSnapshot,
) -> None:
    contract = context.repair_contract
    if contract is None:
        return
    current = component_fingerprints(candidate)
    try:
        changed = changed_components(contract.source_component_fingerprints, current)
    except ValueError as exc:
        raise ToolHandlerError(
            "candidate_repair_component_mismatch",
            "Repair candidate components do not match the source candidate kind.",
            recoverable=False,
        ) from exc
    unexpected = sorted(set(changed) - set(contract.allowed_components))
    if unexpected:
        raise ToolHandlerError(
            "candidate_repair_scope_violation",
            "Candidate repair changed components outside the Evaluator-authorized scope.",
            recoverable=True,
            content={
                "changed_components": changed,
                "allowed_components": list(contract.allowed_components),
                "unexpected_components": unexpected,
            },
            allowed_actions=["retry_with_authorized_components"],
        )


def _normalize_chapter_submission(
    context: ToolExecutionContext,
    request: SubmitChapterCandidateToolInput,
) -> SubmitChapterCandidateInput:
    root = _candidate_root(context)
    observations_input = ChapterCandidateObservationsInput.model_validate(
        _read_required_json_object(
            context.project_path / root / "observations-input.json",
            code="candidate_observations_missing",
        )
    )
    state_patch_input = ChapterCandidateStatePatchInput.model_validate(
        _read_required_json_object(
            context.project_path / root / "state-patch-input.json",
            code="candidate_state_patch_missing",
        )
    )
    canon_versions = _required_canon_version_snapshot(state=_workspace_state(context))

    def observations(
        collection: str,
        values: list[ChapterObservationInput],
    ) -> list[dict[str, str]]:
        return [
            {
                "id": _candidate_item_id(context, "observations", collection, index),
                **item.model_dump(mode="json"),
            }
            for index, item in enumerate(values)
        ]

    normalized_observations = CandidateObservations(
        based_on=(root / "draft.md").as_posix(),
        events=observations("events", observations_input.events),
        character_changes=observations(
            "character_changes", observations_input.character_changes
        ),
        relationship_changes=observations(
            "relationship_changes", observations_input.relationship_changes
        ),
        world_fact_candidates=observations(
            "world_fact_candidates",
            observations_input.world_fact_candidates
        ),
        foreshadowing_candidates=observations(
            "foreshadowing_candidates",
            observations_input.foreshadowing_candidates
        ),
        requires_commit=observations_input.requires_commit,
    )
    final_path = f"chapters/{request.chapter_id}/final.md"
    observations_path = f"chapters/{request.chapter_id}/observations.json"
    operations = [
        _normalize_patch_operation(
            item,
            final_path=final_path,
            expected_version=canon_versions[item.target_file],
            item_id=_candidate_item_id(context, "state_patch", "operations", index),
        )
        for index, item in enumerate(state_patch_input.operations)
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
    expected_version: int,
    item_id: str | None = None,
) -> CandidatePatchOperation:
    semantic = _semantic_patch_operation(operation, final_path=final_path)
    return CandidatePatchOperation(
        id=item_id,
        expected_version=expected_version,
        **semantic,
    )


def _semantic_patch_operation(
    operation: ChapterPatchOperationInput,
    *,
    final_path: str,
) -> dict[str, Any]:
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
    return {
        "op": operation.op,
        "target_file": operation.target_file,
        "target_id": operation.target_id,
        "value": value,
        "evidence": [
            PatchEvidence(file=final_path, quote=quote).model_dump(mode="json")
            for quote in operation.evidence_quotes
        ],
        "rationale": operation.rationale,
    }


def _required_canon_version_snapshot(*, state: dict[str, Any]) -> dict[str, int]:
    raw = state.get("canon_versions")
    if not isinstance(raw, dict):
        raise ToolHandlerError(
            "canon_version_snapshot_missing",
            "Chapter candidate workspace has no Harness-owned canon version snapshot.",
            recoverable=False,
        )
    versions: dict[str, int] = {}
    for target_file in (
        "canon/characters.json",
        "canon/relationships.json",
        "canon/world_facts.json",
        "canon/foreshadowing.json",
    ):
        value = raw.get(target_file)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ToolHandlerError(
                "canon_version_snapshot_invalid",
                "Chapter candidate canon version snapshot is incomplete.",
                recoverable=False,
            )
        versions[target_file] = value
    return versions


def _candidate_item_id(
    context: ToolExecutionContext,
    component: str,
    collection: str,
    index: int,
) -> str:
    seed = "\x1f".join(
        [context.candidate_run_id, component, collection, str(index)]
    )
    return "item-" + sha256(seed.encode("utf-8")).hexdigest()[:24]


def _expect_current_draft_revision(state: dict[str, Any], draft_revision: int) -> None:
    if int(state.get("draft_revision", 0)) != draft_revision:
        raise ToolHandlerError(
            "stale_draft_revision",
            "Structured chapter data references a stale candidate draft.",
            recoverable=True,
            allowed_actions=["reload_chapter_workspace"],
        )


def _expect_component_revision(
    state: dict[str, Any],
    *,
    component: Literal["observations", "state_patch"],
    draft_revision: int,
    write_action: str,
) -> None:
    if int(state.get(f"{component}_draft_revision", 0)) != draft_revision:
        raise ToolHandlerError(
            f"candidate_{component}_missing_or_stale",
            (
                f"Chapter {component.replace('_', ' ')} must be written for draft revision "
                f"{draft_revision} before final submission."
            ),
            recoverable=True,
            allowed_actions=[write_action],
        )


def _json_payload_sha256(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _read_required_json_object(path: Path, *, code: str) -> dict[str, Any]:
    try:
        payload = json.loads(_read_required_text(path, code=code))
    except json.JSONDecodeError as exc:
        raise ToolHandlerError(
            code,
            f"Required candidate JSON is invalid: {path.name}.",
            recoverable=True,
        ) from exc
    if not isinstance(payload, dict):
        raise ToolHandlerError(
            code,
            f"Required candidate JSON must be an object: {path.name}.",
            recoverable=True,
        )
    return cast(dict[str, Any], payload)


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
            artifact_paths=[
                final_relative,
                patch_path.relative_to(context.project_path).as_posix(),
                rejection_path.relative_to(context.project_path).as_posix(),
            ],
            allowed_actions=["retry:submit_chapter_patch_evidence_repair"],
        )
    if any(index >= len(patch.operations) for index in provided_indexes):
        raise ToolHandlerError(
            "invalid_patch_operation_index",
            "Evidence repair references an operation that does not exist.",
            recoverable=True,
            content={"operation_count": len(patch.operations)},
            artifact_paths=[
                final_relative,
                patch_path.relative_to(context.project_path).as_posix(),
                rejection_path.relative_to(context.project_path).as_posix(),
            ],
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
            artifact_paths=[
                final_relative,
                patch_path.relative_to(context.project_path).as_posix(),
                rejection_path.relative_to(context.project_path).as_posix(),
            ],
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
