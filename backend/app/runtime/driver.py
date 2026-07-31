from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol, cast

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.agents.binding import ModelBindingError, ProfileCredential
from app.agents.contracts import AgentRole, ArcPlanProposal
from app.agents.executor import AgentExecutionResult, AgentExecutor
from app.agents.registry import DEFAULT_TASK_REGISTRY, TaskRegistry
from app.db.uow import UnitOfWork
from app.domain.arc.commands import ArcCommandService
from app.domain.arc.outline import ArcOutlineProjectionError, resolve_outline_entry
from app.domain.arc.contracts import (
    ApplyArcTaskRequest,
    CommitArcAutoRequest,
    CreateStoryArcRequest,
    RecordArcReviewRequest,
    RebaseStaleArcRequest,
    SubmitArcRequest,
)
from app.domain.book.commands import BookCommandService
from app.domain.book.contracts import (
    ApplyBookCandidateTaskRequest,
    ApplyBookDiscussionTaskRequest,
    BookDiscussionState,
    RecordBookReviewRequest,
    SubmitBookRequest,
)
from app.domain.authority import (
    AuthorityTaskFailure,
    CommitBookCompletionRequest,
    CommitBookProgressHandoffRequest,
    LoopAuthorityCommandService,
    OpenArcClosureRevisionRequest,
    OpenBookCompletionRevisionRequest,
    RecordArcClosureReviewRequest,
    RecordArcParentReviewRequest,
    RecordBookCompletionReviewRequest,
    RecordBookParentReviewRequest,
)
from app.domain.change_requests import ActivateChangeRequest, ChangeRequestCommandService
from app.domain.chapter.commands import (
    ChapterCommandService,
    ChapterEvidenceVerificationFailure,
)
from app.domain.chapter.contracts import (
    ApplyChapterTaskRequest,
    ChapterRepairContract,
    CommitChapterRequest,
    CreateChapterRequest,
    RecordChapterReviewRequest,
    RebaseStaleChapterRequest,
    SubmitChapterRequest,
)
from app.domain.commands import CommandPreconditionError
from app.domain.feedback import ApplyFeedbackRequest, FeedbackCommandService
from app.profiles import ProfileCatalog, ProfileConfigurationError
from app.runtime.context import HarnessContextBuilder
from app.runtime.failures import (
    DeliveryFailureService,
    HarnessActionFailureService,
    NormalizedDeliveryFailure,
    normalize_harness_action_details,
)
from app.store.agent_tasks import AgentTaskStore
from app.store.command_bus import CommandBus
from app.store.content import prepare_canonical_json
from app.store.execution import ActionableTaskRecord
from app.store.runs import GenerationRunRecord

LOGGER = logging.getLogger(__name__)


class HarnessInvariantError(RuntimeError):
    """Authoritative facts do not describe one legal next Domain Harness action."""

    def __init__(
        self,
        message: str,
        *,
        failure_code: str = "context_assembly_invalid",
    ) -> None:
        super().__init__(message)
        self.failure_code = failure_code
        self.invariant = failure_code


class EvaluationContractAssemblyError(RuntimeError):
    """A frozen task strategy, rubric, or typed contract cannot be assembled."""


class ContextAssemblyError(RuntimeError):
    """Authoritative semantic context cannot be assembled for a frozen task."""


def _stable_lineage_id(*parts: str) -> str:
    material = ":".join(("novelpilot", "correction-lineage", *parts))
    return uuid.uuid5(uuid.NAMESPACE_URL, material).hex


def _normalize_delivery_failure(error: Exception) -> NormalizedDeliveryFailure:
    if isinstance(error, ValidationError):
        details: dict[str, object] | None = {
            "validation_errors": [
                {
                    "type": item["type"],
                    "loc": list(item["loc"]),
                    "message": item["msg"],
                }
                for item in error.errors(include_url=False, include_context=False, include_input=False)
            ]
        }
        return NormalizedDeliveryFailure(
            code="domain_delivery_contract_invalid",
            message="The successful Agent result does not match its Domain delivery contract.",
            exception_type=type(error).__name__,
            details=details,
        )
    if isinstance(error, AuthorityTaskFailure):
        return NormalizedDeliveryFailure(
            code=error.code,
            message=str(error),
            exception_type=type(error).__name__,
        )
    if isinstance(error, CommandPreconditionError):
        return NormalizedDeliveryFailure(
            code="domain_delivery_rejected",
            message=str(error),
            exception_type=type(error).__name__,
        )
    if isinstance(error, ChapterEvidenceVerificationFailure):
        return NormalizedDeliveryFailure(
            code="evidence_correction_verification_failed",
            message=str(error),
            exception_type=type(error).__name__,
        )
    if isinstance(error, HarnessInvariantError):
        return NormalizedDeliveryFailure(
            code="harness_delivery_invariant",
            message=str(error),
            exception_type=type(error).__name__,
        )
    return NormalizedDeliveryFailure(
        code="domain_delivery_unexpected",
        message=(
            "An unexpected implementation error prevented the successful Agent result "
            "from being applied."
        ),
        exception_type=type(error).__name__,
    )


class TaskExecutor(Protocol):
    async def execute(
        self,
        *,
        project_id: str,
        task_id: str,
        attempt_id: str,
        owner_instance_id: str,
        lease_token: str,
        credential: ProfileCredential,
    ) -> AgentExecutionResult: ...

    async def fail_preflight(
        self,
        *,
        project_id: str,
        task_id: str,
        attempt_id: str,
        owner_instance_id: str,
        lease_token: str,
        credential: ProfileCredential,
        error: ModelBindingError,
    ) -> AgentExecutionResult: ...


@dataclass(frozen=True, slots=True)
class _TaskInstruction:
    role: AgentRole
    task_kind: str
    book_id: str
    workspace_lock_version: int
    book_baseline_id: str | None
    arc_id: str | None = None
    arc_baseline_id: str | None = None
    chapter_id: str | None = None
    chapter_baseline_id: str | None = None
    canon_baseline_id: str | None = None
    correction_lineage_id: str | None = None
    correction_lineage_origin: Literal[
        "review_initiated", "user_initiated"
    ] | None = None
    automatic_correction_round: Literal[0, 1] | None = None
    source_arc_parent_review_id: str | None = None
    source_book_parent_review_id: str | None = None
    source_arc_closure_review_id: str | None = None
    source_book_completion_review_id: str | None = None
    source_book_candidate_review_id: str | None = None
    source_arc_candidate_review_id: str | None = None
    source_chapter_candidate_review_id: str | None = None
    source_book_progress_handoff_id: str | None = None
    source_chapter_arc_request_id: str | None = None
    source_arc_book_request_id: str | None = None
    source_arc_closure_id: str | None = None
    source_feedback_id: str | None = None


def _book_parent_review_instruction(
    *,
    book_id: str,
    workspace_lock_version: int,
    book_baseline_id: str | None,
    correction_lineage_id: str,
    correction_lineage_origin: Literal["review_initiated", "user_initiated"],
    automatic_correction_round: Literal[0, 1],
    source_arc_book_request_id: str,
    canon_baseline_id: str | None = None,
    source_book_parent_review_id: str | None = None,
    source_feedback_id: str | None = None,
) -> _TaskInstruction:
    """Freeze Book authority while binding lower evidence through its request."""

    return _TaskInstruction(
        role="evaluator",
        task_kind="evaluate.book_parent_contract",
        book_id=book_id,
        workspace_lock_version=workspace_lock_version,
        book_baseline_id=book_baseline_id,
        canon_baseline_id=canon_baseline_id,
        correction_lineage_id=correction_lineage_id,
        correction_lineage_origin=correction_lineage_origin,
        automatic_correction_round=automatic_correction_round,
        source_book_parent_review_id=source_book_parent_review_id,
        source_arc_book_request_id=source_arc_book_request_id,
        source_feedback_id=source_feedback_id,
    )


@dataclass(frozen=True, slots=True)
class _CommandInstruction:
    kind: Literal[
        "activate_change",
        "create_arc",
        "create_chapter",
        "rebase_arc",
        "rebase_chapter",
        "submit_book",
        "submit_arc",
        "submit_chapter",
        "commit_arc_auto",
        "commit_chapter",
        "open_arc_closure_revision",
        "open_book_completion_revision",
        "commit_book_handoff",
        "commit_book_completion",
    ]
    request: object
    idempotency_key: str


type _Instruction = _TaskInstruction | _CommandInstruction | None


_SEMANTIC_GOALS: dict[str, str] = {
    "book.discuss": "Advance one concrete creator decision while preserving the full discussion record.",
    "book.synthesize": "Synthesize the approved discussion into a coherent whole-book baseline candidate.",
    "book.revise": "Revise the whole-book candidate only for the active formal Book change.",
    "book.repair": "Repair only the evaluator-authorized Book components.",
    "arc.plan": "Plan the next rolling Story Arc under the approved Book and current Canon.",
    "arc.revise": "Revise the current Story Arc only for its active formal change.",
    "arc.repair": "Repair only the evaluator-authorized Story Arc components.",
    "chapter.plan": "Plan the next Chapter under the approved Book, Arc, and current Canon.",
    "chapter.revise.plan": "Revise the committed Chapter plan only within the active change.",
    "chapter.draft": "Write the complete Chapter prose from the frozen Chapter plan.",
    "chapter.revise.draft": "Write the complete revised Chapter prose from the revised plan.",
    "chapter.observe": "Observe the Chapter prose and propose evidence-bound Canon changes.",
    "chapter.revise.observe": "Re-observe revised Chapter prose and propose evidence-bound Canon changes.",
    "chapter.repair.plan": (
        "Replace the invalid mutable Chapter plan within the same frozen Arc and Canon."
    ),
    "chapter.repair.prose": "Repair the complete Chapter prose only within the authorized scope.",
    "chapter.repair.observation": (
        "Repair the Chapter observations and Canon proposals only within the authorized scope."
    ),
    "evaluate.book": "Independently evaluate the whole-book candidate against the Book rubric.",
    "verify_repair.book": "Verify the repaired Book candidate against the prior findings.",
    "evaluate.arc": "Independently evaluate the Story Arc candidate against upstream facts.",
    "verify_repair.arc": "Verify the repaired Story Arc candidate against the prior findings.",
    "evaluate.chapter": "Independently evaluate the complete Chapter candidate and Canon proposal.",
    "verify_repair.chapter": "Verify the repaired Chapter candidate against the prior findings.",
    "evaluate.arc_parent_contract": (
        "Review one evidence-bound Chapter concern at immediate-parent Arc authority."
    ),
    "evaluate.book_parent_contract": (
        "Review one evidence-bound Arc concern at Book authority."
    ),
    "evaluate.arc_closure": (
        "Judge every frozen Arc closure signal against the exact committed boundary."
    ),
    "evaluate.book_completion": (
        "Judge whole-Book completion requirements after the planned final Arc closes."
    ),
    "verify_evidence.chapter": (
        "Verify an evidence-only Chapter correction against byte-frozen approved prose."
    ),
}


class DomainRunDriver:
    """Execute exactly one durable Harness action, then return control to Run Engine."""

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        profile_catalog: ProfileCatalog,
        registry: TaskRegistry = DEFAULT_TASK_REGISTRY,
        executor: TaskExecutor | None = None,
        owner_instance_id: str | None = None,
        now_ms: Callable[[], int] | None = None,
    ) -> None:
        self._engine = engine
        self._profiles = profile_catalog
        self._registry = registry
        self._context = HarnessContextBuilder(engine)
        self._tasks = AgentTaskStore(engine)
        self._executor = executor or AgentExecutor(engine, registry=registry)
        self._owner_instance_id = owner_instance_id or f"domain-driver-{uuid.uuid4().hex}"
        self._now_ms = now_ms or (lambda: time.time_ns() // 1_000_000)
        bus = CommandBus(engine)
        self._books = BookCommandService(bus)
        self._arcs = ArcCommandService(bus)
        self._chapters = ChapterCommandService(bus)
        self._changes = ChangeRequestCommandService(bus)
        self._authority = LoopAuthorityCommandService(bus)
        self._feedback = FeedbackCommandService(bus, now_ms=self._now_ms)
        self._delivery_failures = DeliveryFailureService(
            bus,
            now_ms=self._now_ms,
        )
        self._harness_action_failures = HarnessActionFailureService(
            bus,
            now_ms=self._now_ms,
        )

    async def drive_one(self, run: GenerationRunRecord) -> None:
        async with UnitOfWork(self._engine) as store:
            queued_feedback = await store.feedback.get_oldest_routed(
                project_id=run.project_id
            )
            actionable = await store.execution.find_actionable_for_run(run_id=run.id)
        if queued_feedback is not None:
            if run.status == "failure_paused":
                return
            await self._feedback.apply(
                ApplyFeedbackRequest(
                    project_id=run.project_id,
                    feedback_id=queued_feedback.id,
                ),
                idempotency_key=f"engine:apply-feedback:{queued_feedback.id}",
            )
            return
        if actionable is not None:
            if actionable.task_status == "queued":
                await self._execute_task(actionable)
            else:
                try:
                    await self._deliver_task(actionable)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    LOGGER.exception(
                        "Domain delivery failed for task %s attempt %s",
                        actionable.task_id,
                        actionable.attempt_id,
                    )
                    await self._delivery_failures.failure_pause(
                        task=actionable,
                        failure=_normalize_delivery_failure(error),
                    )
            return

        try:
            instruction = await self._decide_next(run)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await self._pause_harness_action(
                run=run,
                action_key=f"route:{run.id}:{run.lock_version}",
                failure_code=cast(
                    str,
                    getattr(error, "failure_code", "context_assembly_invalid"),
                ),
                message=(
                    "Deterministic Route could not derive one legal next action "
                    "from current authority."
                ),
                error=error,
                phase="route",
            )
            return
        if isinstance(instruction, _TaskInstruction):
            action_key = self._task_freeze_action_key(run, instruction)
            try:
                await self._freeze_task(run, instruction)
            except asyncio.CancelledError:
                raise
            except EvaluationContractAssemblyError as error:
                await self._pause_harness_action(
                    run=run,
                    action_key=action_key,
                    failure_code="evaluation_contract_invalid",
                    message=(
                        "The Agent task strategy, rubric, or typed contract could "
                        "not be frozen."
                    ),
                    error=error,
                    phase="evaluation_contract",
                    task_kind=instruction.task_kind,
                )
            except ContextAssemblyError as error:
                context_cause = error.__cause__
                await self._pause_harness_action(
                    run=run,
                    action_key=action_key,
                    failure_code=cast(
                        str,
                        getattr(
                            context_cause,
                            "code",
                            "context_assembly_invalid",
                        ),
                    ),
                    message=(
                        "The Agent task context could not be assembled from current "
                        "authoritative facts."
                    ),
                    error=error,
                    phase="context",
                    task_kind=instruction.task_kind,
                )
            except Exception as error:
                await self._pause_harness_action(
                    run=run,
                    action_key=action_key,
                    failure_code="execution_failure",
                    message="The Harness failed before the Agent task was durably frozen.",
                    error=error,
                    phase="task_freeze",
                    task_kind=instruction.task_kind,
                )
        elif isinstance(instruction, _CommandInstruction):
            try:
                await self._apply_command(instruction)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                await self._pause_harness_action(
                    run=run,
                    action_key=f"domain-command:{instruction.idempotency_key}",
                    failure_code="execution_failure",
                    message="The deterministic Domain command could not be committed.",
                    error=error,
                    phase="domain_command",
                )

    async def _pause_harness_action(
        self,
        *,
        run: GenerationRunRecord,
        action_key: str,
        failure_code: str,
        message: str,
        error: Exception,
        phase: str,
        task_kind: str | None = None,
    ) -> None:
        LOGGER.exception(
            "Harness action %s failed before a deliverable Agent task existed",
            action_key,
            exc_info=error,
        )
        details = normalize_harness_action_details(
            error,
            phase=phase,
            task_kind=task_kind,
        )
        await self._harness_action_failures.failure_pause(
            run=run,
            action_key=action_key,
            failure_code=failure_code,
            message=message,
            exception_type=type(error).__name__,
            details=details,
        )

    @staticmethod
    def _task_freeze_action_key(
        run: GenerationRunRecord,
        instruction: _TaskInstruction,
    ) -> str:
        scope_id = instruction.chapter_id or instruction.arc_id or instruction.book_id
        prepared = prepare_canonical_json(
            {
                "run_id": run.id,
                "run_lock_version": run.lock_version,
                "role": instruction.role,
                "task_kind": instruction.task_kind,
                "scope_id": scope_id,
                "workspace_lock_version": instruction.workspace_lock_version,
                "book_baseline_id": instruction.book_baseline_id,
                "arc_baseline_id": instruction.arc_baseline_id,
                "chapter_baseline_id": instruction.chapter_baseline_id,
                "canon_baseline_id": instruction.canon_baseline_id,
                "correction_lineage_id": instruction.correction_lineage_id,
                "automatic_correction_round": instruction.automatic_correction_round,
                "source_arc_parent_review_id": (
                    instruction.source_arc_parent_review_id
                ),
                "source_book_parent_review_id": (
                    instruction.source_book_parent_review_id
                ),
                "source_arc_closure_review_id": (
                    instruction.source_arc_closure_review_id
                ),
                "source_book_completion_review_id": (
                    instruction.source_book_completion_review_id
                ),
                "source_chapter_arc_request_id": (
                    instruction.source_chapter_arc_request_id
                ),
                "source_arc_book_request_id": instruction.source_arc_book_request_id,
                "source_arc_closure_id": instruction.source_arc_closure_id,
                "source_feedback_id": instruction.source_feedback_id,
            }
        )
        return f"freeze-task:{instruction.task_kind}:{prepared.sha256}"

    async def _execute_task(self, task: ActionableTaskRecord) -> None:
        lease_token = uuid.uuid4().hex
        try:
            resolved = self._profiles.resolve(task.profile_id)
        except ProfileConfigurationError as error:
            failure = self._profiles.failure_material(
                task.profile_id,
                message=str(error),
            )
            await self._executor.fail_preflight(
                project_id=task.project_id,
                task_id=task.task_id,
                attempt_id=task.attempt_id,
                owner_instance_id=self._owner_instance_id,
                lease_token=lease_token,
                credential=failure.credential,
                error=failure.error,
            )
            return
        await self._executor.execute(
            project_id=task.project_id,
            task_id=task.task_id,
            attempt_id=task.attempt_id,
            owner_instance_id=self._owner_instance_id,
            lease_token=lease_token,
            credential=resolved.credential,
        )

    async def _freeze_task(
        self,
        run: GenerationRunRecord,
        instruction: _TaskInstruction,
    ) -> None:
        async with UnitOfWork(self._engine) as store:
            project = await store.projects.get(run.project_id)
            workspace_lock_version: int | None = None
            workspace_work_cycle_id: str | None = None
            if instruction.chapter_id is not None:
                chapter_workspace = await store.chapters.get_workspace(
                    project_id=run.project_id,
                    chapter_id=instruction.chapter_id,
                )
                if chapter_workspace is not None:
                    workspace_lock_version = chapter_workspace.lock_version
                    workspace_work_cycle_id = chapter_workspace.work_cycle_id
            elif instruction.arc_id is not None:
                arc_workspace = await store.arcs.get_workspace(
                    project_id=run.project_id,
                    arc_id=instruction.arc_id,
                )
                if arc_workspace is not None:
                    workspace_lock_version = arc_workspace.lock_version
                    workspace_work_cycle_id = arc_workspace.work_cycle_id
            else:
                book_workspace = await store.books.get_workspace(
                    project_id=run.project_id,
                    book_id=instruction.book_id,
                )
                if book_workspace is not None:
                    workspace_lock_version = book_workspace.lock_version
                    workspace_work_cycle_id = book_workspace.work_cycle_id
        if project is None:
            raise HarnessInvariantError("Runnable project no longer exists.")
        if (
            workspace_lock_version != instruction.workspace_lock_version
            or workspace_work_cycle_id is None
        ):
            raise HarnessInvariantError(
                "Task freeze lost its exact workspace semantic work cycle."
            )
        profile_id = self._profile_id(project, instruction.role)
        if profile_id is None:
            raise HarnessInvariantError(
                f"No Profile is selected for role {instruction.role!r}."
            )
        try:
            profile = self._profiles.resolve(profile_id).snapshot
        except ProfileConfigurationError as error:
            profile = self._profiles.failure_material(
                profile_id,
                message=str(error),
            ).snapshot
        try:
            semantic_goal = _SEMANTIC_GOALS[instruction.task_kind]
            definition = self._registry.get(
                role=instruction.role,
                task_kind=instruction.task_kind,
                contract_version=1,
            )
            evaluation_strategy = None
            if instruction.role == "evaluator":
                if self._registry.evaluation_strategies is None:
                    raise HarnessInvariantError(
                        "Evaluator task registry has no evaluation strategies."
                    )
                evaluation_strategy = self._registry.evaluation_strategies.for_task(
                    instruction.task_kind
                )
        except Exception as error:
            raise EvaluationContractAssemblyError(
                f"Cannot assemble frozen contract for {instruction.task_kind!r}."
            ) from error
        try:
            context = await self._context.build(
                task_kind=instruction.task_kind,
                project_id=run.project_id,
                book_id=instruction.book_id,
                arc_id=instruction.arc_id,
                chapter_id=instruction.chapter_id,
                semantic_goal=semantic_goal,
                definition=definition,
                evaluation_strategy=evaluation_strategy,
                source_arc_parent_review_id=instruction.source_arc_parent_review_id,
                source_book_parent_review_id=instruction.source_book_parent_review_id,
                source_arc_closure_review_id=instruction.source_arc_closure_review_id,
                source_book_completion_review_id=(
                    instruction.source_book_completion_review_id
                ),
                source_book_candidate_review_id=(
                    instruction.source_book_candidate_review_id
                ),
                source_arc_candidate_review_id=(
                    instruction.source_arc_candidate_review_id
                ),
                source_chapter_candidate_review_id=(
                    instruction.source_chapter_candidate_review_id
                ),
                source_book_progress_handoff_id=(
                    instruction.source_book_progress_handoff_id
                ),
                source_chapter_arc_request_id=instruction.source_chapter_arc_request_id,
                source_arc_book_request_id=instruction.source_arc_book_request_id,
                source_arc_closure_id=instruction.source_arc_closure_id,
                canon_baseline_id=instruction.canon_baseline_id,
            )
        except Exception as error:
            raise ContextAssemblyError(
                f"Cannot assemble context for {instruction.task_kind!r}."
            ) from error
        context_fingerprint = prepare_canonical_json(context.manifest).sha256
        scope_id = instruction.chapter_id or instruction.arc_id or instruction.book_id
        task_key = ":".join(
            [
                run.id,
                instruction.task_kind,
                scope_id,
                str(instruction.workspace_lock_version),
                workspace_work_cycle_id,
                instruction.book_baseline_id or "none",
                instruction.arc_baseline_id or "none",
                instruction.chapter_baseline_id or "none",
                instruction.correction_lineage_id or "none",
                str(instruction.automatic_correction_round)
                if instruction.automatic_correction_round is not None
                else "none",
                instruction.source_arc_parent_review_id or "none",
                instruction.source_book_parent_review_id or "none",
                instruction.source_arc_closure_review_id or "none",
                instruction.source_book_completion_review_id or "none",
                instruction.source_book_candidate_review_id or "none",
                instruction.source_arc_candidate_review_id or "none",
                instruction.source_chapter_candidate_review_id or "none",
                instruction.source_book_progress_handoff_id or "none",
                instruction.source_chapter_arc_request_id or "none",
                instruction.source_arc_book_request_id or "none",
                instruction.source_arc_closure_id or "none",
                instruction.source_feedback_id or "none",
                instruction.canon_baseline_id or project.current_canon_baseline_id,
                context_fingerprint,
            ]
        )
        task_id = uuid.uuid4().hex
        plan = self._registry.freeze_plan(
            task_id=task_id,
            project_id=run.project_id,
            run_id=run.id,
            task_key=task_key,
            action_key=f"{instruction.task_kind}:{scope_id}",
            role=instruction.role,
            task_kind=instruction.task_kind,
            contract_version=1,
            book_id=instruction.book_id,
            canon_baseline_id=(
                instruction.canon_baseline_id or project.current_canon_baseline_id
            ),
            semantic_goal=semantic_goal,
            prompt=context.prompt,
            context_manifest=context.manifest,
            profile_snapshot=profile,
            arc_id=instruction.arc_id,
            chapter_id=instruction.chapter_id,
            workspace_lock_version=instruction.workspace_lock_version,
            workspace_work_cycle_id=workspace_work_cycle_id,
            book_baseline_id=instruction.book_baseline_id,
            arc_baseline_id=instruction.arc_baseline_id,
            chapter_baseline_id=instruction.chapter_baseline_id,
            correction_lineage_id=instruction.correction_lineage_id,
            correction_lineage_origin=instruction.correction_lineage_origin,
            automatic_correction_round=instruction.automatic_correction_round,
            source_arc_parent_review_id=instruction.source_arc_parent_review_id,
            source_book_parent_review_id=instruction.source_book_parent_review_id,
            source_arc_closure_review_id=instruction.source_arc_closure_review_id,
            source_book_completion_review_id=(
                instruction.source_book_completion_review_id
            ),
            source_book_candidate_review_id=(
                instruction.source_book_candidate_review_id
            ),
            source_arc_candidate_review_id=instruction.source_arc_candidate_review_id,
            source_chapter_candidate_review_id=(
                instruction.source_chapter_candidate_review_id
            ),
            source_book_progress_handoff_id=(
                instruction.source_book_progress_handoff_id
            ),
            source_chapter_arc_request_id=instruction.source_chapter_arc_request_id,
            source_arc_book_request_id=instruction.source_arc_book_request_id,
            source_arc_closure_id=instruction.source_arc_closure_id,
            source_feedback_id=instruction.source_feedback_id,
        )
        await self._tasks.create_initial(
            plan=plan,
            attempt_id=uuid.uuid4().hex,
            created_at_ms=self._now_ms(),
        )

    @staticmethod
    def _profile_id(project: object, role: AgentRole) -> str | None:
        if role == "book_strategist":
            return cast(str | None, getattr(project, "book_profile_id")) or cast(
                str | None, getattr(project, "default_profile_id")
            )
        if role == "arc_planner":
            return cast(str | None, getattr(project, "arc_profile_id")) or cast(
                str | None, getattr(project, "default_profile_id")
            )
        if role == "chapter_writer":
            return cast(str | None, getattr(project, "chapter_profile_id")) or cast(
                str | None, getattr(project, "default_profile_id")
            )
        return cast(str | None, getattr(project, "evaluator_profile_id")) or cast(
            str | None, getattr(project, "default_profile_id")
        )

    async def _deliver_task(self, task: ActionableTaskRecord) -> None:
        key = f"engine:deliver:{task.task_id}:{task.attempt_id}"
        lock_version = task.workspace_lock_version
        if lock_version is None:
            raise HarnessInvariantError("Deliverable Agent task has no workspace lock version.")
        if task.task_kind == "book.discuss":
            await self._books.apply_discussion_result(
                ApplyBookDiscussionTaskRequest(
                    project_id=task.project_id,
                    book_id=task.book_id,
                    task_id=task.task_id,
                    attempt_id=task.attempt_id,
                    expected_workspace_lock_version=lock_version,
                ),
                idempotency_key=key,
            )
            return
        if task.task_kind in {"book.synthesize", "book.revise", "book.repair"}:
            await self._books.apply_candidate_result(
                ApplyBookCandidateTaskRequest(
                    project_id=task.project_id,
                    book_id=task.book_id,
                    task_id=task.task_id,
                    attempt_id=task.attempt_id,
                    expected_workspace_lock_version=lock_version,
                ),
                idempotency_key=key,
            )
            return
        if task.task_kind in {"evaluate.book", "verify_repair.book"}:
            async with UnitOfWork(self._engine) as store:
                book_submission = await store.books.find_pending_submission(
                    project_id=task.project_id,
                    book_id=task.book_id,
                )
            if book_submission is None:
                raise HarnessInvariantError("Book evaluator result has no pending submission.")
            await self._books.record_review(
                RecordBookReviewRequest(
                    project_id=task.project_id,
                    book_id=task.book_id,
                    submission_id=book_submission.id,
                    evaluator_task_id=task.task_id,
                    evaluator_attempt_id=task.attempt_id,
                    rubric_id=self._required_task_rubric_id(task),
                    rubric_version=self._required_task_rubric_version(task),
                    deterministic_precheck={"passed": True, "manifest": "book-submission-v1"},
                ),
                idempotency_key=key,
            )
            return
        if task.task_kind in {"arc.plan", "arc.revise", "arc.repair"}:
            if task.arc_id is None:
                raise HarnessInvariantError("Story Arc task has no arc_id.")
            await self._arcs.apply_task_result(
                ApplyArcTaskRequest(
                    project_id=task.project_id,
                    book_id=task.book_id,
                    arc_id=task.arc_id,
                    task_id=task.task_id,
                    attempt_id=task.attempt_id,
                    expected_workspace_lock_version=lock_version,
                ),
                idempotency_key=key,
            )
            return
        if task.task_kind in {"evaluate.arc", "verify_repair.arc"}:
            if task.arc_id is None:
                raise HarnessInvariantError("Story Arc evaluator task has no arc_id.")
            async with UnitOfWork(self._engine) as store:
                arc_submission = await store.arcs.find_pending_submission(
                    project_id=task.project_id,
                    arc_id=task.arc_id,
                )
            if arc_submission is None:
                raise HarnessInvariantError("Story Arc evaluator result has no submission.")
            await self._arcs.record_review(
                RecordArcReviewRequest(
                    project_id=task.project_id,
                    book_id=task.book_id,
                    arc_id=task.arc_id,
                    submission_id=arc_submission.id,
                    evaluator_task_id=task.task_id,
                    evaluator_attempt_id=task.attempt_id,
                    rubric_id=self._required_task_rubric_id(task),
                    rubric_version=self._required_task_rubric_version(task),
                    deterministic_precheck={"passed": True, "manifest": "arc-submission-v1"},
                ),
                idempotency_key=key,
            )
            return
        if task.task_kind in {
            "chapter.plan",
            "chapter.revise.plan",
            "chapter.draft",
            "chapter.revise.draft",
            "chapter.observe",
            "chapter.revise.observe",
            "chapter.repair.plan",
            "chapter.repair.prose",
            "chapter.repair.observation",
        }:
            await self._deliver_chapter_component(task, lock_version=lock_version, key=key)
            return
        if task.task_kind in {"evaluate.chapter", "verify_repair.chapter"}:
            if task.chapter_id is None:
                raise HarnessInvariantError("Chapter evaluator task has no chapter_id.")
            async with UnitOfWork(self._engine) as store:
                chapter_submission = await store.chapters.find_pending_submission(
                    project_id=task.project_id,
                    chapter_id=task.chapter_id,
                )
            if chapter_submission is None:
                raise HarnessInvariantError("Chapter evaluator result has no submission.")
            await self._chapters.record_review(
                RecordChapterReviewRequest(
                    project_id=task.project_id,
                    chapter_id=task.chapter_id,
                    submission_id=chapter_submission.id,
                    evaluator_task_id=task.task_id,
                    evaluator_attempt_id=task.attempt_id,
                    rubric_id=self._required_task_rubric_id(task),
                    rubric_version=self._required_task_rubric_version(task),
                ),
                idempotency_key=key,
            )
            return
        if task.task_kind == "verify_evidence.chapter":
            if task.chapter_id is None:
                raise HarnessInvariantError(
                    "Chapter evidence evaluator task has no chapter_id."
                )
            async with UnitOfWork(self._engine) as store:
                chapter_submission = await store.chapters.find_pending_submission(
                    project_id=task.project_id,
                    chapter_id=task.chapter_id,
                )
            if chapter_submission is None:
                raise HarnessInvariantError(
                    "Chapter evidence evaluator result has no submission."
                )
            await self._chapters.record_evidence_review(
                RecordChapterReviewRequest(
                    project_id=task.project_id,
                    chapter_id=task.chapter_id,
                    submission_id=chapter_submission.id,
                    evaluator_task_id=task.task_id,
                    evaluator_attempt_id=task.attempt_id,
                    rubric_id=self._required_task_rubric_id(task),
                    rubric_version=self._required_task_rubric_version(task),
                ),
                idempotency_key=key,
            )
            return
        if task.task_kind == "evaluate.arc_parent_contract":
            if task.arc_id is None or task.source_chapter_arc_request_id is None:
                raise HarnessInvariantError(
                    "Arc parent review lacks its exact Chapter-to-Arc request."
                )
            await self._authority.record_arc_parent_review(
                RecordArcParentReviewRequest(
                    project_id=task.project_id,
                    book_id=task.book_id,
                    arc_id=task.arc_id,
                    request_id=task.source_chapter_arc_request_id,
                    task_id=task.task_id,
                    attempt_id=task.attempt_id,
                ),
                idempotency_key=key,
            )
            return
        if task.task_kind == "evaluate.book_parent_contract":
            if task.source_arc_book_request_id is None:
                raise HarnessInvariantError(
                    "Book parent review lacks its exact Arc-to-Book request."
                )
            await self._authority.record_book_parent_review(
                RecordBookParentReviewRequest(
                    project_id=task.project_id,
                    book_id=task.book_id,
                    request_id=task.source_arc_book_request_id,
                    task_id=task.task_id,
                    attempt_id=task.attempt_id,
                ),
                idempotency_key=key,
            )
            return
        if task.task_kind == "evaluate.arc_closure":
            if task.arc_id is None:
                raise HarnessInvariantError("Arc closure task has no arc_id.")
            await self._authority.record_arc_closure_review(
                RecordArcClosureReviewRequest(
                    project_id=task.project_id,
                    book_id=task.book_id,
                    arc_id=task.arc_id,
                    task_id=task.task_id,
                    attempt_id=task.attempt_id,
                ),
                idempotency_key=key,
            )
            return
        if task.task_kind == "evaluate.book_completion":
            await self._authority.record_book_completion_review(
                RecordBookCompletionReviewRequest(
                    project_id=task.project_id,
                    book_id=task.book_id,
                    task_id=task.task_id,
                    attempt_id=task.attempt_id,
                ),
                idempotency_key=key,
            )
            return
        raise HarnessInvariantError(f"No delivery command for task kind {task.task_kind!r}.")

    @staticmethod
    def _required_task_rubric_id(task: ActionableTaskRecord) -> str:
        if task.rubric_id is None or not task.rubric_id.strip():
            raise HarnessInvariantError("Evaluator task has no frozen rubric identity.")
        return task.rubric_id

    @staticmethod
    def _required_task_rubric_version(task: ActionableTaskRecord) -> int:
        if task.rubric_version is None or task.rubric_version < 1:
            raise HarnessInvariantError("Evaluator task has no frozen rubric version.")
        return task.rubric_version

    async def _deliver_chapter_component(
        self,
        task: ActionableTaskRecord,
        *,
        lock_version: int,
        key: str,
    ) -> None:
        if task.chapter_id is None:
            raise HarnessInvariantError("Chapter component task has no chapter_id.")
        request = ApplyChapterTaskRequest(
            project_id=task.project_id,
            chapter_id=task.chapter_id,
            task_id=task.task_id,
            attempt_id=task.attempt_id,
            expected_workspace_lock_version=lock_version,
        )
        methods = {
            "chapter.plan": self._chapters.apply_plan_result,
            "chapter.revise.plan": self._chapters.apply_revision_plan_result,
            "chapter.draft": self._chapters.apply_draft_result,
            "chapter.revise.draft": self._chapters.apply_revision_draft_result,
            "chapter.observe": self._chapters.apply_observation_result,
            "chapter.revise.observe": self._chapters.apply_revision_observation_result,
            "chapter.repair.plan": self._chapters.apply_repair_result,
            "chapter.repair.prose": self._chapters.apply_repair_result,
            "chapter.repair.observation": self._chapters.apply_repair_result,
        }
        await methods[task.task_kind](request, idempotency_key=key)

    async def _decide_next(self, run: GenerationRunRecord) -> _Instruction:
        async with UnitOfWork(self._engine) as store:
            project = await store.projects.get(run.project_id)
            book = await store.books.get_for_project(run.project_id)
            if project is None or book is None:
                raise HarnessInvariantError("Runnable Run has no Project/Book aggregate.")
            if project.lifecycle_status == "completed" and book.lifecycle_status == "completed":
                return None
            if project.lifecycle_status != "active" or book.current_completion_id is not None:
                raise HarnessInvariantError("Runnable Run points at a non-active Project.")
            book_workspace = await store.books.get_workspace(
                project_id=project.id,
                book_id=book.id,
            )
            if book_workspace is None:
                raise HarnessInvariantError("Book workspace is missing.")

            correction_feedback = (
                await store.feedback.get_unstarted_correction_lineage(
                    project_id=project.id,
                    run_id=run.id,
                )
            )
            if correction_feedback is not None:
                lineage_id = correction_feedback.resulting_correction_lineage_id
                if lineage_id is None:
                    raise HarnessInvariantError(
                        "Applied correction feedback lost its user lineage."
                    )
                if correction_feedback.arc_parent_review_id is not None:
                    arc_parent_review = await store.arc_parent_reviews.get(
                        project_id=project.id,
                        review_id=correction_feedback.arc_parent_review_id,
                    )
                    if arc_parent_review is None:
                        raise HarnessInvariantError(
                            "Applied Arc-parent feedback lost its source review."
                        )
                    arc = await store.arcs.get(
                        project_id=project.id,
                        arc_id=arc_parent_review.arc_id,
                    )
                    workspace = await store.arcs.get_workspace(
                        project_id=project.id,
                        arc_id=arc_parent_review.arc_id,
                    )
                    if arc is None or workspace is None:
                        raise HarnessInvariantError(
                            "Applied Arc-parent feedback lost its target."
                        )
                    return _TaskInstruction(
                        role="evaluator",
                        task_kind="evaluate.arc_parent_contract",
                        book_id=book.id,
                        arc_id=arc.id,
                        workspace_lock_version=workspace.lock_version,
                        book_baseline_id=book.current_baseline_id,
                        arc_baseline_id=arc.current_baseline_id,
                        correction_lineage_id=lineage_id,
                        correction_lineage_origin="user_initiated",
                        automatic_correction_round=0,
                        source_arc_parent_review_id=arc_parent_review.id,
                        source_chapter_arc_request_id=arc_parent_review.request_id,
                        source_feedback_id=correction_feedback.id,
                    )
                if correction_feedback.book_parent_review_id is not None:
                    book_parent_review = await store.book_parent_reviews.get(
                        project_id=project.id,
                        review_id=correction_feedback.book_parent_review_id,
                    )
                    if book_parent_review is None:
                        raise HarnessInvariantError(
                            "Applied Book-parent feedback lost its source review."
                        )
                    book_change = await store.changes.get_arc_book(
                        project_id=project.id,
                        request_id=book_parent_review.request_id,
                    )
                    if book_change is None:
                        raise HarnessInvariantError(
                            "Applied Book-parent feedback lost its source Arc."
                        )
                    return _book_parent_review_instruction(
                        book_id=book.id,
                        workspace_lock_version=book_workspace.lock_version,
                        book_baseline_id=book.current_baseline_id,
                        correction_lineage_id=lineage_id,
                        correction_lineage_origin="user_initiated",
                        automatic_correction_round=0,
                        source_book_parent_review_id=book_parent_review.id,
                        source_arc_book_request_id=book_parent_review.request_id,
                        source_feedback_id=correction_feedback.id,
                    )
                if correction_feedback.arc_closure_review_id is not None:
                    arc_closure_review = await store.arc_closure_reviews.get(
                        project_id=project.id,
                        review_id=correction_feedback.arc_closure_review_id,
                    )
                    if arc_closure_review is None:
                        raise HarnessInvariantError(
                            "Applied Arc-closure feedback lost its source review."
                        )
                    workspace = await store.arcs.get_workspace(
                        project_id=project.id,
                        arc_id=arc_closure_review.arc_id,
                    )
                    if workspace is None:
                        raise HarnessInvariantError(
                            "Applied Arc-closure feedback lost its workspace."
                        )
                    return _TaskInstruction(
                        role="evaluator",
                        task_kind="evaluate.arc_closure",
                        book_id=book.id,
                        arc_id=arc_closure_review.arc_id,
                        workspace_lock_version=workspace.lock_version,
                        book_baseline_id=arc_closure_review.book_baseline_id,
                        arc_baseline_id=arc_closure_review.arc_baseline_id,
                        canon_baseline_id=arc_closure_review.canon_baseline_id,
                        correction_lineage_id=lineage_id,
                        correction_lineage_origin="user_initiated",
                        automatic_correction_round=0,
                        source_arc_closure_review_id=arc_closure_review.id,
                        source_feedback_id=correction_feedback.id,
                    )
                if correction_feedback.book_completion_review_id is not None:
                    book_completion_review = await store.book_completion_reviews.get(
                        project_id=project.id,
                        review_id=correction_feedback.book_completion_review_id,
                    )
                    if book_completion_review is None:
                        raise HarnessInvariantError(
                            "Applied Book-completion feedback lost its source review."
                        )
                    return _TaskInstruction(
                        role="evaluator",
                        task_kind="evaluate.book_completion",
                        book_id=book.id,
                        workspace_lock_version=book_workspace.lock_version,
                        book_baseline_id=book.current_baseline_id,
                        canon_baseline_id=book_completion_review.canon_baseline_id,
                        correction_lineage_id=lineage_id,
                        correction_lineage_origin="user_initiated",
                        automatic_correction_round=0,
                        source_book_completion_review_id=book_completion_review.id,
                        source_arc_closure_id=book_completion_review.arc_closure_id,
                        source_book_progress_handoff_id=(
                            book_completion_review.book_progress_handoff_id
                        ),
                        source_feedback_id=correction_feedback.id,
                    )
                raise HarnessInvariantError(
                    "Applied correction feedback has no relational source review."
                )

            unresolved_changes = await store.changes.list_unresolved(
                project_id=project.id
            )
            for change in unresolved_changes:
                if change.status != "open":
                    continue
                lineage_id = _stable_lineage_id(
                    run.id,
                    change.request_kind,
                    change.id,
                    change.target_baseline_id,
                )
                if change.request_kind == "chapter_to_arc":
                    target = await store.arcs.get_workspace(
                        project_id=project.id,
                        arc_id=change.target_id,
                    )
                    if target is None:
                        raise HarnessInvariantError(
                            "Open Chapter-to-Arc request lost its target."
                        )
                    return _TaskInstruction(
                        role="evaluator",
                        task_kind="evaluate.arc_parent_contract",
                        book_id=book.id,
                        arc_id=change.target_id,
                        workspace_lock_version=target.lock_version,
                        book_baseline_id=book.current_baseline_id,
                        arc_baseline_id=change.target_baseline_id,
                        correction_lineage_id=lineage_id,
                        correction_lineage_origin="review_initiated",
                        automatic_correction_round=0,
                        source_chapter_arc_request_id=change.id,
                    )
                full_change = await store.changes.get_arc_book(
                    project_id=project.id,
                    request_id=change.id,
                )
                if full_change is None:
                    raise HarnessInvariantError(
                        "Open Arc-to-Book request lost its source Arc."
                    )
                return _book_parent_review_instruction(
                    book_id=book.id,
                    workspace_lock_version=book_workspace.lock_version,
                    book_baseline_id=change.target_baseline_id,
                    correction_lineage_id=lineage_id,
                    correction_lineage_origin="review_initiated",
                    automatic_correction_round=0,
                    source_arc_book_request_id=change.id,
                )

            for change in unresolved_changes:
                if change.status != "reviewed" or change.latest_parent_review_id is None:
                    continue
                if change.request_kind == "chapter_to_arc":
                    review = await store.arc_parent_reviews.get(
                        project_id=project.id,
                        review_id=change.latest_parent_review_id,
                    )
                    target = await store.arcs.get_workspace(
                        project_id=project.id,
                        arc_id=change.target_id,
                    )
                    if review is None or target is None:
                        raise HarnessInvariantError(
                            "Reviewed Chapter-to-Arc request lost its authority."
                        )
                    if (
                        review.disposition == "arc_revision_warranted"
                        and review.opened_arc_workspace_id is None
                    ):
                        return _CommandInstruction(
                            kind="activate_change",
                            request=ActivateChangeRequest(
                                project_id=project.id,
                                change_request_id=change.id,
                                request_kind="chapter_to_arc",
                                expected_target_baseline_id=change.target_baseline_id,
                                expected_workspace_lock_version=target.lock_version,
                            ),
                            idempotency_key=(
                                f"engine:activate-arc-review:{review.id}"
                            ),
                        )
                    if (
                        review.disposition
                        in {"keep_arc", "chapter_evidence_review_required"}
                        and review.automatic_correction_round == 0
                    ):
                        correction = (
                            await store.chapters.get_correction_workspace_for_review(
                                project_id=project.id,
                                arc_id=change.target_id,
                                source_arc_parent_review_id=review.id,
                            )
                        )
                        if correction is None:
                            raise HarnessInvariantError(
                                "Arc parent review lost its downward Chapter correction."
                            )
                        _, correction_workspace = correction
                        if (
                            correction_workspace.correction_lineage_id
                            != review.correction_lineage_id
                            or correction_workspace.automatic_correction_round != 1
                        ):
                            raise HarnessInvariantError(
                                "Arc parent successor lost its correction lineage."
                            )
                        if correction_workspace.state == "idle":
                            target = await store.arcs.get_workspace(
                                project_id=project.id,
                                arc_id=change.target_id,
                            )
                            current_arc = await store.arcs.get(
                                project_id=project.id,
                                arc_id=change.target_id,
                            )
                            if (
                                target is None
                                or current_arc is None
                                or current_arc.current_baseline_id
                                != change.target_baseline_id
                            ):
                                raise HarnessInvariantError(
                                    "Arc parent successor target is no longer current."
                                )
                            return _TaskInstruction(
                                role="evaluator",
                                task_kind="evaluate.arc_parent_contract",
                                book_id=book.id,
                                arc_id=change.target_id,
                                workspace_lock_version=target.lock_version,
                                book_baseline_id=book.current_baseline_id,
                                arc_baseline_id=change.target_baseline_id,
                                canon_baseline_id=project.current_canon_baseline_id,
                                correction_lineage_id=review.correction_lineage_id,
                                correction_lineage_origin=cast(
                                    Literal["review_initiated", "user_initiated"],
                                    review.correction_lineage_origin,
                                ),
                                automatic_correction_round=1,
                                source_arc_parent_review_id=review.id,
                                source_chapter_arc_request_id=change.id,
                                source_feedback_id=review.source_feedback_id,
                            )
                else:
                    book_parent_review = await store.book_parent_reviews.get(
                        project_id=project.id,
                        review_id=change.latest_parent_review_id,
                    )
                    if book_parent_review is None:
                        raise HarnessInvariantError(
                            "Reviewed Arc-to-Book request lost its authority."
                        )
                    if (
                        book_parent_review.disposition
                        == "book_revision_warranted"
                        and book_parent_review.opened_book_workspace_id is None
                    ):
                        return _CommandInstruction(
                            kind="activate_change",
                            request=ActivateChangeRequest(
                                project_id=project.id,
                                change_request_id=change.id,
                                request_kind="arc_to_book",
                                expected_target_baseline_id=change.target_baseline_id,
                                expected_workspace_lock_version=book_workspace.lock_version,
                            ),
                            idempotency_key=(
                                f"engine:activate-book-review:"
                                f"{book_parent_review.id}"
                            ),
                        )
                    if (
                        book_parent_review.disposition
                        in {"keep_book", "arc_evidence_review_required"}
                        and book_parent_review.automatic_correction_round == 0
                    ):
                        full_change = await store.changes.get_arc_book(
                            project_id=project.id,
                            request_id=change.id,
                        )
                        correction_arc = (
                            None
                            if full_change is None
                            else await store.arcs.get(
                                project_id=project.id,
                                arc_id=full_change.arc_id,
                            )
                        )
                        arc_correction_workspace = (
                            None
                            if full_change is None
                            else await store.arcs.get_workspace(
                                project_id=project.id,
                                arc_id=full_change.arc_id,
                            )
                        )
                        if (
                            full_change is None
                            or correction_arc is None
                            or arc_correction_workspace is None
                            or arc_correction_workspace.source_book_parent_review_id
                            != book_parent_review.id
                        ):
                            raise HarnessInvariantError(
                                "Book parent review lost its downward Arc correction."
                            )
                        if (
                            arc_correction_workspace.correction_lineage_id
                            != book_parent_review.correction_lineage_id
                            or arc_correction_workspace.automatic_correction_round
                            != 1
                        ):
                            raise HarnessInvariantError(
                                "Book parent successor lost its correction lineage."
                            )
                        if arc_correction_workspace.state == "idle":
                            return _book_parent_review_instruction(
                                book_id=book.id,
                                workspace_lock_version=book_workspace.lock_version,
                                book_baseline_id=book.current_baseline_id,
                                canon_baseline_id=project.current_canon_baseline_id,
                                correction_lineage_id=(
                                    book_parent_review.correction_lineage_id
                                ),
                                correction_lineage_origin=cast(
                                    Literal["review_initiated", "user_initiated"],
                                    book_parent_review.correction_lineage_origin,
                                ),
                                automatic_correction_round=1,
                                source_book_parent_review_id=book_parent_review.id,
                                source_arc_book_request_id=change.id,
                                source_feedback_id=book_parent_review.source_feedback_id,
                            )

            book_instruction = await self._decide_book(
                store=store,
                run=run,
                project=project,
                book=book,
                workspace=book_workspace,
                has_open_book_change=any(
                    change.request_kind == "arc_to_book"
                    and change.status == "reviewed"
                    for change in unresolved_changes
                ),
            )
            if book_instruction is not None:
                return book_instruction
            return await self._decide_arc(
                store=store,
                run=run,
                project=project,
                book=book,
                book_workspace=book_workspace,
            )

    async def _decide_book(
        self,
        *,
        store: object,
        run: GenerationRunRecord,
        project: object,
        book: object,
        workspace: object,
        has_open_book_change: bool,
    ) -> _Instruction:
        # StoreSession is intentionally kept behind a structural local variable so this
        # runtime layer does not expose it in its public protocol.
        session = store
        books = getattr(session, "books")
        content = getattr(session, "content")
        execution = getattr(session, "execution")
        project_id = cast(str, getattr(project, "id"))
        book_id = cast(str, getattr(book, "id"))
        pending = await books.find_pending_submission(project_id=project_id, book_id=book_id)
        submission_review = (
            None
            if pending is None
            else await books.get_review_for_submission(
                project_id=project_id,
                submission_id=pending.id,
            )
        )
        lock_version = cast(int, getattr(workspace, "lock_version"))
        current_baseline = cast(str | None, getattr(book, "current_baseline_id"))
        if pending is not None:
            if (
                submission_review is None
                or submission_review.submission_id != pending.id
            ):
                task_kind = (
                    "verify_repair.book"
                    if cast(int, getattr(workspace, "semantic_repair_count")) > 0
                    else "evaluate.book"
                )
                return _TaskInstruction(
                    role="evaluator",
                    task_kind=task_kind,
                    book_id=book_id,
                    workspace_lock_version=lock_version,
                    book_baseline_id=pending.base_book_baseline_id,
                    source_book_candidate_review_id=cast(
                        str | None, getattr(workspace, "active_repair_review_id")
                    ),
                )
            raise HarnessInvariantError("Reviewed Book submission remained on a runnable Run.")

        state_name = cast(str, getattr(workspace, "state"))
        if state_name in {"blocked_by_user", "blocked_by_upstream", "stale"}:
            raise HarnessInvariantError("Blocked Book workspace remained on a runnable Run.")
        if state_name == "idle":
            return None
        discussion = BookDiscussionState.model_validate_json(
            (
                await content.get_packed(
                    project_id=project_id,
                    ref_id=cast(str, getattr(workspace, "discussion_state_ref_id")),
                )
            ).unpack_and_verify()
        )
        if discussion.readiness_status == "awaiting_agent":
            return _TaskInstruction(
                role="book_strategist",
                task_kind="book.discuss",
                book_id=book_id,
                workspace_lock_version=lock_version,
                book_baseline_id=current_baseline,
            )
        if discussion.readiness_status == "continue":
            raise HarnessInvariantError("Book discussion awaiting user input remained runnable.")

        candidate_ready = all(
            getattr(workspace, field) is not None
            for field in (
                "candidate_constraints_ref_id",
                "candidate_titles_ref_id",
                "candidate_rolling_plan_ref_id",
                "candidate_completion_contract_ref_id",
                "candidate_arc_topology_ref_id",
            )
        )
        active_repair_review = (
            None
            if getattr(workspace, "active_repair_review_id") is None
            else await books.get_review(
                project_id=project_id,
                review_id=cast(str, getattr(workspace, "active_repair_review_id")),
            )
        )
        if active_repair_review is not None:
            repaired = await execution.has_applied_task(
                project_id=project_id,
                run_id=run.id,
                task_kind="book.repair",
                book_id=book_id,
                book_baseline_id=current_baseline,
                workspace_work_cycle_id=cast(
                    str, getattr(workspace, "work_cycle_id")
                ),
                source_book_candidate_review_id=active_repair_review.id,
            )
            if not repaired:
                if cast(int, getattr(workspace, "semantic_repair_count")) >= cast(
                    int, getattr(workspace, "semantic_repair_limit")
                ):
                    raise HarnessInvariantError(
                        "Book semantic correction for this frozen review is exhausted.",
                        failure_code="semantic_repair_exhausted",
                    )
                return _TaskInstruction(
                    role="book_strategist",
                    task_kind="book.repair",
                    book_id=book_id,
                    workspace_lock_version=lock_version,
                    book_baseline_id=current_baseline,
                    source_book_candidate_review_id=active_repair_review.id,
                )
            if not candidate_ready:
                raise HarnessInvariantError(
                    "Applied Book repair lost the complete candidate."
                )
        if current_baseline is None and not candidate_ready:
            return _TaskInstruction(
                role="book_strategist",
                task_kind="book.synthesize",
                book_id=book_id,
                workspace_lock_version=lock_version,
                book_baseline_id=None,
            )
        if current_baseline is not None and not candidate_ready:
            revised = await execution.has_applied_task(
                project_id=project_id,
                run_id=run.id,
                task_kind="book.revise",
                book_id=book_id,
                book_baseline_id=current_baseline,
                workspace_work_cycle_id=cast(
                    str, getattr(workspace, "work_cycle_id")
                ),
                source_feedback_id=cast(
                    str | None, getattr(workspace, "source_feedback_id")
                ),
                source_book_parent_review_id=cast(
                    str | None, getattr(workspace, "source_book_parent_review_id")
                ),
                source_book_completion_review_id=cast(
                    str | None,
                    getattr(workspace, "source_book_completion_review_id"),
                ),
                source_book_progress_handoff_id=cast(
                    str | None,
                    getattr(workspace, "source_book_progress_handoff_id"),
                ),
            )
            if not revised and (has_open_book_change or state_name == "active"):
                return _TaskInstruction(
                    role="book_strategist",
                    task_kind="book.revise",
                    book_id=book_id,
                    workspace_lock_version=lock_version,
                    book_baseline_id=current_baseline,
                    source_feedback_id=cast(
                        str | None, getattr(workspace, "source_feedback_id")
                    ),
                    source_book_parent_review_id=cast(
                        str | None,
                        getattr(workspace, "source_book_parent_review_id"),
                    ),
                    source_book_completion_review_id=cast(
                        str | None,
                        getattr(workspace, "source_book_completion_review_id"),
                    ),
                    source_book_progress_handoff_id=cast(
                        str | None,
                        getattr(workspace, "source_book_progress_handoff_id"),
                    ),
                )
        if candidate_ready:
            return _CommandInstruction(
                kind="submit_book",
                request=SubmitBookRequest(
                    project_id=project_id,
                    book_id=book_id,
                    expected_workspace_lock_version=lock_version,
                ),
                idempotency_key=f"engine:submit-book:{book_id}:{lock_version}",
            )
        raise HarnessInvariantError("Active Book workspace has no legal next action.")

    async def _decide_arc(
        self,
        *,
        store: object,
        run: GenerationRunRecord,
        project: object,
        book: object,
        book_workspace: object,
    ) -> _Instruction:
        session = store
        arcs = getattr(session, "arcs")
        execution = getattr(session, "execution")
        project_id = cast(str, getattr(project, "id"))
        book_id = cast(str, getattr(book, "id"))
        book_baseline_id = cast(str | None, getattr(book, "current_baseline_id"))
        canon_baseline_id = cast(str, getattr(project, "current_canon_baseline_id"))
        if book_baseline_id is None:
            raise HarnessInvariantError("Arc routing requires an approved Book baseline.")
        book_baseline = await getattr(session, "books").get_baseline(
            project_id=project_id,
            book_id=book_id,
            baseline_id=book_baseline_id,
        )
        if book_baseline is None:
            raise HarnessInvariantError("Current Book baseline is missing.")
        arc = await arcs.get_unfinished_for_book(project_id=project_id, book_id=book_id)
        if arc is None:
            latest = await arcs.get_latest_for_book(project_id=project_id, book_id=book_id)
            if latest is None:
                return _CommandInstruction(
                    kind="create_arc",
                    request=CreateStoryArcRequest(
                        project_id=project_id,
                        book_id=book_id,
                        expected_book_baseline_id=book_baseline_id,
                        expected_canon_baseline_id=canon_baseline_id,
                        expected_ordinal=1,
                        source_progress_handoff_id=None,
                    ),
                    idempotency_key=f"engine:create-initial-arc:{book_id}:{book_baseline_id}",
                )
            if (
                latest.lifecycle_status != "completed"
                or latest.current_baseline_id is None
                or latest.current_closure_id is None
            ):
                raise HarnessInvariantError("Book has no unfinished Arc and no completed boundary.")
            closure = await getattr(session, "arc_closures").get(
                project_id=project_id,
                closure_id=latest.current_closure_id,
            )
            if closure is None:
                raise HarnessInvariantError(
                    "Completed Story Arc lost its formal closure."
                )
            if latest.ordinal < book_baseline.arc_contract_count:
                if latest.ordinal >= book_baseline.final_arc_ordinal:
                    raise HarnessInvariantError(
                        "Book topology marks a non-terminal Arc as final.",
                        failure_code="book_arc_route_gap",
                    )
                handoff = await getattr(
                    session, "book_progress_handoffs"
                ).get_for_arc_closure(
                    project_id=project_id,
                    arc_closure_id=closure.id,
                )
                if handoff is None:
                    return _CommandInstruction(
                        kind="commit_book_handoff",
                        request=CommitBookProgressHandoffRequest(
                            project_id=project_id,
                            book_id=book_id,
                            source_arc_closure_id=closure.id,
                        ),
                        idempotency_key=(
                            f"engine:commit-book-handoff:{book_baseline_id}:{closure.id}"
                        ),
                    )
                if (
                    handoff.book_baseline_id != book_baseline_id
                    or handoff.next_arc_ordinal != latest.ordinal + 1
                    or handoff.canon_baseline_id != canon_baseline_id
                ):
                    raise HarnessInvariantError(
                        "Book progress handoff does not identify the unique next Arc.",
                        failure_code="book_arc_route_gap",
                    )
                return _CommandInstruction(
                    kind="create_arc",
                    request=CreateStoryArcRequest(
                        project_id=project_id,
                        book_id=book_id,
                        expected_book_baseline_id=book_baseline_id,
                        expected_canon_baseline_id=canon_baseline_id,
                        expected_ordinal=handoff.next_arc_ordinal,
                        source_progress_handoff_id=handoff.id,
                    ),
                    idempotency_key=(
                        f"engine:create-arc-from-handoff:{handoff.id}"
                    ),
                )
            if latest.ordinal != book_baseline.final_arc_ordinal:
                raise HarnessInvariantError(
                    "Closed Story Arc does not align with the approved Book topology.",
                    failure_code="book_arc_route_gap",
                )
            completion_review = (
                None
                if cast(
                    str | None, getattr(book, "latest_completion_review_id")
                )
                is None
                else await getattr(session, "book_completion_reviews").get(
                    project_id=project_id,
                    review_id=cast(
                        str, getattr(book, "latest_completion_review_id")
                    ),
                )
            )
            revision_predecessor = (
                None
                if completion_review is not None
                else await getattr(
                    session, "book_completion_reviews"
                ).get_latest_opened_revision_for_closure(
                    project_id=project_id,
                    book_id=book_id,
                    arc_closure_id=closure.id,
                    workspace_id=cast(str, getattr(book_workspace, "id")),
                )
            )
            if (
                completion_review is None
                or completion_review.arc_closure_id != closure.id
                or completion_review.book_baseline_id != book_baseline_id
            ):
                predecessor = (
                    completion_review
                    if completion_review is not None
                    and completion_review.arc_closure_id == closure.id
                    else revision_predecessor
                )
                if predecessor is not None and (
                    predecessor.book_baseline_id != book_baseline_id
                    and book_baseline.parent_baseline_id
                    != predecessor.book_baseline_id
                ):
                    raise HarnessInvariantError(
                        "Book completion successor is not a direct Book lineage child."
                    )
                return _TaskInstruction(
                    role="evaluator",
                    task_kind="evaluate.book_completion",
                    book_id=book_id,
                    workspace_lock_version=cast(
                        int, getattr(book_workspace, "lock_version")
                    ),
                    book_baseline_id=book_baseline_id,
                    canon_baseline_id=closure.canon_baseline_id,
                    correction_lineage_id=(
                        _stable_lineage_id(
                            run.id,
                            "book_completion",
                            closure.id,
                            book_baseline_id,
                        )
                        if predecessor is None
                        else predecessor.correction_lineage_id
                    ),
                    correction_lineage_origin=(
                        "review_initiated"
                        if predecessor is None
                        else cast(
                            Literal["review_initiated", "user_initiated"],
                            predecessor.correction_lineage_origin,
                        )
                    ),
                    automatic_correction_round=(
                        0 if predecessor is None else 1
                    ),
                    source_book_completion_review_id=(
                        None if predecessor is None else predecessor.id
                    ),
                    source_arc_closure_id=closure.id,
                    source_book_progress_handoff_id=(
                        predecessor.book_progress_handoff_id
                        if predecessor is not None
                        else cast(
                            str | None,
                            getattr(book, "current_progress_handoff_id"),
                        )
                    ),
                    source_feedback_id=(
                        None
                        if predecessor is None
                        else predecessor.source_feedback_id
                    ),
                )
            if completion_review.disposition == "complete_book":
                return _CommandInstruction(
                    kind="commit_book_completion",
                    request=CommitBookCompletionRequest(
                        project_id=project_id,
                        book_id=book_id,
                        completion_review_id=completion_review.id,
                    ),
                    idempotency_key=(
                        f"engine:commit-book-completion:{completion_review.id}"
                    ),
                )
            if (
                completion_review.disposition == "book_revision_warranted"
                and completion_review.opened_book_workspace_id is None
            ):
                return _CommandInstruction(
                    kind="open_book_completion_revision",
                    request=OpenBookCompletionRevisionRequest(
                        project_id=project_id,
                        book_id=book_id,
                        completion_review_id=completion_review.id,
                        expected_workspace_lock_version=cast(
                            int, getattr(book_workspace, "lock_version")
                        ),
                    ),
                    idempotency_key=(
                        "engine:open-book-completion-revision:"
                        f"{completion_review.id}"
                    ),
                )
            if completion_review.disposition in {
                "waiting_for_user",
                "no_legal_route",
                "book_revision_warranted",
            }:
                raise HarnessInvariantError(
                    "Runnable Book completion has no completed disposition action."
                )
            raise HarnessInvariantError(
                "Unknown Book completion disposition "
                f"{completion_review.disposition!r}."
            )

        workspace = await arcs.get_workspace(project_id=project_id, arc_id=arc.id)
        if workspace is None:
            raise HarnessInvariantError("Current Story Arc workspace is missing.")
        if workspace.state == "stale":
            return _CommandInstruction(
                kind="rebase_arc",
                request=RebaseStaleArcRequest(
                    project_id=project_id,
                    book_id=book_id,
                    arc_id=arc.id,
                    expected_workspace_lock_version=workspace.lock_version,
                    expected_book_baseline_id=book_baseline_id,
                    expected_arc_baseline_id=arc.current_baseline_id,
                    expected_canon_baseline_id=canon_baseline_id,
                ),
                idempotency_key=(
                    f"engine:rebase-stale-arc:{arc.id}:{workspace.lock_version}:"
                    f"{book_baseline_id}:{canon_baseline_id}"
                ),
            )
        pending = await arcs.find_pending_submission(project_id=project_id, arc_id=arc.id)
        submission_review = (
            None
            if pending is None
            else await arcs.get_review_for_submission(
                project_id=project_id,
                submission_id=pending.id,
            )
        )
        if pending is not None:
            if (
                submission_review is None
                or submission_review.submission_id != pending.id
            ):
                task_kind = (
                    "verify_repair.arc"
                    if workspace.semantic_repair_count > 0
                    else "evaluate.arc"
                )
                return _TaskInstruction(
                    role="evaluator",
                    task_kind=task_kind,
                    book_id=book_id,
                    arc_id=arc.id,
                    workspace_lock_version=workspace.lock_version,
                    book_baseline_id=pending.book_baseline_id,
                    arc_baseline_id=pending.base_arc_baseline_id,
                    source_arc_candidate_review_id=(
                        workspace.active_repair_review_id
                    ),
                    source_arc_parent_review_id=workspace.source_arc_parent_review_id,
                    source_arc_closure_review_id=(
                        workspace.source_arc_closure_review_id
                    ),
                    source_book_parent_review_id=(
                        workspace.source_book_parent_review_id
                    ),
                    source_book_completion_review_id=(
                        workspace.source_book_completion_review_id
                    ),
                    source_book_progress_handoff_id=(
                        workspace.book_progress_handoff_id
                    ),
                    source_feedback_id=workspace.source_feedback_id,
                )
            if submission_review.decision == "pass":
                gate = await arcs.find_pending_gate(project_id=project_id, arc_id=arc.id)
                if gate is not None:
                    raise HarnessInvariantError("Pending Arc approval gate remained runnable.")
                if cast(str, getattr(project, "operation_mode")) != "full_auto":
                    raise HarnessInvariantError("Participatory Arc review has no approval gate.")
                return _CommandInstruction(
                    kind="commit_arc_auto",
                    request=CommitArcAutoRequest(
                        project_id=project_id,
                        book_id=book_id,
                        arc_id=arc.id,
                        submission_id=pending.id,
                        review_id=submission_review.id,
                        expected_current_baseline_id=arc.current_baseline_id,
                    ),
                    idempotency_key=(
                        f"engine:commit-arc:{pending.id}:{submission_review.id}"
                    ),
                )
            raise HarnessInvariantError("Rejected Arc submission remained pending.")

        if workspace.state in {"blocked_by_user", "blocked_by_upstream", "stale"}:
            raise HarnessInvariantError("Blocked Story Arc workspace remained runnable.")
        if workspace.state == "active":
            active_repair_review = (
                None
                if workspace.active_repair_review_id is None
                else await arcs.get_review(
                    project_id=project_id,
                    review_id=workspace.active_repair_review_id,
                )
            )
            if active_repair_review is not None:
                repaired = await execution.has_applied_task(
                    project_id=project_id,
                    run_id=run.id,
                    task_kind="arc.repair",
                    book_id=book_id,
                    arc_id=arc.id,
                    book_baseline_id=workspace.book_baseline_id,
                    arc_baseline_id=workspace.base_arc_baseline_id,
                    workspace_work_cycle_id=workspace.work_cycle_id,
                    source_arc_candidate_review_id=active_repair_review.id,
                    source_arc_parent_review_id=workspace.source_arc_parent_review_id,
                    source_arc_closure_review_id=(
                        workspace.source_arc_closure_review_id
                    ),
                    source_book_parent_review_id=(
                        workspace.source_book_parent_review_id
                    ),
                    source_book_completion_review_id=(
                        workspace.source_book_completion_review_id
                    ),
                    source_book_progress_handoff_id=(
                        workspace.book_progress_handoff_id
                    ),
                    source_feedback_id=workspace.source_feedback_id,
                )
                if not repaired:
                    if (
                        workspace.semantic_repair_count
                        >= workspace.semantic_repair_limit
                    ):
                        raise HarnessInvariantError(
                            "Arc semantic correction for this frozen review is exhausted.",
                            failure_code="semantic_repair_exhausted",
                        )
                    return _TaskInstruction(
                        role="arc_planner",
                        task_kind="arc.repair",
                        book_id=book_id,
                        arc_id=arc.id,
                        workspace_lock_version=workspace.lock_version,
                        book_baseline_id=workspace.book_baseline_id,
                        arc_baseline_id=workspace.base_arc_baseline_id,
                        source_arc_candidate_review_id=active_repair_review.id,
                        source_arc_parent_review_id=(
                            workspace.source_arc_parent_review_id
                        ),
                        source_arc_closure_review_id=(
                            workspace.source_arc_closure_review_id
                        ),
                        source_book_parent_review_id=(
                            workspace.source_book_parent_review_id
                        ),
                        source_book_completion_review_id=(
                            workspace.source_book_completion_review_id
                        ),
                        source_book_progress_handoff_id=(
                            workspace.book_progress_handoff_id
                        ),
                        source_feedback_id=workspace.source_feedback_id,
                    )
                if workspace.plan_ref_id is None:
                    raise HarnessInvariantError(
                        "Applied Story Arc repair lost the complete plan."
                    )
            if workspace.base_arc_baseline_id is not None:
                revised = await execution.has_applied_task(
                    project_id=project_id,
                    run_id=run.id,
                    task_kind="arc.revise",
                    book_id=book_id,
                    arc_id=arc.id,
                    book_baseline_id=workspace.book_baseline_id,
                    arc_baseline_id=workspace.base_arc_baseline_id,
                    workspace_work_cycle_id=workspace.work_cycle_id,
                    source_feedback_id=workspace.source_feedback_id,
                    source_arc_parent_review_id=workspace.source_arc_parent_review_id,
                    source_arc_closure_review_id=(
                        workspace.source_arc_closure_review_id
                    ),
                    source_book_parent_review_id=workspace.source_book_parent_review_id,
                    source_book_completion_review_id=(
                        workspace.source_book_completion_review_id
                    ),
                    source_book_progress_handoff_id=(
                        workspace.book_progress_handoff_id
                    ),
                )
                if not revised:
                    return _TaskInstruction(
                        role="arc_planner",
                        task_kind="arc.revise",
                        book_id=book_id,
                        arc_id=arc.id,
                        workspace_lock_version=workspace.lock_version,
                        book_baseline_id=workspace.book_baseline_id,
                        arc_baseline_id=workspace.base_arc_baseline_id,
                        source_feedback_id=workspace.source_feedback_id,
                        source_arc_parent_review_id=(
                            workspace.source_arc_parent_review_id
                        ),
                        source_arc_closure_review_id=(
                            workspace.source_arc_closure_review_id
                        ),
                        source_book_parent_review_id=(
                            workspace.source_book_parent_review_id
                        ),
                        source_book_completion_review_id=(
                            workspace.source_book_completion_review_id
                        ),
                        source_book_progress_handoff_id=(
                            workspace.book_progress_handoff_id
                        ),
                    )
            elif workspace.plan_ref_id is None:
                return _TaskInstruction(
                    role="arc_planner",
                    task_kind="arc.plan",
                    book_id=book_id,
                    arc_id=arc.id,
                    workspace_lock_version=workspace.lock_version,
                    book_baseline_id=workspace.book_baseline_id,
                    arc_baseline_id=None,
                    source_book_progress_handoff_id=(
                        workspace.book_progress_handoff_id
                    ),
                )
            if workspace.plan_ref_id is not None:
                return _CommandInstruction(
                    kind="submit_arc",
                    request=SubmitArcRequest(
                        project_id=project_id,
                        book_id=book_id,
                        arc_id=arc.id,
                        expected_workspace_lock_version=workspace.lock_version,
                    ),
                    idempotency_key=f"engine:submit-arc:{arc.id}:{workspace.lock_version}",
                )
            raise HarnessInvariantError("Active Story Arc workspace has no plan action.")
        if workspace.state == "idle" and arc.lifecycle_status == "closing":
            active_lower_correction = (
                await getattr(session, "chapters").get_non_idle_workspace_for_arc(
                    project_id=project_id,
                    arc_id=arc.id,
                )
            )
            if active_lower_correction is not None:
                _, lower_workspace = active_lower_correction
                if (
                    lower_workspace.source_arc_parent_review_id is None
                    and lower_workspace.source_arc_closure_review_id is None
                ):
                    raise HarnessInvariantError(
                        "Closing Arc contains unrelated unfinished Chapter work."
                    )
                return await self._decide_chapter(
                    store=session,
                    run=run,
                    project_id=project_id,
                    book_id=book_id,
                    book_baseline_id=book_baseline_id,
                    canon_baseline_id=canon_baseline_id,
                    arc=arc,
                    arc_baseline_id=cast(str, arc.current_baseline_id),
                )
            latest_closure_review = (
                None
                if arc.latest_closure_review_id is None
                else await getattr(session, "arc_closure_reviews").get(
                    project_id=project_id,
                    review_id=arc.latest_closure_review_id,
                )
            )
            if latest_closure_review is None:
                source_review = (
                    None
                    if workspace.source_arc_closure_review_id is None
                    else await getattr(session, "arc_closure_reviews").get(
                        project_id=project_id,
                        review_id=workspace.source_arc_closure_review_id,
                    )
                )
                if source_review is None:
                    lineage_id = _stable_lineage_id(
                        run.id,
                        "arc_closure",
                        arc.id,
                        cast(str, arc.current_baseline_id),
                    )
                    lineage_origin: Literal[
                        "review_initiated", "user_initiated"
                    ] = "review_initiated"
                    correction_round: Literal[0, 1] = 0
                    source_review_id = None
                else:
                    if (
                        workspace.correction_lineage_id
                        != source_review.correction_lineage_id
                        or workspace.automatic_correction_round != 1
                    ):
                        raise HarnessInvariantError(
                            "Arc closure successor lost its correction lineage."
                        )
                    lineage_id = source_review.correction_lineage_id
                    lineage_origin = cast(
                        Literal["review_initiated", "user_initiated"],
                        source_review.correction_lineage_origin,
                    )
                    correction_round = 1
                    source_review_id = source_review.id
                return _TaskInstruction(
                    role="evaluator",
                    task_kind="evaluate.arc_closure",
                    book_id=book_id,
                    arc_id=arc.id,
                    workspace_lock_version=workspace.lock_version,
                    book_baseline_id=book_baseline_id,
                    arc_baseline_id=arc.current_baseline_id,
                    correction_lineage_id=lineage_id,
                    correction_lineage_origin=lineage_origin,
                    automatic_correction_round=correction_round,
                    source_arc_closure_review_id=source_review_id,
                )
            if (
                latest_closure_review.disposition == "arc_revision_warranted"
                and latest_closure_review.opened_arc_workspace_id is None
            ):
                return _CommandInstruction(
                    kind="open_arc_closure_revision",
                    request=OpenArcClosureRevisionRequest(
                        project_id=project_id,
                        book_id=book_id,
                        arc_id=arc.id,
                        closure_review_id=latest_closure_review.id,
                        expected_workspace_lock_version=workspace.lock_version,
                    ),
                    idempotency_key=(
                        f"engine:open-arc-closure-revision:"
                        f"{latest_closure_review.id}"
                    ),
                )
            if (
                latest_closure_review.disposition
                == "chapter_evidence_review_required"
                and latest_closure_review.automatic_correction_round == 0
            ):
                correction = (
                    await getattr(
                        session, "chapters"
                    ).get_correction_workspace_for_review(
                        project_id=project_id,
                        arc_id=arc.id,
                        source_arc_closure_review_id=latest_closure_review.id,
                    )
                )
                if correction is None:
                    raise HarnessInvariantError(
                        "Arc closure review lost its Chapter evidence correction."
                    )
                _, correction_workspace = correction
                if (
                    correction_workspace.state != "idle"
                    or correction_workspace.correction_lineage_id
                    != latest_closure_review.correction_lineage_id
                    or correction_workspace.automatic_correction_round != 1
                ):
                    raise HarnessInvariantError(
                        "Arc closure successor evidence is incomplete."
                    )
                return _TaskInstruction(
                    role="evaluator",
                    task_kind="evaluate.arc_closure",
                    book_id=book_id,
                    arc_id=arc.id,
                    workspace_lock_version=workspace.lock_version,
                    book_baseline_id=book_baseline_id,
                    arc_baseline_id=arc.current_baseline_id,
                    canon_baseline_id=canon_baseline_id,
                    correction_lineage_id=(
                        latest_closure_review.correction_lineage_id
                    ),
                    correction_lineage_origin=cast(
                        Literal["review_initiated", "user_initiated"],
                        latest_closure_review.correction_lineage_origin,
                    ),
                    automatic_correction_round=1,
                    source_arc_closure_review_id=latest_closure_review.id,
                    source_feedback_id=latest_closure_review.source_feedback_id,
                )
            if latest_closure_review.disposition in {
                "book_review_required",
                "chapter_evidence_review_required",
                "waiting_for_user",
                "no_legal_route",
            }:
                raise HarnessInvariantError(
                    "Runnable Arc closure has no completed disposition action."
                )
            raise HarnessInvariantError(
                "Passing Arc closure review did not complete its Arc atomically."
            )
        if workspace.state != "idle" or arc.current_baseline_id is None:
            raise HarnessInvariantError("Story Arc current baseline is not ready for Chapters.")
        return await self._decide_chapter(
            store=session,
            run=run,
            project_id=project_id,
            book_id=book_id,
            book_baseline_id=book_baseline_id,
            canon_baseline_id=canon_baseline_id,
            arc=arc,
            arc_baseline_id=arc.current_baseline_id,
        )

    async def _decide_chapter(
        self,
        *,
        store: object,
        run: GenerationRunRecord,
        project_id: str,
        book_id: str,
        book_baseline_id: str,
        canon_baseline_id: str,
        arc: object,
        arc_baseline_id: str,
    ) -> _Instruction:
        chapters = getattr(store, "chapters")
        arcs = getattr(store, "arcs")
        content = getattr(store, "content")
        execution = getattr(store, "execution")
        arc_id = cast(str, getattr(arc, "id"))
        active = await chapters.get_non_idle_workspace_for_arc(
            project_id=project_id,
            arc_id=arc_id,
        )
        if active is None:
            baseline = await arcs.get_baseline(
                project_id=project_id,
                arc_id=arc_id,
                baseline_id=arc_baseline_id,
            )
            if baseline is None:
                raise HarnessInvariantError("Current Story Arc baseline does not exist.")
            cumulative_committed = await chapters.count_committed_for_book(
                book_id=book_id
            )
            if (
                cumulative_committed
                < baseline.closure_cumulative_chapter_count
            ):
                try:
                    arc_plan = ArcPlanProposal.model_validate_json(
                        (
                            await content.get_packed(
                                project_id=project_id,
                                ref_id=baseline.plan_ref_id,
                            )
                        ).unpack_and_verify()
                    )
                except ValidationError as error:
                    raise HarnessInvariantError(
                        "The current Arc baseline contains an invalid outline plan.",
                        failure_code="arc_outline_projection_invalid",
                    ) from error
                book_ordinal, arc_ordinal = await chapters.next_ordinals(
                    book_id=book_id,
                    arc_id=arc_id,
                )
                try:
                    resolve_outline_entry(
                        baseline=baseline,
                        plan=arc_plan,
                        arc_ordinal=arc_ordinal,
                        book_ordinal=book_ordinal,
                    )
                except ArcOutlineProjectionError as error:
                    raise HarnessInvariantError(
                        (
                            "The next Chapter has no unique assignment in the "
                            "current Arc baseline."
                        ),
                        failure_code="arc_outline_slot_missing",
                    ) from error
                return _CommandInstruction(
                    kind="create_chapter",
                    request=CreateChapterRequest(
                        project_id=project_id,
                        book_id=book_id,
                        arc_id=arc_id,
                        expected_book_baseline_id=book_baseline_id,
                        expected_arc_baseline_id=arc_baseline_id,
                        expected_canon_baseline_id=canon_baseline_id,
                    ),
                    idempotency_key=(
                        f"engine:create-chapter:{arc_id}:{cumulative_committed + 1}:"
                        f"{arc_baseline_id}:"
                        f"{canon_baseline_id}"
                    ),
                )
            raise HarnessInvariantError(
                "Story Arc reached its cumulative closure checkpoint without "
                "entering closure review."
            )

        chapter, workspace = active
        if workspace.state == "stale":
            return _CommandInstruction(
                kind="rebase_chapter",
                request=RebaseStaleChapterRequest(
                    project_id=project_id,
                    book_id=book_id,
                    arc_id=arc_id,
                    chapter_id=chapter.id,
                    expected_workspace_lock_version=workspace.lock_version,
                    expected_book_baseline_id=book_baseline_id,
                    expected_arc_baseline_id=arc_baseline_id,
                    expected_chapter_baseline_id=chapter.current_baseline_id,
                    expected_canon_baseline_id=canon_baseline_id,
                ),
                idempotency_key=(
                    f"engine:rebase-stale-chapter:{chapter.id}:"
                    f"{workspace.lock_version}:{book_baseline_id}:"
                    f"{arc_baseline_id}:{canon_baseline_id}"
                ),
            )
        if workspace.state in {"blocked_by_user", "blocked_by_upstream", "stale"}:
            raise HarnessInvariantError("Blocked Chapter workspace remained runnable.")
        if workspace.state != "active":
            raise HarnessInvariantError("Non-idle Chapter workspace is not active.")
        evidence_correction = False
        if workspace.source_arc_parent_review_id is not None:
            source_review = await getattr(store, "arc_parent_reviews").get(
                project_id=project_id,
                review_id=workspace.source_arc_parent_review_id,
            )
            if source_review is None:
                raise HarnessInvariantError(
                    "Chapter correction lost its source Arc parent review."
                )
            evidence_correction = (
                source_review.disposition
                == "chapter_evidence_review_required"
            )
        elif workspace.source_arc_closure_review_id is not None:
            source_closure_review = await getattr(
                store, "arc_closure_reviews"
            ).get(
                project_id=project_id,
                review_id=workspace.source_arc_closure_review_id,
            )
            if source_closure_review is None:
                raise HarnessInvariantError(
                    "Chapter correction lost its source Arc closure review."
                )
            evidence_correction = (
                source_closure_review.disposition
                == "chapter_evidence_review_required"
            )
        pending = await chapters.find_pending_submission(
            project_id=project_id,
            chapter_id=chapter.id,
        )
        submission_review = (
            None
            if pending is None
            else await chapters.get_review_for_submission(
                project_id=project_id,
                submission_id=pending.id,
            )
        )
        if pending is not None:
            if (
                submission_review is None
                or submission_review.submission_id != pending.id
            ):
                task_kind = (
                    "verify_evidence.chapter"
                    if evidence_correction
                    else (
                        "verify_repair.chapter"
                        if workspace.semantic_repair_count > 0
                        else "evaluate.chapter"
                    )
                )
                return _TaskInstruction(
                    role="evaluator",
                    task_kind=task_kind,
                    book_id=book_id,
                    arc_id=arc_id,
                    chapter_id=chapter.id,
                    workspace_lock_version=workspace.lock_version,
                    book_baseline_id=pending.book_baseline_id,
                    arc_baseline_id=pending.arc_baseline_id,
                    chapter_baseline_id=pending.base_chapter_baseline_id,
                    correction_lineage_id=workspace.correction_lineage_id,
                    correction_lineage_origin=cast(
                        Literal["review_initiated", "user_initiated"] | None,
                        workspace.correction_lineage_origin,
                    ),
                    automatic_correction_round=cast(
                        Literal[0, 1] | None,
                        workspace.automatic_correction_round,
                    ),
                    source_arc_parent_review_id=(
                        workspace.source_arc_parent_review_id
                    ),
                    source_arc_closure_review_id=(
                        workspace.source_arc_closure_review_id
                    ),
                    source_chapter_candidate_review_id=(
                        workspace.active_repair_review_id
                    ),
                    source_feedback_id=workspace.source_feedback_id,
                )
            if submission_review.decision == "pass":
                return _CommandInstruction(
                    kind="commit_chapter",
                    request=CommitChapterRequest(
                        project_id=project_id,
                        chapter_id=chapter.id,
                        submission_id=pending.id,
                        review_id=submission_review.id,
                        expected_current_chapter_baseline_id=chapter.current_baseline_id,
                        expected_canon_baseline_id=canon_baseline_id,
                    ),
                    idempotency_key=(
                        f"engine:commit-chapter:{pending.id}:{submission_review.id}:"
                        f"{canon_baseline_id}"
                    ),
                )
            raise HarnessInvariantError("Rejected Chapter submission remained pending.")

        if evidence_correction:
            if (
                workspace.base_chapter_baseline_id is None
                or workspace.plan_ref_id is None
                or workspace.draft_ref_id is None
            ):
                raise HarnessInvariantError(
                    "Evidence correction lost its byte-frozen Chapter content."
                )
            if workspace.observations_ref_id is None:
                return self._chapter_task(
                    "chapter.revise.observe",
                    chapter,
                    workspace,
                )
            if workspace.candidate_canon_patch_ref_id is None:
                raise HarnessInvariantError(
                    "Evidence correction observations have no bound Canon intent."
                )
            return _CommandInstruction(
                kind="submit_chapter",
                request=SubmitChapterRequest(
                    project_id=project_id,
                    chapter_id=chapter.id,
                    expected_workspace_lock_version=workspace.lock_version,
                ),
                idempotency_key=(
                    f"engine:submit-chapter-evidence:{chapter.id}:"
                    f"{workspace.lock_version}"
                ),
            )

        active_repair_review = (
            None
            if workspace.active_repair_review_id is None
            else await chapters.get_review(
                project_id=project_id,
                review_id=workspace.active_repair_review_id,
            )
        )
        if active_repair_review is not None:
            if active_repair_review.repair_contract_ref_id is None:
                raise HarnessInvariantError("Chapter local repair has no contract.")
            repair = ChapterRepairContract.model_validate_json(
                (
                    await content.get_packed(
                        project_id=project_id,
                        ref_id=active_repair_review.repair_contract_ref_id,
                    )
                ).unpack_and_verify()
            )
            scope = set(repair.authorized_components)
            plan_repaired = await execution.has_applied_task(
                project_id=project_id,
                run_id=run.id,
                task_kind="chapter.repair.plan",
                book_id=book_id,
                arc_id=arc_id,
                chapter_id=chapter.id,
                book_baseline_id=workspace.book_baseline_id,
                arc_baseline_id=workspace.arc_baseline_id,
                chapter_baseline_id=workspace.base_chapter_baseline_id,
                workspace_work_cycle_id=workspace.work_cycle_id,
                source_chapter_candidate_review_id=active_repair_review.id,
                source_feedback_id=workspace.source_feedback_id,
                source_arc_parent_review_id=workspace.source_arc_parent_review_id,
                source_arc_closure_review_id=workspace.source_arc_closure_review_id,
            )
            prose_repaired = await execution.has_applied_task(
                project_id=project_id,
                run_id=run.id,
                task_kind="chapter.repair.prose",
                book_id=book_id,
                arc_id=arc_id,
                chapter_id=chapter.id,
                book_baseline_id=workspace.book_baseline_id,
                arc_baseline_id=workspace.arc_baseline_id,
                chapter_baseline_id=workspace.base_chapter_baseline_id,
                workspace_work_cycle_id=workspace.work_cycle_id,
                source_chapter_candidate_review_id=active_repair_review.id,
                source_feedback_id=workspace.source_feedback_id,
                source_arc_parent_review_id=workspace.source_arc_parent_review_id,
                source_arc_closure_review_id=workspace.source_arc_closure_review_id,
            )
            observations_repaired = await execution.has_applied_task(
                project_id=project_id,
                run_id=run.id,
                task_kind="chapter.repair.observation",
                book_id=book_id,
                arc_id=arc_id,
                chapter_id=chapter.id,
                book_baseline_id=workspace.book_baseline_id,
                arc_baseline_id=workspace.arc_baseline_id,
                chapter_baseline_id=workspace.base_chapter_baseline_id,
                workspace_work_cycle_id=workspace.work_cycle_id,
                source_chapter_candidate_review_id=active_repair_review.id,
                source_feedback_id=workspace.source_feedback_id,
                source_arc_parent_review_id=workspace.source_arc_parent_review_id,
                source_arc_closure_review_id=workspace.source_arc_closure_review_id,
            )
            if (
                not any((plan_repaired, prose_repaired, observations_repaired))
                and workspace.semantic_repair_count >= workspace.semantic_repair_limit
                and repair.repair_stage != "derived_dependency_closure"
            ):
                raise HarnessInvariantError(
                    "Chapter semantic correction for this frozen review is exhausted.",
                    failure_code="semantic_repair_exhausted",
                )
            repairs_plan = "plan" in scope
            repairs_prose = "prose" in scope and not repairs_plan
            if repair.repair_stage == "derived_dependency_closure" and (
                repairs_plan
                or repairs_prose
                or not scope <= {"observations", "canon"}
            ):
                raise HarnessInvariantError(
                    "Chapter derived dependency closure has an illegal repair scope."
                )
            if repairs_plan and not plan_repaired:
                return self._chapter_task("chapter.repair.plan", chapter, workspace)
            if repairs_prose and not prose_repaired:
                return self._chapter_task("chapter.repair.prose", chapter, workspace)
            if workspace.draft_ref_id is None:
                if not repairs_plan:
                    raise HarnessInvariantError(
                        "Chapter repair lost prose without a plan replacement."
                    )
                draft_kind = (
                    "chapter.revise.draft"
                    if workspace.base_chapter_baseline_id is not None
                    else "chapter.draft"
                )
                return self._chapter_task(draft_kind, chapter, workspace)
            if workspace.observations_ref_id is None:
                observe_kind = (
                    "chapter.revise.observe"
                    if workspace.base_chapter_baseline_id is not None
                    else "chapter.observe"
                )
                return self._chapter_task(observe_kind, chapter, workspace)
            if (
                not repairs_plan
                and not repairs_prose
                and scope.intersection({"observations", "canon"})
                and not observations_repaired
            ):
                return self._chapter_task(
                    "chapter.repair.observation", chapter, workspace
                )
            return _CommandInstruction(
                kind="submit_chapter",
                request=SubmitChapterRequest(
                    project_id=project_id,
                    chapter_id=chapter.id,
                    expected_workspace_lock_version=workspace.lock_version,
                ),
                idempotency_key=f"engine:submit-chapter:{chapter.id}:{workspace.lock_version}",
            )

        if workspace.base_chapter_baseline_id is not None:
            revised_plan = await execution.has_applied_task(
                project_id=project_id,
                run_id=run.id,
                task_kind="chapter.revise.plan",
                book_id=book_id,
                arc_id=arc_id,
                chapter_id=chapter.id,
                book_baseline_id=workspace.book_baseline_id,
                arc_baseline_id=workspace.arc_baseline_id,
                chapter_baseline_id=workspace.base_chapter_baseline_id,
                workspace_work_cycle_id=workspace.work_cycle_id,
                source_feedback_id=workspace.source_feedback_id,
                source_arc_parent_review_id=workspace.source_arc_parent_review_id,
                source_arc_closure_review_id=workspace.source_arc_closure_review_id,
            )
            if not revised_plan:
                return self._chapter_task("chapter.revise.plan", chapter, workspace)
            if workspace.draft_ref_id is None:
                return self._chapter_task("chapter.revise.draft", chapter, workspace)
            if workspace.observations_ref_id is None:
                return self._chapter_task("chapter.revise.observe", chapter, workspace)
        else:
            if workspace.plan_ref_id is None:
                return self._chapter_task("chapter.plan", chapter, workspace)
            if workspace.draft_ref_id is None:
                return self._chapter_task("chapter.draft", chapter, workspace)
            if workspace.observations_ref_id is None:
                return self._chapter_task("chapter.observe", chapter, workspace)
        if workspace.candidate_canon_patch_ref_id is None:
            raise HarnessInvariantError("Chapter observations have no bound Canon patch.")
        return _CommandInstruction(
            kind="submit_chapter",
            request=SubmitChapterRequest(
                project_id=project_id,
                chapter_id=chapter.id,
                expected_workspace_lock_version=workspace.lock_version,
            ),
            idempotency_key=f"engine:submit-chapter:{chapter.id}:{workspace.lock_version}",
        )

    @staticmethod
    def _chapter_task(task_kind: str, chapter: object, workspace: object) -> _TaskInstruction:
        return _TaskInstruction(
            role="chapter_writer",
            task_kind=task_kind,
            book_id=cast(str, getattr(chapter, "book_id")),
            arc_id=cast(str, getattr(chapter, "arc_id")),
            chapter_id=cast(str, getattr(chapter, "id")),
            workspace_lock_version=cast(int, getattr(workspace, "lock_version")),
            book_baseline_id=cast(str, getattr(workspace, "book_baseline_id")),
            arc_baseline_id=cast(str, getattr(workspace, "arc_baseline_id")),
            chapter_baseline_id=cast(
                str | None, getattr(workspace, "base_chapter_baseline_id")
            ),
            correction_lineage_id=cast(
                str | None, getattr(workspace, "correction_lineage_id")
            ),
            correction_lineage_origin=cast(
                Literal["review_initiated", "user_initiated"] | None,
                getattr(workspace, "correction_lineage_origin"),
            ),
            automatic_correction_round=cast(
                Literal[0, 1] | None,
                getattr(workspace, "automatic_correction_round"),
            ),
            source_arc_parent_review_id=cast(
                str | None, getattr(workspace, "source_arc_parent_review_id")
            ),
            source_arc_closure_review_id=cast(
                str | None, getattr(workspace, "source_arc_closure_review_id")
            ),
            source_chapter_candidate_review_id=cast(
                str | None, getattr(workspace, "active_repair_review_id")
            ),
            source_feedback_id=cast(
                str | None, getattr(workspace, "source_feedback_id")
            ),
        )

    async def _apply_command(self, instruction: _CommandInstruction) -> None:
        if instruction.kind == "activate_change":
            await self._changes.activate(
                cast(ActivateChangeRequest, instruction.request),
                idempotency_key=instruction.idempotency_key,
            )
        elif instruction.kind == "create_arc":
            await self._arcs.create_story_arc(
                cast(CreateStoryArcRequest, instruction.request),
                idempotency_key=instruction.idempotency_key,
            )
        elif instruction.kind == "create_chapter":
            await self._chapters.create_chapter(
                cast(CreateChapterRequest, instruction.request),
                idempotency_key=instruction.idempotency_key,
            )
        elif instruction.kind == "rebase_arc":
            await self._arcs.rebase_stale_workspace(
                cast(RebaseStaleArcRequest, instruction.request),
                idempotency_key=instruction.idempotency_key,
            )
        elif instruction.kind == "rebase_chapter":
            await self._chapters.rebase_stale_workspace(
                cast(RebaseStaleChapterRequest, instruction.request),
                idempotency_key=instruction.idempotency_key,
            )
        elif instruction.kind == "submit_book":
            await self._books.submit_for_review(
                cast(SubmitBookRequest, instruction.request),
                idempotency_key=instruction.idempotency_key,
            )
        elif instruction.kind == "submit_arc":
            await self._arcs.submit_for_review(
                cast(SubmitArcRequest, instruction.request),
                idempotency_key=instruction.idempotency_key,
            )
        elif instruction.kind == "submit_chapter":
            await self._chapters.submit_for_review(
                cast(SubmitChapterRequest, instruction.request),
                idempotency_key=instruction.idempotency_key,
            )
        elif instruction.kind == "commit_arc_auto":
            await self._arcs.commit_baseline_auto(
                cast(CommitArcAutoRequest, instruction.request),
                idempotency_key=instruction.idempotency_key,
            )
        elif instruction.kind == "commit_chapter":
            await self._chapters.commit_chapter_and_canon(
                cast(CommitChapterRequest, instruction.request),
                idempotency_key=instruction.idempotency_key,
            )
        elif instruction.kind == "open_arc_closure_revision":
            await self._authority.open_arc_closure_revision(
                cast(OpenArcClosureRevisionRequest, instruction.request),
                idempotency_key=instruction.idempotency_key,
            )
        elif instruction.kind == "open_book_completion_revision":
            await self._authority.open_book_completion_revision(
                cast(OpenBookCompletionRevisionRequest, instruction.request),
                idempotency_key=instruction.idempotency_key,
            )
        elif instruction.kind == "commit_book_handoff":
            await self._authority.commit_book_progress_handoff(
                cast(CommitBookProgressHandoffRequest, instruction.request),
                idempotency_key=instruction.idempotency_key,
            )
        elif instruction.kind == "commit_book_completion":
            await self._authority.commit_book_completion(
                cast(CommitBookCompletionRequest, instruction.request),
                idempotency_key=instruction.idempotency_key,
            )
        else:  # pragma: no cover - Literal exhaustiveness guard.
            raise HarnessInvariantError(f"Unknown deterministic command {instruction.kind!r}.")
