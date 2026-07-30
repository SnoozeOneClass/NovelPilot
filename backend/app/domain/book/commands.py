from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable
from dataclasses import replace
from typing import cast

from pydantic import BaseModel, ValidationError

from app.agents.contracts import BookDiscussionResult
from app.agents.registry import DEFAULT_EVALUATION_STRATEGY_REGISTRY
from app.db.uow import StoreSession
from app.domain.book.contracts import (
    ApplyBookCandidateRequest,
    ApplyBookCandidateResult,
    ApplyBookCandidateTaskRequest,
    ApplyBookCandidateTaskResult,
    ApplyBookDiscussionTaskRequest,
    ApplyBookDiscussionTaskResult,
    ApproveBookRequest,
    ApproveBookResult,
    BookCandidatePack,
    BookArcContract,
    BookArcTopology,
    BookArcTopologySuffix,
    BookDiscussionState,
    BookEvaluation,
    BookRepairContract,
    BookRepairPatch,
    BookSuccessorCandidateProposal,
    BookTranscript,
    CompletionContract,
    RecordBookUserInputRequest,
    RecordBookUserInputResult,
    RecordBookReviewRequest,
    RecordBookReviewResult,
    SubmitBookRequest,
    SubmitBookResult,
)
from app.domain.book.discussion import bind_agent_result, bind_user_input
from app.domain.commands import (
    Actor,
    CommandEffect,
    CommandEnvelope,
    CommandExecution,
    CommandPreconditionError,
    EventDraft,
)
from app.domain.projects import ProjectNotFoundError
from app.store.books import (
    BookApprovalRecord,
    BookBaselineRecord,
    BookReviewRecord,
    BookSubmissionRecord,
    BookWorkspaceRecord,
)
from app.store.command_bus import CommandBus
from app.store.content import prepare_canonical_json, prepare_exact_text
from app.store.execution import SuccessfulTaskRecord


class BookNotFoundError(LookupError):
    pass


def _task_matches_workspace(
    task: SuccessfulTaskRecord,
    workspace: BookWorkspaceRecord,
    *,
    expected_lock_version: int,
) -> bool:
    return (
        task.delivery_state == "pending"
        and task.role == "book_strategist"
        and task.scope_layer == "book"
        and task.book_id == workspace.book_id
        and task.arc_id is None
        and task.chapter_id is None
        and task.workspace_lock_version == expected_lock_version == workspace.lock_version
        and task.workspace_work_cycle_id == workspace.work_cycle_id
        and task.book_baseline_id == workspace.base_book_baseline_id
        and task.canon_baseline_id == workspace.base_canon_baseline_id
        and workspace.state == "active"
    )


def _merge_book_repair(
    *,
    current: BookCandidatePack,
    patch: BookRepairPatch,
    contract: BookRepairContract,
    topology_effective_after_arc_ordinal: int,
) -> BookCandidatePack:
    authorized = set(contract.authorized_components)
    requested = {change.component for change in patch.changes}
    unauthorized = requested.difference(authorized)
    if unauthorized:
        raise CommandPreconditionError(
            "Book repair changed unauthorized components: "
            + ", ".join(sorted(unauthorized))
        )
    merged = current.model_dump(mode="python")
    for change in patch.changes:
        if change.component == "arc_topology":
            merged[change.component] = _compose_book_arc_topology(
                current=current.arc_topology,
                effective_after_arc_ordinal=topology_effective_after_arc_ordinal,
                suffix=change.value,
            )
        else:
            merged[change.component] = change.value
    candidate = BookCandidatePack.model_validate(merged)
    if candidate == current:
        raise CommandPreconditionError("Book repair result made no authorized change.")
    return candidate


def _compose_book_arc_topology(
    *,
    current: BookArcTopology | None,
    effective_after_arc_ordinal: int,
    suffix: BookArcTopologySuffix,
) -> BookArcTopology:
    if effective_after_arc_ordinal < 0:
        raise CommandPreconditionError("Book topology effective boundary is invalid.")
    if current is None:
        if effective_after_arc_ordinal != 0:
            raise CommandPreconditionError(
                "An initial Book topology must start at effective ordinal zero."
            )
        prefix: list[BookArcContract] = []
    else:
        if effective_after_arc_ordinal > len(current.arcs):
            raise CommandPreconditionError(
                "Book topology effective boundary exceeds the current topology."
            )
        prefix = list(current.arcs[:effective_after_arc_ordinal])

    if suffix.arcs:
        normalized_prefix = [
            item.model_copy(update={"is_final": False}) if item.is_final else item
            for item in prefix
        ]
        composed = [*normalized_prefix, *suffix.arcs]
    else:
        if not prefix:
            raise CommandPreconditionError(
                "An empty Book topology suffix requires a preserved historical prefix."
            )
        composed = [
            *[
                item.model_copy(update={"is_final": False}) if item.is_final else item
                for item in prefix[:-1]
            ],
            prefix[-1].model_copy(update={"is_final": True}),
        ]
    return BookArcTopology(arcs=composed)


async def _topology_effective_after_arc_ordinal(
    session: StoreSession,
    *,
    project_id: str,
    book_id: str,
    base_book_baseline_id: str | None,
) -> int:
    arcs = await session.arcs.list_for_book(project_id=project_id, book_id=book_id)
    if base_book_baseline_id is None:
        if arcs:
            raise CommandPreconditionError(
                "An initial Book workspace cannot have existing Story Arcs."
            )
        return 0
    unfinished = [arc for arc in arcs if arc.lifecycle_status != "completed"]
    if len(unfinished) > 1:
        raise CommandPreconditionError(
            "Book topology authority contains multiple unfinished Story Arcs."
        )
    if unfinished:
        return unfinished[0].ordinal - 1
    return 0 if not arcs else arcs[-1].ordinal


async def _load_book_arc_topology(
    session: StoreSession,
    *,
    project_id: str,
    book_id: str,
    baseline_id: str | None,
) -> BookArcTopology | None:
    if baseline_id is None:
        return None
    baseline = await session.books.get_baseline(
        project_id=project_id,
        book_id=book_id,
        baseline_id=baseline_id,
    )
    if baseline is None:
        raise CommandPreconditionError("Book topology baseline does not exist.")
    packed = await session.content.get_packed(
        project_id=project_id,
        ref_id=baseline.arc_topology_ref_id,
    )
    try:
        topology = BookArcTopology.model_validate_json(packed.unpack_and_verify())
    except ValidationError as error:
        raise CommandPreconditionError(
            "The current Book baseline contains an invalid Arc topology."
        ) from error
    if (
        len(topology.arcs) != baseline.arc_contract_count
        or baseline.final_arc_ordinal != len(topology.arcs)
    ):
        raise CommandPreconditionError(
            "Book Arc topology content disagrees with routing metadata."
        )
    return topology


class BookCommandService:
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
        request: ApplyBookDiscussionTaskRequest,
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

    async def record_user_input(
        self,
        request: RecordBookUserInputRequest,
        *,
        idempotency_key: str,
    ) -> CommandExecution[RecordBookUserInputResult]:
        timestamp = self._now_ms()
        state_ref_id = self._id_factory()
        transcript_ref_id = self._id_factory()
        envelope = self._envelope(
            request=request,
            project_id=request.project_id,
            idempotency_key=idempotency_key,
            command_kind="record_book_user_input",
            actor="user",
            created_at_ms=timestamp,
        )

        async def handler(session: StoreSession) -> CommandEffect[RecordBookUserInputResult]:
            project = await session.projects.get(request.project_id)
            book = await session.books.get_for_project(request.project_id)
            workspace = await session.books.get_workspace(
                project_id=request.project_id,
                book_id=request.book_id,
            )
            if (
                project is None
                or book is None
                or book.id != request.book_id
                or workspace is None
                or workspace.lock_version != request.expected_workspace_lock_version
                or workspace.base_book_baseline_id != book.current_baseline_id
                or workspace.base_canon_baseline_id != project.current_canon_baseline_id
                or workspace.state in {"blocked_by_upstream", "stale"}
            ):
                raise CommandPreconditionError("Book workspace is stale or blocked by upstream.")
            state = BookDiscussionState.model_validate_json(
                (
                    await session.content.get_packed(
                        project_id=request.project_id,
                        ref_id=workspace.discussion_state_ref_id,
                    )
                ).unpack_and_verify()
            )
            transcript = BookTranscript.model_validate_json(
                (
                    await session.content.get_packed(
                        project_id=request.project_id,
                        ref_id=workspace.transcript_ref_id,
                    )
                ).unpack_and_verify()
            )
            updated_state, updated_transcript = bind_user_input(
                state=state,
                transcript=transcript,
                message=request.message,
                suggestion_id=request.suggestion_id,
            )
            pending = await session.books.find_pending_submission(
                project_id=request.project_id,
                book_id=request.book_id,
            )
            if pending is not None:
                await session.books.close_submission(
                    project_id=request.project_id,
                    submission_id=pending.id,
                    disposition="superseded",
                    reason_code="workspace_edited",
                    closed_at_ms=timestamp,
                )
            state_ref = await session.content.put(
                project_id=request.project_id,
                prepared=prepare_canonical_json(updated_state),
                semantic_kind="book.discussion_state",
                media_type="application/json",
                schema_id="book-discussion-state",
                schema_version=1,
                ref_id=state_ref_id,
                created_at_ms=timestamp,
            )
            transcript_ref = await session.content.put(
                project_id=request.project_id,
                prepared=prepare_canonical_json(updated_transcript),
                semantic_kind="book.transcript",
                media_type="application/json",
                schema_id="book-transcript",
                schema_version=1,
                ref_id=transcript_ref_id,
                created_at_ms=timestamp,
            )
            updated_workspace = replace(
                workspace,
                state="active",
                lock_version=workspace.lock_version + 1,
                discussion_state_ref_id=state_ref.id,
                transcript_ref_id=transcript_ref.id,
                candidate_constraints_ref_id=None,
                candidate_titles_ref_id=None,
                candidate_rolling_plan_ref_id=None,
                candidate_completion_contract_ref_id=None,
                candidate_arc_topology_ref_id=None,
                readiness_status="continue",
                stale_reason_code=None,
                stale_at_ms=None,
                updated_at_ms=timestamp,
            )
            if not await session.books.compare_and_set_workspace(
                record=updated_workspace,
                expected_lock_version=workspace.lock_version,
            ):
                raise CommandPreconditionError("Book workspace CAS failed.")
            run = await session.runs.get_open_for_project(request.project_id)
            if run is not None and run.status == "waiting_for_user":
                if not await session.runs.start_waiting_run(
                    project_id=request.project_id,
                    run_id=run.id,
                    expected_lock_version=run.lock_version,
                    now_ms=timestamp,
                ):
                    raise CommandPreconditionError("Book input could not wake the waiting Run.")
            result = RecordBookUserInputResult(
                project_id=request.project_id,
                book_id=request.book_id,
                workspace_lock_version=updated_workspace.lock_version,
                selected_title=updated_state.selected_title,
            )
            return CommandEffect(
                result=result,
                events=(
                    EventDraft(
                        event_type="book.user_input_recorded",
                        aggregate_type="book",
                        aggregate_id=request.book_id,
                        payload={
                            "workspace_lock_version": updated_workspace.lock_version,
                            "suggestion_id": request.suggestion_id,
                        },
                    ),
                ),
            )

        return await self._command_bus.execute(
            envelope=envelope,
            result_type=RecordBookUserInputResult,
            handler=handler,
        )

    async def apply_discussion_result(
        self,
        request: ApplyBookDiscussionTaskRequest,
        *,
        idempotency_key: str,
    ) -> CommandExecution[ApplyBookDiscussionTaskResult]:
        task, raw = await self._read_successful_result(request)
        if (
            task.task_kind != "book.discuss"
            or task.role != "book_strategist"
            or task.scope_layer != "book"
            or task.book_id != request.book_id
        ):
            raise CommandPreconditionError("Task is not a Book discussion task.")
        agent_result = BookDiscussionResult.model_validate_json(raw)
        timestamp = self._now_ms()
        direction_ref_id = self._id_factory()
        state_ref_id = self._id_factory()
        transcript_ref_id = self._id_factory()
        envelope = self._envelope(
            request=request,
            project_id=request.project_id,
            idempotency_key=idempotency_key,
            command_kind="apply_book_discussion_result",
            actor="engine",
            source_task_id=request.task_id,
            created_at_ms=timestamp,
        )

        async def handler(session: StoreSession) -> CommandEffect[ApplyBookDiscussionTaskResult]:
            current_task = await session.execution.get_successful_task(
                project_id=request.project_id,
                task_id=request.task_id,
                attempt_id=request.attempt_id,
            )
            workspace = await session.books.get_workspace(
                project_id=request.project_id,
                book_id=request.book_id,
            )
            if current_task != task or workspace is None:
                raise CommandPreconditionError("Book task or workspace no longer exists.")
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
                    raise CommandPreconditionError("Stale Book task delivery changed concurrently.")
                result = ApplyBookDiscussionTaskResult(
                    project_id=request.project_id,
                    book_id=request.book_id,
                    task_id=request.task_id,
                    delivery="discarded_stale",
                    workspace_lock_version=workspace.lock_version,
                    readiness_status=(
                        "ready" if workspace.readiness_status == "ready" else "continue"
                    ),
                    selected_title=None,
                )
                return CommandEffect(
                    result=result,
                    events=(
                        EventDraft(
                            event_type="book.task_result_discarded_stale",
                            aggregate_type="book",
                            aggregate_id=request.book_id,
                            payload={"task_id": request.task_id, "component": "discussion"},
                        ),
                    ),
                )
            state = BookDiscussionState.model_validate_json(
                (
                    await session.content.get_packed(
                        project_id=request.project_id,
                        ref_id=workspace.discussion_state_ref_id,
                    )
                ).unpack_and_verify()
            )
            transcript = BookTranscript.model_validate_json(
                (
                    await session.content.get_packed(
                        project_id=request.project_id,
                        ref_id=workspace.transcript_ref_id,
                    )
                ).unpack_and_verify()
            )
            updated_state, updated_transcript = bind_agent_result(
                book_id=request.book_id,
                state=state,
                transcript=transcript,
                result=agent_result,
            )
            pending = await session.books.find_pending_submission(
                project_id=request.project_id,
                book_id=request.book_id,
            )
            if pending is not None:
                await session.books.close_submission(
                    project_id=request.project_id,
                    submission_id=pending.id,
                    disposition="superseded",
                    reason_code="workspace_edited",
                    closed_at_ms=timestamp,
                )
            prepared_items = (
                prepare_exact_text(updated_state.direction_draft),
                prepare_canonical_json(updated_state),
                prepare_canonical_json(updated_transcript),
            )
            descriptors = (
                ("book.direction_draft", "text/plain; charset=utf-8", None, None),
                (
                    "book.discussion_state",
                    "application/json",
                    "book-discussion-state",
                    1,
                ),
                ("book.transcript", "application/json", "book-transcript", 1),
            )
            refs = [
                await session.content.put(
                    project_id=request.project_id,
                    prepared=prepared,
                    semantic_kind=descriptor[0],
                    media_type=descriptor[1],
                    schema_id=descriptor[2],
                    schema_version=descriptor[3],
                    ref_id=ref_id,
                    created_at_ms=timestamp,
                )
                for prepared, descriptor, ref_id in zip(
                    prepared_items,
                    descriptors,
                    (direction_ref_id, state_ref_id, transcript_ref_id),
                    strict=True,
                )
            ]
            updated_workspace = replace(
                workspace,
                state="active",
                lock_version=workspace.lock_version + 1,
                direction_draft_ref_id=refs[0].id,
                discussion_state_ref_id=refs[1].id,
                transcript_ref_id=refs[2].id,
                candidate_constraints_ref_id=None,
                candidate_titles_ref_id=None,
                candidate_rolling_plan_ref_id=None,
                candidate_completion_contract_ref_id=None,
                candidate_arc_topology_ref_id=None,
                readiness_status=updated_state.readiness_status,
                stale_reason_code=None,
                stale_at_ms=None,
                updated_at_ms=timestamp,
            )
            if not await session.books.compare_and_set_workspace(
                record=updated_workspace,
                expected_lock_version=workspace.lock_version,
            ):
                raise CommandPreconditionError("Book workspace CAS failed.")
            if not await session.execution.mark_delivery_applied(
                project_id=request.project_id,
                task_id=request.task_id,
                attempt_id=request.attempt_id,
                command_id=envelope.command_id,
                updated_at_ms=timestamp,
            ):
                raise CommandPreconditionError("Book task delivery is no longer pending.")
            if updated_state.readiness_status == "awaiting_agent":  # pragma: no cover
                raise AssertionError("An applied Book Agent turn must resolve its readiness.")
            if updated_state.readiness_status == "continue":
                if not await session.runs.ensure_wait_for_user(
                    run_id=task.run_id,
                    reason_code="book_direction_input",
                    now_ms=timestamp,
                ):
                    raise CommandPreconditionError("Run could not enter Book discussion wait.")
            result = ApplyBookDiscussionTaskResult(
                project_id=request.project_id,
                book_id=request.book_id,
                task_id=request.task_id,
                delivery="applied",
                workspace_lock_version=updated_workspace.lock_version,
                readiness_status=updated_state.readiness_status,
                selected_title=updated_state.selected_title,
            )
            return CommandEffect(
                result=result,
                events=(
                    EventDraft(
                        event_type="book.discussion_updated",
                        aggregate_type="book",
                        aggregate_id=request.book_id,
                        payload={
                            "task_id": request.task_id,
                            "turn": updated_state.turn_count,
                            "readiness_status": updated_state.readiness_status,
                            "workspace_lock_version": updated_workspace.lock_version,
                        },
                    ),
                ),
            )

        return await self._command_bus.execute(
            envelope=envelope,
            result_type=ApplyBookDiscussionTaskResult,
            handler=handler,
        )

    async def apply_candidate_result(
        self,
        request: ApplyBookCandidateTaskRequest,
        *,
        idempotency_key: str,
    ) -> CommandExecution[ApplyBookCandidateTaskResult]:
        task, raw = await self._read_successful_result(request)
        if (
            task.task_kind not in {"book.synthesize", "book.revise", "book.repair"}
            or task.role != "book_strategist"
            or task.scope_layer != "book"
            or task.book_id != request.book_id
        ):
            raise CommandPreconditionError("Task is not an authorized Book candidate task.")
        workspace_snapshot: BookWorkspaceRecord | None = None
        review_snapshot: BookReviewRecord | None = None
        prepared_repair = None
        if task.task_kind == "book.repair":
            patch = BookRepairPatch.model_validate_json(raw)
            candidate: BookCandidatePack | None = None
            async with self._command_bus.read_unit_of_work() as session:
                workspace_snapshot = await session.books.get_workspace(
                    project_id=request.project_id,
                    book_id=request.book_id,
                )
                if workspace_snapshot is not None and _task_matches_workspace(
                    task,
                    workspace_snapshot,
                    expected_lock_version=request.expected_workspace_lock_version,
                ):
                    review_snapshot = (
                        None
                        if workspace_snapshot.active_repair_review_id is None
                        else await session.books.get_review(
                            project_id=request.project_id,
                            review_id=workspace_snapshot.active_repair_review_id,
                        )
                    )
                    if (
                        review_snapshot is None
                        or task.source_book_candidate_review_id != review_snapshot.id
                        or review_snapshot.decision != "local_repair"
                        or review_snapshot.repair_contract_ref_id is None
                        or workspace_snapshot.semantic_repair_count
                        >= workspace_snapshot.semantic_repair_limit
                    ):
                        raise CommandPreconditionError(
                            "Book has no active local repair budget."
                        )
                    repair_contract = BookRepairContract.model_validate_json(
                        (
                            await session.content.get_packed(
                                project_id=request.project_id,
                                ref_id=review_snapshot.repair_contract_ref_id,
                            )
                        ).unpack_and_verify()
                    )
                    state = BookDiscussionState.model_validate_json(
                        (
                            await session.content.get_packed(
                                project_id=request.project_id,
                                ref_id=workspace_snapshot.discussion_state_ref_id,
                            )
                        ).unpack_and_verify()
                    )
                    component_refs = (
                        workspace_snapshot.direction_draft_ref_id,
                        workspace_snapshot.candidate_constraints_ref_id,
                        workspace_snapshot.candidate_rolling_plan_ref_id,
                        workspace_snapshot.candidate_completion_contract_ref_id,
                        workspace_snapshot.candidate_arc_topology_ref_id,
                    )
                    if (
                        state.selected_title is None
                        or state.selected_title_source is None
                        or workspace_snapshot.candidate_titles_ref_id is None
                        or any(reference is None for reference in component_refs)
                    ):
                        raise CommandPreconditionError(
                            "Book repair has no complete current candidate."
                        )
                    (
                        direction_ref,
                        constraints_ref,
                        rolling_ref,
                        completion_ref,
                        topology_ref,
                    ) = cast(
                        tuple[str, str, str, str, str],
                        component_refs,
                    )
                    topology_effective_after = (
                        await _topology_effective_after_arc_ordinal(
                            session,
                            project_id=request.project_id,
                            book_id=request.book_id,
                            base_book_baseline_id=(
                                workspace_snapshot.base_book_baseline_id
                            ),
                        )
                    )
                    current = BookCandidatePack(
                        direction=(
                            await session.content.get_packed(
                                project_id=request.project_id,
                                ref_id=direction_ref,
                            )
                        )
                        .unpack_and_verify()
                        .decode("utf-8"),
                        constraints=json.loads(
                            (
                                await session.content.get_packed(
                                    project_id=request.project_id,
                                    ref_id=constraints_ref,
                                )
                            ).unpack_and_verify()
                        ),
                        selected_title=state.selected_title,
                        rolling_plan=json.loads(
                            (
                                await session.content.get_packed(
                                    project_id=request.project_id,
                                    ref_id=rolling_ref,
                                )
                            ).unpack_and_verify()
                        ),
                        completion_contract=CompletionContract.model_validate_json(
                            (
                                await session.content.get_packed(
                                    project_id=request.project_id,
                                    ref_id=completion_ref,
                                )
                            ).unpack_and_verify()
                        ),
                        arc_topology=BookArcTopology.model_validate_json(
                            (
                                await session.content.get_packed(
                                    project_id=request.project_id,
                                    ref_id=topology_ref,
                                )
                            ).unpack_and_verify()
                        ),
                    )
                    candidate = _merge_book_repair(
                        current=current,
                        patch=patch,
                        contract=repair_contract,
                        topology_effective_after_arc_ordinal=(
                            topology_effective_after
                        ),
                    )
                    prepared_repair = (
                        prepare_exact_text(candidate.direction),
                        prepare_canonical_json(candidate.constraints),
                        prepare_canonical_json(
                            {
                                "selected_title": candidate.selected_title,
                                "title_source": state.selected_title_source,
                            }
                        ),
                        prepare_canonical_json(candidate.rolling_plan),
                        prepare_canonical_json(candidate.completion_contract),
                        prepare_canonical_json(candidate.arc_topology),
                    )
        elif task.task_kind == "book.revise":
            proposal = BookSuccessorCandidateProposal.model_validate_json(raw)
            candidate = None
            async with self._command_bus.read_unit_of_work() as session:
                workspace_snapshot = await session.books.get_workspace(
                    project_id=request.project_id,
                    book_id=request.book_id,
                )
                if workspace_snapshot is not None and _task_matches_workspace(
                    task,
                    workspace_snapshot,
                    expected_lock_version=request.expected_workspace_lock_version,
                ):
                    current_topology = await _load_book_arc_topology(
                        session,
                        project_id=request.project_id,
                        book_id=request.book_id,
                        baseline_id=workspace_snapshot.base_book_baseline_id,
                    )
                    topology_effective_after = (
                        await _topology_effective_after_arc_ordinal(
                            session,
                            project_id=request.project_id,
                            book_id=request.book_id,
                            base_book_baseline_id=(
                                workspace_snapshot.base_book_baseline_id
                            ),
                        )
                    )
                    candidate = BookCandidatePack(
                        direction=proposal.direction,
                        constraints=proposal.constraints,
                        selected_title=proposal.selected_title,
                        rolling_plan=proposal.rolling_plan,
                        completion_contract=proposal.completion_contract,
                        arc_topology=_compose_book_arc_topology(
                            current=current_topology,
                            effective_after_arc_ordinal=topology_effective_after,
                            suffix=proposal.arc_topology_suffix,
                        ),
                    )
        else:
            candidate = BookCandidatePack.model_validate_json(raw)
        timestamp = self._now_ms()
        ref_ids = [self._id_factory() for _ in range(6)]
        envelope = self._envelope(
            request=request,
            project_id=request.project_id,
            idempotency_key=idempotency_key,
            command_kind=f"apply_{task.task_kind.replace('.', '_')}_result",
            actor="engine",
            source_task_id=request.task_id,
            created_at_ms=timestamp,
        )

        async def handler(session: StoreSession) -> CommandEffect[ApplyBookCandidateTaskResult]:
            current_task = await session.execution.get_successful_task(
                project_id=request.project_id,
                task_id=request.task_id,
                attempt_id=request.attempt_id,
            )
            workspace = await session.books.get_workspace(
                project_id=request.project_id,
                book_id=request.book_id,
            )
            if current_task != task or workspace is None:
                raise CommandPreconditionError("Book task or workspace no longer exists.")
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
                    raise CommandPreconditionError("Stale Book task delivery changed concurrently.")
                result = ApplyBookCandidateTaskResult(
                    project_id=request.project_id,
                    book_id=request.book_id,
                    task_id=request.task_id,
                    delivery="discarded_stale",
                    workspace_lock_version=workspace.lock_version,
                )
                return CommandEffect(
                    result=result,
                    events=(
                        EventDraft(
                            event_type="book.task_result_discarded_stale",
                            aggregate_type="book",
                            aggregate_id=request.book_id,
                            payload={"task_id": request.task_id, "component": "candidate"},
                        ),
                    ),
                )
            state = BookDiscussionState.model_validate_json(
                (
                    await session.content.get_packed(
                        project_id=request.project_id,
                        ref_id=workspace.discussion_state_ref_id,
                    )
                ).unpack_and_verify()
            )
            if (
                state.readiness_status != "ready"
                or state.selected_title is None
                or state.selected_title_source is None
                or candidate is None
                or candidate.selected_title != state.selected_title
            ):
                raise CommandPreconditionError(
                    "Book candidate does not preserve the approved discussion title."
                )
            prepared = prepared_repair or (
                prepare_exact_text(candidate.direction),
                prepare_canonical_json(candidate.constraints),
                prepare_canonical_json(
                    {
                        "selected_title": candidate.selected_title,
                        "title_source": state.selected_title_source,
                    }
                ),
                prepare_canonical_json(candidate.rolling_plan),
                prepare_canonical_json(candidate.completion_contract),
                prepare_canonical_json(candidate.arc_topology),
            )
            repair_increment = 0
            if task.task_kind == "book.repair":
                review = (
                    None
                    if workspace.active_repair_review_id is None
                    else await session.books.get_review(
                        project_id=request.project_id,
                        review_id=workspace.active_repair_review_id,
                    )
                )
                if (
                    workspace_snapshot is None
                    or workspace != workspace_snapshot
                    or review_snapshot is None
                    or review != review_snapshot
                    or task.source_book_candidate_review_id != review_snapshot.id
                    or prepared_repair is None
                    or workspace.semantic_repair_count >= workspace.semantic_repair_limit
                ):
                    raise CommandPreconditionError(
                        "Book repair authorization changed before result delivery."
                    )
                repair_increment = 1

            pending = await session.books.find_pending_submission(
                project_id=request.project_id,
                book_id=request.book_id,
            )
            if pending is not None:
                await session.books.close_submission(
                    project_id=request.project_id,
                    submission_id=pending.id,
                    disposition="superseded",
                    reason_code="workspace_edited",
                    closed_at_ms=timestamp,
                )
            descriptors = (
                ("book.direction", "text/plain; charset=utf-8", None, None),
                ("book.constraints", "application/json", "book-constraints", 1),
                ("book.title", "application/json", "book-title", 1),
                ("book.rolling_plan", "application/json", "book-rolling-plan", 2),
                (
                    "book.completion_contract",
                    "application/json",
                    "book-completion-contract",
                    2,
                ),
                (
                    "book.arc_topology",
                    "application/json",
                    "book-arc-topology",
                    1,
                ),
            )
            refs = [
                await session.content.put(
                    project_id=request.project_id,
                    prepared=payload,
                    semantic_kind=descriptor[0],
                    media_type=descriptor[1],
                    schema_id=descriptor[2],
                    schema_version=descriptor[3],
                    ref_id=ref_id,
                    created_at_ms=timestamp,
                )
                for payload, descriptor, ref_id in zip(
                    prepared,
                    descriptors,
                    ref_ids,
                    strict=True,
                )
            ]
            updated_workspace = replace(
                workspace,
                state="active",
                lock_version=workspace.lock_version + 1,
                direction_draft_ref_id=refs[0].id,
                candidate_constraints_ref_id=refs[1].id,
                candidate_titles_ref_id=refs[2].id,
                candidate_rolling_plan_ref_id=refs[3].id,
                candidate_completion_contract_ref_id=refs[4].id,
                candidate_arc_topology_ref_id=refs[5].id,
                readiness_status="ready",
                semantic_repair_count=workspace.semantic_repair_count + repair_increment,
                stale_reason_code=None,
                stale_at_ms=None,
                updated_at_ms=timestamp,
            )
            if not await session.books.compare_and_set_workspace(
                record=updated_workspace,
                expected_lock_version=workspace.lock_version,
            ):
                raise CommandPreconditionError("Book workspace CAS failed.")
            if not await session.execution.mark_delivery_applied(
                project_id=request.project_id,
                task_id=request.task_id,
                attempt_id=request.attempt_id,
                command_id=envelope.command_id,
                updated_at_ms=timestamp,
            ):
                raise CommandPreconditionError("Book task delivery is no longer pending.")
            result = ApplyBookCandidateTaskResult(
                project_id=request.project_id,
                book_id=request.book_id,
                task_id=request.task_id,
                delivery="applied",
                workspace_lock_version=updated_workspace.lock_version,
            )
            return CommandEffect(
                result=result,
                events=(
                    EventDraft(
                        event_type="book.candidate_updated",
                        aggregate_type="book",
                        aggregate_id=request.book_id,
                        payload={
                            "task_id": request.task_id,
                            "task_kind": task.task_kind,
                            "workspace_lock_version": updated_workspace.lock_version,
                        },
                    ),
                ),
            )

        return await self._command_bus.execute(
            envelope=envelope,
            result_type=ApplyBookCandidateTaskResult,
            handler=handler,
        )

    async def apply_candidate(
        self,
        request: ApplyBookCandidateRequest,
        *,
        idempotency_key: str,
    ) -> CommandExecution[ApplyBookCandidateResult]:
        timestamp = self._now_ms()
        envelope = self._envelope(
            request=request,
            project_id=request.project_id,
            idempotency_key=idempotency_key,
            command_kind="apply_book_candidate",
            actor="engine",
            created_at_ms=timestamp,
        )
        prepared = (
            prepare_exact_text(request.candidate.direction),
            prepare_canonical_json(request.candidate.constraints),
            prepare_canonical_json(
                {
                    "selected_title": request.candidate.selected_title,
                    "title_source": request.selected_title_source,
                }
            ),
            prepare_canonical_json(request.candidate.rolling_plan),
            prepare_canonical_json(request.candidate.completion_contract),
            prepare_canonical_json(request.candidate.arc_topology),
        )
        ref_ids = [self._id_factory() for _ in prepared]

        async def handler(session: StoreSession) -> CommandEffect[ApplyBookCandidateResult]:
            project = await session.projects.get(request.project_id)
            if project is None:
                raise ProjectNotFoundError(request.project_id)
            book = await session.books.get_for_project(request.project_id)
            if book is None or book.id != request.book_id:
                raise BookNotFoundError(request.book_id)
            workspace = await session.books.get_workspace(
                project_id=request.project_id,
                book_id=request.book_id,
            )
            if (
                workspace is None
                or workspace.lock_version != request.expected_workspace_lock_version
                or workspace.base_canon_baseline_id != project.current_canon_baseline_id
                or workspace.state in {"blocked_by_user", "blocked_by_upstream", "stale"}
            ):
                raise CommandPreconditionError("Book workspace is stale or blocked.")

            pending = await session.books.find_pending_submission(
                project_id=request.project_id,
                book_id=request.book_id,
            )
            if pending is not None:
                await session.books.close_submission(
                    project_id=request.project_id,
                    submission_id=pending.id,
                    disposition="superseded",
                    reason_code="workspace_edited",
                    closed_at_ms=timestamp,
                )

            descriptors = (
                ("book.direction", "text/plain; charset=utf-8", None, None),
                ("book.constraints", "application/json", "book-constraints", 1),
                ("book.title", "application/json", "book-title", 1),
                ("book.rolling_plan", "application/json", "book-rolling-plan", 2),
                (
                    "book.completion_contract",
                    "application/json",
                    "book-completion-contract",
                    2,
                ),
                (
                    "book.arc_topology",
                    "application/json",
                    "book-arc-topology",
                    1,
                ),
            )
            refs = [
                await session.content.put(
                    project_id=request.project_id,
                    prepared=payload,
                    semantic_kind=semantic_kind,
                    media_type=media_type,
                    schema_id=schema_id,
                    schema_version=schema_version,
                    ref_id=ref_id,
                    created_at_ms=timestamp,
                )
                for payload, ref_id, (
                    semantic_kind,
                    media_type,
                    schema_id,
                    schema_version,
                ) in zip(prepared, ref_ids, descriptors, strict=True)
            ]
            updated = replace(
                workspace,
                state="active",
                lock_version=workspace.lock_version + 1,
                direction_draft_ref_id=refs[0].id,
                candidate_constraints_ref_id=refs[1].id,
                candidate_titles_ref_id=refs[2].id,
                candidate_rolling_plan_ref_id=refs[3].id,
                candidate_completion_contract_ref_id=refs[4].id,
                candidate_arc_topology_ref_id=refs[5].id,
                readiness_status="ready",
                stale_reason_code=None,
                stale_at_ms=None,
                updated_at_ms=timestamp,
            )
            if not await session.books.compare_and_set_workspace(
                record=updated,
                expected_lock_version=request.expected_workspace_lock_version,
            ):
                raise CommandPreconditionError("Book workspace CAS failed.")
            result = ApplyBookCandidateResult(
                project_id=request.project_id,
                book_id=request.book_id,
                workspace_id=workspace.id,
                workspace_lock_version=updated.lock_version,
            )
            return CommandEffect(
                result=result,
                events=(
                    EventDraft(
                        event_type="book.workspace_updated",
                        aggregate_type="book",
                        aggregate_id=request.book_id,
                        payload={"workspace_lock_version": updated.lock_version},
                    ),
                ),
            )

        return await self._command_bus.execute(
            envelope=envelope,
            result_type=ApplyBookCandidateResult,
            handler=handler,
        )

    async def submit_for_review(
        self,
        request: SubmitBookRequest,
        *,
        idempotency_key: str,
    ) -> CommandExecution[SubmitBookResult]:
        timestamp = self._now_ms()
        submission_id = self._id_factory()
        manifest_ref_id = self._id_factory()
        async with self._command_bus.read_unit_of_work() as read_session:
            workspace_snapshot = await read_session.books.get_workspace(
                project_id=request.project_id,
                book_id=request.book_id,
            )
            source_feedback = (
                None
                if workspace_snapshot is None
                or workspace_snapshot.source_feedback_id is None
                else await read_session.feedback.get(
                    project_id=request.project_id,
                    feedback_id=workspace_snapshot.source_feedback_id,
                )
            )
        if workspace_snapshot is None:
            raise BookNotFoundError(request.book_id)
        required_refs = (
            workspace_snapshot.candidate_constraints_ref_id,
            workspace_snapshot.candidate_titles_ref_id,
            workspace_snapshot.candidate_rolling_plan_ref_id,
            workspace_snapshot.candidate_completion_contract_ref_id,
            workspace_snapshot.candidate_arc_topology_ref_id,
        )
        if (
            workspace_snapshot.lock_version != request.expected_workspace_lock_version
            or workspace_snapshot.readiness_status != "ready"
            or any(reference is None for reference in required_refs)
        ):
            raise CommandPreconditionError("Book workspace is not ready for review.")
        if workspace_snapshot.source_feedback_id is not None and (
            source_feedback is None
            or source_feedback.status != "applied"
            or source_feedback.route_layer != "book"
            or source_feedback.book_id != request.book_id
            or source_feedback.content_ref_id != workspace_snapshot.guidance_ref_id
        ):
            raise CommandPreconditionError(
                "Book guidance lost its exact applied feedback source."
            )
        manifest = {
            "schema": "book-review-manifest-v4",
            "workspace_id": workspace_snapshot.id,
            "workspace_lock_version": workspace_snapshot.lock_version,
            "work_cycle_id": workspace_snapshot.work_cycle_id,
            "base_book_baseline_id": workspace_snapshot.base_book_baseline_id,
            "canon_baseline_id": workspace_snapshot.base_canon_baseline_id,
            "direction_ref_id": workspace_snapshot.direction_draft_ref_id,
            "constraints_ref_id": required_refs[0],
            "titles_ref_id": required_refs[1],
            "rolling_plan_ref_id": required_refs[2],
            "completion_contract_ref_id": required_refs[3],
            "arc_topology_ref_id": required_refs[4],
            "guidance_ref_id": workspace_snapshot.guidance_ref_id,
            "source_feedback_id": (
                None if source_feedback is None else source_feedback.id
            ),
            "source_book_parent_review_id": (
                workspace_snapshot.source_book_parent_review_id
            ),
            "source_book_completion_review_id": (
                workspace_snapshot.source_book_completion_review_id
            ),
            "source_book_progress_handoff_id": (
                workspace_snapshot.source_book_progress_handoff_id
            ),
        }
        prepared_manifest = prepare_canonical_json(manifest)
        envelope = self._envelope(
            request=request,
            project_id=request.project_id,
            idempotency_key=idempotency_key,
            command_kind="submit_book_for_review",
            actor="engine",
            created_at_ms=timestamp,
        )

        async def handler(session: StoreSession) -> CommandEffect[SubmitBookResult]:
            current = await session.books.get_workspace(
                project_id=request.project_id,
                book_id=request.book_id,
            )
            if current != workspace_snapshot:
                raise CommandPreconditionError("Book workspace changed while preparing submission.")
            if (
                await session.books.find_pending_submission(
                    project_id=request.project_id,
                    book_id=request.book_id,
                )
                is not None
            ):
                raise CommandPreconditionError("A Book submission is already pending.")
            manifest_ref = await session.content.put(
                project_id=request.project_id,
                prepared=prepared_manifest,
                semantic_kind="book.review_manifest",
                media_type="application/json",
                schema_id="book-review-manifest",
                schema_version=4,
                ref_id=manifest_ref_id,
                created_at_ms=timestamp,
            )
            assert all(reference is not None for reference in required_refs)
            await session.books.insert_submission(
                BookSubmissionRecord(
                    id=submission_id,
                    project_id=request.project_id,
                    book_id=request.book_id,
                    workspace_id=workspace_snapshot.id,
                    workspace_lock_version=workspace_snapshot.lock_version,
                    work_cycle_id=workspace_snapshot.work_cycle_id,
                    base_book_baseline_id=workspace_snapshot.base_book_baseline_id,
                    canon_baseline_id=workspace_snapshot.base_canon_baseline_id,
                    direction_ref_id=workspace_snapshot.direction_draft_ref_id,
                    constraints_ref_id=cast(str, required_refs[0]),
                    titles_ref_id=cast(str, required_refs[1]),
                    rolling_plan_ref_id=cast(str, required_refs[2]),
                    completion_contract_ref_id=cast(str, required_refs[3]),
                    arc_topology_ref_id=cast(str, required_refs[4]),
                    content_manifest_ref_id=manifest_ref.id,
                    content_fingerprint=prepared_manifest.sha256,
                    disposition="pending",
                    close_reason_code=None,
                    created_at_ms=timestamp,
                    closed_at_ms=None,
                )
            )
            result = SubmitBookResult(
                project_id=request.project_id,
                book_id=request.book_id,
                submission_id=submission_id,
                content_fingerprint=prepared_manifest.sha256,
            )
            return CommandEffect(
                result=result,
                events=(
                    EventDraft(
                        event_type="book.submitted",
                        aggregate_type="book",
                        aggregate_id=request.book_id,
                        payload={"submission_id": submission_id},
                    ),
                ),
            )

        return await self._command_bus.execute(
            envelope=envelope,
            result_type=SubmitBookResult,
            handler=handler,
        )

    async def record_review(
        self,
        request: RecordBookReviewRequest,
        *,
        idempotency_key: str,
    ) -> CommandExecution[RecordBookReviewResult]:
        timestamp = self._now_ms()
        review_id = self._id_factory()
        precheck_ref_id = self._id_factory()
        repair_ref_id = self._id_factory()
        async with self._command_bus.read_unit_of_work() as read_session:
            task_snapshot = await read_session.execution.get_successful_task(
                project_id=request.project_id,
                task_id=request.evaluator_task_id,
                attempt_id=request.evaluator_attempt_id,
            )
            if task_snapshot is None:
                raise CommandPreconditionError("Evaluator task has no complete successful result.")
            packed_evaluation = await read_session.content.get_packed(
                project_id=request.project_id,
                ref_id=task_snapshot.result_ref_id,
            )
        evaluation = BookEvaluation.model_validate_json(packed_evaluation.unpack_and_verify())
        if evaluation.decision == "pass" and request.deterministic_precheck.get("passed") is not True:
            raise CommandPreconditionError("Book deterministic prechecks did not pass.")
        prepared_precheck = prepare_canonical_json(request.deterministic_precheck)
        prepared_repair = (
            None
            if evaluation.repair_contract is None
            else prepare_canonical_json(evaluation.repair_contract)
        )
        failure_ref_id = self._id_factory()
        prepared_failure = prepare_canonical_json(
            {
                "code": "semantic_repair_exhausted",
                "message": "Book semantic correction for this frozen review is exhausted.",
                "book_id": request.book_id,
            }
        )
        envelope = self._envelope(
            request=request,
            project_id=request.project_id,
            idempotency_key=idempotency_key,
            command_kind="record_book_review",
            actor="engine",
            source_task_id=request.evaluator_task_id,
            created_at_ms=timestamp,
        )

        async def handler(session: StoreSession) -> CommandEffect[RecordBookReviewResult]:
            submission = await session.books.get_submission(
                project_id=request.project_id,
                submission_id=request.submission_id,
            )
            current_task = await session.execution.get_successful_task(
                project_id=request.project_id,
                task_id=request.evaluator_task_id,
                attempt_id=request.evaluator_attempt_id,
            )
            workspace = await session.books.get_workspace(
                project_id=request.project_id,
                book_id=request.book_id,
            )
            expected_task_kind = (
                "verify_repair.book"
                if workspace is not None and workspace.semantic_repair_count > 0
                else "evaluate.book"
            )
            strategy = DEFAULT_EVALUATION_STRATEGY_REGISTRY.for_task(
                expected_task_kind
            )
            if (
                submission is None
                or submission.book_id != request.book_id
                or submission.disposition != "pending"
                or current_task != task_snapshot
                or task_snapshot.delivery_state != "pending"
                or task_snapshot.role != "evaluator"
                or task_snapshot.task_kind != expected_task_kind
                or task_snapshot.evaluation_strategy_id != strategy.strategy_id
                or task_snapshot.evaluation_strategy_version
                != strategy.strategy_version
                or task_snapshot.rubric_id != strategy.rubric_id
                or task_snapshot.rubric_version != strategy.rubric_version
                or request.rubric_id != strategy.rubric_id
                or request.rubric_version != strategy.rubric_version
                or task_snapshot.scope_layer != "book"
                or task_snapshot.book_id != request.book_id
                or workspace is None
                or task_snapshot.workspace_lock_version != submission.workspace_lock_version
                or task_snapshot.workspace_work_cycle_id != submission.work_cycle_id
                or workspace.work_cycle_id != submission.work_cycle_id
                or task_snapshot.source_book_candidate_review_id
                != workspace.active_repair_review_id
                or task_snapshot.book_baseline_id != submission.base_book_baseline_id
                or task_snapshot.canon_baseline_id != submission.canon_baseline_id
                or workspace.id != submission.workspace_id
                or workspace.lock_version != submission.workspace_lock_version
            ):
                raise CommandPreconditionError("Evaluator result or Book submission is stale.")
            precheck_ref = await session.content.put(
                project_id=request.project_id,
                prepared=prepared_precheck,
                semantic_kind="book.review_precheck",
                media_type="application/json",
                schema_id="book-review-precheck",
                schema_version=1,
                ref_id=precheck_ref_id,
                created_at_ms=timestamp,
            )
            repair_reference: str | None = None
            if prepared_repair is not None:
                repair_reference = (
                    await session.content.put(
                        project_id=request.project_id,
                        prepared=prepared_repair,
                        semantic_kind="book.repair_contract",
                        media_type="application/json",
                        schema_id="book-repair-contract",
                        schema_version=1,
                        ref_id=repair_ref_id,
                        created_at_ms=timestamp,
                    )
                ).id
            await session.books.insert_review(
                BookReviewRecord(
                    id=review_id,
                    project_id=request.project_id,
                    book_id=request.book_id,
                    submission_id=request.submission_id,
                    evaluator_task_id=request.evaluator_task_id,
                    evaluator_attempt_id=request.evaluator_attempt_id,
                    decision=evaluation.decision,
                    rubric_id=request.rubric_id,
                    rubric_version=request.rubric_version,
                    precheck_ref_id=precheck_ref.id,
                    detail_ref_id=task_snapshot.result_ref_id,
                    repair_contract_ref_id=repair_reference,
                    created_at_ms=timestamp,
                )
            )
            if evaluation.decision != "pass":
                if not await session.books.close_submission(
                    project_id=request.project_id,
                    submission_id=request.submission_id,
                    disposition="rejected",
                    reason_code=evaluation.decision,
                    closed_at_ms=timestamp,
                ):
                    raise CommandPreconditionError("Book submission could not be closed.")
                state = "active" if evaluation.decision == "local_repair" else "blocked_by_user"
                updated_workspace = replace(
                    workspace,
                    state=state,
                    lock_version=workspace.lock_version + 1,
                    active_repair_review_id=(
                        review_id
                        if evaluation.decision == "local_repair"
                        else None
                    ),
                    updated_at_ms=timestamp,
                )
                if not await session.books.compare_and_set_workspace(
                    record=updated_workspace,
                    expected_lock_version=workspace.lock_version,
                ):
                    raise CommandPreconditionError("Book review workspace CAS failed.")
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
                        run_id=task_snapshot.run_id,
                        task_id=task_snapshot.task_id,
                        failure_code="semantic_repair_exhausted",
                        failure_ref_id=failure_ref.id,
                        now_ms=timestamp,
                    ):
                        raise CommandPreconditionError("Run cannot pause at repair exhaustion.")
            if evaluation.decision in {"pass", "needs_user"}:
                reason_code = (
                    "book_approval_required"
                    if evaluation.decision == "pass"
                    else "book_review_needs_user"
                )
                if not await session.runs.ensure_wait_for_user(
                    run_id=task_snapshot.run_id,
                    reason_code=reason_code,
                    now_ms=timestamp,
                ):
                    raise CommandPreconditionError("Run could not enter the Book review wait.")
            if not await session.execution.mark_delivery_applied(
                project_id=request.project_id,
                task_id=request.evaluator_task_id,
                attempt_id=request.evaluator_attempt_id,
                command_id=envelope.command_id,
                updated_at_ms=timestamp,
            ):
                raise CommandPreconditionError("Evaluator delivery was already consumed.")
            result = RecordBookReviewResult(
                project_id=request.project_id,
                book_id=request.book_id,
                submission_id=request.submission_id,
                review_id=review_id,
                decision=evaluation.decision,
            )
            return CommandEffect(
                result=result,
                events=(
                    EventDraft(
                        event_type="book.reviewed",
                        aggregate_type="book",
                        aggregate_id=request.book_id,
                        payload={
                            "submission_id": request.submission_id,
                            "review_id": review_id,
                            "decision": evaluation.decision,
                        },
                    ),
                ),
            )

        return await self._command_bus.execute(
            envelope=envelope,
            result_type=RecordBookReviewResult,
            handler=handler,
        )

    async def approve_and_commit(
        self,
        request: ApproveBookRequest,
        *,
        idempotency_key: str,
    ) -> CommandExecution[ApproveBookResult]:
        timestamp = self._now_ms()
        approval_id = self._id_factory()
        baseline_id = self._id_factory()
        discussion_ref_id = self._id_factory()
        async with self._command_bus.read_unit_of_work() as read_session:
            submission_snapshot = await read_session.books.get_submission(
                project_id=request.project_id,
                submission_id=request.submission_id,
            )
            if submission_snapshot is None:
                raise CommandPreconditionError("Book submission does not exist.")
            workspace_snapshot = await read_session.books.get_workspace(
                project_id=request.project_id,
                book_id=request.book_id,
            )
            if workspace_snapshot is None:
                raise CommandPreconditionError("Book workspace does not exist.")
            packed_titles = await read_session.content.get_packed(
                project_id=request.project_id,
                ref_id=submission_snapshot.titles_ref_id,
            )
            packed_contract = await read_session.content.get_packed(
                project_id=request.project_id,
                ref_id=submission_snapshot.completion_contract_ref_id,
            )
            packed_topology = await read_session.content.get_packed(
                project_id=request.project_id,
                ref_id=submission_snapshot.arc_topology_ref_id,
            )
            packed_discussion = await read_session.content.get_packed(
                project_id=request.project_id,
                ref_id=workspace_snapshot.discussion_state_ref_id,
            )
        title_payload = json.loads(packed_titles.unpack_and_verify())
        if not isinstance(title_payload, dict):
            raise CommandPreconditionError("Reviewed title payload is malformed.")
        selected_title = title_payload.get("selected_title")
        title_source = title_payload.get("title_source")
        if (
            not isinstance(selected_title, str)
            or not selected_title.strip()
            or title_source not in {"recommended", "custom"}
        ):
            raise CommandPreconditionError("Reviewed title payload contains an invalid title.")
        CompletionContract.model_validate_json(packed_contract.unpack_and_verify())
        topology = BookArcTopology.model_validate_json(
            packed_topology.unpack_and_verify()
        )
        discussion = BookDiscussionState.model_validate_json(
            packed_discussion.unpack_and_verify()
        ).model_copy(
            update={
                "selected_title": selected_title,
                "selected_title_source": title_source,
                "question": None,
                "suggestions": [],
                "readiness_status": "ready",
                "readiness_reason": "The reviewed Book candidate was explicitly approved.",
            }
        )
        prepared_discussion = prepare_canonical_json(discussion)
        envelope = self._envelope(
            request=request,
            project_id=request.project_id,
            idempotency_key=idempotency_key,
            command_kind="approve_and_commit_book_baseline",
            actor="user",
            created_at_ms=timestamp,
        )

        async def handler(session: StoreSession) -> CommandEffect[ApproveBookResult]:
            project = await session.projects.get(request.project_id)
            book = await session.books.get_for_project(request.project_id)
            submission = await session.books.get_submission(
                project_id=request.project_id,
                submission_id=request.submission_id,
            )
            review = await session.books.get_review(
                project_id=request.project_id,
                review_id=request.review_id,
            )
            workspace = await session.books.get_workspace(
                project_id=request.project_id,
                book_id=request.book_id,
            )
            if (
                project is None
                or book is None
                or book.id != request.book_id
                or book.current_baseline_id != request.expected_current_baseline_id
                or submission != submission_snapshot
                or submission.disposition != "pending"
                or submission.canon_baseline_id != project.current_canon_baseline_id
                or review is None
                or review.submission_id != submission.id
                or review.decision != "pass"
                or workspace is None
                or workspace != workspace_snapshot
                or workspace.id != submission.workspace_id
                or workspace.lock_version != submission.workspace_lock_version
                or workspace.base_book_baseline_id != request.expected_current_baseline_id
            ):
                raise CommandPreconditionError("Book approval facts are stale or incomplete.")
            baseline_version = await session.books.next_baseline_version(book_id=request.book_id)
            if request.expected_current_baseline_id is None:
                expected_version = 1
            else:
                current_version = await session.books.get_baseline_version(
                    project_id=request.project_id,
                    book_id=request.book_id,
                    baseline_id=request.expected_current_baseline_id,
                )
                if current_version is None:
                    raise CommandPreconditionError("Book current baseline identity is invalid.")
                expected_version = current_version + 1
            if baseline_version != expected_version:
                raise CommandPreconditionError("Book baseline version does not follow current head.")
            topology_effective_after = await _topology_effective_after_arc_ordinal(
                session,
                project_id=request.project_id,
                book_id=request.book_id,
                base_book_baseline_id=request.expected_current_baseline_id,
            )
            current_topology = await _load_book_arc_topology(
                session,
                project_id=request.project_id,
                book_id=request.book_id,
                baseline_id=request.expected_current_baseline_id,
            )
            expected_topology = _compose_book_arc_topology(
                current=current_topology,
                effective_after_arc_ordinal=topology_effective_after,
                suffix=BookArcTopologySuffix(
                    arcs=topology.arcs[topology_effective_after:]
                ),
            )
            if topology != expected_topology:
                raise CommandPreconditionError(
                    "Book successor topology changed the frozen historical prefix."
                )
            await session.books.insert_approval(
                BookApprovalRecord(
                    id=approval_id,
                    project_id=request.project_id,
                    book_id=request.book_id,
                    submission_id=submission.id,
                    review_id=review.id,
                    decision="approved",
                    selected_title=selected_title,
                    title_source=title_source,
                    created_at_ms=timestamp,
                )
            )
            await session.books.insert_baseline(
                BookBaselineRecord(
                    id=baseline_id,
                    project_id=request.project_id,
                    book_id=request.book_id,
                    baseline_version=baseline_version,
                    parent_baseline_id=request.expected_current_baseline_id,
                    submission_id=submission.id,
                    review_id=review.id,
                    approval_id=approval_id,
                    approved_title=selected_title,
                    title_source=title_source,
                    direction_ref_id=submission.direction_ref_id,
                    constraints_ref_id=submission.constraints_ref_id,
                    rolling_plan_ref_id=submission.rolling_plan_ref_id,
                    completion_contract_ref_id=submission.completion_contract_ref_id,
                    arc_topology_ref_id=submission.arc_topology_ref_id,
                    arc_contract_count=len(topology.arcs),
                    final_arc_ordinal=len(topology.arcs),
                    topology_effective_after_arc_ordinal=(
                        topology_effective_after
                    ),
                    created_at_ms=timestamp,
                )
            )
            if not await session.books.compare_and_set_current_baseline(
                project_id=request.project_id,
                book_id=request.book_id,
                expected_baseline_id=request.expected_current_baseline_id,
                new_baseline_id=baseline_id,
                updated_at_ms=timestamp,
            ):
                raise CommandPreconditionError("Book current baseline CAS failed.")
            if not await session.books.close_submission(
                project_id=request.project_id,
                submission_id=submission.id,
                disposition="promoted",
                reason_code="baseline_committed",
                closed_at_ms=timestamp,
            ):
                raise CommandPreconditionError("Book submission promotion failed.")
            discussion_ref = await session.content.put(
                project_id=request.project_id,
                prepared=prepared_discussion,
                semantic_kind="book.discussion_state",
                media_type="application/json",
                schema_id="book-discussion-state",
                schema_version=1,
                ref_id=discussion_ref_id,
                created_at_ms=timestamp,
            )
            updated_workspace = replace(
                workspace,
                state="idle",
                lock_version=workspace.lock_version + 1,
                active_repair_review_id=None,
                base_book_baseline_id=baseline_id,
                base_canon_baseline_id=submission.canon_baseline_id,
                direction_draft_ref_id=submission.direction_ref_id,
                discussion_state_ref_id=discussion_ref.id,
                readiness_status="ready",
                guidance_ref_id=None,
                semantic_repair_count=0,
                stale_reason_code=None,
                stale_at_ms=None,
                updated_at_ms=timestamp,
            )
            if not await session.books.compare_and_set_workspace(
                record=updated_workspace,
                expected_lock_version=workspace.lock_version,
            ):
                raise CommandPreconditionError("Book workspace reset CAS failed.")
            run = await session.runs.get_open_for_project(request.project_id)
            if run is not None and run.status == "waiting_for_user":
                if not await session.runs.start_waiting_run(
                    project_id=request.project_id,
                    run_id=run.id,
                    expected_lock_version=run.lock_version,
                    now_ms=timestamp,
                ):
                    raise CommandPreconditionError("Book approval could not wake the Run.")
            resolved_arc_requests = await session.changes.resolve_for_book_baseline(
                project_id=request.project_id,
                book_id=request.book_id,
                previous_baseline_id=request.expected_current_baseline_id,
                new_baseline_id=baseline_id,
                now_ms=timestamp,
            )
            result = ApproveBookResult(
                project_id=request.project_id,
                book_id=request.book_id,
                baseline_id=baseline_id,
                baseline_version=baseline_version,
                approved_title=selected_title,
            )
            events = [
                EventDraft(
                        event_type="book.baseline_committed",
                        aggregate_type="book",
                        aggregate_id=request.book_id,
                        payload={
                            "baseline_id": baseline_id,
                            "baseline_version": baseline_version,
                            "approved_title": selected_title,
                        },
                    )
            ]
            if resolved_arc_requests:
                events.append(
                    EventDraft(
                        event_type="change_request.resolved",
                        aggregate_type="book",
                        aggregate_id=request.book_id,
                        payload={
                            "book_baseline_id": baseline_id,
                            "arc_request_count": resolved_arc_requests,
                        },
                    )
                )
            return CommandEffect(result=result, events=tuple(events))

        return await self._command_bus.execute(
            envelope=envelope,
            result_type=ApproveBookResult,
            handler=handler,
        )
