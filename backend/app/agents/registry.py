from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Literal, cast

from pydantic import BaseModel

from app.agents.contracts import (
    AgentRole,
    AgentTaskPlan,
    ArcPlanProposal,
    BookDiscussionResult,
    CapabilityName,
    ChapterDraftResult,
    ChapterObservationRepairPatch,
    ChapterObservationResult,
    ChapterPlanProposal,
    LayerEvaluationResult,
    JsonValue,
    OutputMode,
    ProfileSnapshot,
    ScopeLayer,
    finalize_chapter_prose,
)
from app.domain.arc.contracts import ArcEvaluation, ArcRepairPatch
from app.domain.book.contracts import BookCandidatePack, BookEvaluation, BookRepairPatch
from app.domain.evaluation import (
    ArcClosureEvaluation,
    ArcParentContractEvaluation,
    BookBoundaryEvaluation,
    BookParentContractEvaluation,
    ChapterEvidenceCorrectionEvaluation,
)

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
    "Return substantive semantic Book content only. Every required constraints, rolling-plan, "
    "and completion-requirement field must contain concrete content. Keep "
    "maximum_chapter_count greater than or equal to minimum_chapter_count. Do not invent "
    "storage IDs, approval state, routes, or commands."
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
    "Every blocking issue must name a natural-language semantic subject and a unique non-empty "
    "affected_components set. The Harness derives local repair authorization from the union; "
    "do not return a separate repair scope. Use plan as the only affected component when the "
    "mutable Chapter plan itself is infeasible but can be replaced under the same frozen Arc "
    "and Canon; the Harness will invalidate and regenerate all downstream Chapter components. "
    "During verify_repair.chapter, mark recurrence='persists_after_authorized_repair' only when "
    "the same semantic issue remains after its authorized correction; otherwise use 'new'. "
    "Use escalate_to_arc only for a concrete evidence-bound concern that the immediate parent "
    "Arc cannot remain applicable. Chapter evaluation cannot create a user wait. Never judge "
    "or route directly to Book. The Harness owns approval, routing, IDs, and state changes."
)

BOOK_CANDIDATE_RUBRIC = (
    "Check that the whole-book direction, genre/reader promise, premise engine, stable world "
    "and character invariants, long-term directions, pacing/ending tendency, Chapter range, "
    "and every keyed completion requirement are substantive, mutually coherent, feasible, "
    "and usable by later Arc planning. Empty placeholders fail. Do not request scenes or a "
    "Chapter-by-Chapter outline."
)
ARC_CANDIDATE_RUBRIC = (
    "Check that the stage purpose and desired state transition serve the current Book "
    "contract; conflict and pacing trajectories are stage-level and feasible; character, "
    "foreshadowing, and prohibition obligations are explicit; the cumulative Chapter range "
    "and selected closure checkpoint are coherent; and every closure signal is observable. "
    "Advisory beats must not become immutable Chapter slots."
)
CHAPTER_CANDIDATE_RUBRIC = (
    "Apply a minimum completion gate to the complete Chapter candidate against its frozen goal, "
    "relevant Canon, immediate Arc constraints, continuity, minimum prose usability, and "
    "evidence-supported observations. Reject only an affirmative conflict with frozen prose, "
    "committed Chapter history, Canon, the Arc contract, or the Book contract. The current "
    "Chapter may establish an ordinary fact for the first time; earlier silence and the absence "
    "of an explicit prior negation are not evidence against it. For example, a current-scene "
    "statement that a character has not recorded an event for twelve years is admissible when "
    "the scene establishes it and committed history contains no affirmative record of that "
    "event; history need not separately prove the non-recording. Require stronger support only "
    "for a claim that changes an upper contract, closes a core mystery, assigns culpability, or "
    "contradicts an explicitly uncertain governing fact. Report only blocking issues. If the "
    "mutable Chapter plan is infeasible but the same Arc and Canon remain applicable, authorize "
    "plan-only local repair. Question the Arc only when its immediate parent contract may no "
    "longer remain applicable, and never judge Book."
)
ARC_PARENT_REVIEW_RUBRIC = (
    "Review one evidence-bound Chapter-to-Arc request against the current Arc baseline and "
    "committed facts. Judge whether the Arc contract remains applicable, warrants its normal "
    "revision workflow, requires lower Chapter evidence review, cannot be judged without a "
    "specific creator-owned fact, or raises a concern requiring Book review. If lower evidence "
    "review is required, identify exactly one human-visible Chapter ordinal and a scoped "
    "observations/Canon correction goal; never emit a storage ID. Do not author a replacement "
    "Arc or claim the Book contract is invalid."
)
BOOK_PARENT_REVIEW_RUBRIC = (
    "Review one evidence-bound Arc-to-Book request against the current Book baseline and "
    "committed history. Judge whether the Book contract remains applicable, warrants its "
    "normal human-approved revision workflow, requires Arc evidence review, or cannot be "
    "judged without a specific creator-owned fact. Do not author replacement Book content."
)
ARC_CLOSURE_RUBRIC = (
    "For every frozen Arc closure signal, return satisfied, unresolved, or contradicted with "
    "committed Chapter/Canon evidence. Separately judge Arc-contract applicability, any "
    "immediate Book-review concern, and any Chapter evidence concern. If Chapter evidence "
    "correction is required, identify exactly one human-visible Chapter ordinal and a scoped "
    "observations/Canon correction goal; never emit a storage ID. Reaching the Chapter "
    "checkpoint is not semantic completion and you must not select a Domain command."
)
BOOK_BOUNDARY_RUBRIC = (
    "For every frozen Book completion requirement, return satisfied, unresolved, or "
    "contradicted with committed evidence. Judge ending trajectory as regular-Arc-needed, "
    "final-Arc-ready, completion-ready, or unable-to-judge, and separately judge Book-contract "
    "applicability. Do not invent the next Arc, author Book revisions, or select the route."
)
CHAPTER_EVIDENCE_RUBRIC = (
    "Verify only whether corrected observations and Canon intent are supported by byte-frozen "
    "approved prose and remain consistent with committed descendant facts. Do not apply the "
    "general literary Chapter rubric, propose prose changes, or reopen unrelated findings."
)


class UnknownTaskContractError(LookupError):
    """The Harness requested a role/task/version not in the finite registry."""


@dataclass(frozen=True, slots=True)
class EvaluationStrategyDefinition:
    strategy_id: str
    strategy_version: int
    task_kind: str
    scope_layer: ScopeLayer
    objective: str
    context_policy_id: str
    context_policy_version: int
    context_includes: tuple[str, ...]
    context_excludes: tuple[str, ...]
    rubric_id: str
    rubric_version: int
    rubric_text: str
    deterministic_prechecks: tuple[str, ...]
    legal_semantic_signals: tuple[str, ...]
    output_model: type[BaseModel]
    output_schema_version: int


class EvaluationStrategyRegistry:
    def __init__(self, strategies: list[EvaluationStrategyDefinition]) -> None:
        by_id: dict[tuple[str, int], EvaluationStrategyDefinition] = {}
        by_task: dict[str, EvaluationStrategyDefinition] = {}
        for strategy in strategies:
            identity = (strategy.strategy_id, strategy.strategy_version)
            if identity in by_id:
                raise ValueError(f"Duplicate evaluation strategy: {identity!r}")
            if strategy.task_kind in by_task:
                raise ValueError(
                    f"Duplicate evaluation task strategy: {strategy.task_kind!r}"
                )
            required_text = (
                strategy.objective,
                strategy.rubric_id,
                strategy.rubric_text,
            )
            if any(not value.strip() for value in required_text):
                raise ValueError(f"Incomplete evaluation strategy: {identity!r}")
            if not strategy.context_includes or not strategy.context_excludes:
                raise ValueError(
                    f"Evaluation strategy must freeze include/exclude policy: {identity!r}"
                )
            if not strategy.deterministic_prechecks:
                raise ValueError(
                    f"Evaluation strategy must freeze deterministic prechecks: {identity!r}"
                )
            if not strategy.legal_semantic_signals:
                raise ValueError(
                    f"Evaluation strategy must freeze legal signals: {identity!r}"
                )
            by_id[identity] = strategy
            by_task[strategy.task_kind] = strategy
        self._by_id = by_id
        self._by_task = by_task

    def __iter__(self) -> Iterator[EvaluationStrategyDefinition]:
        return iter(self._by_id.values())

    def for_task(self, task_kind: str) -> EvaluationStrategyDefinition:
        try:
            return self._by_task[task_kind]
        except KeyError as exc:
            raise UnknownTaskContractError(
                f"No evaluation strategy for task_kind={task_kind!r}."
            ) from exc


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
    evaluation_strategy_id: str | None = None
    evaluation_strategy_version: int | None = None
    rubric_id: str | None = None
    rubric_version: int | None = None
    rubric_text: str | None = None
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
    def __init__(
        self,
        definitions: list[TaskDefinition],
        *,
        evaluation_strategies: EvaluationStrategyRegistry | None = None,
    ) -> None:
        indexed: dict[tuple[AgentRole, str, int], TaskDefinition] = {}
        for definition in definitions:
            key = (definition.role, definition.task_kind, definition.contract_version)
            if key in indexed:
                raise ValueError(f"Duplicate task contract: {key!r}")
            if (definition.rubric_id is None) != (definition.rubric_version is None):
                raise ValueError(f"Incomplete rubric identity for {key!r}")
            if (definition.evaluation_strategy_id is None) != (
                definition.evaluation_strategy_version is None
            ):
                raise ValueError(f"Incomplete evaluation strategy identity for {key!r}")
            if definition.rubric_id is None:
                if definition.rubric_text is not None:
                    raise ValueError(f"Rubric text without rubric identity for {key!r}")
            elif definition.rubric_text is None or not definition.rubric_text.strip():
                raise ValueError(f"Missing concrete rubric text for {key!r}")
            if definition.role == "evaluator":
                if evaluation_strategies is None:
                    raise ValueError(f"Evaluator task lacks a strategy registry: {key!r}")
                strategy = evaluation_strategies.for_task(definition.task_kind)
                expected = (
                    strategy.strategy_id,
                    strategy.strategy_version,
                    strategy.rubric_id,
                    strategy.rubric_version,
                    strategy.rubric_text,
                    strategy.output_model,
                )
                actual = (
                    definition.evaluation_strategy_id,
                    definition.evaluation_strategy_version,
                    definition.rubric_id,
                    definition.rubric_version,
                    definition.rubric_text,
                    definition.output_model,
                )
                if actual != expected:
                    raise ValueError(
                        f"Evaluator task/strategy drift for {definition.task_kind!r}."
                    )
            elif definition.evaluation_strategy_id is not None:
                raise ValueError(f"Producer task cannot bind evaluator strategy: {key!r}")
            if definition.output_mode == "text_streaming" and definition.text_finalizer is None:
                raise ValueError(f"Text task {key!r} requires an explicit pure finalizer.")
            if definition.output_mode == "native_json_schema" and definition.text_finalizer is not None:
                raise ValueError(f"Native task {key!r} cannot define a text finalizer.")
            indexed[key] = definition
        self._definitions: Mapping[tuple[AgentRole, str, int], TaskDefinition] = indexed
        self.evaluation_strategies = evaluation_strategies

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
        correction_lineage_id: str | None = None,
        correction_lineage_origin: Literal[
            "review_initiated", "user_initiated"
        ]
        | None = None,
        automatic_correction_round: Literal[0, 1] | None = None,
        source_arc_parent_review_id: str | None = None,
        source_book_parent_review_id: str | None = None,
        source_arc_closure_review_id: str | None = None,
        source_book_boundary_review_id: str | None = None,
        source_chapter_arc_request_id: str | None = None,
        source_arc_book_request_id: str | None = None,
        source_arc_closure_id: str | None = None,
        source_feedback_id: str | None = None,
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
            correction_lineage_id=correction_lineage_id,
            correction_lineage_origin=correction_lineage_origin,
            automatic_correction_round=automatic_correction_round,
            source_arc_parent_review_id=source_arc_parent_review_id,
            source_book_parent_review_id=source_book_parent_review_id,
            source_arc_closure_review_id=source_arc_closure_review_id,
            source_book_boundary_review_id=source_book_boundary_review_id,
            source_chapter_arc_request_id=source_chapter_arc_request_id,
            source_arc_book_request_id=source_arc_book_request_id,
            source_arc_closure_id=source_arc_closure_id,
            source_feedback_id=source_feedback_id,
            semantic_goal=semantic_goal,
            prompt=prompt,
            context_manifest=context_manifest,
            context_policy_id=definition.context_policy_id,
            context_policy_version=definition.context_policy_version,
            output_schema_id=definition.output_schema_id,
            output_schema_version=definition.output_schema_version,
            output_schema=definition.output_schema,
            evaluation_strategy_id=definition.evaluation_strategy_id,
            evaluation_strategy_version=definition.evaluation_strategy_version,
            rubric_id=definition.rubric_id,
            rubric_version=definition.rubric_version,
            rubric_text=definition.rubric_text,
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
    rubric_text: str | None = None,
    evaluation_strategy_id: str | None = None,
    evaluation_strategy_version: int | None = None,
    output_schema_version: int = 1,
) -> TaskDefinition:
    return TaskDefinition(
        role=role,
        task_kind=task_kind,
        contract_version=1,
        scope_layer=scope_layer,
        output_mode="native_json_schema",
        output_model=output_model,
        output_schema_id=f"{task_kind}-result",
        output_schema_version=output_schema_version,
        context_policy_id=context_policy_id,
        context_policy_version=1,
        task_instructions=instructions,
        evaluation_strategy_id=evaluation_strategy_id,
        evaluation_strategy_version=evaluation_strategy_version,
        rubric_id=rubric_id,
        rubric_version=1 if rubric_id else None,
        rubric_text=rubric_text,
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


def _evaluation(
    strategy: EvaluationStrategyDefinition,
    *,
    instructions: str,
) -> TaskDefinition:
    return _native(
        "evaluator",
        strategy.task_kind,
        strategy.scope_layer,
        strategy.output_model,
        context_policy_id=strategy.context_policy_id,
        instructions=(
            f"{strategy.objective} Apply this frozen rubric: "
            f"{strategy.rubric_text} {instructions}"
        ),
        rubric_id=strategy.rubric_id,
        rubric_text=strategy.rubric_text,
        evaluation_strategy_id=strategy.strategy_id,
        evaluation_strategy_version=strategy.strategy_version,
        output_schema_version=strategy.output_schema_version,
    )


def _strategy(
    *,
    task_kind: str,
    scope_layer: ScopeLayer,
    objective: str,
    context_policy_id: str,
    context_includes: tuple[str, ...],
    context_excludes: tuple[str, ...],
    rubric_id: str,
    rubric_text: str,
    deterministic_prechecks: tuple[str, ...],
    legal_semantic_signals: tuple[str, ...],
    output_model: type[BaseModel],
    strategy_version: int = 1,
    output_schema_version: int = 1,
) -> EvaluationStrategyDefinition:
    return EvaluationStrategyDefinition(
        strategy_id=f"{task_kind}-strategy",
        strategy_version=strategy_version,
        task_kind=task_kind,
        scope_layer=scope_layer,
        objective=objective,
        context_policy_id=context_policy_id,
        context_policy_version=1,
        context_includes=context_includes,
        context_excludes=context_excludes,
        rubric_id=rubric_id,
        rubric_version=1,
        rubric_text=rubric_text,
        deterministic_prechecks=deterministic_prechecks,
        legal_semantic_signals=legal_semantic_signals,
        output_model=output_model,
        output_schema_version=output_schema_version,
    )


DEFAULT_EVALUATION_STRATEGY_REGISTRY = EvaluationStrategyRegistry(
    [
        _strategy(
            task_kind="evaluate.book",
            scope_layer="book",
            objective="Evaluate one frozen Book candidate without rewriting it.",
            context_policy_id="book-evaluator-context-v2",
            context_includes=(
                "candidate_book_pack",
                "creator_confirmed_decisions",
                "current_canon_summary",
            ),
            context_excludes=(
                "future_arc_or_chapter_plans",
                "harness_route_commands",
                "storage_identifiers",
            ),
            rubric_id="book-candidate-rubric-v2",
            rubric_text=BOOK_CANDIDATE_RUBRIC,
            deterministic_prechecks=(
                "candidate_components_present",
                "book_workspace_version_current",
                "completion_requirement_keys_unique",
            ),
            legal_semantic_signals=("pass", "local_repair", "needs_user"),
            output_model=BookEvaluation,
        ),
        _strategy(
            task_kind="evaluate.arc",
            scope_layer="arc",
            objective="Evaluate one frozen Story Arc candidate without planning Chapters.",
            context_policy_id="arc-evaluator-context-v2",
            context_includes=(
                "candidate_arc_contract",
                "current_book_baseline",
                "current_canon",
                "prior_formal_arc_closure",
                "current_book_progress_handoff",
            ),
            context_excludes=(
                "future_chapter_slots",
                "harness_route_commands",
                "unrelated_execution_evidence",
            ),
            rubric_id="arc-candidate-rubric-v2",
            rubric_text=ARC_CANDIDATE_RUBRIC,
            deterministic_prechecks=(
                "arc_workspace_version_current",
                "arc_chapter_range_ordered",
                "closure_signal_keys_unique",
                "book_capacity_available",
            ),
            legal_semantic_signals=(
                "pass",
                "local_repair",
                "escalate_to_book",
                "needs_user",
            ),
            output_model=ArcEvaluation,
        ),
        _strategy(
            task_kind="evaluate.chapter",
            scope_layer="chapter",
            objective="Evaluate one complete frozen Chapter candidate.",
            context_policy_id="chapter-evaluator-context-v3",
            context_includes=(
                "chapter_goal_and_plan",
                "complete_prose",
                "observations_and_canon_intent",
                "current_arc_contract",
                "relevant_canon",
            ),
            context_excludes=(
                "book_revision_authority",
                "unrelated_future_secrets",
                "harness_storage_protocol",
            ),
            rubric_id="chapter-candidate-rubric-v4",
            rubric_text=CHAPTER_CANDIDATE_RUBRIC,
            deterministic_prechecks=(
                "frozen_submission_loaded",
                "canon_baseline_loaded",
                "canon_patch_applicable",
            ),
            legal_semantic_signals=(
                "pass",
                "local_repair",
                "escalate_to_arc",
            ),
            output_model=LayerEvaluationResult,
            strategy_version=3,
            output_schema_version=3,
        ),
        _strategy(
            task_kind="evaluate.arc_parent_contract",
            scope_layer="arc",
            objective="Review one Chapter-to-Arc request at Arc authority.",
            context_policy_id="arc-parent-review-context-v1",
            context_includes=(
                "current_arc_baseline",
                "source_chapter_request_and_evidence",
                "predecessor_arc_parent_review",
                "committed_arc_chapters",
                "current_canon",
                "current_book_baseline",
            ),
            context_excludes=(
                "replacement_arc_content",
                "book_revision_commands",
                "unrelated_agent_history",
            ),
            rubric_id="arc-parent-contract-rubric-v1",
            rubric_text=ARC_PARENT_REVIEW_RUBRIC,
            deterministic_prechecks=(
                "request_open_and_current",
                "source_chapter_review_applied",
                "target_arc_baseline_current",
            ),
            legal_semantic_signals=(
                "remains_applicable",
                "revision_warranted",
                "book_review_required",
                "chapter_evidence_review_required",
                "chapter_evidence_target",
                "unable_to_judge",
            ),
            output_model=ArcParentContractEvaluation,
        ),
        _strategy(
            task_kind="evaluate.book_parent_contract",
            scope_layer="book",
            objective="Review one Arc-to-Book request at Book authority.",
            context_policy_id="book-parent-review-context-v1",
            context_includes=(
                "current_book_baseline",
                "source_arc_request_and_evidence",
                "predecessor_book_parent_review",
                "formal_arc_closures",
                "committed_chapter_set",
                "current_canon",
            ),
            context_excludes=(
                "replacement_book_content",
                "chapter_repair_details",
                "harness_route_commands",
            ),
            rubric_id="book-parent-contract-rubric-v1",
            rubric_text=BOOK_PARENT_REVIEW_RUBRIC,
            deterministic_prechecks=(
                "request_open_and_current",
                "source_arc_review_applied",
                "target_book_baseline_current",
            ),
            legal_semantic_signals=(
                "remains_applicable",
                "revision_warranted",
                "arc_evidence_review_required",
                "unable_to_judge",
            ),
            output_model=BookParentContractEvaluation,
        ),
        _strategy(
            task_kind="evaluate.arc_closure",
            scope_layer="arc",
            objective="Evaluate the current Arc at its frozen closure checkpoint.",
            context_policy_id="arc-closure-context-v1",
            context_includes=(
                "current_arc_contract",
                "exact_committed_arc_chapter_set",
                "chapter_observations",
                "predecessor_arc_closure_review",
                "closure_frozen_canon",
                "current_book_baseline",
            ),
            context_excludes=(
                "future_chapter_generation",
                "replacement_arc_content",
                "later_mutable_canon",
            ),
            rubric_id="arc-closure-rubric-v1",
            rubric_text=ARC_CLOSURE_RUBRIC,
            deterministic_prechecks=(
                "closure_checkpoint_reached_exactly",
                "chapter_set_fingerprint_matches",
                "arc_book_canon_identities_current",
                "no_pending_lower_work",
            ),
            legal_semantic_signals=(
                "signal_statuses",
                "arc_contract_judgment",
                "book_review_concern",
                "chapter_evidence_concern",
                "chapter_evidence_target",
                "creator_input_need",
            ),
            output_model=ArcClosureEvaluation,
        ),
        _strategy(
            task_kind="evaluate.book_boundary",
            scope_layer="book",
            objective="Evaluate Book progress and ending readiness after a formal Arc closure.",
            context_policy_id="book-boundary-context-v1",
            context_includes=(
                "current_book_baseline",
                "source_formal_arc_closure",
                "closure_frozen_canon",
                "closure_frozen_chapter_set",
                "predecessor_book_boundary_review",
                "prior_book_progress_handoff",
            ),
            context_excludes=(
                "next_arc_candidate",
                "replacement_book_content",
                "later_mutable_canon",
            ),
            rubric_id="book-boundary-rubric-v1",
            rubric_text=BOOK_BOUNDARY_RUBRIC,
            deterministic_prechecks=(
                "formal_arc_closure_current",
                "book_baseline_current",
                "closure_inputs_frozen",
                "chapter_count_within_book_maximum",
            ),
            legal_semantic_signals=(
                "requirement_statuses",
                "ending_trajectory_judgment",
                "book_contract_judgment",
                "creator_input_need",
            ),
            output_model=BookBoundaryEvaluation,
        ),
        _strategy(
            task_kind="verify_evidence.chapter",
            scope_layer="chapter",
            objective="Verify one evidence-only Chapter correction.",
            context_policy_id="chapter-evidence-verification-context-v1",
            context_includes=(
                "byte_frozen_chapter_plan",
                "byte_frozen_chapter_prose",
                "corrected_observations_and_canon_intent",
                "descendant_committed_facts",
                "source_parent_review",
            ),
            context_excludes=(
                "general_literary_rubric",
                "prose_rewrite_tools",
                "unrelated_parent_concerns",
            ),
            rubric_id="chapter-evidence-correction-rubric-v1",
            rubric_text=CHAPTER_EVIDENCE_RUBRIC,
            deterministic_prechecks=(
                "plan_and_prose_bytes_unchanged",
                "formal_arc_not_closed",
                "descendant_noncontradiction_context_complete",
            ),
            legal_semantic_signals=(
                "observations_supported_by_frozen_prose",
                "canon_intent_supported_by_frozen_prose",
                "descendant_facts_remain_consistent",
                "creator_input_need",
            ),
            output_model=ChapterEvidenceCorrectionEvaluation,
        ),
        *[
            _strategy(
                task_kind=f"verify_repair.{layer}",
                scope_layer=layer,
                objective=f"Verify only the authorized {layer} repair.",
                context_policy_id=(
                    "chapter-repair-verification-context-v3"
                    if layer == "chapter"
                    else f"{layer}-repair-verification-context-v2"
                ),
                context_includes=(
                    "frozen_candidate_before_repair",
                    "repaired_candidate",
                    "complete_issue_ledger",
                    "authorized_repair_scope",
                    "current_layer_dependencies",
                ),
                context_excludes=(
                    "new_unrelated_rubric",
                    "replacement_parent_content",
                    "harness_route_commands",
                ),
                rubric_id=(
                    "chapter-repair-rubric-v4"
                    if layer == "chapter"
                    else f"{layer}-repair-rubric-v2"
                ),
                rubric_text=(
                    BOOK_CANDIDATE_RUBRIC
                    if layer == "book"
                    else (
                        ARC_CANDIDATE_RUBRIC
                        if layer == "arc"
                        else CHAPTER_CANDIDATE_RUBRIC
                    )
                )
                + " Verify the complete original issue ledger and reject unauthorized changes.",
                deterministic_prechecks=(
                    "repair_scope_matches_authorization",
                    "complete_issue_ledger_present",
                    "frozen_dependencies_current",
                ),
                legal_semantic_signals=(
                    ("pass", "local_repair", "escalate_to_arc")
                    if layer == "chapter"
                    else ("pass", "local_repair", "needs_user")
                ),
                output_model=(
                    BookEvaluation
                    if layer == "book"
                    else ArcEvaluation if layer == "arc" else LayerEvaluationResult
                ),
                strategy_version=3 if layer == "chapter" else 1,
                output_schema_version=3 if layer == "chapter" else 1,
            )
            for layer in ("book", "arc", "chapter")
        ],
    ]
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
            BookRepairPatch,
            context_policy_id="book-repair-context-v1",
            instructions=(
                "Return only a semantic patch whose change components are a subset of the "
                "evaluator-authorized Book repair contract in frozen context. Do not return "
                "selected_title or repeat any omitted Book component; the Harness preserves "
                "omitted content. Every returned replacement must actually differ from the "
                "current component; an unchanged replacement is rejected as a no-op. Do not "
                "invent storage IDs, approval state, routes, or commands."
            ),
            output_schema_version=2,
        ),
        *[
            _native(
                "arc_planner",
                task_kind,
                "arc",
                ArcPlanProposal,
                context_policy_id=f"{task_kind.replace('.', '-')}-context-v2",
                instructions=instructions,
                output_schema_version=2,
            )
            for task_kind, instructions in (
                (
                    "arc.plan",
                    "Plan only the next stage-level Story Arc from the approved Book, current "
                    "Book progress handoff, committed Canon, and prior formal closure. Define "
                    "a target state transition, trajectories, obligations, prohibitions, "
                    "cumulative Chapter range, one selected closure checkpoint, and observable "
                    "closure signals. Advisory beats are optional and are not Chapter slots.",
                ),
                (
                    "arc.revise",
                    "Revise only the current Arc contract under explicit Arc-layer "
                    "authorization and preserve committed Chapter/Canon facts. Any extension "
                    "must select a new cumulative closure checkpoint inside a coherent range.",
                ),
            )
        ],
        _native(
            "arc_planner",
            "arc.repair",
            "arc",
            ArcRepairPatch,
            context_policy_id="arc-repair-context-v1",
            instructions=(
                "Return only a semantic patch whose change components are a subset of the "
                "evaluator-authorized Arc repair scope in frozen context. Do not repeat omitted "
                "Arc components; the Harness preserves them exactly. Every returned replacement "
                "must actually differ from the current component; an unchanged replacement is "
                "rejected as a no-op. Do not invent storage IDs, approval state, routes, or "
                "commands."
            ),
            output_schema_version=3,
        ),
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
            instructions=(
                "Observe the frozen prose and propose semantic Canon assertions without choosing "
                "add, update, or resolve commands and without inventing IDs. For each subject, "
                "state its complete current meaning and whether the chapter semantically resolves "
                "it. Write evidence_hint as a natural semantic rationale; do not copy exact quotes, "
                "offsets, locators, or stored source strings. The Harness owns subject upsert and "
                "optional exact-span binding."
            ),
            output_schema_version=2,
        ),
        _native(
            "chapter_writer",
            "chapter.revise.observe",
            "chapter",
            ChapterObservationResult,
            context_policy_id="chapter-revision-observation-context-v1",
            instructions=(
                "Re-observe the revised prose and propose only semantically evidence-bound Canon "
                "assertions. Do not choose storage operations: state each subject's complete current "
                "meaning and resolved status. Write evidence_hint as a natural rationale, not an "
                "exact quote, offset, locator, or stored source string; the Harness owns subject "
                "upsert and optional exact-span binding."
            ),
            output_schema_version=2,
        ),
        _native(
            "chapter_writer",
            "chapter.repair.plan",
            "chapter",
            ChapterPlanProposal,
            context_policy_id="chapter-plan-repair-context-v1",
            instructions=(
                "Return one complete replacement for the evaluator-authorized mutable Chapter "
                "plan. The replacement must remain within the same frozen Arc and Canon. Do not "
                "repair prose or observations, change upstream authority, or return storage and "
                "Route metadata; the Harness invalidates and regenerates every downstream working "
                "component."
            ),
        ),
        _text(
            "chapter.repair.prose",
            instructions="Return the complete repaired prose only, changing only the authorized repair scope.",
        ),
        _native(
            "chapter_writer",
            "chapter.repair.observation",
            "chapter",
            ChapterObservationRepairPatch,
            context_policy_id="chapter-observation-repair-context-v1",
            instructions=(
                "Return only a semantic patch whose change components are a subset of the "
                "authorized Chapter repair scope in frozen context. Use observations for the "
                "summary and continuity observations, and canon for Canon proposals. Do not "
                "repeat omitted components; the Harness preserves them. Canon evidence_hint is "
                "a natural semantic rationale, never an exact quote, offset, locator, or stored "
                "source string. Canon changes are semantic assertions with a resolved state, not "
                "model-authored storage operations."
            ),
            output_schema_version=3,
        ),
        *[
            _evaluation(
                strategy,
                instructions=(
                    "Return only read-only semantic findings and cited evidence. Never "
                    "author replacement content, select a Domain command, approve a "
                    "candidate, or mutate state. "
                    + (
                        BOOK_EVALUATION_CONTRACT
                        if strategy.output_model is BookEvaluation
                        else (
                            ARC_EVALUATION_CONTRACT
                            if strategy.output_model is ArcEvaluation
                            else (
                                CHAPTER_EVALUATION_CONTRACT
                                if strategy.output_model is LayerEvaluationResult
                                else ""
                            )
                        )
                    )
                ),
            )
            for strategy in DEFAULT_EVALUATION_STRATEGY_REGISTRY
        ],
    ],
    evaluation_strategies=DEFAULT_EVALUATION_STRATEGY_REGISTRY,
)
