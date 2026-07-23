from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import cast

from pydantic import BaseModel

from app.agents.contracts import (
    AgentRole,
    AgentTaskPlan,
    ArcPlanProposal,
    BookDiscussionResult,
    BookProgressAssessment,
    CapabilityName,
    ChapterDraftResult,
    ChapterObservationResult,
    ChapterPlanProposal,
    LayerEvaluationResult,
    JsonValue,
    OutputMode,
    ProfileSnapshot,
    ScopeLayer,
    finalize_chapter_prose,
)
from app.domain.arc.contracts import ArcEvaluation
from app.domain.book.contracts import BookCandidatePack, BookEvaluation

TextFinalizer = Callable[[str], BaseModel]

BOOK_DISCUSSION_INSTRUCTIONS = (
    "Advance exactly one high-value whole-book design decision while preserving the "
    "cumulative working direction. Answer and explain the current creator input in reply. "
    "If another creator decision is needed, return readiness.status='continue' and place "
    "one concrete question plus two or three actionable answers inside readiness; natural "
    "punctuation is not a control protocol. Each suggestion independently may be an ordinary "
    "answer or a formal-title choice, with formal_title set only for the latter. Return "
    "readiness.status='ready' only when the whole-book direction and a formal title are "
    "already settled. Set newly_selected_title only when the latest creator message directly "
    "selected or stated that exact title; put unselected title proposals in suggestions. "
    "Describe superseded decisions semantically. Do not copy storage IDs, locators, exact "
    "evidence strings, or other Harness-owned metadata."
)
BOOK_CANDIDATE_CONTRACT = (
    "Return semantic Book content only. Keep maximum_chapter_count greater than or equal "
    "to minimum_chapter_count. Do not invent storage IDs, approval state, routes, or commands."
)
BOOK_EVALUATION_CONTRACT = (
    "Use decision='local_repair' exactly when a non-null bounded repair_contract is needed; "
    "for pass or needs_user, repair_contract must be null. Report semantic findings only; "
    "the Harness owns approval, routing, IDs, and state changes."
)
ARC_EVALUATION_CONTRACT = (
    "Use decision='local_repair' exactly when repair_scope contains at least one bounded Arc "
    "component; otherwise repair_scope must be empty. Use escalate_to_book only for a Book-level "
    "semantic conflict. The Harness owns approval, routing, IDs, and state changes."
)
CHAPTER_EVALUATION_CONTRACT = (
    "Use decision='local_repair' exactly when repair_scope is non-empty. Use "
    "cross_loop_escalation exactly when escalation_target is arc or book; otherwise "
    "escalation_target must be null. The Harness owns approval, routing, IDs, and state changes."
)


class UnknownTaskContractError(LookupError):
    """The Harness requested a role/task/version not in the finite registry."""


@dataclass(frozen=True, slots=True)
class TaskDefinition:
    role: AgentRole
    task_kind: str
    contract_version: int
    scope_layer: ScopeLayer
    output_mode: OutputMode
    output_model: type[BaseModel]
    output_schema_id: str
    output_schema_version: int
    context_policy_id: str
    context_policy_version: int
    task_instructions: str
    rubric_id: str | None = None
    rubric_version: int | None = None
    text_finalizer: TextFinalizer | None = None

    @property
    def required_capabilities(self) -> tuple[CapabilityName, ...]:
        if self.output_mode == "native_json_schema":
            return ("native_json_schema",)
        return ("text_streaming",)

    @property
    def model_request_limit(self) -> int:
        return 2 if self.output_mode == "native_json_schema" else 1

    @property
    def output_schema(self) -> dict[str, JsonValue]:
        return cast(dict[str, JsonValue], self.output_model.model_json_schema(mode="validation"))


class TaskRegistry:
    def __init__(self, definitions: list[TaskDefinition]) -> None:
        indexed: dict[tuple[AgentRole, str, int], TaskDefinition] = {}
        for definition in definitions:
            key = (definition.role, definition.task_kind, definition.contract_version)
            if key in indexed:
                raise ValueError(f"Duplicate task contract: {key!r}")
            if (definition.rubric_id is None) != (definition.rubric_version is None):
                raise ValueError(f"Incomplete rubric identity for {key!r}")
            if definition.output_mode == "text_streaming" and definition.text_finalizer is None:
                raise ValueError(f"Text task {key!r} requires an explicit pure finalizer.")
            if definition.output_mode == "native_json_schema" and definition.text_finalizer is not None:
                raise ValueError(f"Native task {key!r} cannot define a text finalizer.")
            indexed[key] = definition
        self._definitions: Mapping[tuple[AgentRole, str, int], TaskDefinition] = indexed

    def __iter__(self) -> Iterator[TaskDefinition]:
        return iter(self._definitions.values())

    def get(self, *, role: AgentRole, task_kind: str, contract_version: int) -> TaskDefinition:
        try:
            return self._definitions[(role, task_kind, contract_version)]
        except KeyError as exc:
            raise UnknownTaskContractError(
                f"Unknown Agent task contract: role={role!r}, task_kind={task_kind!r}, "
                f"version={contract_version}."
            ) from exc

    def freeze_plan(
        self,
        *,
        task_id: str,
        project_id: str,
        run_id: str,
        task_key: str,
        action_key: str,
        role: AgentRole,
        task_kind: str,
        contract_version: int,
        book_id: str,
        canon_baseline_id: str,
        semantic_goal: str,
        prompt: str,
        context_manifest: dict[str, JsonValue],
        profile_snapshot: ProfileSnapshot,
        predecessor_task_id: str | None = None,
        arc_id: str | None = None,
        chapter_id: str | None = None,
        workspace_lock_version: int | None = None,
        book_baseline_id: str | None = None,
        arc_baseline_id: str | None = None,
        chapter_baseline_id: str | None = None,
    ) -> AgentTaskPlan:
        definition = self.get(
            role=role,
            task_kind=task_kind,
            contract_version=contract_version,
        )
        return AgentTaskPlan(
            task_id=task_id,
            project_id=project_id,
            run_id=run_id,
            task_key=task_key,
            action_key=action_key,
            predecessor_task_id=predecessor_task_id,
            role=role,
            task_kind=task_kind,
            contract_version=contract_version,
            scope_layer=definition.scope_layer,
            book_id=book_id,
            arc_id=arc_id,
            chapter_id=chapter_id,
            workspace_lock_version=workspace_lock_version,
            book_baseline_id=book_baseline_id,
            arc_baseline_id=arc_baseline_id,
            chapter_baseline_id=chapter_baseline_id,
            canon_baseline_id=canon_baseline_id,
            semantic_goal=semantic_goal,
            prompt=prompt,
            context_manifest=context_manifest,
            context_policy_id=definition.context_policy_id,
            context_policy_version=definition.context_policy_version,
            output_schema_id=definition.output_schema_id,
            output_schema_version=definition.output_schema_version,
            output_schema=definition.output_schema,
            rubric_id=definition.rubric_id,
            rubric_version=definition.rubric_version,
            output_mode=definition.output_mode,
            required_capabilities=definition.required_capabilities,
            model_request_limit=definition.model_request_limit,
            profile_snapshot=profile_snapshot,
            profile_fingerprint=profile_snapshot.fingerprint,
        )


def _native(
    role: AgentRole,
    task_kind: str,
    scope_layer: ScopeLayer,
    output_model: type[BaseModel],
    *,
    context_policy_id: str,
    instructions: str,
    rubric_id: str | None = None,
) -> TaskDefinition:
    return TaskDefinition(
        role=role,
        task_kind=task_kind,
        contract_version=1,
        scope_layer=scope_layer,
        output_mode="native_json_schema",
        output_model=output_model,
        output_schema_id=f"{task_kind}-result",
        output_schema_version=1,
        context_policy_id=context_policy_id,
        context_policy_version=1,
        task_instructions=instructions,
        rubric_id=rubric_id,
        rubric_version=1 if rubric_id else None,
    )


def _text(
    task_kind: str,
    *,
    instructions: str,
) -> TaskDefinition:
    return TaskDefinition(
        role="chapter_writer",
        task_kind=task_kind,
        contract_version=1,
        scope_layer="chapter",
        output_mode="text_streaming",
        output_model=ChapterDraftResult,
        output_schema_id=f"{task_kind}-result",
        output_schema_version=1,
        context_policy_id="chapter-prose-context-v1",
        context_policy_version=1,
        task_instructions=instructions,
        text_finalizer=finalize_chapter_prose,
    )


DEFAULT_TASK_REGISTRY = TaskRegistry(
    [
        _native(
            "book_strategist",
            "book.discuss",
            "book",
            BookDiscussionResult,
            context_policy_id="book-discussion-context-v1",
            instructions=BOOK_DISCUSSION_INSTRUCTIONS,
        ),
        _native(
            "book_strategist",
            "book.synthesize",
            "book",
            BookCandidatePack,
            context_policy_id="book-synthesis-context-v1",
            instructions=(
                "Synthesize the frozen creator brief and discussion into one coherent Book "
                f"candidate. {BOOK_CANDIDATE_CONTRACT}"
            ),
        ),
        _native(
            "book_strategist",
            "book.revise",
            "book",
            BookCandidatePack,
            context_policy_id="book-revision-context-v1",
            instructions=(
                "Revise only the Book-level intent authorized by the frozen change request. "
                f"{BOOK_CANDIDATE_CONTRACT}"
            ),
        ),
        _native(
            "book_strategist",
            "book.repair",
            "book",
            BookCandidatePack,
            context_policy_id="book-repair-context-v1",
            instructions=(
                "Apply only the evaluator-authorized Book repair scope; preserve all other "
                f"decisions. {BOOK_CANDIDATE_CONTRACT}"
            ),
        ),
        _native(
            "book_strategist",
            "book.assess_progress_or_completion",
            "book",
            BookProgressAssessment,
            context_policy_id="book-completion-context-v1",
            instructions=(
                "Assess whether the approved completion contract is met at this safe Arc "
                "boundary. Return only the semantic next-step decision, rationale, and still "
                "unresolved requirements; do not route or mutate the Run."
            ),
        ),
        *[
            _native(
                "arc_planner",
                task_kind,
                "arc",
                ArcPlanProposal,
                context_policy_id=f"{task_kind.replace('.', '-')}-context-v1",
                instructions=instructions,
            )
            for task_kind, instructions in (
                (
                    "arc.plan",
                    "Plan only the next Story Arc from approved Book and committed Canon facts.",
                ),
                (
                    "arc.revise",
                    "Revise the current Arc only within the explicit Arc-level change request.",
                ),
                (
                    "arc.repair",
                    "Apply only the evaluator-authorized Arc repair scope and preserve the rest.",
                ),
            )
        ],
        _native(
            "chapter_writer",
            "chapter.plan",
            "chapter",
            ChapterPlanProposal,
            context_policy_id="chapter-plan-context-v1",
            instructions="Plan one chapter within the frozen Book, Arc, and Canon contracts.",
        ),
        _native(
            "chapter_writer",
            "chapter.revise.plan",
            "chapter",
            ChapterPlanProposal,
            context_policy_id="chapter-revision-plan-context-v1",
            instructions="Revise the Chapter plan only within the explicit Chapter-level request.",
        ),
        _text(
            "chapter.draft",
            instructions="Write only the complete chapter prose. Do not emit JSON, metadata, or commentary.",
        ),
        _text(
            "chapter.revise.draft",
            instructions="Return the complete revised chapter prose only, preserving all unaffected facts.",
        ),
        _native(
            "chapter_writer",
            "chapter.observe",
            "chapter",
            ChapterObservationResult,
            context_policy_id="chapter-observation-context-v1",
            instructions="Observe the frozen prose and propose semantic Canon changes without inventing IDs.",
        ),
        _native(
            "chapter_writer",
            "chapter.revise.observe",
            "chapter",
            ChapterObservationResult,
            context_policy_id="chapter-revision-observation-context-v1",
            instructions="Re-observe the revised prose and propose only evidence-bound Canon changes.",
        ),
        _text(
            "chapter.repair.prose",
            instructions="Return the complete repaired prose only, changing only the authorized repair scope.",
        ),
        _native(
            "chapter_writer",
            "chapter.repair.observation",
            "chapter",
            ChapterObservationResult,
            context_policy_id="chapter-observation-repair-context-v1",
            instructions="Repair only the authorized observation or Canon proposal components.",
        ),
        _native(
            "evaluator",
            "evaluate.book",
            "book",
            BookEvaluation,
            context_policy_id="book-evaluator-context-v1",
            instructions=(
                "Evaluate the frozen Book candidate against the versioned Book rubric. Never "
                f"rewrite it. {BOOK_EVALUATION_CONTRACT}"
            ),
            rubric_id="book-rubric-v1",
        ),
        _native(
            "evaluator",
            "evaluate.arc",
            "arc",
            ArcEvaluation,
            context_policy_id="arc-evaluator-context-v1",
            instructions=(
                "Evaluate the frozen Arc candidate against the approved Book and committed "
                f"Canon. {ARC_EVALUATION_CONTRACT}"
            ),
            rubric_id="arc-rubric-v1",
        ),
        _native(
            "evaluator",
            "evaluate.chapter",
            "chapter",
            LayerEvaluationResult,
            context_policy_id="chapter-evaluator-context-v1",
            instructions=(
                "Evaluate the frozen chapter candidate and evidence without producing "
                f"replacement prose. {CHAPTER_EVALUATION_CONTRACT}"
            ),
            rubric_id="chapter-rubric-v1",
        ),
        *[
            _native(
                "evaluator",
                f"verify_repair.{layer}",
                layer,
                (
                    BookEvaluation
                    if layer == "book"
                    else ArcEvaluation if layer == "arc" else LayerEvaluationResult
                ),
                context_policy_id=f"{layer}-repair-verification-context-v1",
                instructions=(
                    f"Verify only the repaired {layer} candidate against the original frozen "
                    "findings. "
                    + (
                        BOOK_EVALUATION_CONTRACT
                        if layer == "book"
                        else (
                            ARC_EVALUATION_CONTRACT
                            if layer == "arc"
                            else CHAPTER_EVALUATION_CONTRACT
                        )
                    )
                ),
                rubric_id=f"{layer}-rubric-v1",
            )
            for layer in ("book", "arc", "chapter")
        ],
    ]
)
