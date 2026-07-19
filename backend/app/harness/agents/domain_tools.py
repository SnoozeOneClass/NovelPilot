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
    ensure_repair_workspace,
    finalize_repair_workspace,
    repair_workspace_relative,
    replace_text_component,
    set_story_arc_chapter_count,
    update_structured_item,
    workspace_document,
    workspace_item_value,
    workspace_public_view,
)
from app.harness.agents.models import (
    AgentRole,
    BookCandidateSnapshot,
    CandidateSnapshot,
    ChapterCandidateSnapshot,
    StoryArcCandidateSnapshot,
)
from app.harness.agents.evidence_matching import (
    resolve_semantic_choice,
    resolve_semantic_evidence_quote,
)
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
from app.storage.json_files import read_json


_META_PRIORITY_QUESTION_FRAGMENTS = (
    "which issue should we discuss",
    "which topic should we discuss",
    "what should we discuss first",
    "which direction should we confirm",
    "先讨论哪个",
    "先确认哪个",
    "先聊哪个",
)


class SemanticSetupSuggestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=4_000)
    rationale: str = Field(default="", max_length=2_000)
    recommended: bool = False


class BookDiscussionUpdateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reply: str = Field(min_length=1, max_length=20_000)
    direction_draft: str = Field(min_length=1, max_length=100_000)
    discussion_summary: str = Field(min_length=1, max_length=20_000)
    newly_confirmed_decisions: list[str] = Field(max_length=50)
    superseded_decisions: list["SemanticSupersededDecision"] = Field(max_length=50)
    unresolved_questions: list[str] = Field(max_length=100)
    assumptions: list[str] = Field(max_length=100)
    contradictions: list[str] = Field(max_length=100)
    newly_selected_title: str | None = Field(default=None, max_length=200)
    question: str | None = Field(default=None, max_length=600)
    suggestions: list[SemanticSetupSuggestion] = Field(max_length=3)
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
            if not 2 <= len(self.suggestions) <= 3:
                raise ValueError("Book discussion requires two or three suggestions.")
            normalized_question = self.question.strip().casefold()
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
        elif self.question is not None or self.suggestions:
            raise ValueError("A review-ready Book direction cannot ask another question.")
        return self


class SemanticSupersededDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prior_meaning: str = Field(min_length=1, max_length=4_000)
    replacement: str | None = Field(default=None, max_length=4_000)
    reason: str = Field(min_length=1, max_length=4_000)
    user_evidence: str = Field(min_length=1, max_length=4_000)


class BoundBookDiscussionUpdate(BaseModel):
    """Harness-bound durable discussion state; never used as a provider schema."""

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


class SemanticBookDirectionConstraints(BaseModel):
    model_config = ConfigDict(extra="forbid")

    must_avoid: list[str] = Field(max_length=200)
    creative_freedoms: list[str] = Field(max_length=200)
    open_decisions: list[str] = Field(max_length=200)


class BookDirectionCandidateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    direction_markdown: str = Field(min_length=1, max_length=100_000)
    constraints: SemanticBookDirectionConstraints
    comparison_titles: list[BookTitleSuggestion] = Field(min_length=2, max_length=4)
    rolling_plan_markdown: str = Field(min_length=1, max_length=50_000)

    @model_validator(mode="after")
    def validate_titles(self) -> "BookDirectionCandidateInput":
        titles = [item.title.casefold() for item in self.comparison_titles]
        if len(titles) != len(set(titles)):
            raise ValueError("Comparison titles must be unique.")
        return self


class BoundBookDirectionCandidate(BaseModel):
    """Harness-bound durable Book candidate; never provider-facing."""

    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=0)
    candidate_revision: int = Field(ge=1)
    direction_markdown: str = Field(min_length=1, max_length=100_000)
    constraints: BookDirectionConstraints
    confirmed_decision_coverage: list[ConfirmedDecisionCoverage] = Field(max_length=500)
    recommended_titles: list[BookTitleSuggestion] = Field(min_length=3, max_length=5)
    rolling_plan_markdown: str = Field(min_length=1, max_length=50_000)


class StoryArcCandidateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_markdown: str = Field(min_length=1, max_length=50_000)
    target_chapter_count: int = Field(ge=1, le=30, strict=True)
    change_summary: str = Field(min_length=1, max_length=8_000)


class BoundStoryArcCandidate(BaseModel):
    """Harness-bound durable Story Arc candidate; never provider-facing."""

    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=0)
    intent: Literal["create", "revise"]
    arc_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    plan_markdown: str = Field(min_length=1, max_length=50_000)
    target_chapter_count: int = Field(ge=1, le=30, strict=True)
    change_summary: str = Field(min_length=1, max_length=8_000)


class ChapterPlanCandidateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_markdown: str = Field(min_length=1, max_length=50_000)


class WriteChapterDraftInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(min_length=1, max_length=500_000)


class InspectChapterConsistencyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SubmitChapterCandidateInput(BaseModel):
    """Harness-bound complete Chapter candidate; never provider-facing."""

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


class ChapterPatchOperationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    change_kind: Literal["establish", "update", "remove"]
    entity_kind: Literal[
        "character",
        "relationship",
        "world_fact",
        "foreshadowing",
    ]
    entity_name: str = Field(min_length=1, max_length=256)
    resulting_state: str = Field(min_length=1, max_length=20_000)
    evidence_hint: str = Field(min_length=1, max_length=4_000)
    rationale: str = Field(min_length=1, max_length=8_000)


class ChapterCandidateObservationsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    events: list[ChapterObservationInput] = Field(max_length=100)
    character_changes: list[ChapterObservationInput] = Field(max_length=100)
    relationship_changes: list[ChapterObservationInput] = Field(max_length=100)
    world_fact_candidates: list[ChapterObservationInput] = Field(max_length=100)
    foreshadowing_candidates: list[ChapterObservationInput] = Field(max_length=100)


class ChapterCandidateStatePatchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operations: list[ChapterPatchOperationInput] = Field(max_length=100)


class WriteChapterObservationsInput(BaseModel):
    """Persist semantic observations separately from the terminal submission."""

    model_config = ConfigDict(extra="forbid")

    observations: ChapterCandidateObservationsInput


class WriteChapterStatePatchInput(BaseModel):
    """Persist the candidate canon delta separately from the terminal submission."""

    model_config = ConfigDict(extra="forbid")

    state_patch: ChapterCandidateStatePatchInput


class SubmitChapterCandidateToolInput(BaseModel):
    """Short terminal reference to components already stored in the workspace."""

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, max_length=8_000)


class OpenCandidateRepairInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReplaceCandidateTextInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content_kind: Literal[
        "book_direction",
        "rolling_plan",
        "arc_plan",
        "arc_change_summary",
        "chapter_plan",
        "chapter_draft",
    ]
    content: str = Field(min_length=1, max_length=500_000)


class SetStoryArcChapterCountInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_chapter_count: int = Field(ge=1, le=30, strict=True)


class AddBookRepairItemInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content_kind: Literal[
        "avoidance",
        "creative_freedom",
        "open_question",
        "confirmed_decision",
        "comparison_title",
    ]
    primary: str = Field(min_length=1, max_length=4_000)
    rationale: str | None = Field(
        default=None,
        max_length=8_000,
        description="Required only when adding a comparison title.",
    )

    @model_validator(mode="after")
    def validate_comparison_title_rationale(self) -> "AddBookRepairItemInput":
        if self.content_kind == "comparison_title" and not (
            self.rationale and self.rationale.strip()
        ):
            raise ValueError("A comparison title requires a semantic rationale.")
        return self


class UpdateBookRepairItemInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content_kind: Literal[
        "avoidance",
        "creative_freedom",
        "open_question",
        "confirmed_decision",
        "comparison_title",
    ]
    current_meaning: str = Field(min_length=1, max_length=4_000)
    primary: str = Field(min_length=1, max_length=4_000)
    rationale: str | None = Field(
        default=None,
        max_length=8_000,
        description="Required only when updating a comparison title.",
    )

    @model_validator(mode="after")
    def validate_comparison_title_rationale(self) -> "UpdateBookRepairItemInput":
        if self.content_kind == "comparison_title" and not (
            self.rationale and self.rationale.strip()
        ):
            raise ValueError("A comparison title requires a semantic rationale.")
        return self


class AddChapterObservationRepairInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    observation_kind: Literal[
        "event",
        "character_change",
        "relationship_change",
        "world_fact",
        "foreshadowing",
    ]
    summary: str = Field(min_length=1, max_length=4_000)


class UpdateChapterObservationRepairInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    observation_kind: Literal[
        "event",
        "character_change",
        "relationship_change",
        "world_fact",
        "foreshadowing",
    ]
    current_meaning: str = Field(min_length=1, max_length=4_000)
    summary: str = Field(min_length=1, max_length=4_000)


class AddStatePatchOperationRepairInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: ChapterPatchOperationInput


class UpdateStatePatchOperationRepairInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_meaning: str = Field(min_length=1, max_length=4_000)
    operation: ChapterPatchOperationInput


class DeleteCandidateRepairItemInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    semantic_area: Literal[
        "avoidance",
        "creative_freedom",
        "open_question",
        "confirmed_decision",
        "comparison_title",
        "event",
        "character_change",
        "relationship_change",
        "world_fact_observation",
        "foreshadowing_observation",
        "canon_change",
    ]
    current_meaning: str = Field(min_length=1, max_length=4_000)


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
    repair_phases = frozenset({"direction", "planning", "revision", "chapter"})
    repair_roles: frozenset[AgentRole] = frozenset(
        {"book", "story_arc", "chapter"}
    )
    registry.register(
        ToolSpec(
            name="open_candidate_repair",
            version=1,
            description=(
                "Open the Harness-owned repair workspace and return only semantic candidate "
                "content. Internal identities and versions remain hidden."
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
                "assigns stable identity and binds confirmed-decision evidence."
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
                "Update one Book item selected by its current semantic meaning. Internal item "
                "identity remains Harness-owned."
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
                "Add one semantic Chapter observation; Harness binds exact draft evidence."
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
            description=(
                "Update one Chapter observation selected by current meaning; Harness owns "
                "identity and exact evidence."
            ),
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
                "Add one semantic Chapter canon change; Harness resolves canonical identity, "
                "version, file, and exact evidence."
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
            description=(
                "Update one semantic canon change selected by its current meaning. Internal "
                "operation identity remains Harness-owned."
            ),
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
            description=(
                "Delete one authorized structured item selected by its semantic meaning."
            ),
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
    component = _repair_text_component(context, request.content_kind)
    workspace = replace_text_component(
        context,
        ensure_repair_workspace(context),
        component=component,
        content=request.content,
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
    collection = _book_collection(request.content_kind)
    workspace = ensure_repair_workspace(context)
    primary = request.primary
    if collection == "confirmed_decision_coverage":
        confirmed = _control_string_list(context, "confirmed_decisions")
        resolved = resolve_semantic_choice(
            primary,
            {item: [item] for item in confirmed},
        )
        if resolved is None:
            raise ToolHandlerError(
                "repair_book_authority_unresolved",
                "The semantic decision does not resolve uniquely to a confirmed decision.",
                recoverable=True,
                allowed_actions=["open_candidate_repair"],
            )
        primary = resolved
    secondary = _book_repair_secondary(
        workspace,
        collection=collection,
        primary=primary,
        rationale=request.rationale,
    )
    workspace, _ = add_book_item(
        context,
        workspace,
        collection=collection,
        primary=primary,
        secondary=secondary,
    )
    return _repair_workspace_plan(context, workspace)


def _update_book_repair_item(
    context: ToolExecutionContext, arguments: BaseModel
) -> ToolExecutionPlan:
    request = _typed(arguments, UpdateBookRepairItemInput)
    collection = _book_collection(request.content_kind)
    workspace = ensure_repair_workspace(context)
    item_id = _resolve_workspace_item_id(
        workspace,
        component=_book_collection_component_name(collection),
        collection=collection,
        semantic_hint=request.current_meaning,
    )
    current_value = _workspace_item_by_id(workspace, item_id)
    primary = request.primary
    if collection == "confirmed_decision_coverage":
        if not isinstance(current_value, dict) or not isinstance(
            current_value.get("decision"), str
        ):
            raise ToolHandlerError(
                "repair_book_authority_invalid",
                "Confirmed decision coverage is structurally invalid.",
                recoverable=False,
            )
        primary = current_value["decision"]
    secondary = _book_repair_secondary(
        workspace,
        collection=collection,
        primary=primary,
        rationale=request.rationale,
    )
    value = book_item_update_value(
        workspace,
        item_id=item_id,
        primary=primary,
        secondary=secondary,
    )
    workspace = update_structured_item(
        context,
        workspace,
        item_id=item_id,
        value=value,
    )
    return _repair_workspace_plan(context, workspace)


def _add_chapter_observation_repair(
    context: ToolExecutionContext, arguments: BaseModel
) -> ToolExecutionPlan:
    request = _typed(arguments, AddChapterObservationRepairInput)
    collection = _observation_collection(request.observation_kind)
    workspace = ensure_repair_workspace(context)
    draft = _repair_workspace_draft(workspace)
    quote = resolve_semantic_evidence_quote(draft, [request.summary])
    if quote is None:
        raise ToolHandlerError(
            "candidate_observation_not_supported",
            "The new observation has no uniquely bindable support in the draft.",
            recoverable=True,
            allowed_actions=["replace_candidate_text"],
        )
    workspace, _ = add_chapter_observation(
        context,
        workspace,
        collection=collection,
        summary=request.summary,
        evidence_quote=quote,
    )
    return _repair_workspace_plan(context, workspace)


def _update_chapter_observation_repair(
    context: ToolExecutionContext, arguments: BaseModel
) -> ToolExecutionPlan:
    request = _typed(arguments, UpdateChapterObservationRepairInput)
    collection = _observation_collection(request.observation_kind)
    workspace = ensure_repair_workspace(context)
    item_id = _resolve_workspace_item_id(
        workspace,
        component="observations",
        collection=collection,
        semantic_hint=request.current_meaning,
    )
    quote = resolve_semantic_evidence_quote(
        _repair_workspace_draft(workspace),
        [request.summary],
    )
    if quote is None:
        raise ToolHandlerError(
            "candidate_observation_not_supported",
            "The revised observation has no uniquely bindable support in the draft.",
            recoverable=True,
            allowed_actions=["replace_candidate_text"],
        )
    workspace = update_structured_item(
        context,
        workspace,
        item_id=item_id,
        value={
            "summary": request.summary.strip(),
            "evidence_quote": quote,
        },
    )
    return _repair_workspace_plan(context, workspace)


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
    workspace = ensure_repair_workspace(context)
    operation = _semantic_patch_operation(
        context,
        request.operation,
        final_path=f"chapters/{chapter_id}/final.md",
        draft=_repair_workspace_draft(workspace),
    )
    workspace, _ = add_state_patch_operation(
        context,
        workspace,
        operation=operation,
    )
    return _repair_workspace_plan(context, workspace)


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
    workspace = ensure_repair_workspace(context)
    item_id = _resolve_workspace_item_id(
        workspace,
        component="state_patch",
        collection="operations",
        semantic_hint=request.current_meaning,
    )
    current_operation = _workspace_item_by_id(workspace, item_id)
    requested_target_file = _canon_target_file(request.operation.entity_kind)
    bound_target_id = None
    if (
        isinstance(current_operation, dict)
        and current_operation.get("target_file") == requested_target_file
        and isinstance(current_operation.get("target_id"), str)
    ):
        bound_target_id = current_operation["target_id"]
    operation = _semantic_patch_operation(
        context,
        request.operation,
        final_path=f"chapters/{chapter_id}/final.md",
        draft=_repair_workspace_draft(workspace),
        bound_target_id=bound_target_id,
    )
    workspace = update_structured_item(
        context,
        workspace,
        item_id=item_id,
        value=operation,
    )
    return _repair_workspace_plan(context, workspace)


def _delete_candidate_repair_item(
    context: ToolExecutionContext, arguments: BaseModel
) -> ToolExecutionPlan:
    request = _typed(arguments, DeleteCandidateRepairItemInput)
    workspace = ensure_repair_workspace(context)
    component, collection = _repair_item_location(request.semantic_area)
    item_id = _resolve_workspace_item_id(
        workspace,
        component=cast(Any, component),
        collection=collection,
        semantic_hint=request.current_meaning,
    )
    workspace = delete_structured_item(
        context,
        workspace,
        item_id=item_id,
    )
    return _repair_workspace_plan(context, workspace)


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
) -> ToolExecutionPlan:
    relative = repair_workspace_relative(context)
    content: dict[str, Any] = {
        "status": "semantic_change_stored",
        "mutation_count": len(workspace.mutations),
    }
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


def _bind_book_discussion_update(
    context: ToolExecutionContext,
    request: BookDiscussionUpdateInput,
) -> dict[str, Any]:
    expected_revision = _required_expected_revision(context)
    confirmed = _control_string_list(context, "confirmed_decisions")
    for decision in request.newly_confirmed_decisions:
        stripped = decision.strip()
        if stripped and stripped not in confirmed:
            confirmed.append(stripped)

    superseded_payload = context.control_data.get("superseded_decisions", [])
    durable_superseded = [
        SupersededDecision.model_validate(item)
        for item in superseded_payload
        if isinstance(item, dict)
    ]
    turn = context.control_data.get("turn")
    durable_turn = turn if isinstance(turn, int) and not isinstance(turn, bool) else 1
    for item in request.superseded_decisions:
        resolved = resolve_semantic_choice(
            item.prior_meaning,
            {decision: [decision] for decision in confirmed},
        )
        if resolved is None:
            raise ToolHandlerError(
                "book_superseded_decision_unresolved",
                "A superseded decision does not resolve uniquely to current Book meaning.",
                recoverable=True,
                allowed_actions=["retry:submit_book_discussion_update"],
            )
        durable_superseded.append(
            SupersededDecision(
                turn=max(durable_turn, 1),
                decision=resolved,
                replacement=item.replacement,
                reason=item.reason,
                user_evidence=item.user_evidence,
            )
        )

    selected_title = _optional_control_string(context, "selected_title")
    if request.newly_selected_title and request.newly_selected_title.strip():
        selected_title = request.newly_selected_title.strip()
    if request.readiness.status == "ready" and not selected_title:
        raise ToolHandlerError(
            "book_title_not_confirmed",
            "Book Direction cannot become review-ready before a semantic title selection.",
            recoverable=True,
            allowed_actions=["retry:submit_book_discussion_update"],
        )

    question = _normalize_question(request.question)
    suggestions = _bind_setup_suggestions(context, request.suggestions)
    if request.readiness.status == "ready":
        question = None
        suggestions = []
    bound = BoundBookDiscussionUpdate(
        expected_revision=expected_revision,
        reply=request.reply,
        direction_draft=request.direction_draft,
        discussion_summary=request.discussion_summary,
        confirmed_decisions=confirmed,
        superseded_decisions=durable_superseded,
        unresolved_questions=request.unresolved_questions,
        assumptions=request.assumptions,
        contradictions=request.contradictions,
        selected_title=selected_title,
        question=question,
        suggestions=suggestions,
        readiness=request.readiness,
    )
    return bound.model_dump(mode="json")


def _normalize_question(question: str | None) -> str | None:
    if question is None or not question.strip():
        return None
    normalized = re.sub(r"[?？]+", "，", question.strip()).rstrip("，,。!！ ")
    return normalized + "？"


def _bind_setup_suggestions(
    context: ToolExecutionContext,
    suggestions: list[SemanticSetupSuggestion],
) -> list[SetupSuggestion]:
    first_recommended = next(
        (index for index, item in enumerate(suggestions) if item.recommended),
        0,
    )
    return [
        SetupSuggestion(
            id=(
                "suggestion-"
                + sha256(
                    (
                        f"{context.candidate_run_id}\x1f{index}\x1f"
                        f"{item.label}\x1f{item.message}"
                    ).encode("utf-8")
                ).hexdigest()[:20]
            ),
            label=item.label,
            message=item.message,
            rationale=item.rationale,
            recommended=index == first_recommended,
        )
        for index, item in enumerate(suggestions)
    ]


def _book_collection_component_name(collection: str) -> str:
    if collection.startswith("constraints."):
        return "constraints"
    if collection == "confirmed_decision_coverage":
        return "confirmed_decision_coverage"
    return "recommended_titles"


def _repair_text_component(
    context: ToolExecutionContext,
    content_kind: str,
) -> str:
    role_and_component = {
        "book_direction": ("book", "direction"),
        "rolling_plan": ("book", "rolling_plan"),
        "arc_plan": ("story_arc", "plan"),
        "arc_change_summary": ("story_arc", "change_summary"),
        "chapter_plan": ("chapter", "plan"),
        "chapter_draft": ("chapter", "draft"),
    }
    expected_role, component = role_and_component[content_kind]
    if context.identity.role != expected_role:
        raise ToolHandlerError(
            "repair_semantic_area_mismatch",
            "The selected semantic content kind does not belong to this Agent.",
            recoverable=True,
            allowed_actions=["open_candidate_repair"],
        )
    return component


def _book_collection(content_kind: str) -> str:
    return {
        "avoidance": "constraints.must_avoid",
        "creative_freedom": "constraints.creative_freedoms",
        "open_question": "constraints.open_decisions",
        "confirmed_decision": "confirmed_decision_coverage",
        "comparison_title": "recommended_titles",
    }[content_kind]


def _observation_collection(observation_kind: str) -> str:
    return {
        "event": "events",
        "character_change": "character_changes",
        "relationship_change": "relationship_changes",
        "world_fact": "world_fact_candidates",
        "foreshadowing": "foreshadowing_candidates",
    }[observation_kind]


def _repair_item_location(semantic_area: str) -> tuple[str, str]:
    return {
        "avoidance": ("constraints", "constraints.must_avoid"),
        "creative_freedom": ("constraints", "constraints.creative_freedoms"),
        "open_question": ("constraints", "constraints.open_decisions"),
        "confirmed_decision": (
            "confirmed_decision_coverage",
            "confirmed_decision_coverage",
        ),
        "comparison_title": ("recommended_titles", "recommended_titles"),
        "event": ("observations", "events"),
        "character_change": ("observations", "character_changes"),
        "relationship_change": ("observations", "relationship_changes"),
        "world_fact_observation": ("observations", "world_fact_candidates"),
        "foreshadowing_observation": (
            "observations",
            "foreshadowing_candidates",
        ),
        "canon_change": ("state_patch", "operations"),
    }[semantic_area]


def _resolve_workspace_item_id(
    workspace: Any,
    *,
    component: str,
    collection: str,
    semantic_hint: str,
) -> str:
    choices: dict[str, list[str]] = {}
    for handle in workspace.item_handles:
        if handle.component != component or handle.collection != collection:
            continue
        value = workspace_item_value(workspace, handle)
        choices[handle.item_id] = _semantic_labels(value)
    resolved = resolve_semantic_choice(semantic_hint, choices)
    if resolved is None:
        raise ToolHandlerError(
            "repair_semantic_target_unresolved",
            "The requested semantic item does not resolve uniquely in the repair workspace.",
            recoverable=True,
            content={"selection": "ambiguous_or_missing"},
            allowed_actions=["open_candidate_repair"],
        )
    return resolved


def _book_repair_secondary(
    workspace: Any,
    *,
    collection: str,
    primary: str,
    rationale: str | None,
) -> str | None:
    if collection == "recommended_titles":
        return rationale
    if collection != "confirmed_decision_coverage":
        return None
    direction = workspace.current_components.get("direction")
    if not isinstance(direction, str) or not direction.strip():
        raise ToolHandlerError(
            "repair_book_direction_missing",
            "Harness cannot bind decision support without the current Book Direction.",
            recoverable=False,
        )
    return (
        resolve_semantic_evidence_quote(direction, [primary])
        or direction.strip()[:1_000]
    )


def _workspace_item_by_id(workspace: Any, item_id: str) -> Any:
    for handle in workspace.item_handles:
        if handle.item_id == item_id:
            return workspace_item_value(workspace, handle)
    raise ToolHandlerError(
        "repair_item_not_found",
        "Harness could not resolve the selected semantic item.",
        recoverable=True,
        allowed_actions=["open_candidate_repair"],
    )


def _repair_workspace_draft(workspace: Any) -> str:
    draft = workspace.current_components.get("draft")
    if not isinstance(draft, str) or not draft.strip():
        raise ToolHandlerError(
            "repair_draft_missing",
            "Chapter semantic repair requires a current draft.",
            recoverable=False,
        )
    return draft


def _submit_book_discussion_update(
    context: ToolExecutionContext, arguments: BaseModel
) -> ToolExecutionPlan:
    request = _typed(arguments, BookDiscussionUpdateInput)
    expected_revision = _required_expected_revision(context)
    payload = _bind_book_discussion_update(context, request)
    relative = _candidate_relative(context, "discussion-update.json")
    return _terminal_candidate_plan(
        context,
        payload,
        relative,
        checkpoint=f"book-discussion:{expected_revision + 1}",
        summary="Book discussion update submitted.",
    )


def _submit_book_direction_candidate(
    context: ToolExecutionContext, arguments: BaseModel
) -> ToolExecutionPlan:
    request = _typed(arguments, BookDirectionCandidateInput)
    expected_revision = _required_expected_revision(context)
    if context.expected_candidate_revision is None:
        raise ToolHandlerError(
            "missing_expected_candidate_revision",
            "Book Direction activation is missing its Harness candidate revision target.",
            recoverable=False,
        )
    confirmed_decisions = _control_string_list(context, "confirmed_decisions")
    selected_title = _control_string(context, "selected_title")
    constraints = BookDirectionConstraints(
        confirmed=confirmed_decisions,
        must_preserve=confirmed_decisions,
        must_avoid=request.constraints.must_avoid,
        creative_freedoms=request.constraints.creative_freedoms,
        open_decisions=request.constraints.open_decisions,
    )
    coverage = [
        ConfirmedDecisionCoverage(
            decision=decision,
            candidate_evidence=(
                resolve_semantic_evidence_quote(
                    request.direction_markdown,
                    [decision],
                )
                or request.direction_markdown.strip()[:1_000]
            ),
        )
        for decision in confirmed_decisions
    ]
    comparison_titles = [
        item
        for item in request.comparison_titles
        if item.title.casefold() != selected_title.casefold()
    ]
    recommended_titles = [
        BookTitleSuggestion(
            title=selected_title,
            rationale="Harness-preserved user-confirmed formal title.",
        ),
        *comparison_titles[:4],
    ]
    if len(recommended_titles) < 3:
        raise ToolHandlerError(
            "book_comparison_titles_insufficient",
            "Provide at least two semantic comparison titles distinct from the formal title.",
            recoverable=True,
            allowed_actions=["retry:submit_book_direction_candidate"],
        )
    payload = {
        "expected_revision": expected_revision,
        "candidate_revision": context.expected_candidate_revision,
        "direction_markdown": request.direction_markdown,
        "constraints": constraints.model_dump(mode="json"),
        "confirmed_decision_coverage": [
            item.model_dump(mode="json") for item in coverage
        ],
        "recommended_titles": [
            item.model_dump(mode="json") for item in recommended_titles
        ],
        "rolling_plan_markdown": request.rolling_plan_markdown,
    }
    _enforce_repair_scope(
        context,
        BookCandidateSnapshot(
            direction=request.direction_markdown,
            constraints=constraints.model_dump(mode="json"),
            confirmed_decision_coverage=[
                item.model_dump(mode="json") for item in coverage
            ],
            recommended_titles=[
                item.model_dump(mode="json") for item in recommended_titles
            ],
            rolling_plan=request.rolling_plan_markdown,
        ),
    )
    relative = _candidate_relative(context, "book-direction.json")
    return _terminal_candidate_plan(
        context,
        payload,
        relative,
        checkpoint=f"book-direction:{context.expected_candidate_revision}",
        summary="Book Direction candidate submitted for evaluation.",
    )


def _submit_story_arc_candidate(
    context: ToolExecutionContext, arguments: BaseModel
) -> ToolExecutionPlan:
    request = _typed(arguments, StoryArcCandidateInput)
    expected_revision = _required_expected_revision(context)
    arc_id = context.identity.scope_id
    if arc_id is None:
        raise ToolHandlerError(
            "arc_ownership_mismatch",
            "Story Arc activation has no Harness-owned arc identity.",
            recoverable=False,
        )
    intent = "revise" if context.phase == "revision" else "create"
    payload = {
        "expected_revision": expected_revision,
        "intent": intent,
        "arc_id": arc_id,
        **request.model_dump(mode="json"),
    }
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
        payload,
        relative,
        checkpoint=f"story-arc:{arc_id}:{expected_revision + 1}",
        summary="Story Arc candidate submitted for evaluation.",
    )


def _plan_chapter_candidate(
    context: ToolExecutionContext, arguments: BaseModel
) -> ToolExecutionPlan:
    request = _typed(arguments, ChapterPlanCandidateInput)
    chapter_id = _required_scope_id(context, role="chapter")
    expected_revision = _required_expected_revision(context)
    root = _candidate_root(context)
    plan_path = root / "plan.md"
    state_path = root / "workspace.json"
    state = _workspace_state(context)
    current = int(state.get("plan_revision", 0))
    plan_revision = current + 1
    state.update(
        {
            "schema_version": 1,
            "chapter_id": chapter_id,
            "expected_revision": expected_revision,
            "plan_revision": plan_revision,
            "canon_versions": read_canon_versions(context.project_path),
        }
    )
    return ToolExecutionPlan(
        content={"status": "plan_stored"},
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
    _required_scope_id(context, role="chapter")
    _required_expected_revision(context)
    root = _candidate_root(context)
    draft_path = root / "draft.md"
    state_path = root / "workspace.json"
    state = _workspace_state(context)
    if int(state.get("plan_revision", 0)) < 1:
        raise ToolHandlerError(
            "stale_plan_revision",
            "Draft does not reference the current candidate plan revision.",
            recoverable=True,
            allowed_actions=["reload_chapter_workspace"],
        )
    draft_revision = int(state.get("draft_revision", 0)) + 1
    content = request.content.strip()
    state["draft_revision"] = draft_revision
    state["draft_sha256"] = sha256(content.encode("utf-8")).hexdigest()
    return ToolExecutionPlan(
        content={
            "status": "draft_stored",
            "characters": len(content),
        },
        files={
            draft_path.as_posix(): content.rstrip() + "\n",
            state_path.as_posix(): json_document(state),
        },
        artifact_paths=[draft_path.as_posix(), state_path.as_posix()],
        allowed_actions=["write_chapter_draft", "inspect_chapter_consistency"],
    )


def _inspect_chapter_consistency(
    context: ToolExecutionContext, arguments: BaseModel
) -> ToolExecutionPlan:
    _typed(arguments, InspectChapterConsistencyInput)
    chapter_id = _required_scope_id(context, role="chapter")
    _required_expected_revision(context)
    root = _candidate_root(context)
    draft_path = root / "draft.md"
    state = _workspace_state(context)
    draft_revision = int(state.get("draft_revision", 0))
    if draft_revision < 1:
        raise ToolHandlerError(
            "stale_draft_revision",
            "Consistency inspection requires a current candidate draft.",
            recoverable=True,
            allowed_actions=["write_chapter_draft"],
        )
    draft = _read_required_text(
        context.project_path / draft_path,
        code="candidate_draft_missing",
    )
    evidence = {
        "schema_version": 1,
        "chapter_id": chapter_id,
        "draft_revision": draft_revision,
        "draft_sha256": sha256(draft.encode("utf-8")).hexdigest(),
        "characters": len(draft),
        "paragraphs": len([item for item in draft.split("\n\n") if item.strip()]),
        "empty": not bool(draft.strip()),
        "semantic_verdict": None,
    }
    relative = root / "consistency.json"
    return ToolExecutionPlan(
        content={
            "characters": evidence["characters"],
            "paragraphs": evidence["paragraphs"],
            "empty": evidence["empty"],
        },
        files={relative.as_posix(): json_document(evidence)},
        artifact_paths=[relative.as_posix()],
        allowed_actions=[
            "write_chapter_draft",
            "write_chapter_observations",
            "write_chapter_state_patch",
        ],
    )


def _write_chapter_observations(
    context: ToolExecutionContext, arguments: BaseModel
) -> ToolExecutionPlan:
    request = _typed(arguments, WriteChapterObservationsInput)
    _required_scope_id(context, role="chapter")
    _required_expected_revision(context)
    root = _candidate_root(context)
    state_path = root / "workspace.json"
    component_path = root / "observations-input.json"
    state = _workspace_state(context)
    draft_revision = int(state.get("draft_revision", 0))
    if draft_revision < 1:
        raise ToolHandlerError(
            "candidate_draft_missing",
            "Semantic observations require a current candidate draft.",
            recoverable=True,
            allowed_actions=["write_chapter_draft"],
        )
    payload = request.observations.model_dump(mode="json")
    state["observations_draft_revision"] = draft_revision
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
            "status": "observations_stored",
            "observation_count": observation_count,
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
    _required_scope_id(context, role="chapter")
    _required_expected_revision(context)
    root = _candidate_root(context)
    state_path = root / "workspace.json"
    component_path = root / "state-patch-input.json"
    state = _workspace_state(context)
    draft_revision = int(state.get("draft_revision", 0))
    if draft_revision < 1:
        raise ToolHandlerError(
            "candidate_draft_missing",
            "Semantic canon changes require a current candidate draft.",
            recoverable=True,
            allowed_actions=["write_chapter_draft"],
        )
    payload = request.state_patch.model_dump(mode="json")
    state["state_patch_draft_revision"] = draft_revision
    state["state_patch_sha256"] = _json_payload_sha256(payload)
    return ToolExecutionPlan(
        content={
            "status": "canon_changes_stored",
            "operation_count": len(request.state_patch.operations),
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
    chapter_id = _required_scope_id(context, role="chapter")
    expected_revision = _required_expected_revision(context)
    expected_candidate_revision = (
        context.repair_contract.next_candidate_revision
        if context.repair_contract is not None
        else 1
    )
    root = _candidate_root(context)
    state = _workspace_state(context)
    plan_revision = int(state.get("plan_revision", 0))
    draft_revision = int(state.get("draft_revision", 0))
    if plan_revision < 1:
        raise ToolHandlerError(
            "stale_plan_revision",
            "Chapter submission references a stale plan revision.",
            recoverable=True,
            allowed_actions=["reload_chapter_workspace"],
        )
    if draft_revision < 1:
        raise ToolHandlerError(
            "stale_draft_revision",
            "Chapter submission references a stale draft revision.",
            recoverable=True,
            allowed_actions=["reload_chapter_workspace"],
        )
    _expect_component_revision(
        state,
        component="observations",
        draft_revision=draft_revision,
        write_action="write_chapter_observations",
    )
    _expect_component_revision(
        state,
        component="state_patch",
        draft_revision=draft_revision,
        write_action="write_chapter_state_patch",
    )
    request = _normalize_chapter_submission(
        context,
        tool_request,
        chapter_id=chapter_id,
        expected_revision=expected_revision,
        candidate_revision=expected_candidate_revision,
        plan_revision=plan_revision,
        draft_revision=draft_revision,
    )
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
        checkpoint_id=f"chapter:{chapter_id}:{expected_candidate_revision}",
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
    *,
    chapter_id: str,
    expected_revision: int,
    candidate_revision: int,
    plan_revision: int,
    draft_revision: int,
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
    draft = _read_required_text(
        context.project_path / root / "draft.md",
        code="candidate_draft_missing",
    )

    def observations(
        collection: str,
        values: list[ChapterObservationInput],
    ) -> list[dict[str, str]]:
        normalized: list[dict[str, str]] = []
        for index, item in enumerate(values):
            quote = resolve_semantic_evidence_quote(draft, [item.summary])
            if quote is None:
                raise ToolHandlerError(
                    "candidate_observation_not_supported",
                    "A semantic observation has no uniquely bindable support in the draft.",
                    recoverable=True,
                    content={"collection": collection, "semantic_index": index},
                    allowed_actions=[
                        "write_chapter_observations",
                        "write_chapter_draft",
                    ],
                )
            normalized.append(
                {
                    "id": _candidate_item_id(
                        context,
                        "observations",
                        collection,
                        index,
                    ),
                    "summary": item.summary,
                    "evidence_quote": quote,
                }
            )
        return normalized

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
        requires_commit=bool(state_patch_input.operations),
    )
    final_path = f"chapters/{chapter_id}/final.md"
    observations_path = f"chapters/{chapter_id}/observations.json"
    operations = [
        _normalize_patch_operation(
            context,
            item,
            final_path=final_path,
            draft=draft,
            canon_versions=canon_versions,
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
        chapter_id=chapter_id,
        expected_revision=expected_revision,
        candidate_revision=candidate_revision,
        plan_revision=plan_revision,
        draft_revision=draft_revision,
        summary=request.summary,
        observations=normalized_observations,
        state_patch=normalized_patch,
    )


def _normalize_patch_operation(
    context: ToolExecutionContext,
    operation: ChapterPatchOperationInput,
    *,
    final_path: str,
    draft: str,
    canon_versions: dict[str, int],
    item_id: str | None = None,
) -> CandidatePatchOperation:
    semantic = _semantic_patch_operation(
        context,
        operation,
        final_path=final_path,
        draft=draft,
    )
    return CandidatePatchOperation(
        id=item_id,
        expected_version=canon_versions[semantic["target_file"]],
        **semantic,
    )


def _semantic_patch_operation(
    context: ToolExecutionContext,
    operation: ChapterPatchOperationInput,
    *,
    final_path: str,
    draft: str,
    bound_target_id: str | None = None,
) -> dict[str, Any]:
    target_file = _canon_target_file(operation.entity_kind)
    target_id = bound_target_id or _resolve_canon_target_id(
        context.project_path,
        target_file=target_file,
        semantic_name=operation.entity_name,
        allow_create=operation.change_kind == "establish",
    )
    if operation.change_kind == "remove":
        op = "delete"
        value: dict[str, Any] = {}
    else:
        op = "upsert"
        current = _canon_item(context.project_path, target_file, target_id)
        value = dict(current) if isinstance(current, dict) else {}
        value.update(
            {
                "name": operation.entity_name.strip(),
                "semantic_state": operation.resulting_state.strip(),
            }
        )
    evidence_quote = resolve_semantic_evidence_quote(
        draft,
        [
            operation.evidence_hint,
            operation.rationale,
            operation.entity_name,
            operation.resulting_state,
        ],
    )
    if evidence_quote is None:
        raise ToolHandlerError(
            "candidate_canon_fact_not_supported",
            "A proposed canon change has no uniquely bindable support in the draft.",
            recoverable=True,
            content={
                "entity_kind": operation.entity_kind,
                "entity_name": operation.entity_name,
            },
            allowed_actions=["write_chapter_state_patch", "write_chapter_draft"],
        )
    return {
        "op": op,
        "target_file": target_file,
        "target_id": target_id,
        "value": value,
        "evidence": [
            PatchEvidence(file=final_path, quote=evidence_quote).model_dump(mode="json")
        ],
        "rationale": operation.rationale,
    }


def _canon_target_file(entity_kind: str) -> str:
    targets = {
        "character": "canon/characters.json",
        "relationship": "canon/relationships.json",
        "world_fact": "canon/world_facts.json",
        "foreshadowing": "canon/foreshadowing.json",
    }
    return targets[entity_kind]


def _resolve_canon_target_id(
    project_path: Path,
    *,
    target_file: str,
    semantic_name: str,
    allow_create: bool,
) -> str:
    payload = read_json(project_path / target_file, default=None)
    items = payload.get("items") if isinstance(payload, dict) else None
    choices: dict[str, list[str]] = {}
    if isinstance(items, dict):
        for key, value in items.items():
            if isinstance(key, str):
                choices[key] = _semantic_labels(value)
    resolved = resolve_semantic_choice(semantic_name, choices)
    if resolved is not None:
        return resolved
    if not allow_create:
        raise ToolHandlerError(
            "canon_entity_not_resolved",
            "The semantic canon target does not resolve uniquely in current canon.",
            recoverable=True,
            content={"semantic_name": semantic_name},
            allowed_actions=["write_chapter_state_patch"],
        )
    seed = f"{target_file}\x1f{semantic_name.strip().casefold()}"
    return "canon-" + sha256(seed.encode("utf-8")).hexdigest()[:24]


def _canon_item(project_path: Path, target_file: str, target_id: str) -> object:
    payload = read_json(project_path / target_file, default=None)
    items = payload.get("items") if isinstance(payload, dict) else None
    return items.get(target_id) if isinstance(items, dict) else None


def _semantic_labels(value: Any) -> list[str]:
    labels: list[str] = []
    if isinstance(value, str):
        labels.append(value)
    elif isinstance(value, dict):
        for key, item in value.items():
            if key in {"id", "name", "title", "label", "subject", "object"}:
                if isinstance(item, str):
                    labels.append(item)
            elif isinstance(item, (str, int, float, bool)):
                labels.append(str(item))
    elif isinstance(value, list):
        labels.extend(str(item) for item in value if isinstance(item, (str, int)))
    return labels


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


def _terminal_candidate_plan(
    context: ToolExecutionContext,
    request: BaseModel | dict[str, Any],
    relative: Path,
    *,
    checkpoint: str,
    summary: str,
) -> ToolExecutionPlan:
    payload = (
        request.model_dump(mode="json")
        if isinstance(request, BaseModel)
        else request
    )
    return ToolExecutionPlan(
        content={
            "summary": summary,
            "candidate_path": relative.as_posix(),
            "promotable": False,
        },
        files={relative.as_posix(): json_document(payload)},
        checkpoint_id=checkpoint,
        artifact_paths=[relative.as_posix()],
        allowed_actions=["evaluate_candidate"],
    )


def _required_expected_revision(context: ToolExecutionContext) -> int:
    value = context.expected_revision
    if value is None:
        raise ToolHandlerError(
            "missing_expected_revision",
            "Harness activation is missing its internal revision envelope.",
            recoverable=False,
        )
    return value


def _required_scope_id(
    context: ToolExecutionContext,
    *,
    role: AgentRole,
) -> str:
    if context.identity.role != role or context.identity.scope_id is None:
        raise ToolHandlerError(
            f"{role}_ownership_mismatch",
            f"Harness activation has no owned {role} scope.",
            recoverable=False,
        )
    return context.identity.scope_id


def _control_string_list(context: ToolExecutionContext, key: str) -> list[str]:
    value = context.control_data.get(key)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ToolHandlerError(
            "control_envelope_invalid",
            f"Harness control data is missing semantic authority: {key}.",
            recoverable=False,
        )
    return list(dict.fromkeys(item.strip() for item in value if item.strip()))


def _control_string(context: ToolExecutionContext, key: str) -> str:
    value = _optional_control_string(context, key)
    if value is None:
        raise ToolHandlerError(
            "control_envelope_invalid",
            f"Harness control data is missing semantic authority: {key}.",
            recoverable=False,
        )
    return value


def _optional_control_string(
    context: ToolExecutionContext,
    key: str,
) -> str | None:
    value = context.control_data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ToolHandlerError(
            "control_envelope_invalid",
            f"Harness control data has an invalid semantic authority value: {key}.",
            recoverable=False,
        )
    stripped = value.strip()
    return stripped or None


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
