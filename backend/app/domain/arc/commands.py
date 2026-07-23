from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import replace
from typing import Literal

from pydantic import BaseModel

from app.agents.contracts import ArcPlanProposal
from app.db.uow import StoreSession
from app.domain.arc.contracts import (
    ApplyArcTaskRequest,
    ApplyArcTaskResult,
    ApproveArcRequest,
    ArcEvaluation,
    ArcRepairContract,
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
        and task.book_baseline_id == workspace.book_baseline_id
        and task.arc_baseline_id == workspace.base_arc_baseline_id
        and task.canon_baseline_id == workspace.canon_baseline_id
        and workspace.state == "active"
    )


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
            if project is None:
                raise ProjectNotFoundError(request.project_id)
            if (
                book is None
                or book.id != request.book_id
                or book.lifecycle_status != "active"
                or book.current_completion_id is not None
                or book.current_baseline_id != request.expected_book_baseline_id
                or project.current_canon_baseline_id != request.expected_canon_baseline_id
            ):
                raise CommandPreconditionError("Book or Canon dependencies are not current.")
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
            ):
                raise CommandPreconditionError("The prior Arc is not at a safe completion boundary.")
            ordinal = await session.arcs.next_ordinal(book_id=request.book_id)
            await session.arcs.insert(
                ArcRecord(
                    id=arc_id,
                    project_id=request.project_id,
                    book_id=request.book_id,
                    ordinal=ordinal,
                    purpose=request.purpose,
                    lifecycle_status="planning",
                    current_baseline_id=None,
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
                    base_arc_baseline_id=None,
                    book_baseline_id=request.expected_book_baseline_id,
                    canon_baseline_id=request.expected_canon_baseline_id,
                    prior_arc_id=None if prior_arc is None else prior_arc.id,
                    prior_arc_baseline_id=(
                        None if prior_arc is None else prior_arc.current_baseline_id
                    ),
                    plan_ref_id=None,
                    recommended_target_chapter_count=None,
                    repair_policy_id="semantic-repair-v1",
                    semantic_repair_count=0,
                    semantic_repair_limit=5,
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
                purpose=request.purpose,
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
                            "purpose": request.purpose,
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
                base_arc_baseline_id=arc.current_baseline_id,
                book_baseline_id=request.expected_book_baseline_id,
                canon_baseline_id=request.expected_canon_baseline_id,
                plan_ref_id=None,
                recommended_target_chapter_count=None,
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
        proposal = ArcPlanProposal.model_validate_json(raw)
        prepared_plan = prepare_canonical_json(proposal)
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
            if current_task != task or workspace is None:
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

            repair_increment = 0
            if task.task_kind == "arc.repair":
                review = await session.arcs.get_latest_review(
                    project_id=request.project_id,
                    arc_id=request.arc_id,
                )
                if (
                    review is None
                    or review.decision != "local_repair"
                    or review.repair_contract_ref_id is None
                    or workspace.plan_ref_id is None
                    or workspace.semantic_repair_count >= workspace.semantic_repair_limit
                ):
                    raise CommandPreconditionError("Arc has no active local repair budget.")
                repair_contract = ArcRepairContract.model_validate_json(
                    (
                        await session.content.get_packed(
                            project_id=request.project_id,
                            ref_id=review.repair_contract_ref_id,
                        )
                    ).unpack_and_verify()
                )
                current_plan = ArcPlanProposal.model_validate_json(
                    (
                        await session.content.get_packed(
                            project_id=request.project_id,
                            ref_id=workspace.plan_ref_id,
                        )
                    ).unpack_and_verify()
                )
                before = current_plan.model_dump(mode="json")
                after = proposal.model_dump(mode="json")
                changed = {key for key in before if before[key] != after[key]}
                if not changed:
                    raise CommandPreconditionError("Arc repair result made no authorized change.")
                unauthorized = changed.difference(repair_contract.authorized_components)
                if unauthorized:
                    raise CommandPreconditionError(
                        "Arc repair changed unauthorized components: "
                        + ", ".join(sorted(unauthorized))
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
                schema_version=1,
                ref_id=plan_ref_id,
                created_at_ms=timestamp,
            )
            updated_workspace = replace(
                workspace,
                state="active",
                lock_version=workspace.lock_version + 1,
                plan_ref_id=plan_ref.id,
                recommended_target_chapter_count=proposal.target_chapter_count,
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
                            "recommended_target_chapter_count": (
                                proposal.target_chapter_count
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
                or arc.lifecycle_status not in {"planning", "active", "completed"}
                or workspace is None
                or workspace.lock_version != request.expected_workspace_lock_version
                or workspace.state != "active"
                or workspace.plan_ref_id is None
                or workspace.recommended_target_chapter_count is None
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
            manifest = {
                "schema": "arc-review-manifest-v1",
                "workspace_id": workspace.id,
                "workspace_lock_version": workspace.lock_version,
                "base_arc_baseline_id": workspace.base_arc_baseline_id,
                "book_baseline_id": workspace.book_baseline_id,
                "canon_baseline_id": workspace.canon_baseline_id,
                "prior_arc_id": workspace.prior_arc_id,
                "prior_arc_baseline_id": workspace.prior_arc_baseline_id,
                "purpose": arc.purpose,
                "plan_ref_id": workspace.plan_ref_id,
                "recommended_target_chapter_count": (
                    workspace.recommended_target_chapter_count
                ),
            }
            prepared_manifest = prepare_canonical_json(manifest)
            manifest_ref = await session.content.put(
                project_id=request.project_id,
                prepared=prepared_manifest,
                semantic_kind="arc.review_manifest",
                media_type="application/json",
                schema_id="arc-review-manifest",
                schema_version=1,
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
                    base_arc_baseline_id=workspace.base_arc_baseline_id,
                    book_baseline_id=workspace.book_baseline_id,
                    canon_baseline_id=workspace.canon_baseline_id,
                    prior_arc_id=workspace.prior_arc_id,
                    prior_arc_baseline_id=workspace.prior_arc_baseline_id,
                    purpose=arc.purpose,
                    plan_ref_id=workspace.plan_ref_id,
                    recommended_target_chapter_count=(
                        workspace.recommended_target_chapter_count
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
                "message": "Arc semantic repair limit of five has been exhausted.",
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
            submission = await session.arcs.get_submission(
                project_id=request.project_id,
                submission_id=request.submission_id,
            )
            workspace = await session.arcs.get_workspace(
                project_id=request.project_id,
                arc_id=request.arc_id,
            )
            expected_task_kind = (
                "verify_repair.arc"
                if workspace is not None and workspace.semantic_repair_count > 0
                else "evaluate.arc"
            )
            if (
                current_task != task
                or project is None
                or submission is None
                or submission.book_id != request.book_id
                or submission.arc_id != request.arc_id
                or submission.disposition != "pending"
                or workspace is None
                or workspace.id != submission.workspace_id
                or workspace.lock_version != submission.workspace_lock_version
                or task.delivery_state != "pending"
                or task.role != "evaluator"
                or task.task_kind != expected_task_kind
                or task.scope_layer != "arc"
                or task.book_id != request.book_id
                or task.arc_id != request.arc_id
                or task.workspace_lock_version != submission.workspace_lock_version
                or task.book_baseline_id != submission.book_baseline_id
                or task.arc_baseline_id != submission.base_arc_baseline_id
                or task.canon_baseline_id != submission.canon_baseline_id
            ):
                raise CommandPreconditionError("Arc evaluation facts are stale or mismatched.")
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
                        schema_version=1,
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
                            source_submission_id=submission.id,
                            source_review_id=review_id,
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
                    if not await session.runs.failure_pause(
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
            target_chapter_count=None,
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
            target_chapter_count=request.target_chapter_count,
            approval_gate_id=request.approval_gate_id,
            authorization_kind="human_approval",
            idempotency_key=idempotency_key,
        )

    async def _commit_baseline(
        self,
        *,
        request: CommitArcAutoRequest,
        target_chapter_count: int | None,
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
            if (
                project is None
                or book is None
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
                or workspace.base_arc_baseline_id != request.expected_current_baseline_id
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
                final_target = submission.recommended_target_chapter_count
            else:
                if (
                    approval_gate_id is None
                    or gate is None
                    or gate.id != approval_gate_id
                    or gate.submission_id != submission.id
                    or gate.review_id != review.id
                    or target_chapter_count is None
                ):
                    raise CommandPreconditionError("Arc approval gate is stale or incomplete.")
                final_target = target_chapter_count
            committed_count = await session.arcs.count_committed_chapters(
                arc_id=request.arc_id
            )
            if final_target < committed_count:
                raise CommandPreconditionError(
                    "Arc target cannot be smaller than its committed Chapter count."
                )
            baseline_version = await session.arcs.next_baseline_version(
                arc_id=request.arc_id
            )
            if request.expected_current_baseline_id is None:
                expected_version = 1
            else:
                current_version = await session.arcs.get_baseline_version(
                    project_id=request.project_id,
                    arc_id=request.arc_id,
                    baseline_id=request.expected_current_baseline_id,
                )
                if current_version is None:
                    raise CommandPreconditionError("Arc current baseline identity is invalid.")
                expected_version = current_version + 1
            if baseline_version != expected_version:
                raise CommandPreconditionError("Arc baseline version does not follow current head.")
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
                        target_chapter_count=final_target,
                        created_at_ms=timestamp,
                    )
                )
            lifecycle_status: Literal["active", "completed"] = (
                "completed" if committed_count == final_target else "active"
            )
            await session.arcs.insert_baseline(
                ArcBaselineRecord(
                    id=baseline_id,
                    project_id=request.project_id,
                    book_id=request.book_id,
                    arc_id=request.arc_id,
                    baseline_version=baseline_version,
                    parent_baseline_id=request.expected_current_baseline_id,
                    submission_id=submission.id,
                    review_id=review.id,
                    book_baseline_id=submission.book_baseline_id,
                    canon_baseline_id=submission.canon_baseline_id,
                    prior_arc_id=submission.prior_arc_id,
                    prior_arc_baseline_id=submission.prior_arc_baseline_id,
                    purpose=submission.purpose,
                    plan_ref_id=submission.plan_ref_id,
                    recommended_target_chapter_count=(
                        submission.recommended_target_chapter_count
                    ),
                    target_chapter_count=final_target,
                    authorization_kind=authorization_kind,
                    approval_gate_id=approval_gate_id,
                    approval_id=approval_id,
                    created_at_ms=timestamp,
                )
            )
            if not await session.arcs.compare_and_set_current_baseline(
                project_id=request.project_id,
                arc_id=request.arc_id,
                expected_baseline_id=request.expected_current_baseline_id,
                new_baseline_id=baseline_id,
                updated_at_ms=timestamp,
                lifecycle_status=lifecycle_status,
                completed_at_ms=timestamp if lifecycle_status == "completed" else None,
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
                base_arc_baseline_id=baseline_id,
                book_baseline_id=submission.book_baseline_id,
                canon_baseline_id=submission.canon_baseline_id,
                plan_ref_id=submission.plan_ref_id,
                recommended_target_chapter_count=(
                    submission.recommended_target_chapter_count
                ),
                guidance_ref_id=None,
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
                previous_baseline_id=request.expected_current_baseline_id,
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
                target_chapter_count=final_target,
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
                            "target_chapter_count": final_target,
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
                    target_chapter_count=None,
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
