from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import replace
from typing import Literal

from pydantic import BaseModel

from app.agents.contracts import ArcPlanProposal
from app.agents.registry import DEFAULT_EVALUATION_STRATEGY_REGISTRY
from app.db.uow import StoreSession
from app.domain.arc.contracts import (
    ApplyArcTaskRequest,
    ApplyArcTaskResult,
    ApproveArcRequest,
    ArcEvaluation,
    ArcRepairContract,
    ArcRepairPatch,
    CommitArcAutoRequest,
    CommitArcResult,
    CreateStoryArcRequest,
    CreateStoryArcResult,
    RecordArcReviewRequest,
    RecordArcReviewResult,
    RebaseStaleArcRequest,
    RebaseStaleArcResult,
    RejectArcGateRequest,
    RejectArcGateResult,
    SubmitArcRequest,
    SubmitArcResult,
)
from app.domain.arc.outline import ArcOutlineProjectionError, resolve_outline_entry
from app.domain.book.contracts import BookArcContract, BookArcTopology
from app.domain.commands import (
    Actor,
    CommandEffect,
    CommandEnvelope,
    CommandExecution,
    CommandPreconditionError,
    EventDraft,
)
from app.domain.projects import ProjectNotFoundError
from app.store.arcs import (
    ArcApprovalGateRecord,
    ArcApprovalRecord,
    ArcBaselineRecord,
    ArcBookChangeRequestRecord,
    ArcRecord,
    ArcReviewRecord,
    ArcSubmissionRecord,
    ArcWorkspaceRecord,
)
from app.store.command_bus import CommandBus
from app.store.content import prepare_canonical_json
from app.store.execution import SuccessfulTaskRecord


class ArcNotFoundError(LookupError):
    pass


def _task_matches_workspace(
    task: SuccessfulTaskRecord,
    workspace: ArcWorkspaceRecord,
    *,
    expected_lock_version: int,
) -> bool:
    return (
        task.delivery_state == "pending"
        and task.scope_layer == "arc"
        and task.book_id == workspace.book_id
        and task.arc_id == workspace.arc_id
        and task.chapter_id is None
        and task.workspace_lock_version == expected_lock_version == workspace.lock_version
        and task.workspace_work_cycle_id == workspace.work_cycle_id
        and task.book_baseline_id == workspace.book_baseline_id
        and task.arc_baseline_id == workspace.base_arc_baseline_id
        and task.canon_baseline_id == workspace.canon_baseline_id
        and workspace.state == "active"
    )


def _merge_arc_repair(
    *,
    current: ArcPlanProposal,
    patch: ArcRepairPatch,
    contract: ArcRepairContract,
) -> ArcPlanProposal:
    authorized = set(contract.authorized_components)
    requested = {change.component for change in patch.changes}
    unauthorized = requested.difference(authorized)
    if unauthorized:
        raise CommandPreconditionError(
            "Arc repair changed unauthorized components: "
            + ", ".join(sorted(unauthorized))
        )
    merged = current.model_dump(mode="python")
    for change in patch.changes:
        merged[change.component] = change.value
    proposal = ArcPlanProposal.model_validate(merged)
    if proposal == current:
        raise CommandPreconditionError("Arc repair result made no authorized change.")
    return proposal


def _derive_outline_closure_checkpoint(
    *,
    proposal: ArcPlanProposal,
    planned_after_cumulative_chapter_count: int,
    planned_after_arc_chapter_count: int,
    initial_plan: bool,
) -> int:
    if (
        planned_after_cumulative_chapter_count < 0
        or planned_after_arc_chapter_count < 0
        or planned_after_arc_chapter_count
        > planned_after_cumulative_chapter_count
    ):
        raise CommandPreconditionError("Arc outline effective-point counts are invalid.")
    if initial_plan and not proposal.chapter_outline:
        raise CommandPreconditionError("An initial Arc must plan at least one Chapter.")
    return planned_after_cumulative_chapter_count + len(proposal.chapter_outline)


async def _load_assigned_book_arc_contract(
    session: StoreSession,
    *,
    project_id: str,
    book_id: str,
    book_baseline_id: str,
    arc_ordinal: int,
) -> BookArcContract:
    baseline = await session.books.get_baseline(
        project_id=project_id,
        book_id=book_id,
        baseline_id=book_baseline_id,
    )
    if baseline is None:
        raise CommandPreconditionError("Assigned Book baseline does not exist.")
    if (
        arc_ordinal < 1
        or arc_ordinal > baseline.arc_contract_count
        or baseline.final_arc_ordinal != baseline.arc_contract_count
    ):
        raise CommandPreconditionError(
            "Story Arc ordinal is outside the assigned Book topology."
        )
    topology = BookArcTopology.model_validate_json(
        (
            await session.content.get_packed(
                project_id=project_id,
                ref_id=baseline.arc_topology_ref_id,
            )
        ).unpack_and_verify()
    )
    if (
        len(topology.arcs) != baseline.arc_contract_count
        or not topology.arcs[-1].is_final
    ):
        raise CommandPreconditionError(
            "Book Arc topology content disagrees with routing metadata."
        )
    return topology.arcs[arc_ordinal - 1]


class ArcCommandService:
    def __init__(
        self,
        command_bus: CommandBus,
        *,
        id_factory: Callable[[], str] | None = None,
        now_ms: Callable[[], int] | None = None,
    ) -> None:
        self._command_bus = command_bus
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex)
        self._now_ms = now_ms or (lambda: time.time_ns() // 1_000_000)

    def _envelope(
        self,
        *,
        request: BaseModel,
        project_id: str,
        idempotency_key: str,
        command_kind: str,
        actor: Actor,
        created_at_ms: int,
        source_task_id: str | None = None,
    ) -> CommandEnvelope:
        return CommandEnvelope.for_request(
            project_id=project_id,
            idempotency_key=idempotency_key,
            command_kind=command_kind,
            request_schema=f"{command_kind}.request.v1",
            request_payload=request,
            actor=actor,
            command_id=self._id_factory(),
            source_task_id=source_task_id,
            created_at_ms=created_at_ms,
        )

    async def _read_successful_result(
        self,
        request: ApplyArcTaskRequest,
    ) -> tuple[SuccessfulTaskRecord, bytes]:
        async with self._command_bus.read_unit_of_work() as session:
            task = await session.execution.get_successful_task(
                project_id=request.project_id,
                task_id=request.task_id,
                attempt_id=request.attempt_id,
            )
            if task is None:
                raise CommandPreconditionError("Agent task has no complete successful result.")
            packed = await session.content.get_packed(
                project_id=request.project_id,
                ref_id=task.result_ref_id,
            )
        return task, packed.unpack_and_verify()

    async def create_story_arc(
        self,
        request: CreateStoryArcRequest,
        *,
        idempotency_key: str,
    ) -> CommandExecution[CreateStoryArcResult]:
        timestamp = self._now_ms()
        arc_id = self._id_factory()
        workspace_id = self._id_factory()
        envelope = self._envelope(
            request=request,
            project_id=request.project_id,
            idempotency_key=idempotency_key,
            command_kind="create_story_arc",
            actor="engine",
            source_task_id=request.source_task_id,
            created_at_ms=timestamp,
        )

        async def handler(session: StoreSession) -> CommandEffect[CreateStoryArcResult]:
            project = await session.projects.get(request.project_id)
            book = await session.books.get_for_project(request.project_id)
            book_baseline = (
                None
                if book is None or book.current_baseline_id is None
                else await session.books.get_baseline(
                    project_id=request.project_id,
                    book_id=request.book_id,
                    baseline_id=book.current_baseline_id,
                )
            )
            if project is None:
                raise ProjectNotFoundError(request.project_id)
            if (
                book is None
                or book_baseline is None
                or book.id != request.book_id
                or book.lifecycle_status != "active"
                or book.current_completion_id is not None
                or book.current_baseline_id != request.expected_book_baseline_id
                or project.current_canon_baseline_id != request.expected_canon_baseline_id
            ):
                raise CommandPreconditionError("Book or Canon dependencies are not current.")
            if request.expected_ordinal > book_baseline.arc_contract_count:
                raise CommandPreconditionError(
                    "The requested Story Arc ordinal exceeds the approved Book topology."
                )
            if (
                await session.arcs.get_unfinished_for_book(
                    project_id=request.project_id,
                    book_id=request.book_id,
                )
                is not None
            ):
                raise CommandPreconditionError("The Book already has a planning or active Arc.")
            prior_arc = await session.arcs.get_latest_for_book(
                project_id=request.project_id,
                book_id=request.book_id,
            )
            if prior_arc is not None and (
                prior_arc.lifecycle_status != "completed"
                or prior_arc.current_baseline_id is None
                or prior_arc.current_closure_id is None
            ):
                raise CommandPreconditionError("The prior Arc is not at a safe completion boundary.")
            if prior_arc is None:
                if (
                    request.expected_ordinal != 1
                    or request.source_progress_handoff_id is not None
                    or book.current_progress_handoff_id is not None
                ):
                    raise CommandPreconditionError(
                        "The initial Story Arc must be topology ordinal one without a handoff."
                    )
            else:
                if (
                    book.current_progress_handoff_id is None
                    or request.source_progress_handoff_id
                    != book.current_progress_handoff_id
                ):
                    raise CommandPreconditionError(
                        "A later Story Arc requires the current Book progress handoff."
                    )
                prior_closure_id = prior_arc.current_closure_id
                if prior_closure_id is None:  # guarded above; keeps the FK use explicit.
                    raise CommandPreconditionError(
                        "The prior Arc is missing its formal closure."
                    )
                handoff = await session.book_progress_handoffs.get(
                    project_id=request.project_id,
                    handoff_id=book.current_progress_handoff_id,
                )
                closure = await session.arc_closures.get(
                    project_id=request.project_id,
                    closure_id=prior_closure_id,
                )
                if (
                    handoff is None
                    or closure is None
                    or handoff.book_id != request.book_id
                    or handoff.book_baseline_id != request.expected_book_baseline_id
                    or handoff.source_arc_closure_id != prior_closure_id
                    or closure.book_id != request.book_id
                    or closure.arc_id != prior_arc.id
                    or handoff.next_arc_ordinal != request.expected_ordinal
                    or request.expected_ordinal != prior_arc.ordinal + 1
                ):
                    raise CommandPreconditionError(
                        "The requested Story Arc does not match the current Book handoff."
                    )
            ordinal = await session.arcs.next_ordinal(book_id=request.book_id)
            if ordinal != request.expected_ordinal:
                raise CommandPreconditionError(
                    "The requested Story Arc ordinal is duplicated or leaves a gap."
                )
            await session.arcs.insert(
                ArcRecord(
                    id=arc_id,
                    project_id=request.project_id,
                    book_id=request.book_id,
                    ordinal=ordinal,
                    lifecycle_status="planning",
                    current_baseline_id=None,
                    latest_closure_review_id=None,
                    current_closure_id=None,
                    created_at_ms=timestamp,
                    updated_at_ms=timestamp,
                    completed_at_ms=None,
                )
            )
            await session.arcs.insert_workspace(
                ArcWorkspaceRecord(
                    id=workspace_id,
                    project_id=request.project_id,
                    book_id=request.book_id,
                    arc_id=arc_id,
                    state="active",
                    lock_version=1,
                    work_cycle_id=uuid.uuid4().hex,
                    active_repair_review_id=None,
                    base_arc_baseline_id=None,
                    book_baseline_id=request.expected_book_baseline_id,
                    canon_baseline_id=request.expected_canon_baseline_id,
                    book_progress_handoff_id=book.current_progress_handoff_id,
                    prior_arc_id=None if prior_arc is None else prior_arc.id,
                    prior_arc_baseline_id=(
                        None if prior_arc is None else prior_arc.current_baseline_id
                    ),
                    revision_origin="initial",
                    source_arc_parent_review_id=None,
                    source_arc_closure_review_id=None,
                    source_book_parent_review_id=None,
                    source_book_completion_review_id=None,
                    source_feedback_id=None,
                    correction_lineage_id=None,
                    correction_lineage_origin=None,
                    automatic_correction_round=None,
                    plan_ref_id=None,
                    planned_after_cumulative_chapter_count=None,
                    planned_after_arc_chapter_count=None,
                    closure_cumulative_chapter_count=None,
                    repair_policy_id="semantic-repair-v1",
                    semantic_repair_count=0,
                    semantic_repair_limit=1,
                    stale_reason_code=None,
                    stale_at_ms=None,
                    created_at_ms=timestamp,
                    updated_at_ms=timestamp,
                )
            )
            result = CreateStoryArcResult(
                project_id=request.project_id,
                book_id=request.book_id,
                arc_id=arc_id,
                workspace_id=workspace_id,
                ordinal=ordinal,
            )
            return CommandEffect(
                result=result,
                events=(
                    EventDraft(
                        event_type="arc.created",
                        aggregate_type="arc",
                        aggregate_id=arc_id,
                        payload={
                            "book_id": request.book_id,
                            "ordinal": ordinal,
                            "book_baseline_id": request.expected_book_baseline_id,
                        },
                    ),
                ),
            )

        return await self._command_bus.execute(
            envelope=envelope,
            result_type=CreateStoryArcResult,
            handler=handler,
        )

    async def rebase_stale_workspace(
        self,
        request: RebaseStaleArcRequest,
        *,
        idempotency_key: str,
    ) -> CommandExecution[RebaseStaleArcResult]:
        timestamp = self._now_ms()
        envelope = self._envelope(
            request=request,
            project_id=request.project_id,
            idempotency_key=idempotency_key,
            command_kind="rebase_stale_arc_workspace",
            actor="engine",
            created_at_ms=timestamp,
        )

        async def handler(session: StoreSession) -> CommandEffect[RebaseStaleArcResult]:
            project = await session.projects.get(request.project_id)
            book = await session.books.get_for_project(request.project_id)
            arc = await session.arcs.get(
                project_id=request.project_id,
                arc_id=request.arc_id,
            )
            workspace = await session.arcs.get_workspace(
                project_id=request.project_id,
                arc_id=request.arc_id,
            )
            if (
                project is None
                or book is None
                or book.id != request.book_id
                or book.current_baseline_id != request.expected_book_baseline_id
                or project.current_canon_baseline_id
                != request.expected_canon_baseline_id
                or arc is None
                or arc.book_id != request.book_id
                or arc.current_baseline_id != request.expected_arc_baseline_id
                or workspace is None
                or workspace.state != "stale"
                or workspace.lock_version != request.expected_workspace_lock_version
            ):
                raise CommandPreconditionError("Stale Arc rebase dependencies changed.")
            parent_arc_baseline_id = (
                arc.current_baseline_id
                if arc.current_baseline_id is not None
                else workspace.base_arc_baseline_id
            )
            parent_book_rebase = (
                workspace.stale_reason_code == "upstream_book_revised"
            )
            pending = await session.arcs.find_pending_submission(
                project_id=request.project_id,
                arc_id=request.arc_id,
            )
            if pending is not None and not await session.arcs.close_submission(
                project_id=request.project_id,
                submission_id=pending.id,
                disposition="superseded",
                reason_code="stale_workspace_rebased",
                closed_at_ms=timestamp,
            ):
                raise CommandPreconditionError("Stale Arc submission changed.")
            gate = await session.arcs.find_pending_gate(
                project_id=request.project_id,
                arc_id=request.arc_id,
            )
            if gate is not None and not await session.arcs.close_approval_gate(
                project_id=request.project_id,
                gate_id=gate.id,
                state="superseded",
                closed_at_ms=timestamp,
            ):
                raise CommandPreconditionError("Stale Arc approval gate changed.")
            updated = replace(
                workspace,
                state="active",
                lock_version=workspace.lock_version + 1,
                work_cycle_id=uuid.uuid4().hex,
                active_repair_review_id=None,
                base_arc_baseline_id=parent_arc_baseline_id,
                book_baseline_id=request.expected_book_baseline_id,
                canon_baseline_id=request.expected_canon_baseline_id,
                revision_origin=(
                    "parent_book_baseline_rebase"
                    if parent_book_rebase
                    else workspace.revision_origin
                ),
                book_progress_handoff_id=(
                    None
                    if parent_book_rebase
                    else workspace.book_progress_handoff_id
                ),
                source_arc_parent_review_id=None,
                source_arc_closure_review_id=None,
                source_book_parent_review_id=None,
                source_book_completion_review_id=None,
                source_feedback_id=None,
                correction_lineage_id=None,
                correction_lineage_origin=None,
                automatic_correction_round=None,
                plan_ref_id=None,
                planned_after_cumulative_chapter_count=None,
                planned_after_arc_chapter_count=None,
                closure_cumulative_chapter_count=None,
                semantic_repair_count=0,
                stale_reason_code=None,
                stale_at_ms=None,
                updated_at_ms=timestamp,
            )
            if not await session.arcs.compare_and_set_workspace(
                record=updated,
                expected_lock_version=workspace.lock_version,
            ):
                raise CommandPreconditionError("Stale Arc workspace rebase CAS failed.")
            result = RebaseStaleArcResult(
                project_id=request.project_id,
                arc_id=request.arc_id,
                workspace_lock_version=updated.lock_version,
                base_arc_baseline_id=updated.base_arc_baseline_id,
                book_baseline_id=updated.book_baseline_id,
                canon_baseline_id=updated.canon_baseline_id,
            )
            return CommandEffect(
                result=result,
                events=(
                    EventDraft(
                        event_type="arc.workspace_rebased",
                        aggregate_type="arc",
                        aggregate_id=request.arc_id,
                        payload={
                            "workspace_lock_version": updated.lock_version,
                            "book_baseline_id": updated.book_baseline_id,
                            "arc_baseline_id": updated.base_arc_baseline_id,
                            "canon_baseline_id": updated.canon_baseline_id,
                        },
                    ),
                ),
            )

        return await self._command_bus.execute(
            envelope=envelope,
            result_type=RebaseStaleArcResult,
            handler=handler,
        )

    async def apply_task_result(
        self,
        request: ApplyArcTaskRequest,
        *,
        idempotency_key: str,
    ) -> CommandExecution[ApplyArcTaskResult]:
        task, raw = await self._read_successful_result(request)
        if (
            task.task_kind not in {"arc.plan", "arc.revise", "arc.repair"}
            or task.role != "arc_planner"
            or task.scope_layer != "arc"
            or task.book_id != request.book_id
            or task.arc_id != request.arc_id
        ):
            raise CommandPreconditionError("Task is not an authorized Arc planning task.")
        workspace_snapshot: ArcWorkspaceRecord | None = None
        review_snapshot: ArcReviewRecord | None = None
        if task.task_kind == "arc.repair":
            patch = ArcRepairPatch.model_validate_json(raw)
            proposal: ArcPlanProposal | None = None
            async with self._command_bus.read_unit_of_work() as session:
                workspace_snapshot = await session.arcs.get_workspace(
                    project_id=request.project_id,
                    arc_id=request.arc_id,
                )
                if workspace_snapshot is not None and _task_matches_workspace(
                    task,
                    workspace_snapshot,
                    expected_lock_version=request.expected_workspace_lock_version,
                ):
                    review_snapshot = (
                        None
                        if workspace_snapshot.active_repair_review_id is None
                        else await session.arcs.get_review(
                            project_id=request.project_id,
                            review_id=workspace_snapshot.active_repair_review_id,
                        )
                    )
                    if (
                        review_snapshot is None
                        or task.source_arc_candidate_review_id != review_snapshot.id
                        or review_snapshot.decision != "local_repair"
                        or review_snapshot.repair_contract_ref_id is None
                        or workspace_snapshot.plan_ref_id is None
                        or workspace_snapshot.semantic_repair_count
                        >= workspace_snapshot.semantic_repair_limit
                    ):
                        raise CommandPreconditionError(
                            "Arc has no active local repair budget."
                        )
                    repair_contract = ArcRepairContract.model_validate_json(
                        (
                            await session.content.get_packed(
                                project_id=request.project_id,
                                ref_id=review_snapshot.repair_contract_ref_id,
                            )
                        ).unpack_and_verify()
                    )
                    current_plan = ArcPlanProposal.model_validate_json(
                        (
                            await session.content.get_packed(
                                project_id=request.project_id,
                                ref_id=workspace_snapshot.plan_ref_id,
                            )
                        ).unpack_and_verify()
                    )
                    proposal = _merge_arc_repair(
                        current=current_plan,
                        patch=patch,
                        contract=repair_contract,
                    )
        else:
            proposal = ArcPlanProposal.model_validate_json(raw)
        prepared_plan = None if proposal is None else prepare_canonical_json(proposal)
        timestamp = self._now_ms()
        plan_ref_id = self._id_factory()
        envelope = self._envelope(
            request=request,
            project_id=request.project_id,
            idempotency_key=idempotency_key,
            command_kind=f"apply_{task.task_kind.replace('.', '_')}_result",
            actor="engine",
            source_task_id=request.task_id,
            created_at_ms=timestamp,
        )

        async def handler(session: StoreSession) -> CommandEffect[ApplyArcTaskResult]:
            current_task = await session.execution.get_successful_task(
                project_id=request.project_id,
                task_id=request.task_id,
                attempt_id=request.attempt_id,
            )
            workspace = await session.arcs.get_workspace(
                project_id=request.project_id,
                arc_id=request.arc_id,
            )
            arc = await session.arcs.get(
                project_id=request.project_id,
                arc_id=request.arc_id,
            )
            if current_task != task or workspace is None or arc is None:
                raise CommandPreconditionError("Arc task or workspace no longer exists.")
            if not _task_matches_workspace(
                task,
                workspace,
                expected_lock_version=request.expected_workspace_lock_version,
            ):
                if not await session.execution.mark_delivery_discarded_stale(
                    project_id=request.project_id,
                    task_id=request.task_id,
                    attempt_id=request.attempt_id,
                    updated_at_ms=timestamp,
                ):
                    raise CommandPreconditionError("Stale Arc task delivery changed concurrently.")
                result = ApplyArcTaskResult(
                    project_id=request.project_id,
                    arc_id=request.arc_id,
                    task_id=request.task_id,
                    delivery="discarded_stale",
                    workspace_lock_version=workspace.lock_version,
                )
                return CommandEffect(
                    result=result,
                    events=(
                        EventDraft(
                            event_type="arc.task_result_discarded_stale",
                            aggregate_type="arc",
                            aggregate_id=request.arc_id,
                            payload={"task_id": request.task_id},
                        ),
                    ),
                )

            if proposal is None or prepared_plan is None:
                raise CommandPreconditionError(
                    "Current Arc task has no prepared result for this workspace."
                )
            await _load_assigned_book_arc_contract(
                session,
                project_id=request.project_id,
                book_id=request.book_id,
                book_baseline_id=workspace.book_baseline_id,
                arc_ordinal=arc.ordinal,
            )
            if task.task_kind == "arc.repair":
                planned_after_cumulative_chapter_count = (
                    workspace.planned_after_cumulative_chapter_count
                )
                planned_after_arc_chapter_count = (
                    workspace.planned_after_arc_chapter_count
                )
                if (
                    planned_after_cumulative_chapter_count is None
                    or planned_after_arc_chapter_count is None
                ):
                    raise CommandPreconditionError(
                        "Arc repair workspace has no frozen outline effective point."
                    )
            else:
                planned_after_cumulative_chapter_count = (
                    await session.chapters.count_committed_for_book(
                        book_id=request.book_id
                    )
                )
                planned_after_arc_chapter_count = (
                    await session.chapters.count_committed(arc_id=request.arc_id)
                )
            closure_cumulative_chapter_count = _derive_outline_closure_checkpoint(
                proposal=proposal,
                planned_after_cumulative_chapter_count=(
                    planned_after_cumulative_chapter_count
                ),
                planned_after_arc_chapter_count=planned_after_arc_chapter_count,
                initial_plan=workspace.base_arc_baseline_id is None,
            )
            repair_increment = 0
            if task.task_kind == "arc.repair":
                review = (
                    None
                    if workspace.active_repair_review_id is None
                    else await session.arcs.get_review(
                        project_id=request.project_id,
                        review_id=workspace.active_repair_review_id,
                    )
                )
                if (
                    workspace_snapshot is None
                    or workspace != workspace_snapshot
                    or review_snapshot is None
                    or review != review_snapshot
                    or task.source_arc_candidate_review_id != review_snapshot.id
                    or workspace.semantic_repair_count >= workspace.semantic_repair_limit
                ):
                    raise CommandPreconditionError(
                        "Arc repair authorization changed before result delivery."
                    )
                repair_increment = 1

            pending_gate = await session.arcs.find_pending_gate(
                project_id=request.project_id,
                arc_id=request.arc_id,
            )
            if pending_gate is not None and not await session.arcs.close_approval_gate(
                project_id=request.project_id,
                gate_id=pending_gate.id,
                state="superseded",
                closed_at_ms=timestamp,
            ):
                raise CommandPreconditionError("Arc approval gate changed concurrently.")
            pending = await session.arcs.find_pending_submission(
                project_id=request.project_id,
                arc_id=request.arc_id,
            )
            if pending is not None and not await session.arcs.close_submission(
                project_id=request.project_id,
                submission_id=pending.id,
                disposition="superseded",
                reason_code="workspace_edited",
                closed_at_ms=timestamp,
            ):
                raise CommandPreconditionError("Arc submission changed concurrently.")
            plan_ref = await session.content.put(
                project_id=request.project_id,
                prepared=prepared_plan,
                semantic_kind="arc.plan",
                media_type="application/json",
                schema_id="arc-plan-proposal",
                schema_version=4,
                ref_id=plan_ref_id,
                created_at_ms=timestamp,
            )
            updated_workspace = replace(
                workspace,
                state="active",
                lock_version=workspace.lock_version + 1,
                plan_ref_id=plan_ref.id,
                planned_after_cumulative_chapter_count=(
                    planned_after_cumulative_chapter_count
                ),
                planned_after_arc_chapter_count=planned_after_arc_chapter_count,
                closure_cumulative_chapter_count=(
                    closure_cumulative_chapter_count
                ),
                semantic_repair_count=workspace.semantic_repair_count + repair_increment,
                stale_reason_code=None,
                stale_at_ms=None,
                updated_at_ms=timestamp,
            )
            if not await session.arcs.compare_and_set_workspace(
                record=updated_workspace,
                expected_lock_version=workspace.lock_version,
            ):
                raise CommandPreconditionError("Arc workspace CAS failed.")
            if not await session.execution.mark_delivery_applied(
                project_id=request.project_id,
                task_id=request.task_id,
                attempt_id=request.attempt_id,
                command_id=envelope.command_id,
                updated_at_ms=timestamp,
            ):
                raise CommandPreconditionError("Arc task delivery is no longer pending.")
            result = ApplyArcTaskResult(
                project_id=request.project_id,
                arc_id=request.arc_id,
                task_id=request.task_id,
                delivery="applied",
                workspace_lock_version=updated_workspace.lock_version,
            )
            return CommandEffect(
                result=result,
                events=(
                    EventDraft(
                        event_type="arc.workspace_updated",
                        aggregate_type="arc",
                        aggregate_id=request.arc_id,
                        payload={
                            "task_id": request.task_id,
                            "task_kind": task.task_kind,
                            "workspace_lock_version": updated_workspace.lock_version,
                            "planned_after_cumulative_chapter_count": (
                                planned_after_cumulative_chapter_count
                            ),
                            "planned_after_arc_chapter_count": (
                                planned_after_arc_chapter_count
                            ),
                            "closure_cumulative_chapter_count": (
                                closure_cumulative_chapter_count
                            ),
                        },
                    ),
                ),
            )

        return await self._command_bus.execute(
            envelope=envelope,
            result_type=ApplyArcTaskResult,
            handler=handler,
        )

    async def submit_for_review(
        self,
        request: SubmitArcRequest,
        *,
        idempotency_key: str,
    ) -> CommandExecution[SubmitArcResult]:
        timestamp = self._now_ms()
        submission_id = self._id_factory()
        manifest_ref_id = self._id_factory()
        envelope = self._envelope(
            request=request,
            project_id=request.project_id,
            idempotency_key=idempotency_key,
            command_kind="submit_arc_for_review",
            actor="engine",
            created_at_ms=timestamp,
        )

        async def handler(session: StoreSession) -> CommandEffect[SubmitArcResult]:
            project = await session.projects.get(request.project_id)
            book = await session.books.get_for_project(request.project_id)
            arc = await session.arcs.get(project_id=request.project_id, arc_id=request.arc_id)
            workspace = await session.arcs.get_workspace(
                project_id=request.project_id,
                arc_id=request.arc_id,
            )
            if (
                project is None
                or book is None
                or arc is None
                or arc.book_id != request.book_id
                or arc.lifecycle_status not in {"planning", "active", "closing"}
                or workspace is None
                or workspace.lock_version != request.expected_workspace_lock_version
                or workspace.state != "active"
                or workspace.plan_ref_id is None
                or workspace.planned_after_cumulative_chapter_count is None
                or workspace.planned_after_arc_chapter_count is None
                or workspace.closure_cumulative_chapter_count is None
                or workspace.book_baseline_id != book.current_baseline_id
                or workspace.canon_baseline_id != project.current_canon_baseline_id
            ):
                raise CommandPreconditionError("Arc workspace is not ready for review.")
            if (
                await session.arcs.find_pending_submission(
                    project_id=request.project_id,
                    arc_id=request.arc_id,
                )
                is not None
            ):
                raise CommandPreconditionError("An Arc submission is already pending.")
            source_feedback = (
                None
                if workspace.source_feedback_id is None
                else await session.feedback.get(
                    project_id=request.project_id,
                    feedback_id=workspace.source_feedback_id,
                )
            )
            if workspace.source_feedback_id is not None and (
                source_feedback is None
                or source_feedback.status != "applied"
                or source_feedback.route_layer != "arc"
                or source_feedback.book_id != request.book_id
                or source_feedback.arc_id != request.arc_id
                or source_feedback.content_ref_id != workspace.guidance_ref_id
            ):
                raise CommandPreconditionError(
                    "Arc guidance lost its exact applied feedback source."
                )
            manifest = {
                "schema": "arc-review-manifest-v6",
                "workspace_id": workspace.id,
                "workspace_lock_version": workspace.lock_version,
                "work_cycle_id": workspace.work_cycle_id,
                "base_arc_baseline_id": workspace.base_arc_baseline_id,
                "book_baseline_id": workspace.book_baseline_id,
                "canon_baseline_id": workspace.canon_baseline_id,
                "prior_arc_id": workspace.prior_arc_id,
                "prior_arc_baseline_id": workspace.prior_arc_baseline_id,
                "plan_ref_id": workspace.plan_ref_id,
                "planned_after_cumulative_chapter_count": (
                    workspace.planned_after_cumulative_chapter_count
                ),
                "planned_after_arc_chapter_count": (
                    workspace.planned_after_arc_chapter_count
                ),
                "closure_cumulative_chapter_count": (
                    workspace.closure_cumulative_chapter_count
                ),
                "guidance_ref_id": workspace.guidance_ref_id,
                "source_feedback_id": (
                    None if source_feedback is None else source_feedback.id
                ),
            }
            prepared_manifest = prepare_canonical_json(manifest)
            manifest_ref = await session.content.put(
                project_id=request.project_id,
                prepared=prepared_manifest,
                semantic_kind="arc.review_manifest",
                media_type="application/json",
                schema_id="arc-review-manifest",
                schema_version=6,
                ref_id=manifest_ref_id,
                created_at_ms=timestamp,
            )
            await session.arcs.insert_submission(
                ArcSubmissionRecord(
                    id=submission_id,
                    project_id=request.project_id,
                    book_id=request.book_id,
                    arc_id=request.arc_id,
                    workspace_id=workspace.id,
                    workspace_lock_version=workspace.lock_version,
                    work_cycle_id=workspace.work_cycle_id,
                    base_arc_baseline_id=workspace.base_arc_baseline_id,
                    book_baseline_id=workspace.book_baseline_id,
                    canon_baseline_id=workspace.canon_baseline_id,
                    prior_arc_id=workspace.prior_arc_id,
                    prior_arc_baseline_id=workspace.prior_arc_baseline_id,
                    plan_ref_id=workspace.plan_ref_id,
                    planned_after_cumulative_chapter_count=(
                        workspace.planned_after_cumulative_chapter_count
                    ),
                    planned_after_arc_chapter_count=(
                        workspace.planned_after_arc_chapter_count
                    ),
                    closure_cumulative_chapter_count=(
                        workspace.closure_cumulative_chapter_count
                    ),
                    content_manifest_ref_id=manifest_ref.id,
                    content_fingerprint=prepared_manifest.sha256,
                    disposition="pending",
                    close_reason_code=None,
                    created_at_ms=timestamp,
                    closed_at_ms=None,
                )
            )
            result = SubmitArcResult(
                project_id=request.project_id,
                arc_id=request.arc_id,
                submission_id=submission_id,
                content_fingerprint=prepared_manifest.sha256,
            )
            return CommandEffect(
                result=result,
                events=(
                    EventDraft(
                        event_type="arc.submitted",
                        aggregate_type="arc",
                        aggregate_id=request.arc_id,
                        payload={"submission_id": submission_id},
                    ),
                ),
            )

        return await self._command_bus.execute(
            envelope=envelope,
            result_type=SubmitArcResult,
            handler=handler,
        )

    async def record_review(
        self,
        request: RecordArcReviewRequest,
        *,
        idempotency_key: str,
    ) -> CommandExecution[RecordArcReviewResult]:
        timestamp = self._now_ms()
        review_id = self._id_factory()
        precheck_ref_id = self._id_factory()
        repair_ref_id = self._id_factory()
        gate_id = self._id_factory()
        change_request_id = self._id_factory()
        failure_ref_id = self._id_factory()
        async with self._command_bus.read_unit_of_work() as session:
            task = await session.execution.get_successful_task(
                project_id=request.project_id,
                task_id=request.evaluator_task_id,
                attempt_id=request.evaluator_attempt_id,
            )
            if task is None:
                raise CommandPreconditionError("Evaluator task has no successful result.")
            raw = (
                await session.content.get_packed(
                    project_id=request.project_id,
                    ref_id=task.result_ref_id,
                )
            ).unpack_and_verify()
        evaluation = ArcEvaluation.model_validate_json(raw)
        if evaluation.decision == "pass" and request.deterministic_precheck.get("passed") is not True:
            raise CommandPreconditionError("Arc deterministic prechecks did not pass.")
        prepared_precheck = prepare_canonical_json(request.deterministic_precheck)
        repair_contract = (
            ArcRepairContract(
                authorized_components=evaluation.repair_scope,
                issues=evaluation.issues,
            )
            if evaluation.decision == "local_repair"
            else None
        )
        prepared_repair = (
            None if repair_contract is None else prepare_canonical_json(repair_contract)
        )
        prepared_failure = prepare_canonical_json(
            {
                "code": "semantic_repair_exhausted",
                "message": "Arc semantic correction for this frozen review is exhausted.",
                "arc_id": request.arc_id,
            }
        )
        envelope = self._envelope(
            request=request,
            project_id=request.project_id,
            idempotency_key=idempotency_key,
            command_kind="record_arc_review",
            actor="engine",
            source_task_id=request.evaluator_task_id,
            created_at_ms=timestamp,
        )

        async def handler(session: StoreSession) -> CommandEffect[RecordArcReviewResult]:
            current_task = await session.execution.get_successful_task(
                project_id=request.project_id,
                task_id=request.evaluator_task_id,
                attempt_id=request.evaluator_attempt_id,
            )
            project = await session.projects.get(request.project_id)
            book = await session.books.get_for_project(request.project_id)
            submission = await session.arcs.get_submission(
                project_id=request.project_id,
                submission_id=request.submission_id,
            )
            workspace = await session.arcs.get_workspace(
                project_id=request.project_id,
                arc_id=request.arc_id,
            )
            book_baseline = (
                None
                if book is None or submission is None
                else await session.books.get_baseline(
                    project_id=request.project_id,
                    book_id=request.book_id,
                    baseline_id=submission.book_baseline_id,
                )
            )
            expected_task_kind = (
                "verify_repair.arc"
                if workspace is not None and workspace.semantic_repair_count > 0
                else "evaluate.arc"
            )
            strategy = DEFAULT_EVALUATION_STRATEGY_REGISTRY.for_task(
                expected_task_kind
            )
            if (
                current_task != task
                or project is None
                or book is None
                or submission is None
                or book.current_baseline_id != submission.book_baseline_id
                or book_baseline is None
                or submission.book_id != request.book_id
                or submission.arc_id != request.arc_id
                or submission.disposition != "pending"
                or workspace is None
                or workspace.id != submission.workspace_id
                or workspace.lock_version != submission.workspace_lock_version
                or task.delivery_state != "pending"
                or task.role != "evaluator"
                or task.task_kind != expected_task_kind
                or task.evaluation_strategy_id != strategy.strategy_id
                or task.evaluation_strategy_version
                != strategy.strategy_version
                or task.rubric_id != strategy.rubric_id
                or task.rubric_version != strategy.rubric_version
                or request.rubric_id != strategy.rubric_id
                or request.rubric_version != strategy.rubric_version
                or task.scope_layer != "arc"
                or task.book_id != request.book_id
                or task.arc_id != request.arc_id
                or task.workspace_lock_version != submission.workspace_lock_version
                or task.workspace_work_cycle_id != submission.work_cycle_id
                or workspace.work_cycle_id != submission.work_cycle_id
                or task.source_arc_candidate_review_id
                != workspace.active_repair_review_id
                or task.book_baseline_id != submission.book_baseline_id
                or task.arc_baseline_id != submission.base_arc_baseline_id
                or task.canon_baseline_id != submission.canon_baseline_id
            ):
                raise CommandPreconditionError("Arc evaluation facts are stale or mismatched.")
            if evaluation.decision == "pass":
                current_book_chapter_count = (
                    await session.chapters.count_committed_for_book(
                        book_id=request.book_id
                    )
                )
                current_arc_chapter_count = await session.chapters.count_committed(
                    arc_id=request.arc_id
                )
                if (
                    current_book_chapter_count
                    != submission.planned_after_cumulative_chapter_count
                    or current_arc_chapter_count
                    != submission.planned_after_arc_chapter_count
                ):
                    raise CommandPreconditionError(
                        "Arc outline effective point changed before review."
                    )
                proposal = ArcPlanProposal.model_validate_json(
                    (
                        await session.content.get_packed(
                            project_id=request.project_id,
                            ref_id=submission.plan_ref_id,
                        )
                    ).unpack_and_verify()
                )
                expected_closure_checkpoint = _derive_outline_closure_checkpoint(
                    proposal=proposal,
                    planned_after_cumulative_chapter_count=(
                        submission.planned_after_cumulative_chapter_count
                    ),
                    planned_after_arc_chapter_count=(
                        submission.planned_after_arc_chapter_count
                    ),
                    initial_plan=submission.base_arc_baseline_id is None,
                )
                if (
                    submission.closure_cumulative_chapter_count
                    != expected_closure_checkpoint
                ):
                    raise CommandPreconditionError(
                        "Arc submission checkpoint is not derived from its Chapter outline."
                    )
            precheck_ref = await session.content.put(
                project_id=request.project_id,
                prepared=prepared_precheck,
                semantic_kind="arc.deterministic_precheck",
                media_type="application/json",
                schema_id="arc-precheck",
                schema_version=1,
                ref_id=precheck_ref_id,
                created_at_ms=timestamp,
            )
            repair_reference = None
            if prepared_repair is not None:
                repair_reference = (
                    await session.content.put(
                        project_id=request.project_id,
                        prepared=prepared_repair,
                        semantic_kind="arc.repair_contract",
                        media_type="application/json",
                        schema_id="arc-repair-contract",
                        schema_version=2,
                        ref_id=repair_ref_id,
                        created_at_ms=timestamp,
                    )
                ).id
            await session.arcs.insert_review(
                ArcReviewRecord(
                    id=review_id,
                    project_id=request.project_id,
                    book_id=request.book_id,
                    arc_id=request.arc_id,
                    submission_id=submission.id,
                    evaluator_task_id=request.evaluator_task_id,
                    evaluator_attempt_id=request.evaluator_attempt_id,
                    decision=evaluation.decision,
                    rubric_id=request.rubric_id,
                    rubric_version=request.rubric_version,
                    precheck_ref_id=precheck_ref.id,
                    detail_ref_id=task.result_ref_id,
                    repair_contract_ref_id=repair_reference,
                    created_at_ms=timestamp,
                )
            )
            approval_gate_id: str | None = None
            events: list[EventDraft] = []
            next_action: Literal[
                "auto_commit",
                "await_approval",
                "repair",
                "await_user",
                "escalated_to_book",
                "failure_paused",
            ]
            if evaluation.decision == "pass":
                if project.operation_mode == "participatory":
                    await session.arcs.insert_approval_gate(
                        ArcApprovalGateRecord(
                            id=gate_id,
                            project_id=request.project_id,
                            book_id=request.book_id,
                            arc_id=request.arc_id,
                            submission_id=submission.id,
                            review_id=review_id,
                            reason="participatory_mode",
                            state="pending",
                            created_at_ms=timestamp,
                            closed_at_ms=None,
                        )
                    )
                    approval_gate_id = gate_id
                    next_action = "await_approval"
                    run = await session.runs.get(
                        project_id=request.project_id,
                        run_id=task.run_id,
                    )
                    if run is not None and run.status == "running":
                        if not await session.runs.wait_for_user(
                            run_id=task.run_id,
                            reason_code="arc_approval_required",
                            now_ms=timestamp,
                        ):
                            raise CommandPreconditionError("Run could not enter Arc approval wait.")
                    events.append(
                        EventDraft(
                            event_type="arc.approval_required",
                            aggregate_type="arc",
                            aggregate_id=request.arc_id,
                            payload={"approval_gate_id": gate_id},
                        )
                    )
                else:
                    next_action = "auto_commit"
            else:
                if not await session.arcs.close_submission(
                    project_id=request.project_id,
                    submission_id=submission.id,
                    disposition="rejected",
                    reason_code=evaluation.decision,
                    closed_at_ms=timestamp,
                ):
                    raise CommandPreconditionError("Arc submission changed before rejection.")
                state = "active" if evaluation.decision == "local_repair" else "blocked_by_user"
                next_action = "repair" if evaluation.decision == "local_repair" else "await_user"
                if evaluation.decision == "escalate_to_book":
                    state = "blocked_by_upstream"
                    next_action = "escalated_to_book"
                    await session.arcs.insert_book_change_request(
                        ArcBookChangeRequestRecord(
                            id=change_request_id,
                            project_id=request.project_id,
                            book_id=request.book_id,
                            arc_id=request.arc_id,
                            source_candidate_submission_id=submission.id,
                            source_candidate_review_id=review_id,
                            source_arc_parent_review_id=None,
                            source_arc_closure_review_id=None,
                            target_book_baseline_id=submission.book_baseline_id,
                            evidence_ref_id=task.result_ref_id,
                            status="open",
                            created_at_ms=timestamp,
                        )
                    )
                    events.append(
                        EventDraft(
                            event_type="change_request.opened",
                            aggregate_type="arc",
                            aggregate_id=request.arc_id,
                            payload={
                                "change_request_id": change_request_id,
                                "target_layer": "book",
                            },
                        )
                    )
                updated_workspace = replace(
                    workspace,
                    state=state,
                    lock_version=workspace.lock_version + 1,
                    active_repair_review_id=(
                        review_id if evaluation.decision == "local_repair" else None
                    ),
                    updated_at_ms=timestamp,
                )
                if not await session.arcs.compare_and_set_workspace(
                    record=updated_workspace,
                    expected_lock_version=workspace.lock_version,
                ):
                    raise CommandPreconditionError("Arc review workspace CAS failed.")
                if (
                    evaluation.decision == "local_repair"
                    and workspace.semantic_repair_count >= workspace.semantic_repair_limit
                ):
                    failure_ref = await session.content.put(
                        project_id=request.project_id,
                        prepared=prepared_failure,
                        semantic_kind="agent_error_summary",
                        media_type="application/json",
                        schema_id="semantic-repair-exhausted",
                        schema_version=1,
                        ref_id=failure_ref_id,
                        created_at_ms=timestamp,
                    )
                    if not await session.runs.failure_pause_for_task(
                        run_id=task.run_id,
                        task_id=task.task_id,
                        failure_code="semantic_repair_exhausted",
                        failure_ref_id=failure_ref.id,
                        now_ms=timestamp,
                    ):
                        raise CommandPreconditionError("Run cannot pause at Arc repair exhaustion.")
                    next_action = "failure_paused"
                elif evaluation.decision == "needs_user":
                    run = await session.runs.get(
                        project_id=request.project_id,
                        run_id=task.run_id,
                    )
                    if run is not None and run.status == "running":
                        await session.runs.wait_for_user(
                            run_id=task.run_id,
                            reason_code="arc_review_needs_user",
                            now_ms=timestamp,
                        )
            if not await session.execution.mark_delivery_applied(
                project_id=request.project_id,
                task_id=request.evaluator_task_id,
                attempt_id=request.evaluator_attempt_id,
                command_id=envelope.command_id,
                updated_at_ms=timestamp,
            ):
                raise CommandPreconditionError("Arc evaluator delivery changed concurrently.")
            result = RecordArcReviewResult(
                project_id=request.project_id,
                arc_id=request.arc_id,
                submission_id=submission.id,
                review_id=review_id,
                decision=evaluation.decision,
                approval_gate_id=approval_gate_id,
                next_action=next_action,
            )
            events.insert(
                0,
                EventDraft(
                    event_type="arc.reviewed",
                    aggregate_type="arc",
                    aggregate_id=request.arc_id,
                    payload={
                        "submission_id": submission.id,
                        "review_id": review_id,
                        "decision": evaluation.decision,
                    },
                ),
            )
            return CommandEffect(result=result, events=tuple(events))

        return await self._command_bus.execute(
            envelope=envelope,
            result_type=RecordArcReviewResult,
            handler=handler,
        )

    async def commit_baseline_auto(
        self,
        request: CommitArcAutoRequest,
        *,
        idempotency_key: str,
    ) -> CommandExecution[CommitArcResult]:
        return await self._commit_baseline(
            request=request,
            approval_gate_id=None,
            authorization_kind="policy_auto",
            idempotency_key=idempotency_key,
        )

    async def approve_and_commit(
        self,
        request: ApproveArcRequest,
        *,
        idempotency_key: str,
    ) -> CommandExecution[CommitArcResult]:
        return await self._commit_baseline(
            request=request,
            approval_gate_id=request.approval_gate_id,
            authorization_kind="human_approval",
            idempotency_key=idempotency_key,
        )

    async def _commit_baseline(
        self,
        *,
        request: CommitArcAutoRequest,
        approval_gate_id: str | None,
        authorization_kind: Literal["policy_auto", "human_approval"],
        idempotency_key: str,
    ) -> CommandExecution[CommitArcResult]:
        timestamp = self._now_ms()
        baseline_id = self._id_factory()
        approval_id = self._id_factory() if authorization_kind == "human_approval" else None
        actor: Actor = "user" if authorization_kind == "human_approval" else "engine"
        envelope = self._envelope(
            request=request,
            project_id=request.project_id,
            idempotency_key=idempotency_key,
            command_kind=(
                "approve_and_commit_arc_baseline"
                if authorization_kind == "human_approval"
                else "commit_arc_baseline_auto"
            ),
            actor=actor,
            created_at_ms=timestamp,
        )

        async def handler(session: StoreSession) -> CommandEffect[CommitArcResult]:
            project = await session.projects.get(request.project_id)
            book = await session.books.get_for_project(request.project_id)
            book_baseline = (
                None
                if book is None or book.current_baseline_id is None
                else await session.books.get_baseline(
                    project_id=request.project_id,
                    book_id=request.book_id,
                    baseline_id=book.current_baseline_id,
                )
            )
            arc = await session.arcs.get(project_id=request.project_id, arc_id=request.arc_id)
            submission = await session.arcs.get_submission(
                project_id=request.project_id,
                submission_id=request.submission_id,
            )
            review = await session.arcs.get_review(
                project_id=request.project_id,
                review_id=request.review_id,
            )
            workspace = await session.arcs.get_workspace(
                project_id=request.project_id,
                arc_id=request.arc_id,
            )
            parent_arc_baseline_id = (
                None if workspace is None else workspace.base_arc_baseline_id
            )
            parent_book_rebase = (
                workspace is not None
                and workspace.revision_origin
                == "parent_book_baseline_rebase"
            )
            if (
                project is None
                or book is None
                or book_baseline is None
                or arc is None
                or arc.book_id != request.book_id
                or arc.current_baseline_id != request.expected_current_baseline_id
                or submission is None
                or submission.book_id != request.book_id
                or submission.arc_id != request.arc_id
                or submission.disposition != "pending"
                or review is None
                or review.submission_id != submission.id
                or review.decision != "pass"
                or workspace is None
                or workspace.id != submission.workspace_id
                or workspace.lock_version != submission.workspace_lock_version
                or submission.base_arc_baseline_id != parent_arc_baseline_id
                or (
                    not parent_book_rebase
                    and parent_arc_baseline_id
                    != request.expected_current_baseline_id
                )
                or (
                    parent_book_rebase
                    and request.expected_current_baseline_id is not None
                )
                or book.current_baseline_id != submission.book_baseline_id
                or project.current_canon_baseline_id != submission.canon_baseline_id
            ):
                raise CommandPreconditionError("Arc commit facts are stale or incomplete.")
            gate = await session.arcs.find_pending_gate(
                project_id=request.project_id,
                arc_id=request.arc_id,
            )
            if authorization_kind == "policy_auto":
                if project.operation_mode != "full_auto" or gate is not None:
                    raise CommandPreconditionError(
                        "Auto Arc commit cannot bypass current mode or a persistent gate."
                    )
                final_checkpoint = submission.closure_cumulative_chapter_count
            else:
                if (
                    approval_gate_id is None
                    or gate is None
                    or gate.id != approval_gate_id
                    or gate.submission_id != submission.id
                    or gate.review_id != review.id
                ):
                    raise CommandPreconditionError("Arc approval gate is stale or incomplete.")
                final_checkpoint = submission.closure_cumulative_chapter_count
            cumulative_committed_count = (
                await session.chapters.count_committed_for_book(
                    book_id=request.book_id
                )
            )
            arc_committed_count = await session.chapters.count_committed(
                arc_id=request.arc_id
            )
            if (
                cumulative_committed_count
                != submission.planned_after_cumulative_chapter_count
                or arc_committed_count
                != submission.planned_after_arc_chapter_count
            ):
                raise CommandPreconditionError(
                    "Arc outline effective point changed before baseline commit."
                )
            proposal = ArcPlanProposal.model_validate_json(
                (
                    await session.content.get_packed(
                        project_id=request.project_id,
                        ref_id=submission.plan_ref_id,
                    )
                ).unpack_and_verify()
            )
            expected_closure_checkpoint = _derive_outline_closure_checkpoint(
                proposal=proposal,
                planned_after_cumulative_chapter_count=(
                    submission.planned_after_cumulative_chapter_count
                ),
                planned_after_arc_chapter_count=(
                    submission.planned_after_arc_chapter_count
                ),
                initial_plan=parent_arc_baseline_id is None,
            )
            if final_checkpoint != expected_closure_checkpoint:
                raise CommandPreconditionError(
                    "Arc baseline checkpoint is not derived from its reviewed Chapter outline."
                )
            baseline_version = await session.arcs.next_baseline_version(
                arc_id=request.arc_id
            )
            if parent_arc_baseline_id is None:
                expected_version = 1
            else:
                current_version = await session.arcs.get_baseline_version(
                    project_id=request.project_id,
                    arc_id=request.arc_id,
                    baseline_id=parent_arc_baseline_id,
                )
                if current_version is None:
                    raise CommandPreconditionError("Arc current baseline identity is invalid.")
                expected_version = current_version + 1
            if baseline_version != expected_version:
                raise CommandPreconditionError("Arc baseline version does not follow current head.")
            lifecycle_status: Literal["active", "closing"] = (
                "closing"
                if cumulative_committed_count == final_checkpoint
                else "active"
            )
            if authorization_kind == "human_approval":
                assert gate is not None and approval_id is not None
                await session.arcs.insert_approval(
                    ArcApprovalRecord(
                        id=approval_id,
                        project_id=request.project_id,
                        book_id=request.book_id,
                        arc_id=request.arc_id,
                        gate_id=gate.id,
                        submission_id=submission.id,
                        review_id=review.id,
                        decision="approved",
                        created_at_ms=timestamp,
                    )
                )
            new_baseline = ArcBaselineRecord(
                id=baseline_id,
                project_id=request.project_id,
                book_id=request.book_id,
                arc_id=request.arc_id,
                baseline_version=baseline_version,
                parent_baseline_id=parent_arc_baseline_id,
                submission_id=submission.id,
                review_id=review.id,
                book_baseline_id=submission.book_baseline_id,
                canon_baseline_id=submission.canon_baseline_id,
                book_progress_handoff_id=workspace.book_progress_handoff_id,
                prior_arc_id=submission.prior_arc_id,
                prior_arc_baseline_id=submission.prior_arc_baseline_id,
                plan_ref_id=submission.plan_ref_id,
                planned_after_cumulative_chapter_count=(
                    submission.planned_after_cumulative_chapter_count
                ),
                planned_after_arc_chapter_count=(
                    submission.planned_after_arc_chapter_count
                ),
                closure_cumulative_chapter_count=final_checkpoint,
                revision_origin=workspace.revision_origin,
                authorization_kind=authorization_kind,
                approval_gate_id=approval_gate_id,
                approval_id=approval_id,
                created_at_ms=timestamp,
            )
            await session.arcs.insert_baseline(new_baseline)
            drafting_chapter = await session.chapters.get_unfinished_for_arc(
                project_id=request.project_id,
                arc_id=request.arc_id,
            )
            if drafting_chapter is not None:
                if parent_arc_baseline_id is None:
                    raise CommandPreconditionError(
                        "An initial Arc baseline cannot inherit a drafting Chapter."
                    )
                drafting_workspace = await session.chapters.get_workspace(
                    project_id=request.project_id,
                    chapter_id=drafting_chapter.id,
                )
                if (
                    drafting_workspace is None
                    or drafting_workspace.state
                    not in {"active", "blocked_by_upstream", "stale"}
                    or drafting_workspace.arc_baseline_id
                    != parent_arc_baseline_id
                ):
                    raise CommandPreconditionError(
                        "The drafting Chapter cannot be rebound from its old Arc baseline."
                    )
                try:
                    resolve_outline_entry(
                        baseline=new_baseline,
                        plan=proposal,
                        arc_ordinal=drafting_chapter.arc_ordinal,
                        book_ordinal=drafting_chapter.book_ordinal,
                    )
                except ArcOutlineProjectionError as exc:
                    raise CommandPreconditionError(
                        "The Arc successor does not cover its current drafting Chapter."
                    ) from exc
                pending_chapter_submission = (
                    await session.chapters.find_pending_submission(
                        project_id=request.project_id,
                        chapter_id=drafting_chapter.id,
                    )
                )
                if (
                    pending_chapter_submission is not None
                    and not await session.chapters.close_submission(
                        project_id=request.project_id,
                        submission_id=pending_chapter_submission.id,
                        disposition="superseded",
                        reason_code="arc_baseline_replaced",
                        closed_at_ms=timestamp,
                    )
                ):
                    raise CommandPreconditionError(
                        "Drafting Chapter submission changed during Arc replacement."
                    )
                if not await session.chapters.compare_and_set_outline_source(
                    project_id=request.project_id,
                    chapter_id=drafting_chapter.id,
                    expected_outline_arc_baseline_id=(
                        drafting_chapter.outline_arc_baseline_id
                    ),
                    new_outline_arc_baseline_id=baseline_id,
                    updated_at_ms=timestamp,
                ):
                    raise CommandPreconditionError(
                        "Drafting Chapter outline provenance changed concurrently."
                    )
                reset_chapter_workspace = replace(
                    drafting_workspace,
                    state="active",
                    lock_version=drafting_workspace.lock_version + 1,
                    work_cycle_id=uuid.uuid4().hex,
                    active_repair_review_id=None,
                    base_chapter_baseline_id=None,
                    book_baseline_id=submission.book_baseline_id,
                    arc_baseline_id=baseline_id,
                    canon_baseline_id=submission.canon_baseline_id,
                    revision_origin="initial",
                    source_arc_parent_review_id=None,
                    source_arc_closure_review_id=None,
                    source_feedback_id=None,
                    correction_lineage_id=workspace.correction_lineage_id,
                    correction_lineage_origin=(
                        workspace.correction_lineage_origin
                    ),
                    automatic_correction_round=(
                        workspace.automatic_correction_round
                    ),
                    plan_ref_id=None,
                    draft_ref_id=None,
                    observations_ref_id=None,
                    candidate_canon_patch_ref_id=None,
                    guidance_ref_id=None,
                    semantic_repair_count=0,
                    stale_reason_code=None,
                    stale_at_ms=None,
                    updated_at_ms=timestamp,
                )
                if not await session.chapters.compare_and_set_workspace(
                    record=reset_chapter_workspace,
                    expected_lock_version=drafting_workspace.lock_version,
                ):
                    raise CommandPreconditionError(
                        "Drafting Chapter workspace reset changed concurrently."
                    )
            if not await session.arcs.compare_and_set_current_baseline(
                project_id=request.project_id,
                arc_id=request.arc_id,
                expected_baseline_id=request.expected_current_baseline_id,
                new_baseline_id=baseline_id,
                updated_at_ms=timestamp,
                lifecycle_status=lifecycle_status,
                completed_at_ms=None,
            ):
                raise CommandPreconditionError("Arc current baseline CAS failed.")
            if not await session.arcs.close_submission(
                project_id=request.project_id,
                submission_id=submission.id,
                disposition="promoted",
                reason_code="baseline_committed",
                closed_at_ms=timestamp,
            ):
                raise CommandPreconditionError("Arc submission promotion failed.")
            if gate is not None and not await session.arcs.close_approval_gate(
                project_id=request.project_id,
                gate_id=gate.id,
                state="decided",
                closed_at_ms=timestamp,
            ):
                raise CommandPreconditionError("Arc approval gate could not be closed.")
            updated_workspace = replace(
                workspace,
                state="idle",
                lock_version=workspace.lock_version + 1,
                active_repair_review_id=None,
                base_arc_baseline_id=baseline_id,
                book_baseline_id=submission.book_baseline_id,
                canon_baseline_id=submission.canon_baseline_id,
                plan_ref_id=submission.plan_ref_id,
                planned_after_cumulative_chapter_count=(
                    submission.planned_after_cumulative_chapter_count
                ),
                planned_after_arc_chapter_count=(
                    submission.planned_after_arc_chapter_count
                ),
                closure_cumulative_chapter_count=final_checkpoint,
                guidance_ref_id=None,
                source_feedback_id=None,
                semantic_repair_count=0,
                stale_reason_code=None,
                stale_at_ms=None,
                updated_at_ms=timestamp,
            )
            if not await session.arcs.compare_and_set_workspace(
                record=updated_workspace,
                expected_lock_version=workspace.lock_version,
            ):
                raise CommandPreconditionError("Arc workspace reset CAS failed.")
            resolved_requests = await session.changes.resolve_for_arc_baseline(
                project_id=request.project_id,
                book_id=request.book_id,
                arc_id=request.arc_id,
                previous_baseline_id=parent_arc_baseline_id,
                new_baseline_id=baseline_id,
                now_ms=timestamp,
            )
            if authorization_kind == "human_approval":
                run = await session.runs.get_open_for_project(request.project_id)
                if run is not None and run.status == "waiting_for_user":
                    if not await session.runs.start_waiting_run(
                        project_id=request.project_id,
                        run_id=run.id,
                        expected_lock_version=run.lock_version,
                        now_ms=timestamp,
                    ):
                        raise CommandPreconditionError("Run could not leave Arc approval wait.")
            result = CommitArcResult(
                project_id=request.project_id,
                arc_id=request.arc_id,
                baseline_id=baseline_id,
                baseline_version=baseline_version,
                closure_cumulative_chapter_count=final_checkpoint,
                authorization_kind=authorization_kind,
                lifecycle_status=lifecycle_status,
            )
            events = [
                EventDraft(
                        event_type="arc.baseline_committed",
                        aggregate_type="arc",
                        aggregate_id=request.arc_id,
                        payload={
                            "baseline_id": baseline_id,
                            "baseline_version": baseline_version,
                            "authorization_kind": authorization_kind,
                            "closure_cumulative_chapter_count": final_checkpoint,
                            "lifecycle_status": lifecycle_status,
                        },
                    )
            ]
            if resolved_requests:
                events.append(
                    EventDraft(
                        event_type="change_request.resolved",
                        aggregate_type="arc",
                        aggregate_id=request.arc_id,
                        payload={
                            "arc_baseline_id": baseline_id,
                            "chapter_request_count": resolved_requests,
                        },
                    )
                )
            return CommandEffect(result=result, events=tuple(events))

        return await self._command_bus.execute(
            envelope=envelope,
            result_type=CommitArcResult,
            handler=handler,
        )

    async def reject_gate(
        self,
        request: RejectArcGateRequest,
        *,
        idempotency_key: str,
    ) -> CommandExecution[RejectArcGateResult]:
        timestamp = self._now_ms()
        approval_id = self._id_factory()
        envelope = self._envelope(
            request=request,
            project_id=request.project_id,
            idempotency_key=idempotency_key,
            command_kind="reject_arc_gate",
            actor="user",
            created_at_ms=timestamp,
        )

        async def handler(session: StoreSession) -> CommandEffect[RejectArcGateResult]:
            gate = await session.arcs.get_approval_gate(
                project_id=request.project_id,
                gate_id=request.approval_gate_id,
            )
            submission = await session.arcs.get_submission(
                project_id=request.project_id,
                submission_id=request.submission_id,
            )
            review = await session.arcs.get_review(
                project_id=request.project_id,
                review_id=request.review_id,
            )
            workspace = await session.arcs.get_workspace(
                project_id=request.project_id,
                arc_id=request.arc_id,
            )
            if (
                gate is None
                or gate.state != "pending"
                or gate.book_id != request.book_id
                or gate.arc_id != request.arc_id
                or gate.submission_id != request.submission_id
                or gate.review_id != request.review_id
                or submission is None
                or submission.disposition != "pending"
                or review is None
                or review.decision != "pass"
                or workspace is None
                or workspace.id != submission.workspace_id
                or workspace.lock_version != submission.workspace_lock_version
            ):
                raise CommandPreconditionError("Arc rejection facts are stale or incomplete.")
            await session.arcs.insert_approval(
                ArcApprovalRecord(
                    id=approval_id,
                    project_id=request.project_id,
                    book_id=request.book_id,
                    arc_id=request.arc_id,
                    gate_id=gate.id,
                    submission_id=submission.id,
                    review_id=review.id,
                    decision="rejected",
                    created_at_ms=timestamp,
                )
            )
            if not await session.arcs.close_approval_gate(
                project_id=request.project_id,
                gate_id=gate.id,
                state="decided",
                closed_at_ms=timestamp,
            ) or not await session.arcs.close_submission(
                project_id=request.project_id,
                submission_id=submission.id,
                disposition="rejected",
                reason_code="user_rejected",
                closed_at_ms=timestamp,
            ):
                raise CommandPreconditionError("Arc gate or submission changed concurrently.")
            updated_workspace = replace(
                workspace,
                state="active",
                lock_version=workspace.lock_version + 1,
                updated_at_ms=timestamp,
            )
            if not await session.arcs.compare_and_set_workspace(
                record=updated_workspace,
                expected_lock_version=workspace.lock_version,
            ):
                raise CommandPreconditionError("Arc rejection workspace CAS failed.")
            run = await session.runs.get_open_for_project(request.project_id)
            if run is not None and run.status == "waiting_for_user":
                if not await session.runs.start_waiting_run(
                    project_id=request.project_id,
                    run_id=run.id,
                    expected_lock_version=run.lock_version,
                    now_ms=timestamp,
                ):
                    raise CommandPreconditionError("Run could not leave Arc approval wait.")
            result = RejectArcGateResult(
                project_id=request.project_id,
                arc_id=request.arc_id,
                approval_gate_id=gate.id,
                workspace_lock_version=updated_workspace.lock_version,
            )
            return CommandEffect(
                result=result,
                events=(
                    EventDraft(
                        event_type="arc.approval_rejected",
                        aggregate_type="arc",
                        aggregate_id=request.arc_id,
                        payload={"approval_gate_id": gate.id},
                    ),
                ),
            )

        return await self._command_bus.execute(
            envelope=envelope,
            result_type=RejectArcGateResult,
            handler=handler,
        )
