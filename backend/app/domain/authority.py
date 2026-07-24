from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict

from app.agents.contracts import ArcPlanProposal
from app.agents.registry import DEFAULT_EVALUATION_STRATEGY_REGISTRY
from app.db.uow import StoreSession
from app.domain.book.contracts import CompletionContract
from app.domain.commands import (
    CommandEffect,
    CommandEnvelope,
    CommandExecution,
    CommandPreconditionError,
    EventDraft,
)
from app.domain.evaluation import (
    ArcClosureEvaluation,
    ArcParentContractEvaluation,
    BookBoundaryEvaluation,
    BookParentContractEvaluation,
)
from app.store.arcs import (
    ArcBaselineRecord,
    ArcBookChangeRequestRecord,
    ArcRecord,
    ArcWorkspaceRecord,
)
from app.store.authority import (
    ArcClosureRecord,
    ArcClosureReviewRecord,
    ArcParentReviewRecord,
    BookBoundaryReviewRecord,
    BookParentReviewRecord,
    BookProgressHandoffRecord,
)
from app.store.command_bus import CommandBus
from app.store.change_requests import (
    ArcBookChangeRequestRecord as StoredArcBookChangeRequestRecord,
    ChapterArcChangeRequestRecord,
)
from app.store.completion import BookCompletionRecord
from app.store.books import BookBaselineRecord, BookRecord, BookWorkspaceRecord
from app.store.chapters import (
    ChapterBaselineRecord,
    ChapterRecord,
)
from app.store.content import prepare_canonical_json
from app.store.execution import SuccessfulTaskRecord


@dataclass(frozen=True, slots=True)
class _ReviewLineageLinks:
    lineage_id: str
    lineage_origin: str
    correction_round: int
    review_ordinal: int
    predecessor_review_id: str | None
    source_exhausted_review_id: str | None


class AuthorityTaskFailure(RuntimeError):
    """A frozen Evaluator activation cannot produce a legal authority decision."""

    def __init__(self, *, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class RecordArcClosureReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    book_id: str
    arc_id: str
    task_id: str
    attempt_id: str


class RecordArcParentReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    book_id: str
    arc_id: str
    request_id: str
    task_id: str
    attempt_id: str


class RecordArcParentReviewResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    arc_id: str
    request_id: str
    review_id: str
    disposition: Literal[
        "keep_arc",
        "arc_revision_warranted",
        "book_review_required",
        "chapter_evidence_review_required",
        "waiting_for_user",
        "no_legal_route",
    ]
    change_request_id: str | None = None
    downstream_action: Literal[
        "none",
        "chapter_correction_opened",
        "chapter_evidence_correction_opened",
        "historical_rewrite_unsupported",
        "request_resolved",
    ] = "none"


class RecordBookParentReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    book_id: str
    request_id: str
    task_id: str
    attempt_id: str


class RecordBookParentReviewResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    book_id: str
    request_id: str
    review_id: str
    disposition: Literal[
        "keep_book",
        "book_revision_warranted",
        "arc_evidence_review_required",
        "waiting_for_user",
        "no_legal_route",
    ]
    downstream_action: Literal[
        "none",
        "arc_correction_opened",
        "arc_revision_limit_reached",
        "request_resolved",
    ] = "none"


class RecordArcClosureReviewResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    arc_id: str
    review_id: str
    disposition: Literal[
        "pass",
        "arc_revision_warranted",
        "book_review_required",
        "chapter_evidence_review_required",
        "waiting_for_user",
        "no_legal_route",
    ]
    formal_closure_id: str | None = None
    change_request_id: str | None = None
    downstream_action: Literal[
        "none",
        "chapter_evidence_correction_opened",
        "historical_rewrite_unsupported",
    ] = "none"


class RecordBookBoundaryReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    book_id: str
    task_id: str
    attempt_id: str


class RecordBookBoundaryReviewResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    book_id: str
    review_id: str
    disposition: Literal[
        "continue_regular_arc",
        "plan_final_arc",
        "complete_book",
        "book_revision_warranted",
        "waiting_for_user",
        "no_legal_route",
    ]


class CommitBookProgressHandoffRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    book_id: str
    boundary_review_id: str


class CommitBookProgressHandoffResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    book_id: str
    boundary_review_id: str
    handoff_id: str
    next_arc_purpose: Literal["regular", "final"]


class CommitBookCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    book_id: str
    boundary_review_id: str


class CommitBookCompletionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    book_id: str
    boundary_review_id: str
    completion_id: str


class OpenArcClosureRevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    book_id: str
    arc_id: str
    closure_review_id: str
    expected_workspace_lock_version: int


class OpenArcClosureRevisionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    arc_id: str
    closure_review_id: str
    action: Literal["revision_workspace_opened", "arc_revision_limit_reached"]
    workspace_lock_version: int | None = None


class OpenBookBoundaryRevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    book_id: str
    boundary_review_id: str
    expected_workspace_lock_version: int


class OpenBookBoundaryRevisionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    book_id: str
    boundary_review_id: str
    workspace_lock_version: int


class LoopAuthorityCommandService:
    """Convert typed Evaluator evidence into explicit Arc/Book authority."""

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
        source_task_id: str | None,
        timestamp: int,
    ) -> CommandEnvelope:
        return CommandEnvelope.for_request(
            project_id=project_id,
            idempotency_key=idempotency_key,
            command_kind=command_kind,
            request_schema=f"{command_kind}.request.v1",
            request_payload=request,
            actor="engine",
            command_id=self._id_factory(),
            source_task_id=source_task_id,
            created_at_ms=timestamp,
        )

    async def record_arc_parent_review(
        self,
        request: RecordArcParentReviewRequest,
        *,
        idempotency_key: str,
    ) -> CommandExecution[RecordArcParentReviewResult]:
        timestamp = self._now_ms()
        review_id = self._id_factory()
        precheck_ref_id = self._id_factory()
        question_ref_id = self._id_factory()
        change_request_id = self._id_factory()
        async with self._command_bus.read_unit_of_work() as session:
            task = await self._successful_task(
                session,
                project_id=request.project_id,
                task_id=request.task_id,
                attempt_id=request.attempt_id,
                task_kind="evaluate.arc_parent_contract",
            )
            evaluation = ArcParentContractEvaluation.model_validate_json(
                (
                    await session.content.get_packed(
                        project_id=request.project_id,
                        ref_id=task.result_ref_id,
                    )
                ).unpack_and_verify()
            )
            change, arc, baseline, workspace = await self._arc_parent_snapshot(
                session,
                request=request,
                task=task,
            )
            disposition = cast(
                Literal[
                    "keep_arc",
                    "arc_revision_warranted",
                    "book_review_required",
                    "chapter_evidence_review_required",
                    "waiting_for_user",
                    "no_legal_route",
                ],
                self._bounded_review_disposition(
                    disposition=self._arc_parent_disposition(evaluation),
                    task=task,
                    has_creator_question=evaluation.creator_input_need is not None,
                    correction_dispositions=frozenset(
                        {
                            "arc_revision_warranted",
                            "chapter_evidence_review_required",
                        }
                    ),
                ),
            )
            prepared_precheck = prepare_canonical_json(
                {
                    "schema_id": "arc-parent-review-precheck-v1",
                    "passed": True,
                    "request_status": change.status,
                    "target_arc_baseline_current": True,
                    "source_chapter_review_applied": True,
                }
            )
            prepared_question = (
                None
                if evaluation.creator_input_need is None
                else prepare_canonical_json(evaluation.creator_input_need)
            )
            exact_input_fingerprint = prepare_canonical_json(
                {
                    "request_id": change.id,
                    "request_evidence_ref_id": change.evidence_ref_id,
                    "target_arc_baseline_id": baseline.id,
                    "canon_baseline_id": task.canon_baseline_id,
                    "predecessor_review_id": task.source_arc_parent_review_id,
                    "strategy_id": task.evaluation_strategy_id,
                    "strategy_version": task.evaluation_strategy_version,
                }
            ).sha256
        envelope = self._envelope(
            request=request,
            project_id=request.project_id,
            idempotency_key=idempotency_key,
            command_kind="record_arc_parent_review",
            source_task_id=request.task_id,
            timestamp=timestamp,
        )

        async def handler(
            session: StoreSession,
        ) -> CommandEffect[RecordArcParentReviewResult]:
            current_task = await self._successful_task(
                session,
                project_id=request.project_id,
                task_id=request.task_id,
                attempt_id=request.attempt_id,
                task_kind="evaluate.arc_parent_contract",
            )
            current = await self._arc_parent_snapshot(
                session,
                request=request,
                task=task,
            )
            if (
                current_task != task
                or task.delivery_state != "pending"
                or current != (change, arc, baseline, workspace)
            ):
                raise CommandPreconditionError(
                    "Arc parent-review authority changed before delivery."
                )
            latest = await session.arc_parent_reviews.get_latest_for_request(
                project_id=request.project_id,
                request_id=request.request_id,
            )
            source_review = (
                None
                if task.source_arc_parent_review_id is None
                else await session.arc_parent_reviews.get(
                    project_id=request.project_id,
                    review_id=task.source_arc_parent_review_id,
                )
            )
            if (
                (latest is None) != (source_review is None)
                or (
                    latest is not None
                    and source_review is not None
                    and latest.id != source_review.id
                )
            ):
                raise CommandPreconditionError(
                    "Arc parent-review predecessor is not current."
                )
            lineage = self._review_lineage_links(
                task=task,
                source_review=source_review,
            )
            precheck_ref = await session.content.put(
                project_id=request.project_id,
                prepared=prepared_precheck,
                semantic_kind="arc.parent_review_precheck",
                media_type="application/json",
                schema_id="arc-parent-review-precheck",
                schema_version=1,
                ref_id=precheck_ref_id,
                created_at_ms=timestamp,
            )
            question_ref = None
            if prepared_question is not None:
                question_ref = await session.content.put(
                    project_id=request.project_id,
                    prepared=prepared_question,
                    semantic_kind="review.creator_question",
                    media_type="application/json",
                    schema_id="creator-input-need",
                    schema_version=1,
                    ref_id=question_ref_id,
                    created_at_ms=timestamp,
                )
            await session.arc_parent_reviews.insert(
                ArcParentReviewRecord(
                    id=review_id,
                    project_id=request.project_id,
                    book_id=request.book_id,
                    arc_id=request.arc_id,
                    request_id=request.request_id,
                    target_arc_baseline_id=baseline.id,
                    source_task_id=task.task_id,
                    source_attempt_id=task.attempt_id,
                    strategy_id=self._required_text(
                        task.evaluation_strategy_id, "evaluation strategy"
                    ),
                    strategy_version=self._required_int(
                        task.evaluation_strategy_version, "evaluation strategy"
                    ),
                    rubric_id=self._required_text(task.rubric_id, "rubric"),
                    rubric_version=self._required_int(
                        task.rubric_version, "rubric"
                    ),
                    arc_contract_judgment=evaluation.arc_contract_judgment,
                    parent_review_judgment=evaluation.book_review_concern,
                    disposition=disposition,
                    resolution_owner=self._arc_resolution_owner(disposition),
                    detail_ref_id=task.result_ref_id,
                    precheck_ref_id=precheck_ref.id,
                    user_question_ref_id=(
                        None if question_ref is None else question_ref.id
                    ),
                    exact_input_fingerprint=exact_input_fingerprint,
                    correction_lineage_id=lineage.lineage_id,
                    correction_lineage_origin=lineage.lineage_origin,
                    automatic_correction_round=lineage.correction_round,
                    review_ordinal=lineage.review_ordinal,
                    predecessor_review_id=lineage.predecessor_review_id,
                    source_feedback_id=task.source_feedback_id,
                    source_exhausted_review_id=(
                        lineage.source_exhausted_review_id
                    ),
                    opened_arc_workspace_id=None,
                    created_at_ms=timestamp,
                )
            )
            if not await session.changes.mark_chapter_arc_reviewed(
                project_id=request.project_id,
                request_id=request.request_id,
                expected_latest_review_id=(
                    None if source_review is None else source_review.id
                ),
                review_id=review_id,
            ):
                raise CommandPreconditionError(
                    "Chapter-to-Arc latest review pointer CAS failed."
                )
            opened_change_id: str | None = None
            downstream_action: Literal[
                "none",
                "chapter_correction_opened",
                "chapter_evidence_correction_opened",
                "historical_rewrite_unsupported",
                "request_resolved",
            ] = "none"
            events = [
                EventDraft(
                    event_type="arc.parent_reviewed",
                    aggregate_type="arc",
                    aggregate_id=request.arc_id,
                    payload={
                        "request_id": request.request_id,
                        "review_id": review_id,
                        "disposition": disposition,
                    },
                )
            ]
            if disposition == "keep_arc":
                if lineage.correction_round == 1:
                    if not await session.changes.resolve_chapter_arc_without_replacement(
                        project_id=request.project_id,
                        request_id=request.request_id,
                        parent_review_id=review_id,
                        current_arc_baseline_id=baseline.id,
                        resolution_code="arc_parent_successor_kept_arc",
                        now_ms=timestamp,
                    ):
                        raise CommandPreconditionError(
                            "Chapter-to-Arc request could not resolve after successor review."
                        )
                    downstream_action = "request_resolved"
                else:
                    source_chapter = await session.chapters.get(
                        project_id=request.project_id,
                        chapter_id=change.chapter_id,
                    )
                    if source_chapter is None:
                        raise CommandPreconditionError(
                            "Arc parent guidance lost its source Chapter."
                        )
                    downstream_action = await self._open_chapter_correction(
                        session,
                        task=task,
                        chapter=source_chapter,
                        guidance_ref_id=task.result_ref_id,
                        source_arc_parent_review_id=review_id,
                        source_arc_closure_review_id=None,
                        correction_lineage_id=lineage.lineage_id,
                        correction_lineage_origin=lineage.lineage_origin,
                        evidence_only=False,
                        timestamp=timestamp,
                    )
            elif disposition == "chapter_evidence_review_required":
                target = evaluation.chapter_evidence_target
                if target is None:
                    raise AuthorityTaskFailure(
                        code="evaluation_contract_invalid",
                        message="Arc parent evidence review has no semantic Chapter target.",
                    )
                evidence_chapter = await session.chapters.get_by_book_ordinal(
                    project_id=request.project_id,
                    book_id=request.book_id,
                    book_ordinal=target.chapter_book_ordinal,
                )
                if evidence_chapter is None or evidence_chapter.arc_id != request.arc_id:
                    raise AuthorityTaskFailure(
                        code="evaluation_contract_invalid",
                        message=(
                            "Arc parent evidence target is not a Chapter in the current Arc."
                        ),
                    )
                downstream_action = await self._open_chapter_correction(
                    session,
                    task=task,
                    chapter=evidence_chapter,
                    guidance_ref_id=task.result_ref_id,
                    source_arc_parent_review_id=review_id,
                    source_arc_closure_review_id=None,
                    correction_lineage_id=lineage.lineage_id,
                    correction_lineage_origin=lineage.lineage_origin,
                    evidence_only=True,
                    timestamp=timestamp,
                )
            elif disposition == "book_review_required":
                await session.arcs.insert_book_change_request(
                    ArcBookChangeRequestRecord(
                        id=change_request_id,
                        project_id=request.project_id,
                        book_id=request.book_id,
                        arc_id=request.arc_id,
                        source_candidate_submission_id=None,
                        source_candidate_review_id=None,
                        source_arc_parent_review_id=review_id,
                        source_arc_closure_review_id=None,
                        target_book_baseline_id=baseline.book_baseline_id,
                        evidence_ref_id=task.result_ref_id,
                        status="open",
                        created_at_ms=timestamp,
                    )
                )
                opened_change_id = change_request_id
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
            elif disposition == "waiting_for_user":
                if not await session.runs.ensure_wait_for_user(
                    run_id=task.run_id,
                    reason_code=(
                        "evidence_correction_needs_user"
                        if task.automatic_correction_round == 1
                        else "arc_parent_review_needs_user"
                    ),
                    now_ms=timestamp,
                ):
                    raise CommandPreconditionError(
                        "Run could not enter the Arc parent-review wait."
                    )
            if not await session.execution.mark_delivery_applied(
                project_id=request.project_id,
                task_id=request.task_id,
                attempt_id=request.attempt_id,
                command_id=envelope.command_id,
                updated_at_ms=timestamp,
            ):
                raise CommandPreconditionError(
                    "Arc parent-review delivery changed concurrently."
                )
            return CommandEffect(
                result=RecordArcParentReviewResult(
                    project_id=request.project_id,
                    arc_id=request.arc_id,
                    request_id=request.request_id,
                    review_id=review_id,
                    disposition=disposition,
                    change_request_id=opened_change_id,
                    downstream_action=downstream_action,
                ),
                events=tuple(events),
            )

        return await self._command_bus.execute(
            envelope=envelope,
            result_type=RecordArcParentReviewResult,
            handler=handler,
        )

    async def record_book_parent_review(
        self,
        request: RecordBookParentReviewRequest,
        *,
        idempotency_key: str,
    ) -> CommandExecution[RecordBookParentReviewResult]:
        timestamp = self._now_ms()
        review_id = self._id_factory()
        precheck_ref_id = self._id_factory()
        question_ref_id = self._id_factory()
        async with self._command_bus.read_unit_of_work() as session:
            task = await self._successful_task(
                session,
                project_id=request.project_id,
                task_id=request.task_id,
                attempt_id=request.attempt_id,
                task_kind="evaluate.book_parent_contract",
            )
            evaluation = BookParentContractEvaluation.model_validate_json(
                (
                    await session.content.get_packed(
                        project_id=request.project_id,
                        ref_id=task.result_ref_id,
                    )
                ).unpack_and_verify()
            )
            change, book, baseline, workspace = await self._book_parent_snapshot(
                session,
                request=request,
                task=task,
            )
            disposition = cast(
                Literal[
                    "keep_book",
                    "book_revision_warranted",
                    "arc_evidence_review_required",
                    "waiting_for_user",
                    "no_legal_route",
                ],
                self._bounded_review_disposition(
                    disposition=self._book_parent_disposition(evaluation),
                    task=task,
                    has_creator_question=evaluation.creator_input_need is not None,
                    correction_dispositions=frozenset(
                        {
                            "book_revision_warranted",
                            "arc_evidence_review_required",
                        }
                    ),
                ),
            )
            prepared_precheck = prepare_canonical_json(
                {
                    "schema_id": "book-parent-review-precheck-v1",
                    "passed": True,
                    "request_status": change.status,
                    "target_book_baseline_current": True,
                    "source_arc_review_applied": True,
                }
            )
            prepared_question = (
                None
                if evaluation.creator_input_need is None
                else prepare_canonical_json(evaluation.creator_input_need)
            )
            exact_input_fingerprint = prepare_canonical_json(
                {
                    "request_id": change.id,
                    "request_evidence_ref_id": change.evidence_ref_id,
                    "target_book_baseline_id": baseline.id,
                    "canon_baseline_id": task.canon_baseline_id,
                    "predecessor_review_id": task.source_book_parent_review_id,
                    "strategy_id": task.evaluation_strategy_id,
                    "strategy_version": task.evaluation_strategy_version,
                }
            ).sha256
        envelope = self._envelope(
            request=request,
            project_id=request.project_id,
            idempotency_key=idempotency_key,
            command_kind="record_book_parent_review",
            source_task_id=request.task_id,
            timestamp=timestamp,
        )

        async def handler(
            session: StoreSession,
        ) -> CommandEffect[RecordBookParentReviewResult]:
            current_task = await self._successful_task(
                session,
                project_id=request.project_id,
                task_id=request.task_id,
                attempt_id=request.attempt_id,
                task_kind="evaluate.book_parent_contract",
            )
            current = await self._book_parent_snapshot(
                session,
                request=request,
                task=task,
            )
            if (
                current_task != task
                or task.delivery_state != "pending"
                or current != (change, book, baseline, workspace)
            ):
                raise CommandPreconditionError(
                    "Book parent-review authority changed before delivery."
                )
            latest = await session.book_parent_reviews.get_latest_for_request(
                project_id=request.project_id,
                request_id=request.request_id,
            )
            source_review = (
                None
                if task.source_book_parent_review_id is None
                else await session.book_parent_reviews.get(
                    project_id=request.project_id,
                    review_id=task.source_book_parent_review_id,
                )
            )
            if (
                (latest is None) != (source_review is None)
                or (
                    latest is not None
                    and source_review is not None
                    and latest.id != source_review.id
                )
            ):
                raise CommandPreconditionError(
                    "Book parent-review predecessor is not current."
                )
            lineage = self._review_lineage_links(
                task=task,
                source_review=source_review,
            )
            precheck_ref = await session.content.put(
                project_id=request.project_id,
                prepared=prepared_precheck,
                semantic_kind="book.parent_review_precheck",
                media_type="application/json",
                schema_id="book-parent-review-precheck",
                schema_version=1,
                ref_id=precheck_ref_id,
                created_at_ms=timestamp,
            )
            question_ref = None
            if prepared_question is not None:
                question_ref = await session.content.put(
                    project_id=request.project_id,
                    prepared=prepared_question,
                    semantic_kind="review.creator_question",
                    media_type="application/json",
                    schema_id="creator-input-need",
                    schema_version=1,
                    ref_id=question_ref_id,
                    created_at_ms=timestamp,
                )
            review_record = BookParentReviewRecord(
                id=review_id,
                project_id=request.project_id,
                book_id=request.book_id,
                arc_id=change.arc_id,
                request_id=request.request_id,
                target_book_baseline_id=baseline.id,
                source_task_id=task.task_id,
                source_attempt_id=task.attempt_id,
                strategy_id=self._required_text(
                    task.evaluation_strategy_id, "evaluation strategy"
                ),
                strategy_version=self._required_int(
                    task.evaluation_strategy_version, "evaluation strategy"
                ),
                rubric_id=self._required_text(task.rubric_id, "rubric"),
                rubric_version=self._required_int(
                    task.rubric_version, "rubric"
                ),
                book_contract_judgment=evaluation.book_contract_judgment,
                disposition=disposition,
                resolution_owner=self._book_resolution_owner(disposition),
                detail_ref_id=task.result_ref_id,
                precheck_ref_id=precheck_ref.id,
                user_question_ref_id=(
                    None if question_ref is None else question_ref.id
                ),
                exact_input_fingerprint=exact_input_fingerprint,
                correction_lineage_id=lineage.lineage_id,
                correction_lineage_origin=lineage.lineage_origin,
                automatic_correction_round=lineage.correction_round,
                review_ordinal=lineage.review_ordinal,
                predecessor_review_id=lineage.predecessor_review_id,
                source_feedback_id=task.source_feedback_id,
                source_exhausted_review_id=lineage.source_exhausted_review_id,
                opened_book_workspace_id=None,
                created_at_ms=timestamp,
            )
            await session.book_parent_reviews.insert(review_record)
            if not await session.changes.mark_arc_book_reviewed(
                project_id=request.project_id,
                request_id=request.request_id,
                expected_latest_review_id=(
                    None if source_review is None else source_review.id
                ),
                review_id=review_id,
            ):
                raise CommandPreconditionError(
                    "Arc-to-Book latest review pointer CAS failed."
                )
            downstream_action: Literal[
                "none",
                "arc_correction_opened",
                "arc_revision_limit_reached",
                "request_resolved",
            ] = "none"
            events = [
                EventDraft(
                    event_type="book.parent_reviewed",
                    aggregate_type="book",
                    aggregate_id=request.book_id,
                    payload={
                        "request_id": request.request_id,
                        "review_id": review_id,
                        "disposition": disposition,
                    },
                )
            ]
            if disposition == "keep_book":
                if lineage.correction_round == 1:
                    if not await session.changes.resolve_arc_book_without_replacement(
                        project_id=request.project_id,
                        request_id=request.request_id,
                        parent_review_id=review_id,
                        current_book_baseline_id=baseline.id,
                        resolution_code="book_parent_successor_kept_book",
                        now_ms=timestamp,
                    ):
                        raise CommandPreconditionError(
                            "Arc-to-Book request could not resolve after successor review."
                        )
                    downstream_action = "request_resolved"
                else:
                    downstream_action = (
                        await self._open_arc_correction_from_book_review(
                            session,
                            task=task,
                            review=review_record,
                            timestamp=timestamp,
                        )
                    )
            elif disposition == "arc_evidence_review_required":
                downstream_action = (
                    await self._open_arc_correction_from_book_review(
                        session,
                        task=task,
                        review=review_record,
                        timestamp=timestamp,
                    )
                )
            if disposition == "waiting_for_user" and not await session.runs.ensure_wait_for_user(
                run_id=task.run_id,
                reason_code=(
                    "evidence_correction_needs_user"
                    if task.automatic_correction_round == 1
                    else "book_parent_review_needs_user"
                ),
                now_ms=timestamp,
            ):
                raise CommandPreconditionError(
                    "Run could not enter the Book parent-review wait."
                )
            if not await session.execution.mark_delivery_applied(
                project_id=request.project_id,
                task_id=request.task_id,
                attempt_id=request.attempt_id,
                command_id=envelope.command_id,
                updated_at_ms=timestamp,
            ):
                raise CommandPreconditionError(
                    "Book parent-review delivery changed concurrently."
                )
            return CommandEffect(
                result=RecordBookParentReviewResult(
                    project_id=request.project_id,
                    book_id=request.book_id,
                    request_id=request.request_id,
                    review_id=review_id,
                    disposition=disposition,
                    downstream_action=downstream_action,
                ),
                events=tuple(events),
            )

        return await self._command_bus.execute(
            envelope=envelope,
            result_type=RecordBookParentReviewResult,
            handler=handler,
        )

    async def record_arc_closure_review(
        self,
        request: RecordArcClosureReviewRequest,
        *,
        idempotency_key: str,
    ) -> CommandExecution[RecordArcClosureReviewResult]:
        timestamp = self._now_ms()
        review_id = self._id_factory()
        closure_id = self._id_factory()
        change_request_id = self._id_factory()
        manifest_ref_id = self._id_factory()
        precheck_ref_id = self._id_factory()
        question_ref_id = self._id_factory()
        normalized_ref_id = self._id_factory()

        async with self._command_bus.read_unit_of_work() as session:
            task = await self._successful_task(
                session,
                project_id=request.project_id,
                task_id=request.task_id,
                attempt_id=request.attempt_id,
                task_kind="evaluate.arc_closure",
            )
            evaluation = ArcClosureEvaluation.model_validate_json(
                (
                    await session.content.get_packed(
                        project_id=request.project_id,
                        ref_id=task.result_ref_id,
                    )
                ).unpack_and_verify()
            )
            arc, baseline, workspace, chapters = await self._arc_closure_snapshot(
                session,
                request=request,
                task=task,
            )
            plan = ArcPlanProposal.model_validate_json(
                (
                    await session.content.get_packed(
                        project_id=request.project_id,
                        ref_id=baseline.plan_ref_id,
                    )
                ).unpack_and_verify()
            )
            self._validate_arc_signal_coverage(evaluation=evaluation, plan=plan)
            chapter_manifest = {
                "schema_id": "arc-closure-chapter-set-v1",
                "arc_ordinal": arc.ordinal,
                "closure_chapter_count": baseline.closure_chapter_count,
                "chapters": [
                    {
                        "chapter_id": chapter.chapter_id,
                        "chapter_baseline_id": chapter.id,
                        "baseline_version": chapter.baseline_version,
                        "canon_after_id": chapter.canon_after_id,
                        "observations_ref_id": chapter.observations_ref_id,
                    }
                    for chapter in chapters
                ],
            }
            prepared_manifest = prepare_canonical_json(chapter_manifest)
            disposition = cast(
                Literal[
                    "pass",
                    "arc_revision_warranted",
                    "book_review_required",
                    "chapter_evidence_review_required",
                    "waiting_for_user",
                    "no_legal_route",
                ],
                self._bounded_review_disposition(
                    disposition=self._arc_closure_disposition(
                        evaluation=evaluation,
                        plan=plan,
                    ),
                    task=task,
                    has_creator_question=evaluation.creator_input_need is not None,
                    correction_dispositions=frozenset(
                        {
                            "arc_revision_warranted",
                            "chapter_evidence_review_required",
                        }
                    ),
                ),
            )
            precheck = {
                "schema_id": "arc-closure-precheck-v1",
                "passed": True,
                "checkpoint_reached_exactly": len(chapters)
                == baseline.closure_chapter_count,
                "arc_status": arc.lifecycle_status,
                "workspace_state": workspace.state,
                "chapter_set_fingerprint": prepared_manifest.sha256,
            }
            prepared_precheck = prepare_canonical_json(precheck)
            prepared_question = (
                None
                if evaluation.creator_input_need is None
                else prepare_canonical_json(evaluation.creator_input_need)
            )
            prepared_normalized = prepare_canonical_json(
                {
                    "schema_id": "formal-arc-closure-result-v1",
                    "arc_purpose": arc.purpose,
                    "disposition": disposition,
                    "signal_statuses": [
                        item.model_dump(mode="json")
                        for item in evaluation.signal_statuses
                    ],
                    "summary": evaluation.summary,
                    "committed_chapter_count": len(chapters),
                    "chapter_set_fingerprint": prepared_manifest.sha256,
                }
            )
            exact_input_fingerprint = prepare_canonical_json(
                {
                    "book_baseline_id": task.book_baseline_id,
                    "arc_baseline_id": task.arc_baseline_id,
                    "canon_baseline_id": task.canon_baseline_id,
                    "closure_chapter_count": baseline.closure_chapter_count,
                    "chapter_set_fingerprint": prepared_manifest.sha256,
                    "strategy_id": task.evaluation_strategy_id,
                    "strategy_version": task.evaluation_strategy_version,
                }
            ).sha256

        envelope = self._envelope(
            request=request,
            project_id=request.project_id,
            idempotency_key=idempotency_key,
            command_kind="record_arc_closure_review",
            source_task_id=request.task_id,
            timestamp=timestamp,
        )

        async def handler(
            session: StoreSession,
        ) -> CommandEffect[RecordArcClosureReviewResult]:
            current_task = await self._successful_task(
                session,
                project_id=request.project_id,
                task_id=request.task_id,
                attempt_id=request.attempt_id,
                task_kind="evaluate.arc_closure",
            )
            if current_task != task or task.delivery_state != "pending":
                raise CommandPreconditionError("Arc closure task changed before delivery.")
            current_arc, current_baseline, current_workspace, current_chapters = (
                await self._arc_closure_snapshot(
                    session,
                    request=request,
                    task=task,
                )
            )
            if (
                current_arc != arc
                or current_baseline != baseline
                or current_workspace != workspace
                or current_chapters != chapters
            ):
                raise CommandPreconditionError(
                    "Arc closure authority changed before delivery."
                )
            latest = await session.arc_closure_reviews.get_latest_for_arc(
                project_id=request.project_id,
                arc_id=request.arc_id,
            )
            source_review = (
                None
                if task.source_arc_closure_review_id is None
                else await session.arc_closure_reviews.get(
                    project_id=request.project_id,
                    review_id=task.source_arc_closure_review_id,
                )
            )
            if (
                (latest is None) != (source_review is None)
                or (
                    latest is not None
                    and source_review is not None
                    and latest.id != source_review.id
                )
            ):
                raise CommandPreconditionError(
                    "Arc closure predecessor review is not current."
                )
            lineage = self._review_lineage_links(
                task=task,
                source_review=source_review,
            )
            manifest_ref = await session.content.put(
                project_id=request.project_id,
                prepared=prepared_manifest,
                semantic_kind="arc.closure_chapter_set",
                media_type="application/json",
                schema_id="arc-closure-chapter-set",
                schema_version=1,
                ref_id=manifest_ref_id,
                created_at_ms=timestamp,
            )
            precheck_ref = await session.content.put(
                project_id=request.project_id,
                prepared=prepared_precheck,
                semantic_kind="arc.closure_precheck",
                media_type="application/json",
                schema_id="arc-closure-precheck",
                schema_version=1,
                ref_id=precheck_ref_id,
                created_at_ms=timestamp,
            )
            question_ref = None
            if prepared_question is not None:
                question_ref = await session.content.put(
                    project_id=request.project_id,
                    prepared=prepared_question,
                    semantic_kind="review.creator_question",
                    media_type="application/json",
                    schema_id="creator-input-need",
                    schema_version=1,
                    ref_id=question_ref_id,
                    created_at_ms=timestamp,
                )
            review_record = ArcClosureReviewRecord(
                id=review_id,
                project_id=request.project_id,
                book_id=request.book_id,
                arc_id=request.arc_id,
                book_baseline_id=baseline.book_baseline_id,
                arc_baseline_id=baseline.id,
                canon_baseline_id=task.canon_baseline_id,
                terminal_chapter_id=chapters[-1].chapter_id,
                terminal_chapter_baseline_id=chapters[-1].id,
                committed_chapter_count=len(chapters),
                closure_chapter_count=baseline.closure_chapter_count,
                chapter_set_fingerprint=prepared_manifest.sha256,
                chapter_set_manifest_ref_id=manifest_ref.id,
                source_task_id=task.task_id,
                source_attempt_id=task.attempt_id,
                strategy_id=self._required_text(
                    task.evaluation_strategy_id, "evaluation strategy"
                ),
                strategy_version=self._required_int(
                    task.evaluation_strategy_version, "evaluation strategy"
                ),
                rubric_id=self._required_text(task.rubric_id, "rubric"),
                rubric_version=self._required_int(
                    task.rubric_version, "rubric"
                ),
                arc_contract_judgment=evaluation.arc_contract_judgment,
                parent_review_judgment=evaluation.book_review_concern,
                disposition=disposition,
                resolution_owner=self._arc_resolution_owner(disposition),
                detail_ref_id=task.result_ref_id,
                precheck_ref_id=precheck_ref.id,
                user_question_ref_id=(
                    None if question_ref is None else question_ref.id
                ),
                exact_input_fingerprint=exact_input_fingerprint,
                correction_lineage_id=lineage.lineage_id,
                correction_lineage_origin=lineage.lineage_origin,
                automatic_correction_round=lineage.correction_round,
                review_ordinal=lineage.review_ordinal,
                predecessor_review_id=lineage.predecessor_review_id,
                source_feedback_id=task.source_feedback_id,
                source_exhausted_review_id=lineage.source_exhausted_review_id,
                opened_arc_workspace_id=None,
                created_at_ms=timestamp,
            )
            await session.arc_closure_reviews.insert(review_record)
            if not await session.arc_closure_reviews.compare_and_set_latest(
                project_id=request.project_id,
                arc_id=request.arc_id,
                arc_baseline_id=baseline.id,
                expected_review_id=arc.latest_closure_review_id,
                new_review_id=review_id,
                updated_at_ms=timestamp,
            ):
                raise CommandPreconditionError("Arc closure review pointer CAS failed.")

            formal_closure_id: str | None = None
            opened_change_request_id: str | None = None
            downstream_action: Literal[
                "none",
                "chapter_evidence_correction_opened",
                "historical_rewrite_unsupported",
            ] = "none"
            events = [
                EventDraft(
                    event_type="arc.closure_reviewed",
                    aggregate_type="arc",
                    aggregate_id=request.arc_id,
                    payload={
                        "review_id": review_id,
                        "disposition": disposition,
                    },
                )
            ]
            if disposition == "pass":
                normalized_ref = await session.content.put(
                    project_id=request.project_id,
                    prepared=prepared_normalized,
                    semantic_kind="arc.formal_closure_result",
                    media_type="application/json",
                    schema_id="formal-arc-closure-result",
                    schema_version=1,
                    ref_id=normalized_ref_id,
                    created_at_ms=timestamp,
                )
                previous_closure = await session.arc_closures.get_latest_for_arc(
                    project_id=request.project_id,
                    arc_id=request.arc_id,
                )
                version = await session.arc_closures.next_version(arc_id=request.arc_id)
                if version != (1 if previous_closure is None else previous_closure.closure_version + 1):
                    raise CommandPreconditionError("Arc closure version is not contiguous.")
                await session.arc_closures.insert(
                    ArcClosureRecord(
                        id=closure_id,
                        project_id=request.project_id,
                        book_id=request.book_id,
                        arc_id=request.arc_id,
                        closure_version=version,
                        parent_closure_id=(
                            None if previous_closure is None else previous_closure.id
                        ),
                        closure_review_id=review_id,
                        book_baseline_id=baseline.book_baseline_id,
                        arc_baseline_id=baseline.id,
                        canon_baseline_id=task.canon_baseline_id,
                        terminal_chapter_id=chapters[-1].chapter_id,
                        terminal_chapter_baseline_id=chapters[-1].id,
                        committed_chapter_count=len(chapters),
                        chapter_set_fingerprint=prepared_manifest.sha256,
                        chapter_set_manifest_ref_id=manifest_ref.id,
                        normalized_result_ref_id=normalized_ref.id,
                        created_at_ms=timestamp,
                    )
                )
                if not await session.arc_closures.compare_and_set_current(
                    project_id=request.project_id,
                    arc_id=request.arc_id,
                    arc_baseline_id=baseline.id,
                    closure_review_id=review_id,
                    closure_id=closure_id,
                    completed_at_ms=timestamp,
                ):
                    raise CommandPreconditionError("Formal Arc closure pointer CAS failed.")
                formal_closure_id = closure_id
                events.append(
                    EventDraft(
                        event_type="arc.closed",
                        aggregate_type="arc",
                        aggregate_id=request.arc_id,
                        payload={"closure_id": closure_id, "review_id": review_id},
                    )
                )
            elif disposition == "book_review_required":
                await session.arcs.insert_book_change_request(
                    ArcBookChangeRequestRecord(
                        id=change_request_id,
                        project_id=request.project_id,
                        book_id=request.book_id,
                        arc_id=request.arc_id,
                        source_candidate_submission_id=None,
                        source_candidate_review_id=None,
                        source_arc_parent_review_id=None,
                        source_arc_closure_review_id=review_id,
                        target_book_baseline_id=baseline.book_baseline_id,
                        evidence_ref_id=task.result_ref_id,
                        status="open",
                        created_at_ms=timestamp,
                    )
                )
                opened_change_request_id = change_request_id
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
            elif disposition == "chapter_evidence_review_required":
                target = evaluation.chapter_evidence_target
                if target is None:
                    raise AuthorityTaskFailure(
                        code="evaluation_contract_invalid",
                        message="Arc closure evidence review has no semantic Chapter target.",
                    )
                evidence_chapter = await session.chapters.get_by_book_ordinal(
                    project_id=request.project_id,
                    book_id=request.book_id,
                    book_ordinal=target.chapter_book_ordinal,
                )
                if evidence_chapter is None or evidence_chapter.arc_id != request.arc_id:
                    raise AuthorityTaskFailure(
                        code="evaluation_contract_invalid",
                        message=(
                            "Arc closure evidence target is not a Chapter in the current Arc."
                        ),
                    )
                downstream_action = cast(
                    Literal[
                        "chapter_evidence_correction_opened",
                        "historical_rewrite_unsupported",
                    ],
                    await self._open_chapter_correction(
                        session,
                        task=task,
                        chapter=evidence_chapter,
                        guidance_ref_id=task.result_ref_id,
                        source_arc_parent_review_id=None,
                        source_arc_closure_review_id=review_id,
                        correction_lineage_id=lineage.lineage_id,
                        correction_lineage_origin=lineage.lineage_origin,
                        evidence_only=True,
                        timestamp=timestamp,
                    ),
                )
            elif disposition == "waiting_for_user":
                if not await session.runs.ensure_wait_for_user(
                    run_id=task.run_id,
                    reason_code=(
                        "evidence_correction_needs_user"
                        if task.automatic_correction_round == 1
                        else "arc_closure_needs_user"
                    ),
                    now_ms=timestamp,
                ):
                    raise CommandPreconditionError(
                        "Run could not enter the Arc closure creator wait."
                    )

            if not await session.execution.mark_delivery_applied(
                project_id=request.project_id,
                task_id=request.task_id,
                attempt_id=request.attempt_id,
                command_id=envelope.command_id,
                updated_at_ms=timestamp,
            ):
                raise CommandPreconditionError(
                    "Arc closure task delivery changed concurrently."
                )
            return CommandEffect(
                result=RecordArcClosureReviewResult(
                    project_id=request.project_id,
                    arc_id=request.arc_id,
                    review_id=review_id,
                    disposition=disposition,
                    formal_closure_id=formal_closure_id,
                    change_request_id=opened_change_request_id,
                    downstream_action=downstream_action,
                ),
                events=tuple(events),
            )

        return await self._command_bus.execute(
            envelope=envelope,
            result_type=RecordArcClosureReviewResult,
            handler=handler,
        )

    async def open_arc_closure_revision(
        self,
        request: OpenArcClosureRevisionRequest,
        *,
        idempotency_key: str,
    ) -> CommandExecution[OpenArcClosureRevisionResult]:
        timestamp = self._now_ms()
        envelope = self._envelope(
            request=request,
            project_id=request.project_id,
            idempotency_key=idempotency_key,
            command_kind="open_arc_closure_revision",
            source_task_id=None,
            timestamp=timestamp,
        )

        async def handler(
            session: StoreSession,
        ) -> CommandEffect[OpenArcClosureRevisionResult]:
            review = await session.arc_closure_reviews.get(
                project_id=request.project_id,
                review_id=request.closure_review_id,
            )
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
                review is None
                or review.book_id != request.book_id
                or review.arc_id != request.arc_id
                or review.disposition != "arc_revision_warranted"
                or review.opened_arc_workspace_id is not None
                or review.automatic_correction_round != 0
                or project is None
                or project.lifecycle_status != "active"
                or book is None
                or book.id != request.book_id
                or book.current_baseline_id != review.book_baseline_id
                or arc is None
                or arc.lifecycle_status != "closing"
                or arc.current_baseline_id != review.arc_baseline_id
                or arc.latest_closure_review_id != review.id
                or arc.current_closure_id is not None
                or workspace is None
                or workspace.state != "idle"
                or workspace.lock_version != request.expected_workspace_lock_version
            ):
                raise CommandPreconditionError(
                    "Arc closure revision authorization is stale."
                )
            revision_origin = (
                "user_initiated"
                if review.correction_lineage_origin == "user_initiated"
                else "automatic_arc_recovery"
            )
            if (
                revision_origin == "automatic_arc_recovery"
                and await session.arcs.has_automatic_recovery_baseline(
                    arc_id=request.arc_id
                )
            ):
                if not await session.runs.ensure_wait_for_user(
                    run_id=(
                        await self._successful_task(
                            session,
                            project_id=request.project_id,
                            task_id=review.source_task_id,
                            attempt_id=review.source_attempt_id,
                            task_kind="evaluate.arc_closure",
                        )
                    ).run_id,
                    reason_code="arc_revision_limit_reached",
                    now_ms=timestamp,
                ):
                    raise CommandPreconditionError(
                        "Run could not enter the Arc revision-limit wait."
                    )
                return CommandEffect(
                    result=OpenArcClosureRevisionResult(
                        project_id=request.project_id,
                        arc_id=request.arc_id,
                        closure_review_id=review.id,
                        action="arc_revision_limit_reached",
                    ),
                    events=(
                        EventDraft(
                            event_type="arc.revision_limit_reached",
                            aggregate_type="arc",
                            aggregate_id=request.arc_id,
                            payload={"closure_review_id": review.id},
                        ),
                    ),
                )
            updated = replace(
                workspace,
                state="active",
                lock_version=workspace.lock_version + 1,
                base_arc_baseline_id=arc.current_baseline_id,
                book_baseline_id=review.book_baseline_id,
                canon_baseline_id=review.canon_baseline_id,
                revision_origin=revision_origin,
                source_arc_parent_review_id=None,
                source_arc_closure_review_id=review.id,
                source_book_parent_review_id=None,
                source_book_boundary_review_id=None,
                source_feedback_id=None,
                correction_lineage_id=review.correction_lineage_id,
                correction_lineage_origin=review.correction_lineage_origin,
                automatic_correction_round=1,
                plan_ref_id=None,
                minimum_chapter_count=None,
                recommended_closure_chapter_count=None,
                maximum_chapter_count=None,
                closure_chapter_count=None,
                guidance_ref_id=review.detail_ref_id,
                semantic_repair_count=0,
                stale_reason_code=None,
                stale_at_ms=None,
                updated_at_ms=timestamp,
            )
            if not await session.arcs.compare_and_set_workspace(
                record=updated,
                expected_lock_version=workspace.lock_version,
            ):
                raise CommandPreconditionError(
                    "Arc closure revision workspace CAS failed."
                )
            if not await session.arc_closure_reviews.mark_workspace_opened(
                project_id=request.project_id,
                review_id=review.id,
                workspace_id=workspace.id,
            ):
                raise CommandPreconditionError(
                    "Arc closure review workspace pointer CAS failed."
                )
            return CommandEffect(
                result=OpenArcClosureRevisionResult(
                    project_id=request.project_id,
                    arc_id=request.arc_id,
                    closure_review_id=review.id,
                    action="revision_workspace_opened",
                    workspace_lock_version=updated.lock_version,
                ),
                events=(
                    EventDraft(
                        event_type="arc.revision_workspace_opened",
                        aggregate_type="arc",
                        aggregate_id=request.arc_id,
                        payload={
                            "closure_review_id": review.id,
                            "workspace_lock_version": updated.lock_version,
                        },
                    ),
                ),
            )

        return await self._command_bus.execute(
            envelope=envelope,
            result_type=OpenArcClosureRevisionResult,
            handler=handler,
        )

    async def open_book_boundary_revision(
        self,
        request: OpenBookBoundaryRevisionRequest,
        *,
        idempotency_key: str,
    ) -> CommandExecution[OpenBookBoundaryRevisionResult]:
        timestamp = self._now_ms()
        envelope = self._envelope(
            request=request,
            project_id=request.project_id,
            idempotency_key=idempotency_key,
            command_kind="open_book_boundary_revision",
            source_task_id=None,
            timestamp=timestamp,
        )

        async def handler(
            session: StoreSession,
        ) -> CommandEffect[OpenBookBoundaryRevisionResult]:
            review = await session.book_boundary_reviews.get(
                project_id=request.project_id,
                review_id=request.boundary_review_id,
            )
            project = await session.projects.get(request.project_id)
            book = await session.books.get_for_project(request.project_id)
            workspace = await session.books.get_workspace(
                project_id=request.project_id,
                book_id=request.book_id,
            )
            closure = (
                None
                if review is None
                else await session.arc_closures.get(
                    project_id=request.project_id,
                    closure_id=review.arc_closure_id,
                )
            )
            terminal_arc = (
                None
                if closure is None
                else await session.arcs.get(
                    project_id=request.project_id,
                    arc_id=closure.arc_id,
                )
            )
            if (
                review is None
                or review.book_id != request.book_id
                or review.disposition != "book_revision_warranted"
                or review.automatic_correction_round != 0
                or review.opened_book_workspace_id is not None
                or project is None
                or project.lifecycle_status != "active"
                or book is None
                or book.id != request.book_id
                or book.lifecycle_status != "active"
                or book.current_baseline_id != review.book_baseline_id
                or book.latest_boundary_review_id != review.id
                or book.current_progress_handoff_id is not None
                or book.current_completion_id is not None
                or workspace is None
                or workspace.state != "idle"
                or workspace.lock_version
                != request.expected_workspace_lock_version
                or closure is None
                or terminal_arc is None
                or terminal_arc.current_closure_id != closure.id
                or terminal_arc.lifecycle_status != "completed"
            ):
                raise CommandPreconditionError(
                    "Book boundary revision authorization is stale."
                )
            pending = await session.books.find_pending_submission(
                project_id=request.project_id,
                book_id=request.book_id,
            )
            if pending is not None:
                raise CommandPreconditionError(
                    "Book boundary revision cannot replace a pending submission."
                )
            updated = replace(
                workspace,
                state="active",
                lock_version=workspace.lock_version + 1,
                base_book_baseline_id=review.book_baseline_id,
                base_canon_baseline_id=project.current_canon_baseline_id,
                candidate_constraints_ref_id=None,
                candidate_titles_ref_id=None,
                candidate_rolling_plan_ref_id=None,
                candidate_completion_contract_ref_id=None,
                guidance_ref_id=review.detail_ref_id,
                semantic_repair_count=0,
                stale_reason_code=None,
                stale_at_ms=None,
                updated_at_ms=timestamp,
            )
            if not await session.books.compare_and_set_workspace(
                record=updated,
                expected_lock_version=workspace.lock_version,
            ):
                raise CommandPreconditionError(
                    "Book boundary revision workspace CAS failed."
                )
            if not await session.book_boundary_reviews.mark_workspace_opened(
                project_id=request.project_id,
                review_id=review.id,
                workspace_id=workspace.id,
            ):
                raise CommandPreconditionError(
                    "Book boundary review workspace pointer CAS failed."
                )
            return CommandEffect(
                result=OpenBookBoundaryRevisionResult(
                    project_id=request.project_id,
                    book_id=request.book_id,
                    boundary_review_id=review.id,
                    workspace_lock_version=updated.lock_version,
                ),
                events=(
                    EventDraft(
                        event_type="book.boundary_revision_workspace_opened",
                        aggregate_type="book",
                        aggregate_id=request.book_id,
                        payload={
                            "boundary_review_id": review.id,
                            "workspace_lock_version": updated.lock_version,
                        },
                    ),
                ),
            )

        return await self._command_bus.execute(
            envelope=envelope,
            result_type=OpenBookBoundaryRevisionResult,
            handler=handler,
        )

    async def record_book_boundary_review(
        self,
        request: RecordBookBoundaryReviewRequest,
        *,
        idempotency_key: str,
    ) -> CommandExecution[RecordBookBoundaryReviewResult]:
        timestamp = self._now_ms()
        review_id = self._id_factory()
        statuses_ref_id = self._id_factory()
        precheck_ref_id = self._id_factory()
        question_ref_id = self._id_factory()

        async with self._command_bus.read_unit_of_work() as session:
            task = await self._successful_task(
                session,
                project_id=request.project_id,
                task_id=request.task_id,
                attempt_id=request.attempt_id,
                task_kind="evaluate.book_boundary",
            )
            evaluation = BookBoundaryEvaluation.model_validate_json(
                (
                    await session.content.get_packed(
                        project_id=request.project_id,
                        ref_id=task.result_ref_id,
                    )
                ).unpack_and_verify()
            )
            book, baseline, workspace, closure, terminal_arc, chapter_count = (
                await self._book_boundary_snapshot(
                    session,
                    request=request,
                    task=task,
                )
            )
            contract = CompletionContract.model_validate_json(
                (
                    await session.content.get_packed(
                        project_id=request.project_id,
                        ref_id=baseline.completion_contract_ref_id,
                    )
                ).unpack_and_verify()
            )
            self._validate_requirement_coverage(
                evaluation=evaluation,
                contract=contract,
            )
            disposition = cast(
                Literal[
                    "continue_regular_arc",
                    "plan_final_arc",
                    "complete_book",
                    "book_revision_warranted",
                    "waiting_for_user",
                    "no_legal_route",
                ],
                self._bounded_review_disposition(
                    disposition=self._book_boundary_disposition(
                        evaluation=evaluation,
                        contract=contract,
                        terminal_arc_purpose=terminal_arc.purpose,
                        committed_chapter_count=chapter_count,
                    ),
                    task=task,
                    has_creator_question=evaluation.creator_input_need is not None,
                    correction_dispositions=frozenset(
                        {"book_revision_warranted"}
                    ),
                ),
            )
            prepared_statuses = prepare_canonical_json(
                [item.model_dump(mode="json") for item in evaluation.requirement_statuses]
            )
            prepared_precheck = prepare_canonical_json(
                {
                    "schema_id": "book-boundary-precheck-v1",
                    "passed": True,
                    "formal_arc_closure_current": True,
                    "book_baseline_current": True,
                    "closure_inputs_frozen": True,
                    "committed_chapter_count": chapter_count,
                    "book_minimum_chapter_count": contract.minimum_chapter_count,
                    "book_maximum_chapter_count": contract.maximum_chapter_count,
                }
            )
            prepared_question = (
                None
                if evaluation.creator_input_need is None
                else prepare_canonical_json(evaluation.creator_input_need)
            )
            exact_input_fingerprint = prepare_canonical_json(
                {
                    "book_baseline_id": baseline.id,
                    "arc_closure_id": closure.id,
                    "closure_canon_baseline_id": closure.canon_baseline_id,
                    "chapter_set_fingerprint": closure.chapter_set_fingerprint,
                    "committed_chapter_count": chapter_count,
                    "strategy_id": task.evaluation_strategy_id,
                    "strategy_version": task.evaluation_strategy_version,
                }
            ).sha256

        envelope = self._envelope(
            request=request,
            project_id=request.project_id,
            idempotency_key=idempotency_key,
            command_kind="record_book_boundary_review",
            source_task_id=request.task_id,
            timestamp=timestamp,
        )

        async def handler(
            session: StoreSession,
        ) -> CommandEffect[RecordBookBoundaryReviewResult]:
            current_task = await self._successful_task(
                session,
                project_id=request.project_id,
                task_id=request.task_id,
                attempt_id=request.attempt_id,
                task_kind="evaluate.book_boundary",
            )
            if current_task != task or task.delivery_state != "pending":
                raise CommandPreconditionError("Book boundary task changed before delivery.")
            current = await self._book_boundary_snapshot(
                session,
                request=request,
                task=task,
            )
            if current != (
                book,
                baseline,
                workspace,
                closure,
                terminal_arc,
                chapter_count,
            ):
                raise CommandPreconditionError(
                    "Book boundary authority changed before delivery."
                )
            latest = await session.book_boundary_reviews.get_latest_for_book(
                project_id=request.project_id,
                book_id=request.book_id,
            )
            source_review = (
                None
                if task.source_book_boundary_review_id is None
                else await session.book_boundary_reviews.get(
                    project_id=request.project_id,
                    review_id=task.source_book_boundary_review_id,
                )
            )
            if (
                source_review is not None
                and latest is not None
                and latest.id != source_review.id
            ):
                raise CommandPreconditionError(
                    "Book boundary predecessor review is not current."
                )
            if source_review is None and latest is not None:
                if latest.arc_closure_id == closure.id and latest.book_baseline_id == baseline.id:
                    raise CommandPreconditionError(
                        "The exact Book boundary already has an authoritative review."
                    )
            lineage = self._review_lineage_links(
                task=task,
                source_review=source_review,
            )
            statuses_ref = await session.content.put(
                project_id=request.project_id,
                prepared=prepared_statuses,
                semantic_kind="book.boundary_requirement_statuses",
                media_type="application/json",
                schema_id="book-boundary-requirement-statuses",
                schema_version=1,
                ref_id=statuses_ref_id,
                created_at_ms=timestamp,
            )
            precheck_ref = await session.content.put(
                project_id=request.project_id,
                prepared=prepared_precheck,
                semantic_kind="book.boundary_precheck",
                media_type="application/json",
                schema_id="book-boundary-precheck",
                schema_version=1,
                ref_id=precheck_ref_id,
                created_at_ms=timestamp,
            )
            question_ref = None
            if prepared_question is not None:
                question_ref = await session.content.put(
                    project_id=request.project_id,
                    prepared=prepared_question,
                    semantic_kind="review.creator_question",
                    media_type="application/json",
                    schema_id="creator-input-need",
                    schema_version=1,
                    ref_id=question_ref_id,
                    created_at_ms=timestamp,
                )
            await session.book_boundary_reviews.insert(
                BookBoundaryReviewRecord(
                    id=review_id,
                    project_id=request.project_id,
                    book_id=request.book_id,
                    book_baseline_id=baseline.id,
                    arc_closure_id=closure.id,
                    canon_baseline_id=closure.canon_baseline_id,
                    committed_chapter_count=chapter_count,
                    chapter_set_fingerprint=closure.chapter_set_fingerprint,
                    source_task_id=task.task_id,
                    source_attempt_id=task.attempt_id,
                    strategy_id=self._required_text(
                        task.evaluation_strategy_id, "evaluation strategy"
                    ),
                    strategy_version=self._required_int(
                        task.evaluation_strategy_version, "evaluation strategy"
                    ),
                    rubric_id=self._required_text(task.rubric_id, "rubric"),
                    rubric_version=self._required_int(
                        task.rubric_version, "rubric"
                    ),
                    requirement_statuses_ref_id=statuses_ref.id,
                    ending_trajectory_judgment=(
                        evaluation.ending_trajectory_judgment
                    ),
                    book_contract_judgment=evaluation.book_contract_judgment,
                    disposition=disposition,
                    resolution_owner=self._book_resolution_owner(disposition),
                    detail_ref_id=task.result_ref_id,
                    precheck_ref_id=precheck_ref.id,
                    user_question_ref_id=(
                        None if question_ref is None else question_ref.id
                    ),
                    exact_input_fingerprint=exact_input_fingerprint,
                    correction_lineage_id=lineage.lineage_id,
                    correction_lineage_origin=lineage.lineage_origin,
                    automatic_correction_round=lineage.correction_round,
                    review_ordinal=lineage.review_ordinal,
                    predecessor_review_id=lineage.predecessor_review_id,
                    source_feedback_id=task.source_feedback_id,
                    source_exhausted_review_id=(
                        lineage.source_exhausted_review_id
                    ),
                    opened_book_workspace_id=None,
                    created_at_ms=timestamp,
                )
            )
            if not await session.book_boundary_reviews.compare_and_set_latest(
                project_id=request.project_id,
                book_id=request.book_id,
                book_baseline_id=baseline.id,
                expected_review_id=book.latest_boundary_review_id,
                new_review_id=review_id,
                updated_at_ms=timestamp,
            ):
                raise CommandPreconditionError("Book boundary review pointer CAS failed.")
            if disposition == "waiting_for_user" and not await session.runs.ensure_wait_for_user(
                run_id=task.run_id,
                reason_code=(
                    "evidence_correction_needs_user"
                    if task.automatic_correction_round == 1
                    else "book_boundary_needs_user"
                ),
                now_ms=timestamp,
            ):
                raise CommandPreconditionError(
                    "Run could not enter the Book boundary creator wait."
                )
            if not await session.execution.mark_delivery_applied(
                project_id=request.project_id,
                task_id=request.task_id,
                attempt_id=request.attempt_id,
                command_id=envelope.command_id,
                updated_at_ms=timestamp,
            ):
                raise CommandPreconditionError(
                    "Book boundary task delivery changed concurrently."
                )
            return CommandEffect(
                result=RecordBookBoundaryReviewResult(
                    project_id=request.project_id,
                    book_id=request.book_id,
                    review_id=review_id,
                    disposition=disposition,
                ),
                events=(
                    EventDraft(
                        event_type="book.boundary_reviewed",
                        aggregate_type="book",
                        aggregate_id=request.book_id,
                        payload={
                            "review_id": review_id,
                            "arc_closure_id": closure.id,
                            "disposition": disposition,
                        },
                    ),
                ),
            )

        return await self._command_bus.execute(
            envelope=envelope,
            result_type=RecordBookBoundaryReviewResult,
            handler=handler,
        )

    async def commit_book_progress_handoff(
        self,
        request: CommitBookProgressHandoffRequest,
        *,
        idempotency_key: str,
    ) -> CommandExecution[CommitBookProgressHandoffResult]:
        timestamp = self._now_ms()
        handoff_id = self._id_factory()
        remaining_ref_id = self._id_factory()
        guidance_ref_id = self._id_factory()
        async with self._command_bus.read_unit_of_work() as session:
            review = await session.book_boundary_reviews.get(
                project_id=request.project_id,
                review_id=request.boundary_review_id,
            )
            if review is None:
                raise CommandPreconditionError("Book boundary review does not exist.")
            if review.disposition not in {
                "continue_regular_arc",
                "plan_final_arc",
            }:
                raise CommandPreconditionError(
                    "Book boundary review does not authorize a progress handoff."
                )
            statuses = (
                await session.content.get_packed(
                    project_id=request.project_id,
                    ref_id=review.requirement_statuses_ref_id,
                )
            ).unpack_and_verify()
            prepared_remaining = prepare_canonical_json(
                {
                    "schema_id": "book-remaining-requirements-v1",
                    "requirement_statuses": __import__("json").loads(statuses),
                }
            )
            prepared_guidance = prepare_canonical_json(
                {
                    "schema_id": "book-progress-guidance-v1",
                    "boundary_disposition": review.disposition,
                    "ending_trajectory_judgment": review.ending_trajectory_judgment,
                    "book_contract_judgment": review.book_contract_judgment,
                    "source_detail_ref_id": review.detail_ref_id,
                }
            )
        envelope = self._envelope(
            request=request,
            project_id=request.project_id,
            idempotency_key=idempotency_key,
            command_kind="commit_book_progress_handoff",
            source_task_id=review.source_task_id,
            timestamp=timestamp,
        )

        async def handler(
            session: StoreSession,
        ) -> CommandEffect[CommitBookProgressHandoffResult]:
            current_review = await session.book_boundary_reviews.get(
                project_id=request.project_id,
                review_id=request.boundary_review_id,
            )
            book = await session.books.get_for_project(request.project_id)
            closure = await session.arc_closures.get(
                project_id=request.project_id,
                closure_id=review.arc_closure_id,
            )
            if (
                current_review != review
                or book is None
                or book.id != request.book_id
                or book.lifecycle_status != "active"
                or book.current_baseline_id != review.book_baseline_id
                or book.latest_boundary_review_id != review.id
                or book.current_completion_id is not None
                or closure is None
                or closure.id != review.arc_closure_id
            ):
                raise CommandPreconditionError(
                    "Book handoff authority is stale or mismatched."
                )
            previous = await session.book_progress_handoffs.get_latest_for_book(
                project_id=request.project_id,
                book_id=request.book_id,
            )
            version = await session.book_progress_handoffs.next_version(
                book_id=request.book_id
            )
            if version != (1 if previous is None else previous.handoff_version + 1):
                raise CommandPreconditionError("Book handoff version is not contiguous.")
            remaining_ref = await session.content.put(
                project_id=request.project_id,
                prepared=prepared_remaining,
                semantic_kind="book.remaining_requirements",
                media_type="application/json",
                schema_id="book-remaining-requirements",
                schema_version=1,
                ref_id=remaining_ref_id,
                created_at_ms=timestamp,
            )
            guidance_ref = await session.content.put(
                project_id=request.project_id,
                prepared=prepared_guidance,
                semantic_kind="book.progress_guidance",
                media_type="application/json",
                schema_id="book-progress-guidance",
                schema_version=1,
                ref_id=guidance_ref_id,
                created_at_ms=timestamp,
            )
            purpose: Literal["regular", "final"] = (
                "regular"
                if review.disposition == "continue_regular_arc"
                else "final"
            )
            await session.book_progress_handoffs.insert(
                BookProgressHandoffRecord(
                    id=handoff_id,
                    project_id=request.project_id,
                    book_id=request.book_id,
                    handoff_version=version,
                    parent_handoff_id=None if previous is None else previous.id,
                    source_boundary_review_id=review.id,
                    arc_closure_id=review.arc_closure_id,
                    book_baseline_id=review.book_baseline_id,
                    canon_baseline_id=review.canon_baseline_id,
                    next_arc_purpose=purpose,
                    remaining_requirements_ref_id=remaining_ref.id,
                    guidance_ref_id=guidance_ref.id,
                    created_at_ms=timestamp,
                )
            )
            if not await session.book_progress_handoffs.compare_and_set_current(
                project_id=request.project_id,
                book_id=request.book_id,
                book_baseline_id=review.book_baseline_id,
                boundary_review_id=review.id,
                expected_handoff_id=book.current_progress_handoff_id,
                new_handoff_id=handoff_id,
                updated_at_ms=timestamp,
            ):
                raise CommandPreconditionError("Book progress handoff pointer CAS failed.")
            return CommandEffect(
                result=CommitBookProgressHandoffResult(
                    project_id=request.project_id,
                    book_id=request.book_id,
                    boundary_review_id=review.id,
                    handoff_id=handoff_id,
                    next_arc_purpose=purpose,
                ),
                events=(
                    EventDraft(
                        event_type="book.progress_handoff_committed",
                        aggregate_type="book",
                        aggregate_id=request.book_id,
                        payload={
                            "boundary_review_id": review.id,
                            "handoff_id": handoff_id,
                            "next_arc_purpose": purpose,
                        },
                    ),
                ),
            )

        return await self._command_bus.execute(
            envelope=envelope,
            result_type=CommitBookProgressHandoffResult,
            handler=handler,
        )

    async def commit_book_completion(
        self,
        request: CommitBookCompletionRequest,
        *,
        idempotency_key: str,
    ) -> CommandExecution[CommitBookCompletionResult]:
        timestamp = self._now_ms()
        completion_id = self._id_factory()
        decision_ref_id = self._id_factory()
        gate_ref_id = self._id_factory()
        async with self._command_bus.read_unit_of_work() as session:
            review = await session.book_boundary_reviews.get(
                project_id=request.project_id,
                review_id=request.boundary_review_id,
            )
            if review is None or review.disposition != "complete_book":
                raise CommandPreconditionError(
                    "Book boundary review does not authorize completion."
                )
            prepared_decision = prepare_canonical_json(
                {
                    "schema_id": "book-completion-decision-v1",
                    "boundary_review_id": review.id,
                    "ending_trajectory_judgment": (
                        review.ending_trajectory_judgment
                    ),
                    "book_contract_judgment": review.book_contract_judgment,
                    "requirement_statuses_ref_id": review.requirement_statuses_ref_id,
                }
            )
            prepared_gate = prepare_canonical_json(
                {
                    "schema_id": "book-completion-gate-v1",
                    "all_requirements_satisfied": True,
                    "terminal_arc_formally_final": True,
                    "chapter_count": review.committed_chapter_count,
                    "source_arc_closure_id": review.arc_closure_id,
                }
            )
        envelope = self._envelope(
            request=request,
            project_id=request.project_id,
            idempotency_key=idempotency_key,
            command_kind="commit_book_completion",
            source_task_id=review.source_task_id,
            timestamp=timestamp,
        )

        async def handler(
            session: StoreSession,
        ) -> CommandEffect[CommitBookCompletionResult]:
            current_review = await session.book_boundary_reviews.get(
                project_id=request.project_id,
                review_id=request.boundary_review_id,
            )
            project = await session.projects.get(request.project_id)
            book = await session.books.get_for_project(request.project_id)
            closure = await session.arc_closures.get(
                project_id=request.project_id,
                closure_id=review.arc_closure_id,
            )
            terminal_arc = await session.completion.get_terminal_arc(
                project_id=request.project_id,
                book_id=request.book_id,
            )
            terminal_chapter = (
                None
                if terminal_arc is None
                else await session.completion.get_terminal_chapter(
                    project_id=request.project_id,
                    book_id=request.book_id,
                    arc_id=terminal_arc.arc_id,
                )
            )
            task = await session.execution.get_successful_task(
                project_id=request.project_id,
                task_id=review.source_task_id,
                attempt_id=review.source_attempt_id,
            )
            baseline = (
                None
                if book is None or book.current_baseline_id is None
                else await session.books.get_baseline(
                    project_id=request.project_id,
                    book_id=request.book_id,
                    baseline_id=book.current_baseline_id,
                )
            )
            if (
                current_review != review
                or project is None
                or project.lifecycle_status != "active"
                or book is None
                or book.id != request.book_id
                or book.lifecycle_status != "active"
                or book.current_baseline_id != review.book_baseline_id
                or book.latest_boundary_review_id != review.id
                or book.current_completion_id is not None
                or baseline is None
                or closure is None
                or terminal_arc is None
                or terminal_arc.arc_id != closure.arc_id
                or terminal_arc.arc_baseline_id != closure.arc_baseline_id
                or terminal_arc.arc_closure_id != closure.id
                or terminal_arc.lifecycle_status != "completed"
                or terminal_arc.purpose != "final"
                or terminal_chapter is None
                or terminal_chapter.chapter_id != closure.terminal_chapter_id
                or terminal_chapter.chapter_baseline_id
                != closure.terminal_chapter_baseline_id
                or task is None
                or task.delivery_state != "applied"
                or not (
                    baseline.minimum_chapter_count
                    <= review.committed_chapter_count
                    <= baseline.maximum_chapter_count
                )
                or await session.completion.count_committed_chapters(
                    book_id=request.book_id
                )
                != review.committed_chapter_count
                or await session.changes.has_unresolved(project_id=request.project_id)
                or await session.feedback.has_unapplied(project_id=request.project_id)
                or await session.completion.has_lifecycle_blocker(
                    project_id=request.project_id,
                    book_id=request.book_id,
                    source_task_id=review.source_task_id,
                )
            ):
                raise CommandPreconditionError(
                    "Book completion gate facts are stale or incomplete."
                )
            decision_ref = await session.content.put(
                project_id=request.project_id,
                prepared=prepared_decision,
                semantic_kind="book.completion_decision",
                media_type="application/json",
                schema_id="book-completion-decision",
                schema_version=1,
                ref_id=decision_ref_id,
                created_at_ms=timestamp,
            )
            gate_ref = await session.content.put(
                project_id=request.project_id,
                prepared=prepared_gate,
                semantic_kind="book.completion_gate_manifest",
                media_type="application/json",
                schema_id="book-completion-gate-manifest",
                schema_version=1,
                ref_id=gate_ref_id,
                created_at_ms=timestamp,
            )
            latest = await session.completion.get_latest_identity(book_id=request.book_id)
            version = await session.completion.next_version(book_id=request.book_id)
            if version != (1 if latest is None else latest[1] + 1):
                raise CommandPreconditionError("Book completion version is not contiguous.")
            await session.completion.insert(
                BookCompletionRecord(
                    id=completion_id,
                    project_id=request.project_id,
                    book_id=request.book_id,
                    completion_version=version,
                    parent_completion_id=None if latest is None else latest[0],
                    book_baseline_id=baseline.id,
                    book_boundary_review_id=review.id,
                    arc_closure_id=closure.id,
                    terminal_arc_id=terminal_arc.arc_id,
                    terminal_arc_baseline_id=terminal_arc.arc_baseline_id,
                    terminal_chapter_id=terminal_chapter.chapter_id,
                    terminal_chapter_baseline_id=(
                        terminal_chapter.chapter_baseline_id
                    ),
                    canon_baseline_id=closure.canon_baseline_id,
                    committed_chapter_count=review.committed_chapter_count,
                    source_task_id=review.source_task_id,
                    completion_decision_ref_id=decision_ref.id,
                    gate_manifest_ref_id=gate_ref.id,
                    created_at_ms=timestamp,
                )
            )
            if not await session.books.commit_completion(
                project_id=request.project_id,
                book_id=request.book_id,
                expected_baseline_id=baseline.id,
                completion_id=completion_id,
                updated_at_ms=timestamp,
            ):
                raise CommandPreconditionError("Book completion pointer CAS failed.")
            if not await session.projects.set_lifecycle_status(
                project_id=request.project_id,
                expected_status="active",
                new_status="completed",
                updated_at_ms=timestamp,
            ):
                raise CommandPreconditionError("Project completion status CAS failed.")
            if not await session.runs.complete(run_id=task.run_id, now_ms=timestamp):
                raise CommandPreconditionError("Generation Run could not complete.")
            return CommandEffect(
                result=CommitBookCompletionResult(
                    project_id=request.project_id,
                    book_id=request.book_id,
                    boundary_review_id=review.id,
                    completion_id=completion_id,
                ),
                events=(
                    EventDraft(
                        event_type="book.completed",
                        aggregate_type="book",
                        aggregate_id=request.book_id,
                        payload={
                            "completion_id": completion_id,
                            "boundary_review_id": review.id,
                        },
                    ),
                    EventDraft(
                        event_type="run.completed",
                        aggregate_type="run",
                        aggregate_id=task.run_id,
                        payload={"completion_id": completion_id},
                    ),
                ),
            )

        return await self._command_bus.execute(
            envelope=envelope,
            result_type=CommitBookCompletionResult,
            handler=handler,
        )

    @staticmethod
    async def _successful_task(
        session: StoreSession,
        *,
        project_id: str,
        task_id: str,
        attempt_id: str,
        task_kind: str,
    ) -> SuccessfulTaskRecord:
        task = await session.execution.get_successful_task(
            project_id=project_id,
            task_id=task_id,
            attempt_id=attempt_id,
        )
        strategy = DEFAULT_EVALUATION_STRATEGY_REGISTRY.for_task(task_kind)
        if (
            task is None
            or task.role != "evaluator"
            or task.task_kind != task_kind
            or task.evaluation_strategy_id != strategy.strategy_id
            or task.evaluation_strategy_version != strategy.strategy_version
            or task.rubric_id != strategy.rubric_id
            or task.rubric_version != strategy.rubric_version
        ):
            raise CommandPreconditionError(
                f"Successful task does not match {task_kind!r} authority."
            )
        return task

    @staticmethod
    async def _arc_parent_snapshot(
        session: StoreSession,
        *,
        request: RecordArcParentReviewRequest,
        task: SuccessfulTaskRecord,
    ) -> tuple[
        ChapterArcChangeRequestRecord,
        ArcRecord,
        ArcBaselineRecord,
        ArcWorkspaceRecord,
    ]:
        change = await session.changes.get_chapter_arc(
            project_id=request.project_id,
            request_id=request.request_id,
        )
        arc = await session.arcs.get(
            project_id=request.project_id,
            arc_id=request.arc_id,
        )
        workspace = await session.arcs.get_workspace(
            project_id=request.project_id,
            arc_id=request.arc_id,
        )
        baseline = (
            None
            if arc is None or arc.current_baseline_id is None
            else await session.arcs.get_baseline(
                project_id=request.project_id,
                arc_id=request.arc_id,
                baseline_id=arc.current_baseline_id,
            )
        )
        source_review = (
            None
            if change is None
            else await session.chapters.get_review(
                project_id=request.project_id,
                review_id=change.source_review_id,
            )
        )
        project = await session.projects.get(request.project_id)
        if (
            change is None
            or change.book_id != request.book_id
            or change.arc_id != request.arc_id
            or change.status not in {"open", "reviewed"}
            or arc is None
            or arc.book_id != request.book_id
            or arc.lifecycle_status not in {"active", "closing"}
            or baseline is None
            or baseline.id != change.target_arc_baseline_id
            or workspace is None
            or task.scope_layer != "arc"
            or task.book_id != request.book_id
            or task.arc_id != request.arc_id
            or task.book_baseline_id != baseline.book_baseline_id
            or task.arc_baseline_id != baseline.id
            or task.workspace_lock_version != workspace.lock_version
            or task.source_chapter_arc_request_id != request.request_id
            or source_review is None
            or source_review.decision != "escalate_to_arc"
            or project is None
            or project.current_canon_baseline_id != task.canon_baseline_id
        ):
            raise CommandPreconditionError(
                "Arc parent-review facts are stale or incomplete."
            )
        return change, arc, baseline, workspace

    @staticmethod
    async def _book_parent_snapshot(
        session: StoreSession,
        *,
        request: RecordBookParentReviewRequest,
        task: SuccessfulTaskRecord,
    ) -> tuple[
        StoredArcBookChangeRequestRecord,
        BookRecord,
        BookBaselineRecord,
        BookWorkspaceRecord,
    ]:
        change = await session.changes.get_arc_book(
            project_id=request.project_id,
            request_id=request.request_id,
        )
        book = await session.books.get_for_project(request.project_id)
        workspace = (
            None
            if book is None
            else await session.books.get_workspace(
                project_id=request.project_id,
                book_id=request.book_id,
            )
        )
        baseline = (
            None
            if book is None or book.current_baseline_id is None
            else await session.books.get_baseline(
                project_id=request.project_id,
                book_id=request.book_id,
                baseline_id=book.current_baseline_id,
            )
        )
        project = await session.projects.get(request.project_id)
        source_arc = (
            None
            if change is None
            else await session.arcs.get(
                project_id=request.project_id,
                arc_id=change.arc_id,
            )
        )
        if (
            change is None
            or change.book_id != request.book_id
            or change.status not in {"open", "reviewed"}
            or book is None
            or book.id != request.book_id
            or book.lifecycle_status != "active"
            or book.current_completion_id is not None
            or baseline is None
            or baseline.id != change.target_book_baseline_id
            or workspace is None
            or task.scope_layer != "book"
            or task.book_id != request.book_id
            or task.book_baseline_id != baseline.id
            or source_arc is None
            or source_arc.book_id != request.book_id
            or task.arc_baseline_id != source_arc.current_baseline_id
            or task.workspace_lock_version != workspace.lock_version
            or task.source_arc_book_request_id != request.request_id
            or project is None
            or project.current_canon_baseline_id != task.canon_baseline_id
        ):
            raise CommandPreconditionError(
                "Book parent-review facts are stale or incomplete."
            )
        return change, book, baseline, workspace

    @staticmethod
    async def _arc_closure_snapshot(
        session: StoreSession,
        *,
        request: RecordArcClosureReviewRequest,
        task: SuccessfulTaskRecord,
    ) -> tuple[
        ArcRecord,
        ArcBaselineRecord,
        ArcWorkspaceRecord,
        list[ChapterBaselineRecord],
    ]:
        arc = await session.arcs.get(
            project_id=request.project_id,
            arc_id=request.arc_id,
        )
        workspace = await session.arcs.get_workspace(
            project_id=request.project_id,
            arc_id=request.arc_id,
        )
        baseline = (
            None
            if arc is None or arc.current_baseline_id is None
            else await session.arcs.get_baseline(
                project_id=request.project_id,
                arc_id=request.arc_id,
                baseline_id=arc.current_baseline_id,
            )
        )
        committed = await session.chapters.list_committed_baselines(
            project_id=request.project_id,
            book_id=request.book_id,
        )
        arc_chapters = [item for item in committed if item.arc_id == request.arc_id]
        project = await session.projects.get(request.project_id)
        drafting = await session.chapters.get_unfinished_for_arc(
            project_id=request.project_id,
            arc_id=request.arc_id,
        )
        if (
            arc is None
            or arc.book_id != request.book_id
            or arc.lifecycle_status != "closing"
            or arc.current_closure_id is not None
            or baseline is None
            or workspace is None
            or workspace.state != "idle"
            or task.scope_layer != "arc"
            or task.book_id != request.book_id
            or task.arc_id != request.arc_id
            or task.book_baseline_id != baseline.book_baseline_id
            or task.arc_baseline_id != baseline.id
            or task.workspace_lock_version != workspace.lock_version
            or project is None
            or project.current_canon_baseline_id != task.canon_baseline_id
            or drafting is not None
            or len(arc_chapters) != baseline.closure_chapter_count
            or not arc_chapters
        ):
            raise CommandPreconditionError(
                "Arc closure checkpoint facts are stale or incomplete."
            )
        return arc, baseline, workspace, arc_chapters

    @staticmethod
    async def _book_boundary_snapshot(
        session: StoreSession,
        *,
        request: RecordBookBoundaryReviewRequest,
        task: SuccessfulTaskRecord,
    ) -> tuple[
        BookRecord,
        BookBaselineRecord,
        BookWorkspaceRecord,
        ArcClosureRecord,
        ArcRecord,
        int,
    ]:
        book = await session.books.get_for_project(request.project_id)
        workspace = (
            None
            if book is None
            else await session.books.get_workspace(
                project_id=request.project_id,
                book_id=request.book_id,
            )
        )
        baseline = (
            None
            if book is None or book.current_baseline_id is None
            else await session.books.get_baseline(
                project_id=request.project_id,
                book_id=request.book_id,
                baseline_id=book.current_baseline_id,
            )
        )
        closure = (
            None
            if task.source_arc_closure_id is None
            else await session.arc_closures.get(
                project_id=request.project_id,
                closure_id=task.source_arc_closure_id,
            )
        )
        terminal_arc = (
            None
            if closure is None
            else await session.arcs.get(
                project_id=request.project_id,
                arc_id=closure.arc_id,
            )
        )
        source_review = (
            None
            if task.source_book_boundary_review_id is None
            else await session.book_boundary_reviews.get(
                project_id=request.project_id,
                review_id=task.source_book_boundary_review_id,
            )
        )
        closure_matches_book_lineage = False
        if closure is not None and baseline is not None and workspace is not None:
            closure_matches_book_lineage = (
                source_review is None
                and closure.book_baseline_id == baseline.id
            ) or (
                source_review is not None
                and source_review.book_id == request.book_id
                and source_review.arc_closure_id == closure.id
                and source_review.disposition == "book_revision_warranted"
                and source_review.automatic_correction_round == 0
                and source_review.opened_book_workspace_id == workspace.id
                and baseline.parent_baseline_id == source_review.book_baseline_id
                and task.automatic_correction_round == 1
            )
        chapter_count = await session.completion.count_committed_chapters(
            book_id=request.book_id
        )
        if (
            book is None
            or book.id != request.book_id
            or book.lifecycle_status != "active"
            or book.current_completion_id is not None
            or workspace is None
            or workspace.state != "idle"
            or baseline is None
            or task.scope_layer != "book"
            or task.book_id != request.book_id
            or task.book_baseline_id != baseline.id
            or task.workspace_lock_version != workspace.lock_version
            or closure is None
            or closure.book_id != request.book_id
            or not closure_matches_book_lineage
            or closure.canon_baseline_id != task.canon_baseline_id
            or terminal_arc is None
            or terminal_arc.current_closure_id != closure.id
            or terminal_arc.lifecycle_status != "completed"
            or chapter_count < 1
            or chapter_count > baseline.maximum_chapter_count
        ):
            raise CommandPreconditionError(
                "Book boundary facts are stale or incomplete."
            )
        return book, baseline, workspace, closure, terminal_arc, chapter_count

    @staticmethod
    async def _open_chapter_correction(
        session: StoreSession,
        *,
        task: SuccessfulTaskRecord,
        chapter: ChapterRecord,
        guidance_ref_id: str,
        source_arc_parent_review_id: str | None,
        source_arc_closure_review_id: str | None,
        correction_lineage_id: str,
        correction_lineage_origin: str,
        evidence_only: bool,
        timestamp: int,
    ) -> Literal[
        "chapter_correction_opened",
        "chapter_evidence_correction_opened",
        "historical_rewrite_unsupported",
    ]:
        project = await session.projects.get(chapter.project_id)
        arc = await session.arcs.get(
            project_id=chapter.project_id,
            arc_id=chapter.arc_id,
        )
        workspace = await session.chapters.get_workspace(
            project_id=chapter.project_id,
            chapter_id=chapter.id,
        )
        if (
            project is None
            or project.lifecycle_status != "active"
            or arc is None
            or arc.current_baseline_id is None
            or chapter.book_id != task.book_id
            or chapter.arc_id != task.arc_id
            or workspace is None
            or workspace.state not in {"idle", "blocked_by_upstream"}
            or workspace.arc_baseline_id != arc.current_baseline_id
            or (source_arc_parent_review_id is None)
            == (source_arc_closure_review_id is None)
        ):
            raise CommandPreconditionError(
                "Chapter correction authority is stale or incomplete."
            )
        if evidence_only:
            historical_blocker = (
                "formal_arc_closure_exists"
                if arc.current_closure_id is not None
                or arc.lifecycle_status == "completed"
                else None
            )
        else:
            historical_blocker = (
                None
                if chapter.current_baseline_id is None
                else await session.chapters.narrative_replacement_blocker(
                    project_id=chapter.project_id,
                    book_id=chapter.book_id,
                    arc_id=chapter.arc_id,
                    chapter_id=chapter.id,
                )
            )
        if historical_blocker is not None:
            if not await session.runs.ensure_wait_for_user(
                run_id=task.run_id,
                reason_code="historical_rewrite_unsupported",
                now_ms=timestamp,
            ):
                raise CommandPreconditionError(
                    "Run could not enter the historical-rewrite wait."
                )
            return "historical_rewrite_unsupported"

        baseline = (
            None
            if chapter.current_baseline_id is None
            else await session.chapters.get_baseline(
                project_id=chapter.project_id,
                chapter_id=chapter.id,
                baseline_id=chapter.current_baseline_id,
            )
        )
        if chapter.current_baseline_id is not None and baseline is None:
            raise CommandPreconditionError(
                "Current Chapter baseline is missing for correction."
            )
        preserved_plan_ref_id = (
            workspace.plan_ref_id if baseline is None else baseline.plan_ref_id
        )
        preserved_draft_ref_id = (
            workspace.draft_ref_id if baseline is None else baseline.prose_ref_id
        )
        if evidence_only and (
            preserved_plan_ref_id is None or preserved_draft_ref_id is None
        ):
            raise CommandPreconditionError(
                "Evidence correction has no frozen plan/prose to preserve."
            )
        revision_origin = (
            "user_initiated"
            if correction_lineage_origin == "user_initiated"
            else ("arc_evidence_correction" if evidence_only else "local_revision")
        )
        updated = replace(
            workspace,
            state="active",
            lock_version=workspace.lock_version + 1,
            base_chapter_baseline_id=chapter.current_baseline_id,
            book_baseline_id=task.book_baseline_id or workspace.book_baseline_id,
            arc_baseline_id=arc.current_baseline_id,
            canon_baseline_id=project.current_canon_baseline_id,
            revision_origin=revision_origin,
            source_arc_parent_review_id=source_arc_parent_review_id,
            source_arc_closure_review_id=source_arc_closure_review_id,
            source_feedback_id=task.source_feedback_id,
            correction_lineage_id=correction_lineage_id,
            correction_lineage_origin=correction_lineage_origin,
            automatic_correction_round=1,
            plan_ref_id=preserved_plan_ref_id if evidence_only else None,
            draft_ref_id=preserved_draft_ref_id if evidence_only else None,
            observations_ref_id=None,
            candidate_canon_patch_ref_id=None,
            guidance_ref_id=guidance_ref_id,
            semantic_repair_count=0,
            stale_reason_code=None,
            stale_at_ms=None,
            updated_at_ms=timestamp,
        )
        if not await session.chapters.compare_and_set_workspace(
            record=updated,
            expected_lock_version=workspace.lock_version,
        ):
            raise CommandPreconditionError("Chapter correction workspace CAS failed.")
        return (
            "chapter_evidence_correction_opened"
            if evidence_only
            else "chapter_correction_opened"
        )

    @staticmethod
    async def _open_arc_correction_from_book_review(
        session: StoreSession,
        *,
        task: SuccessfulTaskRecord,
        review: BookParentReviewRecord,
        timestamp: int,
    ) -> Literal["arc_correction_opened", "arc_revision_limit_reached"]:
        project = await session.projects.get(review.project_id)
        book = await session.books.get_for_project(review.project_id)
        arc = await session.arcs.get(
            project_id=review.project_id,
            arc_id=review.arc_id,
        )
        workspace = await session.arcs.get_workspace(
            project_id=review.project_id,
            arc_id=review.arc_id,
        )
        if (
            project is None
            or project.lifecycle_status != "active"
            or book is None
            or book.id != review.book_id
            or book.current_baseline_id != review.target_book_baseline_id
            or book.current_completion_id is not None
            or arc is None
            or arc.current_closure_id is not None
            or workspace is None
            or workspace.state not in {"idle", "blocked_by_upstream"}
            or workspace.book_baseline_id != review.target_book_baseline_id
        ):
            raise CommandPreconditionError(
                "Book guidance cannot open the current Arc correction."
            )
        revision_origin = (
            "user_initiated"
            if review.correction_lineage_origin == "user_initiated"
            else (
                "initial"
                if arc.current_baseline_id is None
                else "automatic_arc_recovery"
            )
        )
        if (
            revision_origin == "automatic_arc_recovery"
            and await session.arcs.has_automatic_recovery_baseline(arc_id=arc.id)
        ):
            if not await session.runs.ensure_wait_for_user(
                run_id=task.run_id,
                reason_code="arc_revision_limit_reached",
                now_ms=timestamp,
            ):
                raise CommandPreconditionError(
                    "Run could not enter the Arc revision-limit wait."
                )
            return "arc_revision_limit_reached"

        pending = await session.arcs.find_pending_submission(
            project_id=review.project_id,
            arc_id=arc.id,
        )
        if pending is not None and not await session.arcs.close_submission(
            project_id=review.project_id,
            submission_id=pending.id,
            disposition="superseded",
            reason_code="book_parent_guidance",
            closed_at_ms=timestamp,
        ):
            raise CommandPreconditionError(
                "Arc submission changed before Book guidance."
            )
        gate = await session.arcs.find_pending_gate(
            project_id=review.project_id,
            arc_id=arc.id,
        )
        if gate is not None and not await session.arcs.close_approval_gate(
            project_id=review.project_id,
            gate_id=gate.id,
            state="superseded",
            closed_at_ms=timestamp,
        ):
            raise CommandPreconditionError(
                "Arc approval gate changed before Book guidance."
            )
        updated = replace(
            workspace,
            state="active",
            lock_version=workspace.lock_version + 1,
            base_arc_baseline_id=arc.current_baseline_id,
            book_baseline_id=review.target_book_baseline_id,
            canon_baseline_id=project.current_canon_baseline_id,
            revision_origin=revision_origin,
            source_arc_parent_review_id=None,
            source_arc_closure_review_id=None,
            source_book_parent_review_id=review.id,
            source_book_boundary_review_id=None,
            source_feedback_id=task.source_feedback_id,
            correction_lineage_id=review.correction_lineage_id,
            correction_lineage_origin=review.correction_lineage_origin,
            automatic_correction_round=1,
            plan_ref_id=None,
            minimum_chapter_count=None,
            recommended_closure_chapter_count=None,
            maximum_chapter_count=None,
            closure_chapter_count=None,
            guidance_ref_id=review.detail_ref_id,
            semantic_repair_count=0,
            stale_reason_code=None,
            stale_at_ms=None,
            updated_at_ms=timestamp,
        )
        if not await session.arcs.compare_and_set_workspace(
            record=updated,
            expected_lock_version=workspace.lock_version,
        ):
            raise CommandPreconditionError("Book-guided Arc workspace CAS failed.")
        return "arc_correction_opened"

    @staticmethod
    def _validate_arc_signal_coverage(
        *, evaluation: ArcClosureEvaluation, plan: ArcPlanProposal
    ) -> None:
        expected = {item.signal_key for item in plan.closure_signals}
        actual = {item.signal_key for item in evaluation.signal_statuses}
        if expected != actual:
            raise AuthorityTaskFailure(
                code="evaluation_contract_invalid",
                message=(
                    "Arc closure evaluation did not cover the exact frozen signal set."
                ),
            )

    @staticmethod
    def _validate_requirement_coverage(
        *, evaluation: BookBoundaryEvaluation, contract: CompletionContract
    ) -> None:
        expected = {
            item.requirement_key for item in contract.completion_requirements
        }
        actual = {item.requirement_key for item in evaluation.requirement_statuses}
        if expected != actual:
            raise AuthorityTaskFailure(
                code="evaluation_contract_invalid",
                message=(
                    "Book boundary evaluation did not cover the exact completion contract."
                ),
            )

    @staticmethod
    def _arc_closure_disposition(
        *,
        evaluation: ArcClosureEvaluation,
        plan: ArcPlanProposal,
    ) -> Literal[
        "pass",
        "arc_revision_warranted",
        "book_review_required",
        "chapter_evidence_review_required",
        "waiting_for_user",
        "no_legal_route",
    ]:
        if evaluation.book_review_concern == "book_review_required":
            return "book_review_required"
        if evaluation.arc_contract_judgment == "revision_warranted":
            return "arc_revision_warranted"
        if (
            evaluation.chapter_evidence_concern
            == "chapter_evidence_review_required"
        ):
            return "chapter_evidence_review_required"
        if evaluation.creator_input_need is not None:
            return "waiting_for_user"
        statuses = {item.signal_key: item.status for item in evaluation.signal_statuses}
        required_satisfied = all(
            not signal.required or statuses[signal.signal_key] == "satisfied"
            for signal in plan.closure_signals
        )
        if (
            evaluation.arc_contract_judgment == "remains_applicable"
            and required_satisfied
        ):
            return "pass"
        if evaluation.arc_contract_judgment == "remains_applicable":
            return "arc_revision_warranted"
        return "no_legal_route"

    @staticmethod
    def _arc_parent_disposition(
        evaluation: ArcParentContractEvaluation,
    ) -> Literal[
        "keep_arc",
        "arc_revision_warranted",
        "book_review_required",
        "chapter_evidence_review_required",
        "waiting_for_user",
        "no_legal_route",
    ]:
        if evaluation.book_review_concern == "book_review_required":
            return "book_review_required"
        if evaluation.arc_contract_judgment == "revision_warranted":
            return "arc_revision_warranted"
        if (
            evaluation.chapter_evidence_concern
            == "chapter_evidence_review_required"
        ):
            return "chapter_evidence_review_required"
        if evaluation.creator_input_need is not None:
            return "waiting_for_user"
        if evaluation.arc_contract_judgment == "remains_applicable":
            return "keep_arc"
        return "no_legal_route"

    @staticmethod
    def _book_parent_disposition(
        evaluation: BookParentContractEvaluation,
    ) -> Literal[
        "keep_book",
        "book_revision_warranted",
        "arc_evidence_review_required",
        "waiting_for_user",
        "no_legal_route",
    ]:
        if evaluation.book_contract_judgment == "revision_warranted":
            return "book_revision_warranted"
        if evaluation.arc_evidence_concern == "arc_evidence_review_required":
            return "arc_evidence_review_required"
        if evaluation.creator_input_need is not None:
            return "waiting_for_user"
        if evaluation.book_contract_judgment == "remains_applicable":
            return "keep_book"
        return "no_legal_route"

    @staticmethod
    def _book_boundary_disposition(
        *,
        evaluation: BookBoundaryEvaluation,
        contract: CompletionContract,
        terminal_arc_purpose: str,
        committed_chapter_count: int,
    ) -> Literal[
        "continue_regular_arc",
        "plan_final_arc",
        "complete_book",
        "book_revision_warranted",
        "waiting_for_user",
        "no_legal_route",
    ]:
        if evaluation.book_contract_judgment == "revision_warranted":
            return "book_revision_warranted"
        if evaluation.creator_input_need is not None:
            return "waiting_for_user"
        if evaluation.book_contract_judgment == "unable_to_judge":
            return "no_legal_route"
        all_satisfied = all(
            item.status == "satisfied" for item in evaluation.requirement_statuses
        )
        if evaluation.ending_trajectory_judgment == "completion_ready":
            if (
                all_satisfied
                and terminal_arc_purpose == "final"
                and contract.minimum_chapter_count
                <= committed_chapter_count
                <= contract.maximum_chapter_count
            ):
                return "complete_book"
            return "no_legal_route"
        if evaluation.ending_trajectory_judgment == "final_arc_ready":
            if committed_chapter_count < contract.maximum_chapter_count:
                return "plan_final_arc"
            return "no_legal_route"
        if evaluation.ending_trajectory_judgment == "regular_arc_needed":
            # Keep at least one legal Chapter of capacity for a final Arc.
            if committed_chapter_count + 1 < contract.maximum_chapter_count:
                return "continue_regular_arc"
            return "no_legal_route"
        return "no_legal_route"

    @staticmethod
    def _bounded_review_disposition(
        *,
        disposition: str,
        task: SuccessfulTaskRecord,
        has_creator_question: bool,
        correction_dispositions: frozenset[str],
    ) -> str:
        if disposition == "no_legal_route":
            raise AuthorityTaskFailure(
                code="evaluation_contract_invalid",
                message=(
                    "The Evaluator returned no legal authority route and did not "
                    "supply a concrete creator-owned question."
                ),
            )
        if (
            task.automatic_correction_round == 1
            and disposition in correction_dispositions
        ):
            if not has_creator_question:
                raise AuthorityTaskFailure(
                    code="evaluation_contract_invalid",
                    message=(
                        "The linked successor review requested a second downward "
                        "correction without a concrete creator-owned resolution."
                    ),
                )
            return "waiting_for_user"
        return disposition

    @staticmethod
    def _require_correction_lineage(
        task: SuccessfulTaskRecord,
    ) -> tuple[str, str, int]:
        if (
            task.correction_lineage_id is None
            or task.correction_lineage_origin
            not in {"review_initiated", "user_initiated"}
            or task.automatic_correction_round not in {0, 1}
        ):
            raise CommandPreconditionError(
                "Authority review task lacks a valid correction lineage."
            )
        return (
            task.correction_lineage_id,
            task.correction_lineage_origin,
            task.automatic_correction_round,
        )

    @classmethod
    def _review_lineage_links(
        cls,
        *,
        task: SuccessfulTaskRecord,
        source_review: object | None,
    ) -> _ReviewLineageLinks:
        lineage_id, lineage_origin, correction_round = (
            cls._require_correction_lineage(task)
        )
        source_id = (
            None if source_review is None else str(getattr(source_review, "id"))
        )
        if lineage_origin == "review_initiated":
            if task.source_feedback_id is not None:
                raise CommandPreconditionError(
                    "Review-initiated correction lineage cannot cite user feedback."
                )
            if correction_round == 0:
                if source_review is not None:
                    raise CommandPreconditionError(
                        "Initial review lineage cannot have a predecessor review."
                    )
                predecessor_id = None
            else:
                if (
                    source_review is None
                    or getattr(source_review, "correction_lineage_id")
                    != lineage_id
                    or getattr(source_review, "correction_lineage_origin")
                    != "review_initiated"
                    or getattr(source_review, "automatic_correction_round") != 0
                ):
                    raise CommandPreconditionError(
                        "Round-one review is not linked to its round-zero root."
                    )
                predecessor_id = source_id
            exhausted_id = None
        else:
            if task.source_feedback_id is None:
                raise CommandPreconditionError(
                    "User-initiated correction lineage requires source feedback."
                )
            if correction_round == 0:
                if (
                    source_review is None
                    or getattr(source_review, "automatic_correction_round")
                    not in {0, 1}
                    or getattr(source_review, "disposition") != "waiting_for_user"
                    or getattr(source_review, "resolution_owner") != "creator"
                    or getattr(source_review, "user_question_ref_id") is None
                ):
                    raise CommandPreconditionError(
                        "User lineage root must cite a current creator-wait review."
                    )
                predecessor_id = None
                exhausted_id = source_id
            else:
                if (
                    source_review is None
                    or getattr(source_review, "correction_lineage_id")
                    != lineage_id
                    or getattr(source_review, "correction_lineage_origin")
                    != "user_initiated"
                    or getattr(source_review, "automatic_correction_round") != 0
                    or getattr(source_review, "source_feedback_id")
                    != task.source_feedback_id
                    or getattr(source_review, "source_exhausted_review_id") is None
                ):
                    raise CommandPreconditionError(
                        "User round-one review is not linked to its user lineage root."
                    )
                predecessor_id = source_id
                exhausted_id = str(
                    getattr(source_review, "source_exhausted_review_id")
                )
        return _ReviewLineageLinks(
            lineage_id=lineage_id,
            lineage_origin=lineage_origin,
            correction_round=correction_round,
            review_ordinal=correction_round + 1,
            predecessor_review_id=predecessor_id,
            source_exhausted_review_id=exhausted_id,
        )

    @staticmethod
    def _required_text(value: str | None, label: str) -> str:
        if value is None or not value.strip():
            raise CommandPreconditionError(f"Authority task lacks {label}.")
        return value

    @staticmethod
    def _required_int(value: int | None, label: str) -> int:
        if value is None or value < 1:
            raise CommandPreconditionError(f"Authority task lacks {label} version.")
        return value

    @staticmethod
    def _arc_resolution_owner(disposition: str) -> str:
        return {
            "pass": "none",
            "keep_arc": "none",
            "arc_revision_warranted": "arc",
            "book_review_required": "book",
            "chapter_evidence_review_required": "chapter",
            "waiting_for_user": "creator",
            "no_legal_route": "arc",
        }[disposition]

    @staticmethod
    def _book_resolution_owner(disposition: str) -> str:
        return {
            "keep_book": "none",
            "continue_regular_arc": "none",
            "plan_final_arc": "none",
            "complete_book": "none",
            "book_revision_warranted": "book",
            "arc_evidence_review_required": "arc",
            "waiting_for_user": "creator",
            "no_legal_route": "book",
        }[disposition]
