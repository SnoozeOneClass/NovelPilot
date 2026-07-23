from __future__ import annotations

import json
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from typing import cast

from pydantic import BaseModel, TypeAdapter

from app.agents.contracts import (
    ChapterDraftResult,
    ChapterObservationResult,
    ChapterPlanProposal,
    LayerEvaluationResult,
)
from app.db.uow import StoreSession
from app.domain.chapter.canon import (
    CANON_CATEGORIES,
    AppliedCanonPatch,
    BoundCanonPatch,
    CanonCategory,
    CanonEntry,
    apply_canon_patch,
    bind_canon_patch,
    canon_manifest_fingerprint,
)
from app.domain.chapter.contracts import (
    ApplyChapterTaskRequest,
    ApplyChapterTaskResult,
    ChapterComponent,
    ChapterReviewDecision,
    CommitChapterRequest,
    CommitChapterResult,
    CreateChapterRequest,
    CreateChapterResult,
    RecordChapterReviewRequest,
    RecordChapterReviewResult,
    RebaseStaleChapterRequest,
    RebaseStaleChapterResult,
    SubmitChapterRequest,
    SubmitChapterResult,
)
from app.domain.commands import (
    Actor,
    CommandEffect,
    CommandEnvelope,
    CommandExecution,
    CommandPreconditionError,
    EventDraft,
)
from app.store.canon import CanonBaselineRecord
from app.store.chapters import (
    ChapterBaselineRecord,
    ChapterChangeRequestRecord,
    ChapterRecord,
    ChapterReviewRecord,
    ChapterSubmissionRecord,
    ChapterWorkspaceRecord,
)
from app.store.command_bus import CommandBus
from app.store.content import PreparedContent, prepare_canonical_json, prepare_exact_text
from app.store.execution import SuccessfulTaskRecord


class ChapterNotFoundError(LookupError):
    pass


ComponentMutator = Callable[
    [ChapterWorkspaceRecord, Sequence[str], int],
    ChapterWorkspaceRecord,
]
ComponentPrecondition = Callable[
    [StoreSession, ChapterWorkspaceRecord],
    Awaitable[None],
]


@dataclass(frozen=True, slots=True)
class _PreparedCanonCommit:
    before: CanonBaselineRecord
    applied: AppliedCanonPatch
    prepared_categories: dict[CanonCategory, PreparedContent]


class ChapterCommandService:
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

    async def create_chapter(
        self,
        request: CreateChapterRequest,
        *,
        idempotency_key: str,
    ) -> CommandExecution[CreateChapterResult]:
        timestamp = self._now_ms()
        chapter_id = self._id_factory()
        workspace_id = self._id_factory()
        envelope = self._envelope(
            request=request,
            project_id=request.project_id,
            idempotency_key=idempotency_key,
            command_kind="create_chapter",
            actor="engine",
            created_at_ms=timestamp,
        )

        async def handler(session: StoreSession) -> CommandEffect[CreateChapterResult]:
            context = await session.chapters.get_active_arc_context(
                project_id=request.project_id,
                book_id=request.book_id,
                arc_id=request.arc_id,
            )
            if (
                context is None
                or context.book_baseline_id != request.expected_book_baseline_id
                or context.arc_baseline_id != request.expected_arc_baseline_id
                or context.canon_baseline_id != request.expected_canon_baseline_id
            ):
                raise CommandPreconditionError("Chapter dependencies are no longer current.")
            committed = await session.chapters.count_committed(arc_id=request.arc_id)
            if committed >= context.target_chapter_count:
                raise CommandPreconditionError("The current Arc has reached its Chapter target.")
            book_ordinal, arc_ordinal = await session.chapters.next_ordinals(
                book_id=request.book_id,
                arc_id=request.arc_id,
            )
            await session.chapters.insert(
                ChapterRecord(
                    id=chapter_id,
                    project_id=request.project_id,
                    book_id=request.book_id,
                    arc_id=request.arc_id,
                    book_ordinal=book_ordinal,
                    arc_ordinal=arc_ordinal,
                    lifecycle_status="drafting",
                    current_baseline_id=None,
                    created_at_ms=timestamp,
                    updated_at_ms=timestamp,
                    committed_at_ms=None,
                )
            )
            await session.chapters.insert_workspace(
                ChapterWorkspaceRecord(
                    id=workspace_id,
                    project_id=request.project_id,
                    book_id=request.book_id,
                    arc_id=request.arc_id,
                    chapter_id=chapter_id,
                    state="active",
                    lock_version=1,
                    base_chapter_baseline_id=None,
                    book_baseline_id=context.book_baseline_id,
                    arc_baseline_id=context.arc_baseline_id,
                    canon_baseline_id=context.canon_baseline_id,
                    plan_ref_id=None,
                    draft_ref_id=None,
                    observations_ref_id=None,
                    candidate_canon_patch_ref_id=None,
                    repair_policy_id="semantic-repair-v1",
                    semantic_repair_count=0,
                    semantic_repair_limit=5,
                    stale_reason_code=None,
                    stale_at_ms=None,
                    created_at_ms=timestamp,
                    updated_at_ms=timestamp,
                )
            )
            result = CreateChapterResult(
                project_id=request.project_id,
                chapter_id=chapter_id,
                workspace_id=workspace_id,
                book_ordinal=book_ordinal,
                arc_ordinal=arc_ordinal,
                workspace_lock_version=1,
            )
            return CommandEffect(
                result=result,
                events=(
                    EventDraft(
                        event_type="chapter.created",
                        aggregate_type="chapter",
                        aggregate_id=chapter_id,
                        payload={
                            "book_id": request.book_id,
                            "arc_id": request.arc_id,
                            "book_ordinal": book_ordinal,
                            "arc_ordinal": arc_ordinal,
                        },
                    ),
                ),
            )

        return await self._command_bus.execute(
            envelope=envelope,
            result_type=CreateChapterResult,
            handler=handler,
        )

    async def rebase_stale_workspace(
        self,
        request: RebaseStaleChapterRequest,
        *,
        idempotency_key: str,
    ) -> CommandExecution[RebaseStaleChapterResult]:
        timestamp = self._now_ms()
        envelope = self._envelope(
            request=request,
            project_id=request.project_id,
            idempotency_key=idempotency_key,
            command_kind="rebase_stale_chapter_workspace",
            actor="engine",
            created_at_ms=timestamp,
        )

        async def handler(
            session: StoreSession,
        ) -> CommandEffect[RebaseStaleChapterResult]:
            project = await session.projects.get(request.project_id)
            book = await session.books.get_for_project(request.project_id)
            arc = await session.arcs.get(
                project_id=request.project_id,
                arc_id=request.arc_id,
            )
            chapter = await session.chapters.get(
                project_id=request.project_id,
                chapter_id=request.chapter_id,
            )
            workspace = await session.chapters.get_workspace(
                project_id=request.project_id,
                chapter_id=request.chapter_id,
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
                or chapter is None
                or chapter.book_id != request.book_id
                or chapter.arc_id != request.arc_id
                or chapter.current_baseline_id
                != request.expected_chapter_baseline_id
                or workspace is None
                or workspace.state != "stale"
                or workspace.lock_version != request.expected_workspace_lock_version
            ):
                raise CommandPreconditionError("Stale Chapter rebase dependencies changed.")
            pending = await session.chapters.find_pending_submission(
                project_id=request.project_id,
                chapter_id=request.chapter_id,
            )
            if pending is not None and not await session.chapters.close_submission(
                project_id=request.project_id,
                submission_id=pending.id,
                disposition="superseded",
                reason_code="stale_workspace_rebased",
                closed_at_ms=timestamp,
            ):
                raise CommandPreconditionError("Stale Chapter submission changed.")
            updated = replace(
                workspace,
                state="active",
                lock_version=workspace.lock_version + 1,
                base_chapter_baseline_id=chapter.current_baseline_id,
                book_baseline_id=request.expected_book_baseline_id,
                arc_baseline_id=request.expected_arc_baseline_id,
                canon_baseline_id=request.expected_canon_baseline_id,
                plan_ref_id=None,
                draft_ref_id=None,
                observations_ref_id=None,
                candidate_canon_patch_ref_id=None,
                semantic_repair_count=0,
                stale_reason_code=None,
                stale_at_ms=None,
                updated_at_ms=timestamp,
            )
            if not await session.chapters.compare_and_set_workspace(
                record=updated,
                expected_lock_version=workspace.lock_version,
            ):
                raise CommandPreconditionError(
                    "Stale Chapter workspace rebase CAS failed."
                )
            result = RebaseStaleChapterResult(
                project_id=request.project_id,
                chapter_id=request.chapter_id,
                workspace_lock_version=updated.lock_version,
                base_chapter_baseline_id=updated.base_chapter_baseline_id,
                book_baseline_id=updated.book_baseline_id,
                arc_baseline_id=updated.arc_baseline_id,
                canon_baseline_id=updated.canon_baseline_id,
            )
            return CommandEffect(
                result=result,
                events=(
                    EventDraft(
                        event_type="chapter.workspace_rebased",
                        aggregate_type="chapter",
                        aggregate_id=request.chapter_id,
                        payload={
                            "workspace_lock_version": updated.lock_version,
                            "book_baseline_id": updated.book_baseline_id,
                            "arc_baseline_id": updated.arc_baseline_id,
                            "chapter_baseline_id": updated.base_chapter_baseline_id,
                            "canon_baseline_id": updated.canon_baseline_id,
                        },
                    ),
                ),
            )

        return await self._command_bus.execute(
            envelope=envelope,
            result_type=RebaseStaleChapterResult,
            handler=handler,
        )

    async def apply_plan_result(
        self,
        request: ApplyChapterTaskRequest,
        *,
        idempotency_key: str,
    ) -> CommandExecution[ApplyChapterTaskResult]:
        return await self._apply_plan_like_result(
            request,
            expected_task_kind="chapter.plan",
            idempotency_key=idempotency_key,
        )

    async def apply_revision_plan_result(
        self,
        request: ApplyChapterTaskRequest,
        *,
        idempotency_key: str,
    ) -> CommandExecution[ApplyChapterTaskResult]:
        return await self._apply_plan_like_result(
            request,
            expected_task_kind="chapter.revise.plan",
            idempotency_key=idempotency_key,
        )

    async def _apply_plan_like_result(
        self,
        request: ApplyChapterTaskRequest,
        *,
        expected_task_kind: str,
        idempotency_key: str,
    ) -> CommandExecution[ApplyChapterTaskResult]:
        task, raw = await self._read_successful_result(request)
        plan = ChapterPlanProposal.model_validate_json(raw)

        def mutate(
            workspace: ChapterWorkspaceRecord,
            refs: Sequence[str],
            timestamp: int,
        ) -> ChapterWorkspaceRecord:
            return replace(
                workspace,
                state="active",
                lock_version=workspace.lock_version + 1,
                plan_ref_id=refs[0],
                draft_ref_id=None,
                observations_ref_id=None,
                candidate_canon_patch_ref_id=None,
                updated_at_ms=timestamp,
            )

        return await self._apply_prepared_task(
            request=request,
            task=task,
            expected_task_kind=expected_task_kind,
            component="plan",
            prepared=(prepare_canonical_json(plan),),
            descriptors=(("chapter.plan", "application/json", "chapter-plan", 1),),
            mutate=mutate,
            idempotency_key=idempotency_key,
        )

    async def apply_draft_result(
        self,
        request: ApplyChapterTaskRequest,
        *,
        idempotency_key: str,
    ) -> CommandExecution[ApplyChapterTaskResult]:
        return await self._apply_draft_like_result(
            request,
            expected_task_kind="chapter.draft",
            idempotency_key=idempotency_key,
        )

    async def apply_revision_draft_result(
        self,
        request: ApplyChapterTaskRequest,
        *,
        idempotency_key: str,
    ) -> CommandExecution[ApplyChapterTaskResult]:
        return await self._apply_draft_like_result(
            request,
            expected_task_kind="chapter.revise.draft",
            idempotency_key=idempotency_key,
        )

    async def _apply_draft_like_result(
        self,
        request: ApplyChapterTaskRequest,
        *,
        expected_task_kind: str,
        idempotency_key: str,
    ) -> CommandExecution[ApplyChapterTaskResult]:
        task, raw = await self._read_successful_result(request)
        draft = ChapterDraftResult.model_validate_json(raw)

        def mutate(
            workspace: ChapterWorkspaceRecord,
            refs: Sequence[str],
            timestamp: int,
        ) -> ChapterWorkspaceRecord:
            if workspace.plan_ref_id is None:
                raise CommandPreconditionError("Chapter draft has no frozen plan dependency.")
            return replace(
                workspace,
                state="active",
                lock_version=workspace.lock_version + 1,
                draft_ref_id=refs[0],
                observations_ref_id=None,
                candidate_canon_patch_ref_id=None,
                updated_at_ms=timestamp,
            )

        return await self._apply_prepared_task(
            request=request,
            task=task,
            expected_task_kind=expected_task_kind,
            component="draft",
            prepared=(prepare_exact_text(draft.prose),),
            descriptors=(("chapter.prose", "text/plain; charset=utf-8", None, None),),
            mutate=mutate,
            idempotency_key=idempotency_key,
        )

    async def apply_observation_result(
        self,
        request: ApplyChapterTaskRequest,
        *,
        idempotency_key: str,
    ) -> CommandExecution[ApplyChapterTaskResult]:
        return await self._apply_observation_like_result(
            request,
            expected_task_kind="chapter.observe",
            idempotency_key=idempotency_key,
        )

    async def apply_revision_observation_result(
        self,
        request: ApplyChapterTaskRequest,
        *,
        idempotency_key: str,
    ) -> CommandExecution[ApplyChapterTaskResult]:
        return await self._apply_observation_like_result(
            request,
            expected_task_kind="chapter.revise.observe",
            idempotency_key=idempotency_key,
        )

    async def _apply_observation_like_result(
        self,
        request: ApplyChapterTaskRequest,
        *,
        expected_task_kind: str,
        idempotency_key: str,
    ) -> CommandExecution[ApplyChapterTaskResult]:
        task, raw = await self._read_successful_result(request)
        observations = ChapterObservationResult.model_validate_json(raw)
        async with self._command_bus.read_unit_of_work() as session:
            workspace = await session.chapters.get_workspace(
                project_id=request.project_id,
                chapter_id=request.chapter_id,
            )
            if workspace is None or workspace.draft_ref_id is None:
                raise CommandPreconditionError("Chapter observations have no frozen prose.")
            prose = (
                await session.content.get_packed(
                    project_id=request.project_id,
                    ref_id=workspace.draft_ref_id,
                )
            ).unpack_and_verify().decode("utf-8")
        patch = bind_canon_patch(
            chapter_id=request.chapter_id,
            prose=prose,
            observations=observations,
        )

        def mutate(
            current: ChapterWorkspaceRecord,
            refs: Sequence[str],
            timestamp: int,
        ) -> ChapterWorkspaceRecord:
            if current.draft_ref_id != workspace.draft_ref_id:
                raise CommandPreconditionError("Chapter prose changed while binding observations.")
            return replace(
                current,
                state="active",
                lock_version=current.lock_version + 1,
                observations_ref_id=refs[0],
                candidate_canon_patch_ref_id=refs[1],
                updated_at_ms=timestamp,
            )

        return await self._apply_prepared_task(
            request=request,
            task=task,
            expected_task_kind=expected_task_kind,
            component="observations",
            prepared=(prepare_canonical_json(observations), prepare_canonical_json(patch)),
            descriptors=(
                (
                    "chapter.observations",
                    "application/json",
                    "chapter-observations",
                    1,
                ),
                (
                    "chapter.candidate_canon_patch",
                    "application/json",
                    "chapter-canon-patch",
                    1,
                ),
            ),
            mutate=mutate,
            idempotency_key=idempotency_key,
        )

    async def apply_repair_result(
        self,
        request: ApplyChapterTaskRequest,
        *,
        idempotency_key: str,
    ) -> CommandExecution[ApplyChapterTaskResult]:
        task, raw = await self._read_successful_result(request)
        async with self._command_bus.read_unit_of_work() as session:
            workspace_snapshot = await session.chapters.get_workspace(
                project_id=request.project_id,
                chapter_id=request.chapter_id,
            )
            review_snapshot = await session.chapters.get_latest_review(
                project_id=request.project_id,
                chapter_id=request.chapter_id,
            )
            if (
                workspace_snapshot is None
                or review_snapshot is None
                or review_snapshot.decision != "local_repair"
                or review_snapshot.repair_contract_ref_id is None
            ):
                raise CommandPreconditionError("Chapter has no active local repair contract.")
            repair_contract = json.loads(
                (
                    await session.content.get_packed(
                        project_id=request.project_id,
                        ref_id=review_snapshot.repair_contract_ref_id,
                    )
                ).unpack_and_verify()
            )
        scope = repair_contract.get("repair_scope")
        if not isinstance(scope, list) or any(not isinstance(item, str) for item in scope):
            raise CommandPreconditionError("Chapter repair contract has an invalid scope.")
        allowed_scope = set(scope)
        prepared: Sequence[PreparedContent]
        descriptors: Sequence[tuple[str, str, str | None, int | None]]
        if task.task_kind == "chapter.repair.prose":
            if "prose" not in allowed_scope:
                raise CommandPreconditionError("Repair contract does not authorize prose changes.")
            draft = ChapterDraftResult.model_validate_json(raw)
            component: ChapterComponent = "repair_prose"
            prepared = (prepare_exact_text(draft.prose),)
            descriptors = (("chapter.prose", "text/plain; charset=utf-8", None, None),)

            def mutate(
                workspace: ChapterWorkspaceRecord,
                refs: Sequence[str],
                timestamp: int,
            ) -> ChapterWorkspaceRecord:
                _require_repair_budget(workspace)
                return replace(
                    workspace,
                    state="active",
                    lock_version=workspace.lock_version + 1,
                    draft_ref_id=refs[0],
                    observations_ref_id=None,
                    candidate_canon_patch_ref_id=None,
                    semantic_repair_count=workspace.semantic_repair_count + 1,
                    updated_at_ms=timestamp,
                )

        elif task.task_kind == "chapter.repair.observation":
            if not allowed_scope.intersection({"observations", "canon"}):
                raise CommandPreconditionError(
                    "Repair contract does not authorize observation or Canon changes."
                )
            if workspace_snapshot.draft_ref_id is None:
                raise CommandPreconditionError("Observation repair has no frozen prose.")
            observations = ChapterObservationResult.model_validate_json(raw)
            async with self._command_bus.read_unit_of_work() as session:
                prose = (
                    await session.content.get_packed(
                        project_id=request.project_id,
                        ref_id=workspace_snapshot.draft_ref_id,
                    )
                ).unpack_and_verify().decode("utf-8")
            patch = bind_canon_patch(
                chapter_id=request.chapter_id,
                prose=prose,
                observations=observations,
            )
            component = "repair_observations"
            prepared = (prepare_canonical_json(observations), prepare_canonical_json(patch))
            descriptors = (
                (
                    "chapter.observations",
                    "application/json",
                    "chapter-observations",
                    1,
                ),
                (
                    "chapter.candidate_canon_patch",
                    "application/json",
                    "chapter-canon-patch",
                    1,
                ),
            )

            def mutate(
                workspace: ChapterWorkspaceRecord,
                refs: Sequence[str],
                timestamp: int,
            ) -> ChapterWorkspaceRecord:
                _require_repair_budget(workspace)
                if workspace.draft_ref_id != workspace_snapshot.draft_ref_id:
                    raise CommandPreconditionError("Chapter prose changed before repair delivery.")
                return replace(
                    workspace,
                    state="active",
                    lock_version=workspace.lock_version + 1,
                    observations_ref_id=refs[0],
                    candidate_canon_patch_ref_id=refs[1],
                    semantic_repair_count=workspace.semantic_repair_count + 1,
                    updated_at_ms=timestamp,
                )

        else:
            raise CommandPreconditionError("Task is not a Chapter repair task.")

        async def validate_repair(
            session: StoreSession,
            workspace: ChapterWorkspaceRecord,
        ) -> None:
            latest = await session.chapters.get_latest_review(
                project_id=request.project_id,
                chapter_id=request.chapter_id,
            )
            if latest != review_snapshot or workspace.state != "active":
                raise CommandPreconditionError("Chapter repair authorization is no longer current.")

        return await self._apply_prepared_task(
            request=request,
            task=task,
            expected_task_kind=task.task_kind,
            component=component,
            prepared=prepared,
            descriptors=descriptors,
            mutate=mutate,
            precondition=validate_repair,
            idempotency_key=idempotency_key,
        )

    async def _apply_prepared_task(
        self,
        *,
        request: ApplyChapterTaskRequest,
        task: SuccessfulTaskRecord,
        expected_task_kind: str,
        component: ChapterComponent,
        prepared: Sequence[PreparedContent],
        descriptors: Sequence[tuple[str, str, str | None, int | None]],
        mutate: ComponentMutator,
        precondition: ComponentPrecondition | None = None,
        idempotency_key: str,
    ) -> CommandExecution[ApplyChapterTaskResult]:
        timestamp = self._now_ms()
        envelope = self._envelope(
            request=request,
            project_id=request.project_id,
            idempotency_key=idempotency_key,
            command_kind=f"apply_{expected_task_kind.replace('.', '_')}_result",
            actor="engine",
            source_task_id=request.task_id,
            created_at_ms=timestamp,
        )
        ref_ids = [self._id_factory() for _ in prepared]

        async def handler(session: StoreSession) -> CommandEffect[ApplyChapterTaskResult]:
            current_task = await session.execution.get_successful_task(
                project_id=request.project_id,
                task_id=request.task_id,
                attempt_id=request.attempt_id,
            )
            workspace = await session.chapters.get_workspace(
                project_id=request.project_id,
                chapter_id=request.chapter_id,
            )
            if current_task is None or workspace is None:
                raise CommandPreconditionError("Chapter task or workspace no longer exists.")
            if current_task != task or task.task_kind != expected_task_kind:
                raise CommandPreconditionError("Agent task does not match this Chapter command.")
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
                    raise CommandPreconditionError("Stale task delivery changed concurrently.")
                result = ApplyChapterTaskResult(
                    project_id=request.project_id,
                    chapter_id=request.chapter_id,
                    task_id=request.task_id,
                    component=component,
                    delivery="discarded_stale",
                    workspace_lock_version=workspace.lock_version,
                )
                return CommandEffect(
                    result=result,
                    events=(
                        EventDraft(
                            event_type="chapter.task_result_discarded_stale",
                            aggregate_type="chapter",
                            aggregate_id=request.chapter_id,
                            payload={"task_id": request.task_id, "component": component},
                        ),
                    ),
                )
            if precondition is not None:
                await precondition(session, workspace)

            pending = await session.chapters.find_pending_submission(
                project_id=request.project_id,
                chapter_id=request.chapter_id,
            )
            if pending is not None:
                await session.chapters.close_submission(
                    project_id=request.project_id,
                    submission_id=pending.id,
                    disposition="superseded",
                    reason_code="workspace_edited",
                    closed_at_ms=timestamp,
                )
            references = [
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
            updated = mutate(workspace, [reference.id for reference in references], timestamp)
            if not await session.chapters.compare_and_set_workspace(
                record=updated,
                expected_lock_version=workspace.lock_version,
            ):
                raise CommandPreconditionError("Chapter workspace CAS failed.")
            if not await session.execution.mark_delivery_applied(
                project_id=request.project_id,
                task_id=request.task_id,
                attempt_id=request.attempt_id,
                command_id=envelope.command_id,
                updated_at_ms=timestamp,
            ):
                raise CommandPreconditionError("Task delivery is no longer pending.")
            result = ApplyChapterTaskResult(
                project_id=request.project_id,
                chapter_id=request.chapter_id,
                task_id=request.task_id,
                component=component,
                delivery="applied",
                workspace_lock_version=updated.lock_version,
            )
            return CommandEffect(
                result=result,
                events=(
                    EventDraft(
                        event_type="chapter.workspace_updated",
                        aggregate_type="chapter",
                        aggregate_id=request.chapter_id,
                        payload={
                            "component": component,
                            "workspace_lock_version": updated.lock_version,
                            "task_id": request.task_id,
                        },
                    ),
                ),
            )

        return await self._command_bus.execute(
            envelope=envelope,
            result_type=ApplyChapterTaskResult,
            handler=handler,
        )

    async def _read_successful_result(
        self, request: ApplyChapterTaskRequest
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

    async def submit_for_review(
        self,
        request: SubmitChapterRequest,
        *,
        idempotency_key: str,
    ) -> CommandExecution[SubmitChapterResult]:
        timestamp = self._now_ms()
        submission_id = self._id_factory()
        manifest_ref_id = self._id_factory()
        async with self._command_bus.read_unit_of_work() as session:
            workspace = await session.chapters.get_workspace(
                project_id=request.project_id,
                chapter_id=request.chapter_id,
            )
        if workspace is None:
            raise ChapterNotFoundError(request.chapter_id)
        required = (
            workspace.plan_ref_id,
            workspace.draft_ref_id,
            workspace.observations_ref_id,
            workspace.candidate_canon_patch_ref_id,
        )
        if (
            workspace.lock_version != request.expected_workspace_lock_version
            or workspace.state != "active"
            or any(reference is None for reference in required)
        ):
            raise CommandPreconditionError("Chapter workspace is not complete for review.")
        manifest = {
            "schema": "chapter-review-manifest-v1",
            "workspace_id": workspace.id,
            "workspace_lock_version": workspace.lock_version,
            "base_chapter_baseline_id": workspace.base_chapter_baseline_id,
            "book_baseline_id": workspace.book_baseline_id,
            "arc_baseline_id": workspace.arc_baseline_id,
            "canon_before_id": workspace.canon_baseline_id,
            "plan_ref_id": required[0],
            "draft_ref_id": required[1],
            "observations_ref_id": required[2],
            "candidate_canon_patch_ref_id": required[3],
        }
        prepared_manifest = prepare_canonical_json(manifest)
        envelope = self._envelope(
            request=request,
            project_id=request.project_id,
            idempotency_key=idempotency_key,
            command_kind="submit_chapter_for_review",
            actor="engine",
            created_at_ms=timestamp,
        )

        async def handler(session: StoreSession) -> CommandEffect[SubmitChapterResult]:
            current = await session.chapters.get_workspace(
                project_id=request.project_id,
                chapter_id=request.chapter_id,
            )
            if current != workspace:
                raise CommandPreconditionError("Chapter workspace changed during submission.")
            context = await session.chapters.get_active_arc_context(
                project_id=request.project_id,
                book_id=workspace.book_id,
                arc_id=workspace.arc_id,
            )
            if (
                context is None
                or context.book_baseline_id != workspace.book_baseline_id
                or context.arc_baseline_id != workspace.arc_baseline_id
                or context.canon_baseline_id != workspace.canon_baseline_id
                or await session.chapters.find_pending_submission(
                    project_id=request.project_id,
                    chapter_id=request.chapter_id,
                )
                is not None
            ):
                raise CommandPreconditionError("Chapter submission dependencies are stale.")
            manifest_ref = await session.content.put(
                project_id=request.project_id,
                prepared=prepared_manifest,
                semantic_kind="chapter.review_manifest",
                media_type="application/json",
                schema_id="chapter-review-manifest",
                schema_version=1,
                ref_id=manifest_ref_id,
                created_at_ms=timestamp,
            )
            assert all(reference is not None for reference in required)
            await session.chapters.insert_submission(
                ChapterSubmissionRecord(
                    id=submission_id,
                    project_id=request.project_id,
                    book_id=workspace.book_id,
                    arc_id=workspace.arc_id,
                    chapter_id=request.chapter_id,
                    workspace_id=workspace.id,
                    workspace_lock_version=workspace.lock_version,
                    base_chapter_baseline_id=workspace.base_chapter_baseline_id,
                    book_baseline_id=workspace.book_baseline_id,
                    arc_baseline_id=workspace.arc_baseline_id,
                    canon_before_id=workspace.canon_baseline_id,
                    plan_ref_id=cast(str, required[0]),
                    draft_ref_id=cast(str, required[1]),
                    observations_ref_id=cast(str, required[2]),
                    candidate_canon_patch_ref_id=cast(str, required[3]),
                    content_manifest_ref_id=manifest_ref.id,
                    content_fingerprint=prepared_manifest.sha256,
                    disposition="pending",
                    close_reason_code=None,
                    created_at_ms=timestamp,
                    closed_at_ms=None,
                )
            )
            result = SubmitChapterResult(
                project_id=request.project_id,
                chapter_id=request.chapter_id,
                submission_id=submission_id,
                content_fingerprint=prepared_manifest.sha256,
            )
            return CommandEffect(
                result=result,
                events=(
                    EventDraft(
                        event_type="chapter.submitted",
                        aggregate_type="chapter",
                        aggregate_id=request.chapter_id,
                        payload={"submission_id": submission_id},
                    ),
                ),
            )

        return await self._command_bus.execute(
            envelope=envelope,
            result_type=SubmitChapterResult,
            handler=handler,
        )

    async def record_review(
        self,
        request: RecordChapterReviewRequest,
        *,
        idempotency_key: str,
    ) -> CommandExecution[RecordChapterReviewResult]:
        timestamp = self._now_ms()
        review_id = self._id_factory()
        precheck_ref_id = self._id_factory()
        detail_ref_id = self._id_factory()
        repair_ref_id = self._id_factory()
        change_request_id = self._id_factory()
        failure_ref_id = self._id_factory()
        async with self._command_bus.read_unit_of_work() as session:
            task = await session.execution.get_successful_task(
                project_id=request.project_id,
                task_id=request.evaluator_task_id,
                attempt_id=request.evaluator_attempt_id,
            )
            submission = await session.chapters.get_submission(
                project_id=request.project_id,
                submission_id=request.submission_id,
            )
            if task is None:
                raise CommandPreconditionError("Evaluator task has no successful result.")
            result_bytes = (
                await session.content.get_packed(
                    project_id=request.project_id,
                    ref_id=task.result_ref_id,
                )
            ).unpack_and_verify()
        evaluation = LayerEvaluationResult.model_validate_json(result_bytes)
        decision = _chapter_review_decision(evaluation)
        if decision == "pass" and request.deterministic_precheck.get("passed") is not True:
            raise CommandPreconditionError("Chapter deterministic prechecks did not pass.")
        prepared_precheck = prepare_canonical_json(request.deterministic_precheck)
        prepared_detail = prepare_canonical_json(evaluation)
        repair_contract = (
            {
                "schema": "chapter-repair-contract-v1",
                "repair_scope": evaluation.repair_scope,
                "issues": [issue.model_dump(mode="json") for issue in evaluation.issues],
            }
            if decision == "local_repair"
            else None
        )
        prepared_repair = (
            None if repair_contract is None else prepare_canonical_json(repair_contract)
        )
        prepared_failure = prepare_canonical_json(
            {
                "code": "semantic_repair_exhausted",
                "message": "Chapter semantic repair limit of five has been exhausted.",
                "chapter_id": request.chapter_id,
            }
        )
        envelope = self._envelope(
            request=request,
            project_id=request.project_id,
            idempotency_key=idempotency_key,
            command_kind="record_chapter_review",
            actor="engine",
            source_task_id=request.evaluator_task_id,
            created_at_ms=timestamp,
        )

        async def handler(session: StoreSession) -> CommandEffect[RecordChapterReviewResult]:
            current_task = await session.execution.get_successful_task(
                project_id=request.project_id,
                task_id=request.evaluator_task_id,
                attempt_id=request.evaluator_attempt_id,
            )
            current_submission = await session.chapters.get_submission(
                project_id=request.project_id,
                submission_id=request.submission_id,
            )
            workspace = await session.chapters.get_workspace(
                project_id=request.project_id,
                chapter_id=request.chapter_id,
            )
            expected_task_kind = (
                "verify_repair.chapter"
                if workspace is not None and workspace.semantic_repair_count > 0
                else "evaluate.chapter"
            )
            if (
                task != current_task
                or task.task_kind != expected_task_kind
                or task.delivery_state != "pending"
                or submission is None
                or current_submission != submission
                or submission.disposition != "pending"
                or submission.chapter_id != request.chapter_id
                or task.chapter_id != request.chapter_id
                or task.book_baseline_id != submission.book_baseline_id
                or task.arc_baseline_id != submission.arc_baseline_id
                or task.canon_baseline_id != submission.canon_before_id
                or workspace is None
                or workspace.id != submission.workspace_id
                or workspace.lock_version != submission.workspace_lock_version
            ):
                raise CommandPreconditionError("Chapter evaluation facts are stale or mismatched.")
            precheck_ref = await session.content.put(
                project_id=request.project_id,
                prepared=prepared_precheck,
                semantic_kind="chapter.deterministic_precheck",
                media_type="application/json",
                schema_id="chapter-precheck",
                schema_version=1,
                ref_id=precheck_ref_id,
                created_at_ms=timestamp,
            )
            detail_ref = await session.content.put(
                project_id=request.project_id,
                prepared=prepared_detail,
                semantic_kind="chapter.review_detail",
                media_type="application/json",
                schema_id="chapter-evaluation-result",
                schema_version=1,
                ref_id=detail_ref_id,
                created_at_ms=timestamp,
            )
            repair_ref = None
            if prepared_repair is not None:
                repair_ref = await session.content.put(
                    project_id=request.project_id,
                    prepared=prepared_repair,
                    semantic_kind="chapter.repair_contract",
                    media_type="application/json",
                    schema_id="chapter-repair-contract",
                    schema_version=1,
                    ref_id=repair_ref_id,
                    created_at_ms=timestamp,
                )
            await session.chapters.insert_review(
                ChapterReviewRecord(
                    id=review_id,
                    project_id=request.project_id,
                    book_id=submission.book_id,
                    arc_id=submission.arc_id,
                    chapter_id=request.chapter_id,
                    submission_id=submission.id,
                    evaluator_task_id=request.evaluator_task_id,
                    evaluator_attempt_id=request.evaluator_attempt_id,
                    decision=decision,
                    rubric_id=request.rubric_id,
                    rubric_version=request.rubric_version,
                    precheck_ref_id=precheck_ref.id,
                    detail_ref_id=detail_ref.id,
                    repair_contract_ref_id=None if repair_ref is None else repair_ref.id,
                    created_at_ms=timestamp,
                )
            )
            events: list[EventDraft] = []
            if decision != "pass":
                if not await session.chapters.close_submission(
                    project_id=request.project_id,
                    submission_id=submission.id,
                    disposition="rejected",
                    reason_code=decision,
                    closed_at_ms=timestamp,
                ):
                    raise CommandPreconditionError("Chapter submission changed before rejection.")
                state = "active" if decision == "local_repair" else "blocked_by_user"
                if decision in {"escalate_to_arc", "escalate_to_book"}:
                    state = "blocked_by_upstream"
                    change_record = ChapterChangeRequestRecord(
                        id=change_request_id,
                        project_id=request.project_id,
                        book_id=submission.book_id,
                        arc_id=submission.arc_id,
                        chapter_id=request.chapter_id,
                        source_submission_id=submission.id,
                        source_review_id=review_id,
                        target_baseline_id=(
                            submission.arc_baseline_id
                            if decision == "escalate_to_arc"
                            else submission.book_baseline_id
                        ),
                        evidence_ref_id=detail_ref.id,
                        status="open",
                        created_at_ms=timestamp,
                    )
                    if decision == "escalate_to_arc":
                        await session.chapters.insert_arc_change_request(change_record)
                    else:
                        await session.chapters.insert_book_change_request(change_record)
                    events.append(
                        EventDraft(
                            event_type="change_request.opened",
                            aggregate_type="chapter",
                            aggregate_id=request.chapter_id,
                            payload={
                                "change_request_id": change_request_id,
                                "target_layer": decision.removeprefix("escalate_to_"),
                            },
                        )
                    )
                updated = replace(
                    workspace,
                    state=state,
                    lock_version=workspace.lock_version + 1,
                    updated_at_ms=timestamp,
                )
                if not await session.chapters.compare_and_set_workspace(
                    record=updated,
                    expected_lock_version=workspace.lock_version,
                ):
                    raise CommandPreconditionError("Chapter review workspace CAS failed.")
                if (
                    decision == "local_repair"
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
                        raise CommandPreconditionError("Run cannot pause at repair exhaustion.")
                if decision == "needs_user":
                    if not await session.runs.ensure_wait_for_user(
                        run_id=task.run_id,
                        reason_code="chapter_review_needs_user",
                        now_ms=timestamp,
                    ):
                        raise CommandPreconditionError(
                            "Run could not enter the Chapter review wait."
                        )
            if not await session.execution.mark_delivery_applied(
                project_id=request.project_id,
                task_id=request.evaluator_task_id,
                attempt_id=request.evaluator_attempt_id,
                command_id=envelope.command_id,
                updated_at_ms=timestamp,
            ):
                raise CommandPreconditionError("Evaluator result delivery changed concurrently.")
            result = RecordChapterReviewResult(
                project_id=request.project_id,
                chapter_id=request.chapter_id,
                submission_id=submission.id,
                review_id=review_id,
                decision=decision,
            )
            events.insert(
                0,
                EventDraft(
                    event_type="chapter.reviewed",
                    aggregate_type="chapter",
                    aggregate_id=request.chapter_id,
                    payload={
                        "submission_id": submission.id,
                        "review_id": review_id,
                        "decision": decision,
                    },
                ),
            )
            return CommandEffect(result=result, events=tuple(events))

        return await self._command_bus.execute(
            envelope=envelope,
            result_type=RecordChapterReviewResult,
            handler=handler,
        )

    async def commit_chapter_and_canon(
        self,
        request: CommitChapterRequest,
        *,
        idempotency_key: str,
    ) -> CommandExecution[CommitChapterResult]:
        timestamp = self._now_ms()
        chapter_baseline_id = self._id_factory()
        canon_baseline_id = self._id_factory()
        async with self._command_bus.read_unit_of_work() as session:
            submission = await session.chapters.get_submission(
                project_id=request.project_id,
                submission_id=request.submission_id,
            )
            review = await session.chapters.get_review(
                project_id=request.project_id,
                review_id=request.review_id,
            )
            canon_before = await session.canon.get_baseline(
                project_id=request.project_id,
                baseline_id=request.expected_canon_baseline_id,
            )
            if submission is None or review is None or canon_before is None:
                raise CommandPreconditionError("Chapter commit facts are incomplete.")
            plan_bytes = (
                await session.content.get_packed(
                    project_id=request.project_id,
                    ref_id=submission.plan_ref_id,
                )
            ).unpack_and_verify()
            prose_bytes = (
                await session.content.get_packed(
                    project_id=request.project_id,
                    ref_id=submission.draft_ref_id,
                )
            ).unpack_and_verify()
            patch_bytes = (
                await session.content.get_packed(
                    project_id=request.project_id,
                    ref_id=submission.candidate_canon_patch_ref_id,
                )
            ).unpack_and_verify()
            ref_by_category = _canon_ref_ids(canon_before)
            current_categories = {
                category: TypeAdapter(list[CanonEntry]).validate_json(
                    (
                        await session.content.get_packed(
                            project_id=request.project_id,
                            ref_id=ref_by_category[category],
                        )
                    ).unpack_and_verify()
                )
                for category in CANON_CATEGORIES
            }
        plan = ChapterPlanProposal.model_validate_json(plan_bytes)
        prose = prose_bytes.decode("utf-8")
        patch = BoundCanonPatch.model_validate_json(patch_bytes)
        applied = apply_canon_patch(
            chapter_id=request.chapter_id,
            current=current_categories,
            patch=patch,
        )
        prepared_commit = _PreparedCanonCommit(
            before=canon_before,
            applied=applied,
            prepared_categories={
                category: prepare_canonical_json(applied.categories[category])
                for category in applied.changed_categories
            },
        )
        new_ref_ids = {
            category: self._id_factory() for category in applied.changed_categories
        }
        resulting_ref_ids = {
            category: new_ref_ids.get(category, ref_by_category[category])
            for category in CANON_CATEGORIES
        }
        manifest_fingerprint = canon_manifest_fingerprint(resulting_ref_ids)
        envelope = self._envelope(
            request=request,
            project_id=request.project_id,
            idempotency_key=idempotency_key,
            command_kind="commit_chapter_and_canon",
            actor="engine",
            created_at_ms=timestamp,
        )

        async def handler(session: StoreSession) -> CommandEffect[CommitChapterResult]:
            project = await session.projects.get(request.project_id)
            chapter = await session.chapters.get(
                project_id=request.project_id,
                chapter_id=request.chapter_id,
            )
            current_submission = await session.chapters.get_submission(
                project_id=request.project_id,
                submission_id=request.submission_id,
            )
            current_review = await session.chapters.get_review(
                project_id=request.project_id,
                review_id=request.review_id,
            )
            workspace = await session.chapters.get_workspace(
                project_id=request.project_id,
                chapter_id=request.chapter_id,
            )
            context = (
                None
                if chapter is None
                else await session.chapters.get_active_arc_context(
                    project_id=request.project_id,
                    book_id=chapter.book_id,
                    arc_id=chapter.arc_id,
                    allow_completed=(
                        request.expected_current_chapter_baseline_id is not None
                    ),
                )
            )
            if (
                project is None
                or chapter is None
                or chapter.current_baseline_id
                != request.expected_current_chapter_baseline_id
                or project.current_canon_baseline_id != request.expected_canon_baseline_id
                or current_submission != submission
                or submission.disposition != "pending"
                or submission.chapter_id != request.chapter_id
                or submission.canon_before_id != request.expected_canon_baseline_id
                or current_review != review
                or review.submission_id != submission.id
                or review.decision != "pass"
                or workspace is None
                or workspace.id != submission.workspace_id
                or workspace.lock_version != submission.workspace_lock_version
                or context is None
                or context.book_baseline_id != submission.book_baseline_id
                or context.arc_baseline_id != submission.arc_baseline_id
                or context.canon_baseline_id != submission.canon_before_id
            ):
                raise CommandPreconditionError("Chapter commit facts are stale or unapproved.")
            chapter_version = await session.chapters.next_baseline_version(
                chapter_id=request.chapter_id
            )
            if request.expected_current_chapter_baseline_id is None:
                expected_version = 1
            else:
                current_version = await session.chapters.get_baseline_version(
                    project_id=request.project_id,
                    chapter_id=request.chapter_id,
                    baseline_id=request.expected_current_chapter_baseline_id,
                )
                if current_version is None:
                    raise CommandPreconditionError("Chapter current baseline is invalid.")
                expected_version = current_version + 1
            if chapter_version != expected_version:
                raise CommandPreconditionError("Chapter baseline version is not contiguous.")
            committed_before = await session.chapters.count_committed(arc_id=chapter.arc_id)
            effective_count = (
                committed_before + 1
                if chapter.lifecycle_status == "drafting"
                else committed_before
            )
            if effective_count > context.target_chapter_count:
                raise CommandPreconditionError("Chapter commit exceeds the Arc target count.")

            for category in applied.changed_categories:
                reference = await session.content.put(
                    project_id=request.project_id,
                    prepared=prepared_commit.prepared_categories[category],
                    semantic_kind=f"canon.{category}",
                    media_type="application/json",
                    schema_id=f"canon-{category}",
                    schema_version=1,
                    ref_id=new_ref_ids[category],
                    created_at_ms=timestamp,
                )
                if reference.id != resulting_ref_ids[category]:  # pragma: no cover
                    raise RuntimeError("Canon content reference identity changed.")
            canon_after_id = (
                canon_baseline_id if prepared_commit.applied.changed else canon_before.id
            )
            await session.chapters.insert_baseline(
                ChapterBaselineRecord(
                    id=chapter_baseline_id,
                    project_id=request.project_id,
                    book_id=chapter.book_id,
                    arc_id=chapter.arc_id,
                    chapter_id=chapter.id,
                    baseline_version=chapter_version,
                    parent_baseline_id=request.expected_current_chapter_baseline_id,
                    submission_id=submission.id,
                    review_id=review.id,
                    book_baseline_id=submission.book_baseline_id,
                    arc_baseline_id=submission.arc_baseline_id,
                    canon_before_id=canon_before.id,
                    canon_after_id=canon_after_id,
                    plan_ref_id=submission.plan_ref_id,
                    prose_ref_id=submission.draft_ref_id,
                    observations_ref_id=submission.observations_ref_id,
                    accepted_canon_patch_ref_id=submission.candidate_canon_patch_ref_id,
                    chapter_title=plan.title.strip(),
                    character_count=len(prose),
                    created_at_ms=timestamp,
                )
            )
            if prepared_commit.applied.changed:
                canon_version = await session.canon.next_baseline_version(
                    project_id=request.project_id
                )
                if canon_version != canon_before.baseline_version + 1:
                    raise CommandPreconditionError("Canon baseline version is not contiguous.")
                await session.canon.insert_baseline(
                    CanonBaselineRecord(
                        id=canon_baseline_id,
                        project_id=request.project_id,
                        baseline_version=canon_version,
                        parent_canon_baseline_id=canon_before.id,
                        source_book_id=chapter.book_id,
                        source_arc_id=chapter.arc_id,
                        source_chapter_id=chapter.id,
                        source_chapter_baseline_id=chapter_baseline_id,
                        accepted_patch_ref_id=submission.candidate_canon_patch_ref_id,
                        characters_ref_id=resulting_ref_ids["characters"],
                        relationships_ref_id=resulting_ref_ids["relationships"],
                        world_facts_ref_id=resulting_ref_ids["world_facts"],
                        foreshadowing_ref_id=resulting_ref_ids["foreshadowing"],
                        manifest_fingerprint=manifest_fingerprint,
                        created_at_ms=timestamp,
                    )
                )
                if not await session.canon.compare_and_set_current(
                    project_id=request.project_id,
                    expected_baseline_id=canon_before.id,
                    new_baseline_id=canon_baseline_id,
                    updated_at_ms=timestamp,
                ):
                    raise CommandPreconditionError("Canon current pointer CAS failed.")
            if not await session.chapters.commit_current_baseline(
                project_id=request.project_id,
                chapter_id=chapter.id,
                expected_baseline_id=request.expected_current_chapter_baseline_id,
                new_baseline_id=chapter_baseline_id,
                committed_at_ms=timestamp,
            ):
                raise CommandPreconditionError("Chapter current pointer CAS failed.")
            if not await session.chapters.close_submission(
                project_id=request.project_id,
                submission_id=submission.id,
                disposition="promoted",
                reason_code="baseline_committed",
                closed_at_ms=timestamp,
            ):
                raise CommandPreconditionError("Chapter submission promotion failed.")
            updated_workspace = replace(
                workspace,
                state="idle",
                lock_version=workspace.lock_version + 1,
                base_chapter_baseline_id=chapter_baseline_id,
                canon_baseline_id=canon_after_id,
                guidance_ref_id=None,
                semantic_repair_count=0,
                updated_at_ms=timestamp,
            )
            if not await session.chapters.compare_and_set_workspace(
                record=updated_workspace,
                expected_lock_version=workspace.lock_version,
            ):
                raise CommandPreconditionError("Chapter workspace finalization CAS failed.")
            committed_after = await session.chapters.count_committed(arc_id=chapter.arc_id)
            if committed_after != effective_count:
                raise CommandPreconditionError("Committed Chapter count changed concurrently.")
            if chapter.lifecycle_status == "committed":
                arc_completed = committed_after == context.target_chapter_count
            else:
                arc_completed = await session.chapters.complete_arc_if_target_reached(
                    project_id=request.project_id,
                    arc_id=chapter.arc_id,
                    arc_baseline_id=context.arc_baseline_id,
                    committed_count=committed_after,
                    target_chapter_count=context.target_chapter_count,
                    now_ms=timestamp,
                )
            result = CommitChapterResult(
                project_id=request.project_id,
                chapter_id=chapter.id,
                chapter_baseline_id=chapter_baseline_id,
                chapter_baseline_version=chapter_version,
                canon_before_id=canon_before.id,
                canon_after_id=canon_after_id,
                canon_changed=prepared_commit.applied.changed,
                arc_completed=arc_completed,
            )
            events = [
                EventDraft(
                    event_type="chapter.baseline_committed",
                    aggregate_type="chapter",
                    aggregate_id=chapter.id,
                    payload={
                        "chapter_baseline_id": chapter_baseline_id,
                        "baseline_version": chapter_version,
                        "canon_before_id": canon_before.id,
                        "canon_after_id": canon_after_id,
                        "arc_completed": arc_completed,
                    },
                )
            ]
            if prepared_commit.applied.changed:
                events.append(
                    EventDraft(
                        event_type="canon.baseline_committed",
                        aggregate_type="canon",
                        aggregate_id=canon_baseline_id,
                        payload={
                            "source_chapter_id": chapter.id,
                            "source_chapter_baseline_id": chapter_baseline_id,
                        },
                    )
                )
            return CommandEffect(result=result, events=tuple(events))

        return await self._command_bus.execute(
            envelope=envelope,
            result_type=CommitChapterResult,
            handler=handler,
        )


def _task_matches_workspace(
    task: SuccessfulTaskRecord,
    workspace: ChapterWorkspaceRecord,
    *,
    expected_lock_version: int,
) -> bool:
    return (
        task.delivery_state == "pending"
        and task.project_id == workspace.project_id
        and task.book_id == workspace.book_id
        and task.arc_id == workspace.arc_id
        and task.chapter_id == workspace.chapter_id
        and task.workspace_lock_version == expected_lock_version
        and workspace.lock_version == expected_lock_version
        and task.book_baseline_id == workspace.book_baseline_id
        and task.arc_baseline_id == workspace.arc_baseline_id
        and task.chapter_baseline_id == workspace.base_chapter_baseline_id
        and task.canon_baseline_id == workspace.canon_baseline_id
        and workspace.state == "active"
    )


def _chapter_review_decision(
    evaluation: LayerEvaluationResult,
) -> ChapterReviewDecision:
    if evaluation.decision != "cross_loop_escalation":
        return evaluation.decision
    if evaluation.escalation_target == "arc":
        return "escalate_to_arc"
    if evaluation.escalation_target == "book":
        return "escalate_to_book"
    raise ValueError("Chapter escalation has no valid target.")


def _canon_ref_ids(baseline: CanonBaselineRecord) -> dict[CanonCategory, str]:
    return {
        "characters": baseline.characters_ref_id,
        "relationships": baseline.relationships_ref_id,
        "world_facts": baseline.world_facts_ref_id,
        "foreshadowing": baseline.foreshadowing_ref_id,
    }


def _require_repair_budget(workspace: ChapterWorkspaceRecord) -> None:
    if workspace.semantic_repair_count >= workspace.semantic_repair_limit:
        raise CommandPreconditionError("Chapter semantic repair limit is exhausted.")
