from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from typing import AbstractSet, cast

from pydantic import BaseModel, TypeAdapter, ValidationError

from app.agents.contracts import (
    ArcPlanProposal,
    ChapterDraftResult,
    ChapterEvaluationIssue,
    ChapterObservationRepairPatch,
    ChapterObservationResult,
    ChapterPlanProposal,
    ChapterRepairComponent,
    ChapterRepairVerificationIssue,
    ChapterRepairVerificationResult,
    LayerEvaluationResult,
)
from app.agents.registry import DEFAULT_EVALUATION_STRATEGY_REGISTRY
from app.db.uow import StoreSession
from app.domain.chapter.canon import (
    CANON_CATEGORIES,
    AppliedCanonPatch,
    BoundCanonPatch,
    CanonCategory,
    CanonEntry,
    CanonPatchConflictError,
    apply_canon_patch,
    bind_canon_patch,
    canon_manifest_fingerprint,
)
from app.domain.chapter.contracts import (
    ApplyChapterTaskRequest,
    ApplyChapterTaskResult,
    ChapterComponent,
    ChapterRepairContract,
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
    bind_committed_chapter_observation,
)
from app.domain.commands import (
    Actor,
    CommandEffect,
    CommandEnvelope,
    CommandExecution,
    CommandPreconditionError,
    EventDraft,
)
from app.domain.evaluation import ChapterEvidenceCorrectionEvaluation
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


class ChapterEvidenceVerificationFailure(RuntimeError):
    """An evidence-only correction failed its one non-recursive verification."""


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


def _merge_chapter_observation_repair(
    *,
    current: ChapterObservationResult,
    patch: ChapterObservationRepairPatch,
    allowed_scope: AbstractSet[str],
) -> ChapterObservationResult:
    requested = {change.component for change in patch.changes}
    unauthorized = requested.difference(allowed_scope)
    if unauthorized:
        raise CommandPreconditionError(
            "Chapter observation repair changed unauthorized components: "
            + ", ".join(sorted(unauthorized))
        )
    merged = current.model_dump(mode="python")
    for change in patch.changes:
        if change.component == "observations":
            merged["summary"] = change.summary
            merged["established_facts"] = change.established_facts
        else:
            merged["canon_proposals"] = change.canon_proposals
    observations = ChapterObservationResult.model_validate(merged)
    if observations == current:
        raise CommandPreconditionError(
            "Chapter observation repair result made no authorized change."
        )
    return observations


_CHAPTER_REPAIR_COMPONENT_ORDER: tuple[ChapterRepairComponent, ...] = (
    "plan",
    "prose",
    "observations",
    "canon",
)
_CHAPTER_NARRATIVE_REPAIR_COMPONENTS = frozenset({"plan", "prose"})
_CHAPTER_DERIVED_REPAIR_COMPONENTS = frozenset({"observations", "canon"})


def _chapter_repair_scope(
    evaluation: LayerEvaluationResult | ChapterRepairVerificationResult,
) -> list[ChapterRepairComponent]:
    if evaluation.decision != "local_repair":
        return []
    observed = {
        component
        for issue in evaluation.issues
        for component in issue.observed_components
    }
    return [
        component
        for component in _CHAPTER_REPAIR_COMPONENT_ORDER
        if component in observed
    ]


def _chapter_issue_fingerprint(issue: ChapterEvaluationIssue) -> str:
    def normalize(value: str) -> str:
        return " ".join(value.casefold().split())

    return prepare_canonical_json(
        {
            "kind": issue.kind,
            "code": normalize(issue.code),
            "subject": normalize(issue.subject),
        }
    ).sha256


def _normalize_chapter_evaluation_result(
    *,
    task_kind: str,
    result_bytes: bytes,
) -> ChapterRepairVerificationResult:
    if task_kind == "verify_repair.chapter":
        return ChapterRepairVerificationResult.model_validate_json(result_bytes)
    if task_kind != "evaluate.chapter":
        raise CommandPreconditionError(
            "Chapter review requires an initial evaluation or repair verification task."
        )
    initial = LayerEvaluationResult.model_validate_json(result_bytes)
    return ChapterRepairVerificationResult(
        guidance_authority_judgment=initial.guidance_authority_judgment,
        decision=initial.decision,
        summary=initial.summary,
        issues=[
            ChapterRepairVerificationIssue.model_validate(
                {
                    **issue.model_dump(mode="python"),
                    "recurrence": "new",
                }
            )
            for issue in initial.issues
        ],
    )


def _is_derived_dependency_closure(
    *,
    previous_contract: ChapterRepairContract | None,
    repair_scope: AbstractSet[str],
) -> bool:
    if previous_contract is None:
        return False
    previous_scope = set(previous_contract.authorized_components)
    return (
        previous_contract.repair_stage == "primary_semantic"
        and bool(previous_scope & _CHAPTER_NARRATIVE_REPAIR_COMPONENTS)
        and bool(repair_scope)
        and repair_scope <= _CHAPTER_DERIVED_REPAIR_COMPONENTS
    )


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
            cumulative_committed = (
                await session.chapters.count_committed_for_book(
                    book_id=request.book_id
                )
            )
            if (
                cumulative_committed
                >= context.closure_cumulative_chapter_count
            ):
                raise CommandPreconditionError(
                    "The current Arc reached its frozen cumulative closure checkpoint."
                )
            book_ordinal, arc_ordinal = await session.chapters.next_ordinals(
                book_id=request.book_id,
                arc_id=request.arc_id,
            )
            arc_plan = ArcPlanProposal.model_validate_json(
                (
                    await session.content.get_packed(
                        project_id=request.project_id,
                        ref_id=context.arc_plan_ref_id,
                    )
                ).unpack_and_verify()
            )
            outline_offset = (
                arc_ordinal - context.planned_after_arc_chapter_count - 1
            )
            expected_book_ordinal = (
                context.planned_after_cumulative_chapter_count
                + outline_offset
                + 1
            )
            if (
                outline_offset < 0
                or outline_offset >= len(arc_plan.chapter_outline)
                or book_ordinal != expected_book_ordinal
            ):
                raise CommandPreconditionError(
                    "arc_outline_slot_missing: the next Chapter has no unique "
                    "assignment in the current Arc baseline."
                )
            await session.chapters.insert(
                ChapterRecord(
                    id=chapter_id,
                    project_id=request.project_id,
                    book_id=request.book_id,
                    arc_id=request.arc_id,
                    book_ordinal=book_ordinal,
                    arc_ordinal=arc_ordinal,
                    outline_arc_baseline_id=context.arc_baseline_id,
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
                    work_cycle_id=uuid.uuid4().hex,
                    active_repair_review_id=None,
                    base_chapter_baseline_id=None,
                    book_baseline_id=context.book_baseline_id,
                    arc_baseline_id=context.arc_baseline_id,
                    canon_baseline_id=context.canon_baseline_id,
                    revision_origin="initial",
                    source_arc_parent_review_id=None,
                    source_arc_closure_review_id=None,
                    source_feedback_id=None,
                    correction_lineage_id=None,
                    correction_lineage_origin=None,
                    automatic_correction_round=None,
                    plan_ref_id=None,
                    draft_ref_id=None,
                    observations_ref_id=None,
                    candidate_canon_patch_ref_id=None,
                    repair_policy_id="semantic-repair-v1",
                    semantic_repair_count=0,
                    semantic_repair_limit=1,
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
                            "outline_arc_baseline_id": context.arc_baseline_id,
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
                work_cycle_id=uuid.uuid4().hex,
                active_repair_review_id=None,
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
                    3,
                ),
                (
                    "chapter.candidate_canon_patch",
                    "application/json",
                    "chapter-canon-patch",
                    3,
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
            review_snapshot = (
                None
                if workspace_snapshot is None
                or workspace_snapshot.active_repair_review_id is None
                else await session.chapters.get_review(
                    project_id=request.project_id,
                    review_id=workspace_snapshot.active_repair_review_id,
                )
            )
            if (
                workspace_snapshot is None
                or review_snapshot is None
                or task.source_chapter_candidate_review_id != review_snapshot.id
                or task.workspace_work_cycle_id != workspace_snapshot.work_cycle_id
                or review_snapshot.decision != "local_repair"
                or review_snapshot.repair_contract_ref_id is None
            ):
                raise CommandPreconditionError("Chapter has no active local repair contract.")
            repair_contract = ChapterRepairContract.model_validate_json(
                (
                    await session.content.get_packed(
                        project_id=request.project_id,
                        ref_id=review_snapshot.repair_contract_ref_id,
                    )
                ).unpack_and_verify()
            )
        allowed_scope = set(repair_contract.authorized_components)
        repair_stage = repair_contract.repair_stage
        prepared: Sequence[PreparedContent]
        descriptors: Sequence[tuple[str, str, str | None, int | None]]
        if task.task_kind == "chapter.repair.plan":
            if repair_stage != "primary_semantic":
                raise CommandPreconditionError(
                    "A derived dependency closure cannot replace the Chapter plan."
                )
            if allowed_scope != {"plan"}:
                raise CommandPreconditionError(
                    "A Chapter plan repair must be the only authorized component."
                )
            plan = ChapterPlanProposal.model_validate_json(raw)
            component: ChapterComponent = "repair_plan"
            prepared = (prepare_canonical_json(plan),)
            descriptors = (
                ("chapter.plan", "application/json", "chapter-plan", 1),
            )

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
                    plan_ref_id=refs[0],
                    draft_ref_id=None,
                    observations_ref_id=None,
                    candidate_canon_patch_ref_id=None,
                    semantic_repair_count=workspace.semantic_repair_count + 1,
                    updated_at_ms=timestamp,
                )

        elif task.task_kind == "chapter.repair.prose":
            if repair_stage != "primary_semantic":
                raise CommandPreconditionError(
                    "A derived dependency closure cannot replace Chapter prose."
                )
            if "prose" not in allowed_scope:
                raise CommandPreconditionError("Repair contract does not authorize prose changes.")
            draft = ChapterDraftResult.model_validate_json(raw)
            component = "repair_prose"
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
            if (
                repair_stage == "derived_dependency_closure"
                and not allowed_scope <= _CHAPTER_DERIVED_REPAIR_COMPONENTS
            ):
                raise CommandPreconditionError(
                    "A derived dependency closure may change only observations or Canon."
                )
            if workspace_snapshot.draft_ref_id is None:
                raise CommandPreconditionError("Observation repair has no frozen prose.")
            repair_patch = ChapterObservationRepairPatch.model_validate_json(raw)
            async with self._command_bus.read_unit_of_work() as session:
                source_submission = await session.chapters.get_submission(
                    project_id=request.project_id,
                    submission_id=review_snapshot.submission_id,
                )
                if source_submission is None:
                    raise CommandPreconditionError(
                        "Observation repair has no reviewed source submission."
                    )
                prose = (
                    await session.content.get_packed(
                        project_id=request.project_id,
                        ref_id=workspace_snapshot.draft_ref_id,
                    )
                ).unpack_and_verify().decode("utf-8")
                current_observations = ChapterObservationResult.model_validate_json(
                    (
                        await session.content.get_packed(
                            project_id=request.project_id,
                            ref_id=source_submission.observations_ref_id,
                        )
                    ).unpack_and_verify()
                )
            observations = _merge_chapter_observation_repair(
                current=current_observations,
                patch=repair_patch,
                allowed_scope=allowed_scope,
            )
            bound_patch = bind_canon_patch(
                chapter_id=request.chapter_id,
                prose=prose,
                observations=observations,
            )
            component = "repair_observations"
            prepared = (
                prepare_canonical_json(observations),
                prepare_canonical_json(bound_patch),
            )
            descriptors = (
                (
                    "chapter.observations",
                    "application/json",
                    "chapter-observations",
                    3,
                ),
                (
                    "chapter.candidate_canon_patch",
                    "application/json",
                    "chapter-canon-patch",
                    3,
                ),
            )

            def mutate(
                workspace: ChapterWorkspaceRecord,
                refs: Sequence[str],
                timestamp: int,
            ) -> ChapterWorkspaceRecord:
                if repair_stage == "primary_semantic":
                    _require_repair_budget(workspace)
                    semantic_repair_count = workspace.semantic_repair_count + 1
                else:
                    if (
                        workspace.semantic_repair_count
                        != workspace.semantic_repair_limit
                    ):
                        raise CommandPreconditionError(
                            "Derived evidence closure requires one consumed narrative repair."
                        )
                    semantic_repair_count = workspace.semantic_repair_count
                if workspace.draft_ref_id != workspace_snapshot.draft_ref_id:
                    raise CommandPreconditionError("Chapter prose changed before repair delivery.")
                return replace(
                    workspace,
                    state="active",
                    lock_version=workspace.lock_version + 1,
                    observations_ref_id=refs[0],
                    candidate_canon_patch_ref_id=refs[1],
                    semantic_repair_count=semantic_repair_count,
                    updated_at_ms=timestamp,
                )

        else:
            raise CommandPreconditionError("Task is not a Chapter repair task.")

        async def validate_repair(
            session: StoreSession,
            workspace: ChapterWorkspaceRecord,
        ) -> None:
            active = (
                None
                if workspace.active_repair_review_id is None
                else await session.chapters.get_review(
                    project_id=request.project_id,
                    review_id=workspace.active_repair_review_id,
                )
            )
            if (
                active != review_snapshot
                or task.source_chapter_candidate_review_id != review_snapshot.id
                or task.workspace_work_cycle_id != workspace.work_cycle_id
                or workspace.state != "active"
            ):
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
            source_feedback = (
                None
                if workspace is None or workspace.source_feedback_id is None
                else await session.feedback.get(
                    project_id=request.project_id,
                    feedback_id=workspace.source_feedback_id,
                )
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
        if workspace.source_feedback_id is not None and (
            source_feedback is None
            or source_feedback.status != "applied"
            or source_feedback.route_layer != "chapter"
            or source_feedback.book_id != workspace.book_id
            or source_feedback.arc_id != workspace.arc_id
            or source_feedback.chapter_id != request.chapter_id
            or source_feedback.content_ref_id != workspace.guidance_ref_id
        ):
            raise CommandPreconditionError(
                "Chapter guidance lost its exact applied feedback source."
            )
        manifest = {
            "schema": "chapter-review-manifest-v3",
            "workspace_id": workspace.id,
            "workspace_lock_version": workspace.lock_version,
            "work_cycle_id": workspace.work_cycle_id,
            "base_chapter_baseline_id": workspace.base_chapter_baseline_id,
            "book_baseline_id": workspace.book_baseline_id,
            "arc_baseline_id": workspace.arc_baseline_id,
            "canon_before_id": workspace.canon_baseline_id,
            "plan_ref_id": required[0],
            "draft_ref_id": required[1],
            "observations_ref_id": required[2],
            "candidate_canon_patch_ref_id": required[3],
            "guidance_ref_id": workspace.guidance_ref_id,
            "source_feedback_id": (
                None if source_feedback is None else source_feedback.id
            ),
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
            arc = await session.arcs.get(
                project_id=request.project_id,
                arc_id=workspace.arc_id,
            )
            if current != workspace:
                raise CommandPreconditionError("Chapter workspace changed during submission.")
            context = await session.chapters.get_active_arc_context(
                project_id=request.project_id,
                book_id=workspace.book_id,
                arc_id=workspace.arc_id,
                allow_completed=workspace.base_chapter_baseline_id is not None,
            )
            if (
                context is None
                or arc is None
                or arc.current_closure_id is not None
                or arc.lifecycle_status == "completed"
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
                schema_version=3,
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
                    work_cycle_id=workspace.work_cycle_id,
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

    async def build_submission_precheck(
        self,
        *,
        project_id: str,
        submission_id: str,
    ) -> dict[str, object]:
        """Validate one frozen Chapter/Canon submission without mutating authority."""
        async with self._command_bus.read_unit_of_work() as session:
            submission = await session.chapters.get_submission(
                project_id=project_id,
                submission_id=submission_id,
            )
            if submission is None:
                raise CommandPreconditionError(
                    "Chapter deterministic precheck has no frozen submission."
                )
            canon_before = await session.canon.get_baseline(
                project_id=project_id,
                baseline_id=submission.canon_before_id,
            )
            if canon_before is None:
                raise CommandPreconditionError(
                    "Chapter deterministic precheck lost its Canon baseline."
                )
            patch = BoundCanonPatch.model_validate_json(
                (
                    await session.content.get_packed(
                        project_id=project_id,
                        ref_id=submission.candidate_canon_patch_ref_id,
                    )
                ).unpack_and_verify()
            )
            current_categories = await _load_canon_categories(
                session,
                project_id=project_id,
                baseline=canon_before,
            )
        try:
            apply_canon_patch(
                chapter_id=submission.chapter_id,
                chapter_baseline_id="deterministic-precheck",
                prose_ref_id=submission.draft_ref_id,
                current=current_categories,
                patch=patch,
            )
        except CanonPatchConflictError as error:
            if error.code != "canon_subject_assertion_conflict":
                raise CommandPreconditionError(
                    "Chapter Canon precheck found a non-repairable binding defect."
                ) from error
            operation = error.operation
            return {
                "schema_id": "chapter-submission-precheck-v5",
                "passed": False,
                "checks": {
                    "frozen_submission_loaded": True,
                    "canon_baseline_loaded": True,
                    "canon_patch_applicable": False,
                },
                "issues": [
                    {
                        "kind": "contract_unfulfilled",
                        "code": error.code,
                        "subject": (
                            "candidate Canon assertion"
                            if operation is None
                            else operation.subject
                        ),
                        "summary": str(error),
                        "evidence": [
                            (
                                "The candidate Canon patch contains incompatible "
                                "assertions for one exact semantic subject."
                                if operation is None
                                else (
                                f"{operation.category} subject "
                                f"{operation.subject!r} has incompatible assertions."
                            )
                            )
                        ],
                        "contract_item": (
                            "One Chapter candidate must propose at most one coherent "
                            "current meaning for each exact Canon subject."
                        ),
                        "observed_components": ["canon"],
                    }
                ],
            }
        return {
            "schema_id": "chapter-submission-precheck-v5",
            "passed": True,
            "checks": {
                "frozen_submission_loaded": True,
                "canon_baseline_loaded": True,
                "canon_patch_applicable": True,
            },
            "issues": [],
        }

    async def record_review(
        self,
        request: RecordChapterReviewRequest,
        *,
        idempotency_key: str,
    ) -> CommandExecution[RecordChapterReviewResult]:
        deterministic_precheck = await self.build_submission_precheck(
            project_id=request.project_id,
            submission_id=request.submission_id,
        )
        timestamp = self._now_ms()
        review_id = self._id_factory()
        precheck_ref_id = self._id_factory()
        detail_ref_id = self._id_factory()
        repair_ref_id = self._id_factory()
        change_request_id = self._id_factory()
        failure_ref_id = self._id_factory()
        previous_repair_contract: ChapterRepairContract | None = None
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
            workspace_snapshot = await session.chapters.get_workspace(
                project_id=request.project_id,
                chapter_id=request.chapter_id,
            )
            previous_review = (
                None
                if workspace_snapshot is None
                or workspace_snapshot.active_repair_review_id is None
                else await session.chapters.get_review(
                    project_id=request.project_id,
                    review_id=workspace_snapshot.active_repair_review_id,
                )
            )
            if task is None:
                raise CommandPreconditionError("Evaluator task has no successful result.")
            result_bytes = (
                await session.content.get_packed(
                    project_id=request.project_id,
                    ref_id=task.result_ref_id,
                )
            ).unpack_and_verify()
            if task.task_kind == "verify_repair.chapter":
                if (
                    previous_review is None
                    or previous_review.decision != "local_repair"
                    or previous_review.repair_contract_ref_id is None
                ):
                    raise CommandPreconditionError(
                        "Chapter repair verification lost its source repair contract."
                    )
                previous_repair_contract = ChapterRepairContract.model_validate_json(
                    (
                        await session.content.get_packed(
                            project_id=request.project_id,
                            ref_id=previous_review.repair_contract_ref_id,
                        )
                    ).unpack_and_verify()
                )
        evaluation = _apply_chapter_precheck(
            _normalize_chapter_evaluation_result(
                task_kind=task.task_kind,
                result_bytes=result_bytes,
            ),
            deterministic_precheck,
        )
        decision = _chapter_review_decision(evaluation)
        repair_scope = _chapter_repair_scope(evaluation)
        repair_scope_set = set(repair_scope)
        issue_fingerprints = [
            _chapter_issue_fingerprint(issue) for issue in evaluation.issues
        ]
        previous_issue_fingerprints: set[str] = set()
        if previous_repair_contract is not None:
            previous_issue_fingerprints = set(
                previous_repair_contract.issue_fingerprints
            )
        stalled_issue_fingerprints = sorted(
            {
                fingerprint
                for fingerprint, issue in zip(
                    issue_fingerprints,
                    evaluation.issues,
                    strict=True,
                )
                if (
                    issue.recurrence == "persists_after_authorized_repair"
                    or fingerprint in previous_issue_fingerprints
                )
            }
        )
        derived_dependency_closure = (
            task.task_kind == "verify_repair.chapter"
            and decision == "local_repair"
            and workspace_snapshot is not None
            and workspace_snapshot.semantic_repair_count
            == workspace_snapshot.semantic_repair_limit
            and _is_derived_dependency_closure(
                previous_contract=previous_repair_contract,
                repair_scope=repair_scope_set,
            )
        )
        semantic_repair_stalled = (
            task.task_kind == "verify_repair.chapter"
            and decision == "local_repair"
            and bool(stalled_issue_fingerprints)
            and not derived_dependency_closure
        )
        prepared_precheck = prepare_canonical_json(deterministic_precheck)
        prepared_detail = prepare_canonical_json(evaluation)
        repair_contract = (
            ChapterRepairContract(
                repair_stage=(
                    "derived_dependency_closure"
                    if derived_dependency_closure
                    else "primary_semantic"
                ),
                authorized_components=repair_scope,
                issues=evaluation.issues,
                issue_fingerprints=issue_fingerprints,
                stalled_issue_fingerprints=stalled_issue_fingerprints,
            )
            if decision == "local_repair"
            else None
        )
        prepared_repair = (
            None if repair_contract is None else prepare_canonical_json(repair_contract)
        )
        prepared_failure = prepare_canonical_json(
            {
                "code": (
                    "semantic_repair_stalled"
                    if semantic_repair_stalled
                    else "semantic_repair_exhausted"
                ),
                "message": (
                    "The same Chapter semantic issue persisted after its one "
                    "authorized correction."
                    if semantic_repair_stalled
                    else (
                        "Chapter semantic correction for this frozen review "
                        "is exhausted."
                    )
                ),
                "chapter_id": request.chapter_id,
                "issue_fingerprints": stalled_issue_fingerprints,
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
            current_previous_review = (
                None
                if workspace is None or workspace.active_repair_review_id is None
                else await session.chapters.get_review(
                    project_id=request.project_id,
                    review_id=workspace.active_repair_review_id,
                )
            )
            expected_task_kind = (
                "verify_repair.chapter"
                if workspace is not None and workspace.semantic_repair_count > 0
                else "evaluate.chapter"
            )
            strategy = DEFAULT_EVALUATION_STRATEGY_REGISTRY.for_task(
                expected_task_kind
            )
            if (
                task != current_task
                or task.task_kind != expected_task_kind
                or task.evaluation_strategy_id != strategy.strategy_id
                or task.evaluation_strategy_version
                != strategy.strategy_version
                or task.rubric_id != strategy.rubric_id
                or task.rubric_version != strategy.rubric_version
                or request.rubric_id != strategy.rubric_id
                or request.rubric_version != strategy.rubric_version
                or task.delivery_state != "pending"
                or submission is None
                or current_submission != submission
                or submission.disposition != "pending"
                or submission.chapter_id != request.chapter_id
                or task.chapter_id != request.chapter_id
                or workspace is None
                or task.workspace_work_cycle_id != submission.work_cycle_id
                or workspace.work_cycle_id != submission.work_cycle_id
                or task.source_chapter_candidate_review_id
                != workspace.active_repair_review_id
                or task.source_feedback_id != workspace.source_feedback_id
                or task.source_arc_parent_review_id
                != workspace.source_arc_parent_review_id
                or task.source_arc_closure_review_id
                != workspace.source_arc_closure_review_id
                or task.book_baseline_id != submission.book_baseline_id
                or task.arc_baseline_id != submission.arc_baseline_id
                or task.canon_baseline_id != submission.canon_before_id
                or workspace.id != submission.workspace_id
                or workspace.lock_version != submission.workspace_lock_version
                or current_previous_review != previous_review
            ):
                raise CommandPreconditionError("Chapter evaluation facts are stale or mismatched.")
            if (
                evaluation.guidance_authority_judgment == "not_present"
            ) == (workspace.source_feedback_id is not None):
                raise CommandPreconditionError(
                    "Chapter evaluation did not classify its exact active guidance."
                )
            precheck_ref = await session.content.put(
                project_id=request.project_id,
                prepared=prepared_precheck,
                semantic_kind="chapter.deterministic_precheck",
                media_type="application/json",
                schema_id="chapter-precheck",
                schema_version=5,
                ref_id=precheck_ref_id,
                created_at_ms=timestamp,
            )
            detail_ref = await session.content.put(
                project_id=request.project_id,
                prepared=prepared_detail,
                semantic_kind="chapter.review_detail",
                media_type="application/json",
                schema_id="chapter-evaluation-result",
                schema_version=6,
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
                    schema_version=6,
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
                state = (
                    "active"
                    if decision == "local_repair"
                    else "blocked_by_upstream"
                )
                if decision == "escalate_to_arc":
                    change_record = ChapterChangeRequestRecord(
                        id=change_request_id,
                        project_id=request.project_id,
                        book_id=submission.book_id,
                        arc_id=submission.arc_id,
                        chapter_id=request.chapter_id,
                        source_submission_id=submission.id,
                        source_review_id=review_id,
                        target_baseline_id=submission.arc_baseline_id,
                        evidence_ref_id=detail_ref.id,
                        status="open",
                        created_at_ms=timestamp,
                    )
                    await session.chapters.insert_arc_change_request(change_record)
                    events.append(
                        EventDraft(
                            event_type="change_request.opened",
                            aggregate_type="chapter",
                            aggregate_id=request.chapter_id,
                            payload={
                                "change_request_id": change_request_id,
                                "target_layer": "arc",
                            },
                        )
                    )
                updated = replace(
                    workspace,
                    state=state,
                    lock_version=workspace.lock_version + 1,
                    active_repair_review_id=(
                        review_id if decision == "local_repair" else None
                    ),
                    updated_at_ms=timestamp,
                )
                if not await session.chapters.compare_and_set_workspace(
                    record=updated,
                    expected_lock_version=workspace.lock_version,
                ):
                    raise CommandPreconditionError("Chapter review workspace CAS failed.")
                if (
                    decision == "local_repair"
                    and (
                        semantic_repair_stalled
                        or (
                            workspace.semantic_repair_count
                            >= workspace.semantic_repair_limit
                            and not derived_dependency_closure
                        )
                    )
                ):
                    failure_ref = await session.content.put(
                        project_id=request.project_id,
                        prepared=prepared_failure,
                        semantic_kind="agent_error_summary",
                        media_type="application/json",
                        schema_id=(
                            "semantic-repair-stalled"
                            if semantic_repair_stalled
                            else "semantic-repair-exhausted"
                        ),
                        schema_version=1,
                        ref_id=failure_ref_id,
                        created_at_ms=timestamp,
                    )
                    if not await session.runs.failure_pause_for_task(
                        run_id=task.run_id,
                        task_id=task.task_id,
                        failure_code=(
                            "semantic_repair_stalled"
                            if semantic_repair_stalled
                            else "semantic_repair_exhausted"
                        ),
                        failure_ref_id=failure_ref.id,
                        now_ms=timestamp,
                    ):
                        raise CommandPreconditionError(
                            "Run cannot pause at the Chapter repair terminal boundary."
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

    async def record_evidence_review(
        self,
        request: RecordChapterReviewRequest,
        *,
        idempotency_key: str,
    ) -> CommandExecution[RecordChapterReviewResult]:
        """Accept one evidence-only correction or fail without opening another repair."""
        timestamp = self._now_ms()
        review_id = self._id_factory()
        precheck_ref_id = self._id_factory()
        detail_ref_id = self._id_factory()
        strategy = DEFAULT_EVALUATION_STRATEGY_REGISTRY.for_task(
            "verify_evidence.chapter"
        )
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
            chapter = await session.chapters.get(
                project_id=request.project_id,
                chapter_id=request.chapter_id,
            )
            workspace = await session.chapters.get_workspace(
                project_id=request.project_id,
                chapter_id=request.chapter_id,
            )
            arc = (
                None
                if chapter is None
                else await session.arcs.get(
                    project_id=request.project_id,
                    arc_id=chapter.arc_id,
                )
            )
            baseline = (
                None
                if submission is None
                or submission.base_chapter_baseline_id is None
                else await session.chapters.get_baseline(
                    project_id=request.project_id,
                    chapter_id=request.chapter_id,
                    baseline_id=submission.base_chapter_baseline_id,
                )
            )
            if (
                task is None
                or submission is None
                or chapter is None
                or workspace is None
                or arc is None
                or baseline is None
            ):
                raise CommandPreconditionError(
                    "Evidence correction review facts are incomplete."
                )
            result_bytes = (
                await session.content.get_packed(
                    project_id=request.project_id,
                    ref_id=task.result_ref_id,
                )
            ).unpack_and_verify()
            evaluation = ChapterEvidenceCorrectionEvaluation.model_validate_json(
                result_bytes
            )
            base_plan = await session.content.get_packed(
                project_id=request.project_id,
                ref_id=baseline.plan_ref_id,
            )
            submitted_plan = await session.content.get_packed(
                project_id=request.project_id,
                ref_id=submission.plan_ref_id,
            )
            base_prose = await session.content.get_packed(
                project_id=request.project_id,
                ref_id=baseline.prose_ref_id,
            )
            submitted_prose = await session.content.get_packed(
                project_id=request.project_id,
                ref_id=submission.draft_ref_id,
            )
            committed = await session.chapters.list_committed_baselines(
                project_id=request.project_id,
                book_id=chapter.book_id,
            )

        if not (
            evaluation.observations_supported_by_frozen_prose
            and evaluation.canon_intent_supported_by_frozen_prose
            and evaluation.descendant_facts_remain_consistent
        ):
            raise ChapterEvidenceVerificationFailure(
                "The one-shot evidence correction verification did not pass; "
                "automatic Chapter repair is not authorized."
            )
        if (
            task.role != "evaluator"
            or task.task_kind != "verify_evidence.chapter"
            or task.scope_layer != "chapter"
            or task.chapter_id != request.chapter_id
            or task.book_id != submission.book_id
            or task.arc_id != submission.arc_id
            or task.workspace_lock_version != submission.workspace_lock_version
            or task.book_baseline_id != submission.book_baseline_id
            or task.arc_baseline_id != submission.arc_baseline_id
            or task.chapter_baseline_id != submission.base_chapter_baseline_id
            or task.canon_baseline_id != submission.canon_before_id
            or task.evaluation_strategy_id != strategy.strategy_id
            or task.evaluation_strategy_version != strategy.strategy_version
            or task.rubric_id != strategy.rubric_id
            or task.rubric_version != strategy.rubric_version
            or request.rubric_id != strategy.rubric_id
            or request.rubric_version != strategy.rubric_version
            or task.automatic_correction_round != 1
            or task.correction_lineage_id != workspace.correction_lineage_id
            or (
                task.source_arc_parent_review_id
                != workspace.source_arc_parent_review_id
            )
            or (
                task.source_arc_closure_review_id
                != workspace.source_arc_closure_review_id
            )
            or (task.source_arc_parent_review_id is None)
            == (task.source_arc_closure_review_id is None)
        ):
            raise CommandPreconditionError(
                "Evidence correction task does not match its frozen authority."
            )
        plan_unchanged = (
            base_plan.reference.blob_sha256
            == submitted_plan.reference.blob_sha256
        )
        prose_unchanged = (
            base_prose.reference.blob_sha256
            == submitted_prose.reference.blob_sha256
        )
        formal_arc_not_closed = (
            arc.current_closure_id is None
            and arc.lifecycle_status in {"active", "closing"}
        )
        if (
            not plan_unchanged
            or not prose_unchanged
            or not formal_arc_not_closed
            or chapter.current_baseline_id != baseline.id
            or workspace.revision_origin
            not in {"arc_evidence_correction", "user_initiated"}
        ):
            raise CommandPreconditionError(
                "Evidence correction violated its byte-frozen Chapter boundary."
            )
        descendant_count = sum(
            item.chapter_id != chapter.id
            and item.created_at_ms >= baseline.created_at_ms
            for item in committed
        )
        prepared_precheck = prepare_canonical_json(
            {
                "schema_id": "chapter-evidence-precheck-v1",
                "passed": True,
                "plan_bytes_unchanged": True,
                "prose_bytes_unchanged": True,
                "formal_arc_not_closed": True,
                "descendant_context_count": descendant_count,
            }
        )
        prepared_detail = prepare_canonical_json(evaluation)
        envelope = self._envelope(
            request=request,
            project_id=request.project_id,
            idempotency_key=idempotency_key,
            command_kind="record_chapter_evidence_review",
            actor="engine",
            source_task_id=request.evaluator_task_id,
            created_at_ms=timestamp,
        )

        async def handler(
            session: StoreSession,
        ) -> CommandEffect[RecordChapterReviewResult]:
            current_task = await session.execution.get_successful_task(
                project_id=request.project_id,
                task_id=request.evaluator_task_id,
                attempt_id=request.evaluator_attempt_id,
            )
            current_submission = await session.chapters.get_submission(
                project_id=request.project_id,
                submission_id=request.submission_id,
            )
            current_chapter = await session.chapters.get(
                project_id=request.project_id,
                chapter_id=request.chapter_id,
            )
            current_workspace = await session.chapters.get_workspace(
                project_id=request.project_id,
                chapter_id=request.chapter_id,
            )
            current_arc = await session.arcs.get(
                project_id=request.project_id,
                arc_id=submission.arc_id,
            )
            if (
                current_task != task
                or task.delivery_state != "pending"
                or current_submission != submission
                or submission.disposition != "pending"
                or current_chapter != chapter
                or current_workspace != workspace
                or current_arc != arc
                or chapter.current_baseline_id != baseline.id
            ):
                raise CommandPreconditionError(
                    "Evidence correction authority changed before delivery."
                )
            precheck_ref = await session.content.put(
                project_id=request.project_id,
                prepared=prepared_precheck,
                semantic_kind="chapter.evidence_precheck",
                media_type="application/json",
                schema_id="chapter-evidence-precheck",
                schema_version=1,
                ref_id=precheck_ref_id,
                created_at_ms=timestamp,
            )
            detail_ref = await session.content.put(
                project_id=request.project_id,
                prepared=prepared_detail,
                semantic_kind="chapter.evidence_review_detail",
                media_type="application/json",
                schema_id="chapter-evidence-correction-evaluation",
                schema_version=1,
                ref_id=detail_ref_id,
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
                    decision="pass",
                    rubric_id=request.rubric_id,
                    rubric_version=request.rubric_version,
                    precheck_ref_id=precheck_ref.id,
                    detail_ref_id=detail_ref.id,
                    repair_contract_ref_id=None,
                    created_at_ms=timestamp,
                )
            )
            if not await session.execution.mark_delivery_applied(
                project_id=request.project_id,
                task_id=request.evaluator_task_id,
                attempt_id=request.evaluator_attempt_id,
                command_id=envelope.command_id,
                updated_at_ms=timestamp,
            ):
                raise CommandPreconditionError(
                    "Evidence evaluator delivery changed concurrently."
                )
            return CommandEffect(
                result=RecordChapterReviewResult(
                    project_id=request.project_id,
                    chapter_id=request.chapter_id,
                    submission_id=submission.id,
                    review_id=review_id,
                    decision="pass",
                ),
                events=(
                    EventDraft(
                        event_type="chapter.evidence_reviewed",
                        aggregate_type="chapter",
                        aggregate_id=request.chapter_id,
                        payload={
                            "submission_id": submission.id,
                            "review_id": review_id,
                            "decision": "pass",
                        },
                    ),
                ),
            )

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
            evaluator_task = await session.execution.get_successful_task(
                project_id=request.project_id,
                task_id=review.evaluator_task_id,
                attempt_id=review.evaluator_attempt_id,
            )
            if evaluator_task is None:
                raise CommandPreconditionError(
                    "Chapter commit evaluator evidence is incomplete."
                )
            evidence_correction_source_baseline_id = (
                evaluator_task.chapter_baseline_id
                if evaluator_task.task_kind == "verify_evidence.chapter"
                else None
            )
            if (
                evaluator_task.task_kind == "verify_evidence.chapter"
                and evidence_correction_source_baseline_id is None
            ):
                raise CommandPreconditionError(
                    "Evidence correction lost its source Chapter baseline."
                )
            plan_bytes = (
                await session.content.get_packed(
                    project_id=request.project_id,
                    ref_id=submission.plan_ref_id,
                )
            ).unpack_and_verify()
            packed_prose = await session.content.get_packed(
                project_id=request.project_id,
                ref_id=submission.draft_ref_id,
            )
            prose_bytes = packed_prose.unpack_and_verify()
            observations_bytes = (
                await session.content.get_packed(
                    project_id=request.project_id,
                    ref_id=submission.observations_ref_id,
                )
            ).unpack_and_verify()
            patch_bytes = (
                await session.content.get_packed(
                    project_id=request.project_id,
                    ref_id=submission.candidate_canon_patch_ref_id,
                )
            ).unpack_and_verify()
            ref_by_category = _canon_ref_ids(canon_before)
            current_categories = await _load_canon_categories(
                session,
                project_id=request.project_id,
                baseline=canon_before,
            )
        plan = ChapterPlanProposal.model_validate_json(plan_bytes)
        prose = prose_bytes.decode("utf-8")
        observation_candidate = ChapterObservationResult.model_validate_json(
            observations_bytes
        )
        patch = BoundCanonPatch.model_validate_json(patch_bytes)
        applied = apply_canon_patch(
            chapter_id=request.chapter_id,
            chapter_baseline_id=chapter_baseline_id,
            prose_ref_id=submission.draft_ref_id,
            current=current_categories,
            patch=patch,
            replace_evidence_for_chapter_baseline_id=(
                evidence_correction_source_baseline_id
            ),
        )
        committed_observation = bind_committed_chapter_observation(
            candidate=observation_candidate,
            chapter_id=request.chapter_id,
            chapter_baseline_id=chapter_baseline_id,
            prose_ref_id=submission.draft_ref_id,
            prose_sha256=packed_prose.reference.blob_sha256,
        )
        prepared_committed_observation = prepare_canonical_json(
            committed_observation
        )
        committed_observations_ref_id = self._id_factory()
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
            current_arc = (
                None
                if chapter is None
                else await session.arcs.get(
                    project_id=request.project_id,
                    arc_id=chapter.arc_id,
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
                or current_arc is None
                or current_arc.current_closure_id is not None
                or current_arc.lifecycle_status == "completed"
                or context.book_baseline_id != submission.book_baseline_id
                or context.arc_baseline_id != submission.arc_baseline_id
                or context.canon_baseline_id != submission.canon_before_id
            ):
                raise CommandPreconditionError("Chapter commit facts are stale or unapproved.")
            evidence_only = False
            if workspace.source_arc_parent_review_id is not None:
                source_arc_review = await session.arc_parent_reviews.get(
                    project_id=request.project_id,
                    review_id=workspace.source_arc_parent_review_id,
                )
                evidence_only = (
                    source_arc_review is not None
                    and source_arc_review.disposition
                    == "chapter_evidence_review_required"
                )
            elif workspace.source_arc_closure_review_id is not None:
                source_closure_review = await session.arc_closure_reviews.get(
                    project_id=request.project_id,
                    review_id=workspace.source_arc_closure_review_id,
                )
                evidence_only = (
                    source_closure_review is not None
                    and source_closure_review.disposition
                    == "chapter_evidence_review_required"
                )
            if request.expected_current_chapter_baseline_id is not None:
                if evidence_only:
                    frozen_baseline = await session.chapters.get_baseline(
                        project_id=request.project_id,
                        chapter_id=chapter.id,
                        baseline_id=request.expected_current_chapter_baseline_id,
                    )
                    if frozen_baseline is None:
                        raise CommandPreconditionError(
                            "Evidence correction lost its frozen Chapter baseline."
                        )
                    frozen_plan = await session.content.get_packed(
                        project_id=request.project_id,
                        ref_id=frozen_baseline.plan_ref_id,
                    )
                    submitted_plan = await session.content.get_packed(
                        project_id=request.project_id,
                        ref_id=submission.plan_ref_id,
                    )
                    frozen_prose = await session.content.get_packed(
                        project_id=request.project_id,
                        ref_id=frozen_baseline.prose_ref_id,
                    )
                    submitted_prose = await session.content.get_packed(
                        project_id=request.project_id,
                        ref_id=submission.draft_ref_id,
                    )
                    if (
                        frozen_plan.reference.blob_sha256
                        != submitted_plan.reference.blob_sha256
                        or frozen_prose.reference.blob_sha256
                        != submitted_prose.reference.blob_sha256
                    ):
                        raise CommandPreconditionError(
                            "Evidence correction changed byte-frozen plan or prose."
                        )
                else:
                    historical_blocker = (
                        await session.chapters.narrative_replacement_blocker(
                            project_id=request.project_id,
                            book_id=chapter.book_id,
                            arc_id=chapter.arc_id,
                            chapter_id=chapter.id,
                        )
                    )
                    if historical_blocker is not None:
                        raise CommandPreconditionError(
                            "Narrative replacement is no longer at the current "
                            f"lineage tip: {historical_blocker}."
                        )
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
            cumulative_committed_before = (
                await session.chapters.count_committed_for_book(
                    book_id=chapter.book_id
                )
            )
            effective_cumulative_count = (
                cumulative_committed_before + 1
                if chapter.lifecycle_status == "drafting"
                else cumulative_committed_before
            )
            if (
                effective_cumulative_count
                > context.closure_cumulative_chapter_count
            ):
                raise CommandPreconditionError(
                    "Chapter commit exceeds the frozen Arc cumulative closure checkpoint."
                )

            for category in applied.changed_categories:
                reference = await session.content.put(
                    project_id=request.project_id,
                    prepared=prepared_commit.prepared_categories[category],
                    semantic_kind=f"canon.{category}",
                    media_type="application/json",
                    schema_id=f"canon-{category.replace('_', '-')}",
                    schema_version=3,
                    ref_id=new_ref_ids[category],
                    created_at_ms=timestamp,
                )
                if reference.id != resulting_ref_ids[category]:  # pragma: no cover
                    raise RuntimeError("Canon content reference identity changed.")
            committed_observations_ref = await session.content.put(
                project_id=request.project_id,
                prepared=prepared_committed_observation,
                semantic_kind="chapter.committed_observations",
                media_type="application/json",
                schema_id="chapter-committed-observation",
                schema_version=1,
                ref_id=committed_observations_ref_id,
                created_at_ms=timestamp,
            )
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
                    revision_origin=workspace.revision_origin,
                    source_arc_parent_review_id=workspace.source_arc_parent_review_id,
                    source_arc_closure_review_id=workspace.source_arc_closure_review_id,
                    plan_ref_id=submission.plan_ref_id,
                    prose_ref_id=submission.draft_ref_id,
                    observations_ref_id=committed_observations_ref.id,
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
                active_repair_review_id=None,
                base_chapter_baseline_id=chapter_baseline_id,
                canon_baseline_id=canon_after_id,
                guidance_ref_id=None,
                source_feedback_id=None,
                semantic_repair_count=0,
                updated_at_ms=timestamp,
            )
            if not await session.chapters.compare_and_set_workspace(
                record=updated_workspace,
                expected_lock_version=workspace.lock_version,
            ):
                raise CommandPreconditionError("Chapter workspace finalization CAS failed.")
            cumulative_committed_after = (
                await session.chapters.count_committed_for_book(
                    book_id=chapter.book_id
                )
            )
            if cumulative_committed_after != effective_cumulative_count:
                raise CommandPreconditionError(
                    "Whole-Book committed Chapter count changed concurrently."
                )
            arc_closure_due = (
                cumulative_committed_after
                == context.closure_cumulative_chapter_count
            )
            if chapter.lifecycle_status != "committed" and arc_closure_due:
                if not await session.chapters.begin_arc_closure_review(
                    project_id=request.project_id,
                    arc_id=chapter.arc_id,
                    arc_baseline_id=context.arc_baseline_id,
                    cumulative_committed_count=cumulative_committed_after,
                    closure_cumulative_chapter_count=(
                        context.closure_cumulative_chapter_count
                    ),
                    now_ms=timestamp,
                ):
                    raise CommandPreconditionError(
                        "Arc could not enter its planned closure review boundary."
                    )
            result = CommitChapterResult(
                project_id=request.project_id,
                chapter_id=chapter.id,
                chapter_baseline_id=chapter_baseline_id,
                chapter_baseline_version=chapter_version,
                canon_before_id=canon_before.id,
                canon_after_id=canon_after_id,
                canon_changed=prepared_commit.applied.changed,
                arc_closure_due=arc_closure_due,
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
                        "arc_closure_due": arc_closure_due,
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
        and task.workspace_work_cycle_id == workspace.work_cycle_id
        and task.book_baseline_id == workspace.book_baseline_id
        and task.arc_baseline_id == workspace.arc_baseline_id
        and task.chapter_baseline_id == workspace.base_chapter_baseline_id
        and task.canon_baseline_id == workspace.canon_baseline_id
        and task.source_chapter_candidate_review_id
        == workspace.active_repair_review_id
        and task.source_arc_parent_review_id
        == workspace.source_arc_parent_review_id
        and task.source_arc_closure_review_id
        == workspace.source_arc_closure_review_id
        and task.source_feedback_id == workspace.source_feedback_id
        and workspace.state == "active"
    )


def _chapter_review_decision(
    evaluation: LayerEvaluationResult | ChapterRepairVerificationResult,
) -> ChapterReviewDecision:
    return evaluation.decision


def _apply_chapter_precheck(
    evaluation: ChapterRepairVerificationResult,
    precheck: dict[str, object],
) -> ChapterRepairVerificationResult:
    passed = precheck.get("passed")
    if passed is True:
        return evaluation
    if passed is not False:
        raise CommandPreconditionError(
            "Chapter deterministic precheck has no explicit pass/fail result."
        )
    raw_issues = precheck.get("issues")
    if not isinstance(raw_issues, list) or not raw_issues:
        raise CommandPreconditionError(
            "A failed Chapter deterministic precheck requires typed issues."
        )
    try:
        precheck_issues = [
            ChapterRepairVerificationIssue.model_validate(issue)
            for issue in raw_issues
        ]
    except ValidationError as error:
        raise CommandPreconditionError(
            "Chapter deterministic precheck issues are invalid."
        ) from error
    if (
        evaluation.decision == "local_repair"
        and "plan" in _chapter_repair_scope(evaluation)
    ):
        # Replacing the cohesive plan invalidates and regenerates the downstream
        # observations/Canon candidate, so the stale patch is not a second repair
        # component in this review.
        return evaluation

    return ChapterRepairVerificationResult(
        guidance_authority_judgment=(
            evaluation.guidance_authority_judgment
        ),
        decision="local_repair",
        summary=(
            "Deterministic Chapter/Canon precheck requires a bounded repair. "
            f"{evaluation.summary}"
        ),
        issues=[*evaluation.issues, *precheck_issues],
    )


def _canon_ref_ids(baseline: CanonBaselineRecord) -> dict[CanonCategory, str]:
    return {
        "characters": baseline.characters_ref_id,
        "relationships": baseline.relationships_ref_id,
        "world_facts": baseline.world_facts_ref_id,
        "foreshadowing": baseline.foreshadowing_ref_id,
    }


async def _load_canon_categories(
    session: StoreSession,
    *,
    project_id: str,
    baseline: CanonBaselineRecord,
) -> dict[CanonCategory, list[CanonEntry]]:
    ref_by_category = _canon_ref_ids(baseline)
    return {
        category: TypeAdapter(list[CanonEntry]).validate_json(
            (
                await session.content.get_packed(
                    project_id=project_id,
                    ref_id=ref_by_category[category],
                )
            ).unpack_and_verify()
        )
        for category in CANON_CATEGORIES
    }


def _require_repair_budget(workspace: ChapterWorkspaceRecord) -> None:
    if workspace.semantic_repair_count >= workspace.semantic_repair_limit:
        raise CommandPreconditionError("Chapter semantic repair limit is exhausted.")
