from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, replace
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from app.agents.contracts import (
    ArcPlanProposal,
    ChapterEvaluationIssue,
    ChapterObservationResult,
    EvaluationIssue,
    LayerEvaluationResult,
    SemanticCanonProposal,
)
from app.agents.registry import DEFAULT_EVALUATION_STRATEGY_REGISTRY
from app.db.engine import create_sqlite_async_engine
from app.db.maintenance import alembic_config
from app.db.schema import (
    agent_tasks,
    arc_baselines,
    arc_book_change_requests,
    arc_parent_reviews,
    arc_workspaces,
    book_workspaces,
    book_parent_reviews,
    canon_baselines,
    chapter_baselines,
    chapter_arc_change_requests,
    chapter_reviews,
    chapter_workspaces,
    chapters,
    generation_runs,
)
from app.db.uow import UnitOfWork
from app.domain.authority import (
    AuthorityTaskFailure,
    LoopAuthorityCommandService,
    RecordArcParentReviewRequest,
    RecordBookParentReviewRequest,
)
from app.domain.arc.commands import ArcCommandService
from app.domain.arc.contracts import (
    ApplyArcTaskRequest,
    ArcEvaluation,
    CommitArcAutoRequest,
    RecordArcReviewRequest,
    SubmitArcRequest,
)
from app.domain.chapter.commands import (
    ChapterCommandService,
    ChapterEvidenceVerificationFailure,
)
from app.domain.chapter.canon import CanonEntry
from app.domain.chapter.contracts import (
    ApplyChapterTaskRequest,
    CommitChapterRequest,
    RecordChapterReviewRequest,
    SubmitChapterRequest,
)
from app.domain.change_requests import (
    ActivateChangeRequest,
    ChangeRequestCommandService,
)
from app.domain.evaluation import (
    ArcParentContractEvaluation,
    BookParentContractEvaluation,
    ChapterEvidenceTarget,
    ChapterEvidenceCorrectionEvaluation,
    CreatorInputNeed,
)
from app.domain.feedback import (
    ApplyFeedbackRequest,
    FeedbackCommandService,
    QueueFeedbackRequest,
)
from app.domain.project_state import ProjectStateQuery
from app.runtime.context import ContextFactError, HarnessContextBuilder
from app.runtime.driver import DomainRunDriver
from app.store.command_bus import CommandBus
from app.store.content import ContentRepository
from tests.domain.test_chapter_lifecycle import (
    ReviewedChapter,
    _prepare_reviewed_chapter,
)
from tests.helpers.lifecycle_seed import insert_successful_task


@dataclass(frozen=True, slots=True)
class ArcParentFixture:
    chapter: ReviewedChapter
    request_id: str
    arc_workspace_lock_version: int


async def _prepare_arc_parent_fixture(
    engine: AsyncEngine,
    *,
    project_id: str,
) -> ArcParentFixture:
    chapter = await _prepare_reviewed_chapter(
        engine,
        project_id=project_id,
        target_chapter_count=2,
        canon_change=False,
        evaluation=LayerEvaluationResult(
            guidance_authority_judgment="not_present",
            decision="escalate_to_arc",
            summary="The Chapter preserves evidence that requires Arc authority.",
            issues=[
                ChapterEvaluationIssue(
                    kind="parent_authority_concern",
                    code="arc_authority_required",
                    subject="preserved Chapter evidence",
                    summary="The Chapter evidence requires Arc authority.",
                    evidence=[
                        "The frozen Chapter evidence raises a concern about its direct Arc."
                    ],
                    observed_components=["observations", "canon"],
                )
            ],
        ),
    )
    async with engine.connect() as connection:
        request_id = await connection.scalar(
            select(chapter_arc_change_requests.c.id).where(
                chapter_arc_change_requests.c.chapter_id == chapter.chapter_id
            )
        )
        arc_lock = await connection.scalar(
            select(arc_workspaces.c.lock_version).where(
                arc_workspaces.c.arc_id == chapter.foundation.arc_id
            )
        )
    assert request_id is not None and arc_lock is not None
    return ArcParentFixture(
        chapter=chapter,
        request_id=request_id,
        arc_workspace_lock_version=arc_lock,
    )


async def _record_arc_parent(
    engine: AsyncEngine,
    *,
    fixture: ArcParentFixture,
    suffix: str,
    evaluation: ArcParentContractEvaluation,
    lineage_id: str,
    correction_round: int,
    source_review_id: str | None = None,
) -> str:
    foundation = fixture.chapter.foundation
    task_id, attempt_id = await insert_successful_task(
        engine,
        project_id=foundation.project_id,
        run_id=foundation.run_id,
        task_id=f"{suffix}:task",
        attempt_id=f"{suffix}:attempt",
        role="evaluator",
        task_kind="evaluate.arc_parent_contract",
        scope_layer="arc",
        book_id=foundation.book_id,
        book_baseline_id=foundation.book_baseline_id,
        arc_id=foundation.arc_id,
        arc_baseline_id=foundation.arc_baseline_id,
        canon_baseline_id=foundation.canon_baseline_id,
        workspace_lock_version=fixture.arc_workspace_lock_version,
        correction_lineage_id=lineage_id,
        correction_lineage_origin="review_initiated",
        automatic_correction_round=correction_round,
        source_arc_parent_review_id=source_review_id,
        source_chapter_arc_request_id=fixture.request_id,
        result=evaluation,
    )
    recorded = await LoopAuthorityCommandService(
        CommandBus(engine)
    ).record_arc_parent_review(
        RecordArcParentReviewRequest(
            project_id=foundation.project_id,
            book_id=foundation.book_id,
            arc_id=foundation.arc_id,
            request_id=fixture.request_id,
            task_id=task_id,
            attempt_id=attempt_id,
        ),
        idempotency_key=f"{suffix}:record",
    )
    return recorded.result.review_id


def test_book_parent_review_uses_relational_source_arc_baseline(
    tmp_path: Path,
) -> None:
    database = tmp_path / "book-parent-relational-source.sqlite3"
    command.upgrade(alembic_config(database), "head")

    async def exercise() -> None:
        engine = create_sqlite_async_engine(database)
        try:
            fixture = await _prepare_arc_parent_fixture(
                engine,
                project_id="book-parent-relational-source",
            )
            foundation = fixture.chapter.foundation
            await _record_arc_parent(
                engine,
                fixture=fixture,
                suffix="book-parent-source-arc",
                evaluation=ArcParentContractEvaluation(
                    arc_contract_judgment="remains_applicable",
                    book_review_concern="book_review_required",
                    chapter_evidence_concern="not_required",
                    summary=(
                        "The requested change affects the approved Book promise."
                    ),
                    issues=[
                        EvaluationIssue(
                            kind="parent_authority_concern",
                            code="book_promise_change_requested",
                            subject="approved evidence-preservation promise",
                            summary=(
                                "Only Book authority may decide whether to replace "
                                "the evidence-preservation promise."
                            ),
                            evidence=[
                                "The Chapter request asks Arc to reverse a Book promise."
                            ],
                        )
                    ],
                ),
                lineage_id="book-parent-source-lineage",
                correction_round=0,
            )
            async with engine.connect() as connection:
                request_id = await connection.scalar(
                    select(arc_book_change_requests.c.id).where(
                        arc_book_change_requests.c.arc_id
                        == foundation.arc_id
                    )
                )
                book_lock = await connection.scalar(
                    select(book_workspaces.c.lock_version).where(
                        book_workspaces.c.book_id == foundation.book_id
                    )
                )
            assert request_id is not None and book_lock is not None

            task_id, attempt_id = await insert_successful_task(
                engine,
                project_id=foundation.project_id,
                run_id=foundation.run_id,
                task_id="book-parent-source-book:task",
                attempt_id="book-parent-source-book:attempt",
                role="evaluator",
                task_kind="evaluate.book_parent_contract",
                scope_layer="book",
                book_id=foundation.book_id,
                book_baseline_id=foundation.book_baseline_id,
                arc_baseline_id=None,
                subject_arc_baseline_id=foundation.arc_baseline_id,
                canon_baseline_id=foundation.canon_baseline_id,
                workspace_lock_version=book_lock,
                correction_lineage_id="book-parent-source-lineage",
                correction_lineage_origin="review_initiated",
                automatic_correction_round=0,
                source_arc_book_request_id=request_id,
                result=BookParentContractEvaluation(
                    book_contract_judgment="remains_applicable",
                    arc_evidence_concern="not_required",
                    summary="The current Book promise remains applicable.",
                ),
            )
            recorded = await LoopAuthorityCommandService(
                CommandBus(engine)
            ).record_book_parent_review(
                RecordBookParentReviewRequest(
                    project_id=foundation.project_id,
                    book_id=foundation.book_id,
                    request_id=request_id,
                    task_id=task_id,
                    attempt_id=attempt_id,
                ),
                idempotency_key="book-parent-source:record",
            )
            assert recorded.result.disposition == "keep_book"
            assert recorded.result.downstream_action == "arc_correction_opened"
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_arc_successor_is_the_exact_subject_of_runtime_book_parent_round_one(
    tmp_path: Path,
) -> None:
    database = tmp_path / "book-parent-successor-subject.sqlite3"
    command.upgrade(alembic_config(database), "head")

    async def exercise() -> None:
        engine = create_sqlite_async_engine(database)
        try:
            fixture = await _prepare_arc_parent_fixture(
                engine,
                project_id="book-parent-successor-subject",
            )
            foundation = fixture.chapter.foundation
            lineage_id = "book-parent-successor-lineage"
            await _record_arc_parent(
                engine,
                fixture=fixture,
                suffix="book-parent-successor-source",
                evaluation=ArcParentContractEvaluation(
                    arc_contract_judgment="remains_applicable",
                    book_review_concern="book_review_required",
                    chapter_evidence_concern="not_required",
                    summary="The Chapter concern requires immediate Book authority.",
                    issues=[
                        EvaluationIssue(
                            kind="parent_authority_concern",
                            code="book_contract_review_required",
                            subject="approved Book promise",
                            summary="Only Book may judge whether its promise still applies.",
                            evidence=["The Arc evidence reaches a Book-level promise."],
                        )
                    ],
                ),
                lineage_id=lineage_id,
                correction_round=0,
            )
            async with engine.connect() as connection:
                request_id = await connection.scalar(
                    select(arc_book_change_requests.c.id).where(
                        arc_book_change_requests.c.arc_id == foundation.arc_id
                    )
                )
                book_lock = await connection.scalar(
                    select(book_workspaces.c.lock_version).where(
                        book_workspaces.c.book_id == foundation.book_id
                    )
                )
            assert request_id is not None and book_lock is not None

            round_zero_task, round_zero_attempt = await insert_successful_task(
                engine,
                project_id=foundation.project_id,
                run_id=foundation.run_id,
                task_id="book-parent-round-zero:task",
                attempt_id="book-parent-round-zero:attempt",
                role="evaluator",
                task_kind="evaluate.book_parent_contract",
                scope_layer="book",
                book_id=foundation.book_id,
                book_baseline_id=foundation.book_baseline_id,
                arc_baseline_id=None,
                subject_arc_baseline_id=foundation.arc_baseline_id,
                canon_baseline_id=foundation.canon_baseline_id,
                workspace_lock_version=book_lock,
                correction_lineage_id=lineage_id,
                correction_lineage_origin="review_initiated",
                automatic_correction_round=0,
                source_arc_book_request_id=request_id,
                result=BookParentContractEvaluation(
                    book_contract_judgment="remains_applicable",
                    arc_evidence_concern="not_required",
                    summary="Book remains applicable; Arc must reconcile its evidence.",
                ),
            )
            round_zero = await LoopAuthorityCommandService(
                CommandBus(engine)
            ).record_book_parent_review(
                RecordBookParentReviewRequest(
                    project_id=foundation.project_id,
                    book_id=foundation.book_id,
                    request_id=request_id,
                    task_id=round_zero_task,
                    attempt_id=round_zero_attempt,
                ),
                idempotency_key="book-parent-round-zero:record",
            )
            assert round_zero.result.downstream_action == "arc_correction_opened"

            async with engine.connect() as connection:
                source_plan_ref_id = await connection.scalar(
                    select(arc_baselines.c.plan_ref_id).where(
                        arc_baselines.c.id == foundation.arc_baseline_id
                    )
                )
                correction_lock = await connection.scalar(
                    select(arc_workspaces.c.lock_version).where(
                        arc_workspaces.c.arc_id == foundation.arc_id
                    )
                )
                assert source_plan_ref_id is not None
                packed_plan = await ContentRepository(connection).get_packed(
                    project_id=foundation.project_id,
                    ref_id=source_plan_ref_id,
                )
            assert correction_lock is not None
            successor_plan = ArcPlanProposal.model_validate_json(
                packed_plan.unpack_and_verify()
            )
            planner_task, planner_attempt = await insert_successful_task(
                engine,
                project_id=foundation.project_id,
                run_id=foundation.run_id,
                task_id="book-guided-arc-successor:task",
                attempt_id="book-guided-arc-successor:attempt",
                role="arc_planner",
                task_kind="arc.revise",
                scope_layer="arc",
                book_id=foundation.book_id,
                book_baseline_id=foundation.book_baseline_id,
                arc_id=foundation.arc_id,
                arc_baseline_id=foundation.arc_baseline_id,
                canon_baseline_id=foundation.canon_baseline_id,
                workspace_lock_version=correction_lock,
                source_book_parent_review_id=round_zero.result.review_id,
                result=successor_plan,
            )
            arc_service = ArcCommandService(CommandBus(engine))
            applied = await arc_service.apply_task_result(
                ApplyArcTaskRequest(
                    project_id=foundation.project_id,
                    book_id=foundation.book_id,
                    arc_id=foundation.arc_id,
                    task_id=planner_task,
                    attempt_id=planner_attempt,
                    expected_workspace_lock_version=correction_lock,
                ),
                idempotency_key="book-guided-arc-successor:apply",
            )
            submitted = await arc_service.submit_for_review(
                SubmitArcRequest(
                    project_id=foundation.project_id,
                    book_id=foundation.book_id,
                    arc_id=foundation.arc_id,
                    expected_workspace_lock_version=(
                        applied.result.workspace_lock_version
                    ),
                ),
                idempotency_key="book-guided-arc-successor:submit",
            )
            evaluator_task, evaluator_attempt = await insert_successful_task(
                engine,
                project_id=foundation.project_id,
                run_id=foundation.run_id,
                task_id="book-guided-arc-successor:evaluate",
                attempt_id="book-guided-arc-successor:evaluate-attempt",
                role="evaluator",
                task_kind="evaluate.arc",
                scope_layer="arc",
                book_id=foundation.book_id,
                book_baseline_id=foundation.book_baseline_id,
                arc_id=foundation.arc_id,
                arc_baseline_id=foundation.arc_baseline_id,
                canon_baseline_id=foundation.canon_baseline_id,
                workspace_lock_version=applied.result.workspace_lock_version,
                source_book_parent_review_id=round_zero.result.review_id,
                result=ArcEvaluation(
                    guidance_authority_judgment="not_present",
                    decision="pass",
                    summary="The formal Arc successor follows the Book judgment.",
                ),
            )
            strategy = DEFAULT_EVALUATION_STRATEGY_REGISTRY.for_task(
                "evaluate.arc"
            )
            reviewed = await arc_service.record_review(
                RecordArcReviewRequest(
                    project_id=foundation.project_id,
                    book_id=foundation.book_id,
                    arc_id=foundation.arc_id,
                    submission_id=submitted.result.submission_id,
                    evaluator_task_id=evaluator_task,
                    evaluator_attempt_id=evaluator_attempt,
                    rubric_id=strategy.rubric_id,
                    rubric_version=strategy.rubric_version,
                    deterministic_precheck={"passed": True},
                ),
                idempotency_key="book-guided-arc-successor:review",
            )
            committed = await arc_service.commit_baseline_auto(
                CommitArcAutoRequest(
                    project_id=foundation.project_id,
                    book_id=foundation.book_id,
                    arc_id=foundation.arc_id,
                    submission_id=submitted.result.submission_id,
                    review_id=reviewed.result.review_id,
                    expected_current_baseline_id=foundation.arc_baseline_id,
                ),
                idempotency_key="book-guided-arc-successor:commit",
            )

            async with UnitOfWork(engine) as store:
                run = await store.runs.get(
                    project_id=foundation.project_id,
                    run_id=foundation.run_id,
                )
            assert run is not None
            driver = object.__new__(DomainRunDriver)
            driver._engine = engine
            instruction = await driver._decide_next(run)
            assert instruction is not None
            assert instruction.task_kind == "evaluate.book_parent_contract"
            assert instruction.arc_baseline_id is None
            assert instruction.subject_arc_baseline_id == committed.result.baseline_id
            assert instruction.source_arc_book_request_id == request_id
            assert (
                instruction.source_book_parent_review_id
                == round_zero.result.review_id
            )
            assert instruction.automatic_correction_round == 1

            async with engine.connect() as connection:
                book_cycle = (
                    await connection.execute(
                        select(
                            book_workspaces.c.lock_version,
                            book_workspaces.c.work_cycle_id,
                        ).where(book_workspaces.c.book_id == foundation.book_id)
                    )
                ).one()
            with pytest.raises(
                ContextFactError,
                match="not the current formal Arc head",
            ):
                await HarnessContextBuilder(engine).build(
                    task_kind=instruction.task_kind,
                    project_id=foundation.project_id,
                    book_id=instruction.book_id,
                    arc_id=None,
                    chapter_id=None,
                    semantic_goal="Reject a stale origin masquerading as the subject.",
                    source_book_parent_review_id=(
                        instruction.source_book_parent_review_id
                    ),
                    source_arc_book_request_id=(
                        instruction.source_arc_book_request_id
                    ),
                    canon_baseline_id=foundation.canon_baseline_id,
                    book_baseline_id=foundation.book_baseline_id,
                    subject_arc_baseline_id=foundation.arc_baseline_id,
                    workspace_lock_version=book_cycle.lock_version,
                    workspace_work_cycle_id=book_cycle.work_cycle_id,
                    correction_lineage_id=instruction.correction_lineage_id,
                    correction_lineage_origin=(
                        instruction.correction_lineage_origin
                    ),
                    automatic_correction_round=(
                        instruction.automatic_correction_round
                    ),
                )
            context = await HarnessContextBuilder(engine).build(
                task_kind=instruction.task_kind,
                project_id=foundation.project_id,
                book_id=instruction.book_id,
                arc_id=None,
                chapter_id=None,
                semantic_goal="Review the exact formal Arc successor at Book authority.",
                source_book_parent_review_id=(
                    instruction.source_book_parent_review_id
                ),
                source_arc_book_request_id=instruction.source_arc_book_request_id,
                canon_baseline_id=foundation.canon_baseline_id,
                book_baseline_id=foundation.book_baseline_id,
                subject_arc_baseline_id=instruction.subject_arc_baseline_id,
                workspace_lock_version=book_cycle.lock_version,
                workspace_work_cycle_id=book_cycle.work_cycle_id,
                correction_lineage_id=instruction.correction_lineage_id,
                correction_lineage_origin=instruction.correction_lineage_origin,
                automatic_correction_round=(
                    instruction.automatic_correction_round
                ),
            )
            assert context.manifest["facts"]["authority_subject_arc_baseline_id"] == (
                committed.result.baseline_id
            )

            round_one_task, round_one_attempt = await insert_successful_task(
                engine,
                project_id=foundation.project_id,
                run_id=foundation.run_id,
                task_id="book-parent-round-one:task",
                attempt_id="book-parent-round-one:attempt",
                role="evaluator",
                task_kind="evaluate.book_parent_contract",
                scope_layer="book",
                book_id=foundation.book_id,
                book_baseline_id=foundation.book_baseline_id,
                arc_baseline_id=None,
                subject_arc_baseline_id=committed.result.baseline_id,
                canon_baseline_id=foundation.canon_baseline_id,
                workspace_lock_version=book_cycle.lock_version,
                workspace_work_cycle_id=book_cycle.work_cycle_id,
                correction_lineage_id=lineage_id,
                correction_lineage_origin="review_initiated",
                automatic_correction_round=1,
                source_book_parent_review_id=round_zero.result.review_id,
                source_arc_book_request_id=request_id,
                result=BookParentContractEvaluation(
                    book_contract_judgment="remains_applicable",
                    arc_evidence_concern="not_required",
                    summary="The Arc successor now satisfies the unchanged Book.",
                ),
            )
            round_one = await LoopAuthorityCommandService(
                CommandBus(engine)
            ).record_book_parent_review(
                RecordBookParentReviewRequest(
                    project_id=foundation.project_id,
                    book_id=foundation.book_id,
                    request_id=request_id,
                    task_id=round_one_task,
                    attempt_id=round_one_attempt,
                ),
                idempotency_key="book-parent-round-one:record",
            )
            assert round_one.result.downstream_action == "request_resolved"
            async with engine.connect() as connection:
                reviews = list(
                    (
                        await connection.execute(
                            select(
                                book_parent_reviews.c.automatic_correction_round,
                                book_parent_reviews.c.subject_arc_baseline_id,
                            )
                            .where(
                                book_parent_reviews.c.request_id == request_id
                            )
                            .order_by(
                                book_parent_reviews.c.automatic_correction_round
                            )
                        )
                    ).all()
                )
                round_one_task_binding = (
                    await connection.execute(
                        select(
                            agent_tasks.c.arc_baseline_id,
                            agent_tasks.c.subject_arc_baseline_id,
                        ).where(agent_tasks.c.id == round_one_task)
                    )
                ).one()
                request_status = await connection.scalar(
                    select(arc_book_change_requests.c.status).where(
                        arc_book_change_requests.c.id == request_id
                    )
                )
            assert reviews == [
                (0, foundation.arc_baseline_id),
                (1, committed.result.baseline_id),
            ]
            assert round_one_task_binding.arc_baseline_id is None
            assert (
                round_one_task_binding.subject_arc_baseline_id
                == committed.result.baseline_id
            )
            assert request_status == "resolved"
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def _creator_need() -> CreatorInputNeed:
    return CreatorInputNeed(
        controlled_fact="Whether the witness knowingly concealed the altered statement.",
        question="Did the witness knowingly conceal the altered statement?",
        evidence=[
            "Committed Chapter evidence proves the statement changed, "
            "but not what the witness knew."
        ],
    )


def _derived_evidence_issue() -> EvaluationIssue:
    return EvaluationIssue(
        kind="derived_evidence_mismatch",
        code="chapter_evidence_mismatch",
        subject="committed Chapter observation",
        summary="The derived Chapter evidence overstates the approved prose.",
        evidence=[
            "The committed observation and the approved prose make contrary statements."
        ],
        candidate_claim="The observation records knowledge the prose does not establish.",
        contrary_formal_statement=(
            "The approved prose explicitly leaves the witness's knowledge unresolved."
        ),
    )


def test_initial_authority_creator_wait_projects_and_starts_user_lineage(
    tmp_path: Path,
) -> None:
    database = tmp_path / "initial-creator-wait.sqlite3"
    command.upgrade(alembic_config(database), "head")

    async def exercise() -> None:
        engine = create_sqlite_async_engine(database)
        try:
            fixture = await _prepare_arc_parent_fixture(
                engine,
                project_id="initial-creator-wait",
            )
            foundation = fixture.chapter.foundation
            review_id = await _record_arc_parent(
                engine,
                fixture=fixture,
                suffix="initial-wait",
                evaluation=ArcParentContractEvaluation(
                    arc_contract_judgment="unable_to_judge",
                    book_review_concern="not_required",
                    chapter_evidence_concern="not_required",
                    summary="Only the creator controls the missing intent.",
                    issues=[
                        EvaluationIssue(
                            kind="creator_owned_unknown",
                            code="creator_intent_required",
                            subject="creator-controlled story intent",
                            summary="Only the creator can decide the missing intent.",
                            evidence=[
                                "Committed story evidence cannot resolve this preference."
                            ],
                            creator_question=_creator_need().question,
                        )
                    ],
                    creator_input_need=_creator_need(),
                ),
                lineage_id="initial-wait-lineage",
                correction_round=0,
            )
            state = await ProjectStateQuery(engine).get_project(
                foundation.project_id
            )
            assert state is not None
            assert state.run.status == "waiting_for_user"
            assert state.run.wait_reason_code == "arc_parent_review_needs_user"
            assert state.creator_input_request is not None
            assert state.creator_input_request.review_id == review_id
            assert state.creator_input_request.route_layer == "arc"
            assert state.creator_input_request.question == _creator_need()
            async with engine.connect() as connection:
                assert (
                    await connection.scalar(
                        select(chapter_workspaces.c.lock_version).where(
                            chapter_workspaces.c.chapter_id
                            == fixture.chapter.chapter_id
                        )
                    )
                    == fixture.chapter.workspace_lock_version
                )

            feedback_service = FeedbackCommandService(CommandBus(engine))
            queued = await feedback_service.queue(
                QueueFeedbackRequest(
                    project_id=foundation.project_id,
                    content="Yes. The witness concealed it to protect Mara.",
                    route_layer="arc",
                    book_id=foundation.book_id,
                    arc_id=foundation.arc_id,
                    feedback_kind="correction_wait_response",
                    arc_parent_review_id=review_id,
                ),
                idempotency_key="initial-wait:queue-feedback",
            )
            applied = await feedback_service.apply(
                ApplyFeedbackRequest(
                    project_id=foundation.project_id,
                    feedback_id=queued.result.feedback_id,
                ),
                idempotency_key="initial-wait:apply-feedback",
            )
            assert applied.result.resulting_correction_lineage_id is not None

            refreshed = await ProjectStateQuery(engine).get_project(
                foundation.project_id
            )
            assert refreshed is not None
            assert refreshed.run.status == "running"
            assert refreshed.creator_input_request is None
            async with UnitOfWork(engine) as store:
                pending_lineage = (
                    await store.feedback.get_unstarted_correction_lineage(
                        project_id=foundation.project_id,
                        run_id=foundation.run_id,
                    )
                )
            assert pending_lineage is not None
            assert pending_lineage.id == queued.result.feedback_id
            assert (
                pending_lineage.resulting_correction_lineage_id
                == applied.result.resulting_correction_lineage_id
            )
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_round_one_recurrence_needs_creator_question_and_cannot_open_round_two(
    tmp_path: Path,
) -> None:
    database = tmp_path / "round-one-creator-wait.sqlite3"
    command.upgrade(alembic_config(database), "head")

    async def exercise() -> None:
        engine = create_sqlite_async_engine(database)
        try:
            fixture = await _prepare_arc_parent_fixture(
                engine,
                project_id="round-one-creator-wait",
            )
            foundation = fixture.chapter.foundation
            target = ChapterEvidenceTarget(
                chapter_book_ordinal=1,
                correction_goal=(
                    "Clarify whether the witness knew about the altered statement "
                    "without changing the approved prose."
                ),
            )
            round_zero_review_id = await _record_arc_parent(
                engine,
                fixture=fixture,
                suffix="round-zero",
                evaluation=ArcParentContractEvaluation(
                    arc_contract_judgment="remains_applicable",
                    book_review_concern="not_required",
                    chapter_evidence_concern="chapter_evidence_review_required",
                    chapter_evidence_target=target,
                    summary="The frozen prose needs one evidence-only clarification.",
                    issues=[_derived_evidence_issue()],
                ),
                lineage_id="bounded-correction-lineage",
                correction_round=0,
            )
            async with engine.connect() as connection:
                correction_lock = await connection.scalar(
                    select(chapter_workspaces.c.lock_version).where(
                        chapter_workspaces.c.chapter_id == fixture.chapter.chapter_id
                    )
                )
            assert correction_lock == fixture.chapter.workspace_lock_version + 1

            with pytest.raises(
                AuthorityTaskFailure,
                match="second downward correction",
            ) as captured:
                await _record_arc_parent(
                    engine,
                    fixture=fixture,
                    suffix="round-one-system-defect",
                    evaluation=ArcParentContractEvaluation(
                        arc_contract_judgment="remains_applicable",
                        book_review_concern="not_required",
                        chapter_evidence_concern=(
                            "chapter_evidence_review_required"
                        ),
                        chapter_evidence_target=target,
                        summary="The same correction is requested again.",
                        issues=[_derived_evidence_issue()],
                    ),
                    lineage_id="bounded-correction-lineage",
                    correction_round=1,
                    source_review_id=round_zero_review_id,
                )
            assert captured.value.code == "evaluation_contract_invalid"
            async with engine.connect() as connection:
                assert (
                    await connection.scalar(
                        select(func.count()).select_from(arc_parent_reviews)
                    )
                    == 1
                )

            round_one_review_id = await _record_arc_parent(
                engine,
                fixture=fixture,
                suffix="round-one-creator",
                evaluation=ArcParentContractEvaluation(
                    arc_contract_judgment="unable_to_judge",
                    book_review_concern="not_required",
                    chapter_evidence_concern="not_required",
                    summary="The remaining ambiguity belongs to creator intent.",
                    issues=[
                        EvaluationIssue(
                            kind="creator_owned_unknown",
                            code="creator_intent_required_after_correction",
                            subject="witness intent",
                            summary=(
                                "Committed evidence cannot determine the creator-owned intent."
                            ),
                            evidence=[
                                "The bounded evidence correction has already been consumed."
                            ],
                            creator_question=_creator_need().question,
                        )
                    ],
                    creator_input_need=_creator_need(),
                ),
                lineage_id="bounded-correction-lineage",
                correction_round=1,
                source_review_id=round_zero_review_id,
            )
            state = await ProjectStateQuery(engine).get_project(
                foundation.project_id
            )
            assert state is not None
            assert state.run.wait_reason_code == "evidence_correction_needs_user"
            assert state.creator_input_request is not None
            assert state.creator_input_request.review_id == round_one_review_id
            assert state.creator_input_request.automatic_correction_round == 1
            async with engine.connect() as connection:
                assert (
                    await connection.scalar(
                        select(chapter_workspaces.c.lock_version).where(
                            chapter_workspaces.c.chapter_id
                            == fixture.chapter.chapter_id
                        )
                    )
                    == correction_lock
                )
                assert (
                    await connection.scalar(
                        select(generation_runs.c.failure_source_kind).where(
                            generation_runs.c.id == foundation.run_id
                        )
                    )
                    is None
                )

            feedback_service = FeedbackCommandService(CommandBus(engine))
            queued = await feedback_service.queue(
                QueueFeedbackRequest(
                    project_id=foundation.project_id,
                    content="The witness knew and concealed it to protect Mara.",
                    route_layer="arc",
                    book_id=foundation.book_id,
                    arc_id=foundation.arc_id,
                    feedback_kind="correction_wait_response",
                    arc_parent_review_id=round_one_review_id,
                ),
                idempotency_key="round-one:queue-feedback",
            )
            applied = await feedback_service.apply(
                ApplyFeedbackRequest(
                    project_id=foundation.project_id,
                    feedback_id=queued.result.feedback_id,
                ),
                idempotency_key="round-one:apply-feedback",
            )
            assert applied.result.resulting_correction_lineage_id is not None
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_evidence_correction_preserves_chapter_bytes_and_committed_descendants(
    tmp_path: Path,
) -> None:
    database = tmp_path / "chapter-evidence-correction.sqlite3"
    command.upgrade(alembic_config(database), "head")

    async def exercise() -> None:
        engine = create_sqlite_async_engine(database)
        try:
            first = await _prepare_reviewed_chapter(
                engine,
                project_id="chapter-evidence-correction",
                target_chapter_count=4,
                canon_change=True,
            )
            chapter_service = ChapterCommandService(CommandBus(engine))
            first_commit = await chapter_service.commit_chapter_and_canon(
                CommitChapterRequest(
                    project_id=first.foundation.project_id,
                    chapter_id=first.chapter_id,
                    submission_id=first.submission_id,
                    review_id=first.review_id,
                    expected_canon_baseline_id=first.foundation.canon_baseline_id,
                ),
                idempotency_key="evidence:first-commit",
            )
            current_foundation = replace(
                first.foundation,
                canon_baseline_id=first_commit.result.canon_after_id,
            )
            second = await _prepare_reviewed_chapter(
                engine,
                project_id=first.foundation.project_id,
                target_chapter_count=4,
                canon_change=False,
                foundation=current_foundation,
                idempotency_suffix=":second",
            )
            second_commit = await chapter_service.commit_chapter_and_canon(
                CommitChapterRequest(
                    project_id=second.foundation.project_id,
                    chapter_id=second.chapter_id,
                    submission_id=second.submission_id,
                    review_id=second.review_id,
                    expected_canon_baseline_id=second.foundation.canon_baseline_id,
                ),
                idempotency_key="evidence:second-commit",
            )
            source = await _prepare_reviewed_chapter(
                engine,
                project_id=first.foundation.project_id,
                target_chapter_count=4,
                canon_change=False,
                foundation=current_foundation,
                idempotency_suffix=":source",
                evaluation=LayerEvaluationResult(
                    guidance_authority_judgment="not_present",
                    decision="escalate_to_arc",
                    summary="Arc authority must inspect earlier derived evidence.",
                    issues=[
                        ChapterEvaluationIssue(
                            kind="parent_authority_concern",
                            code="arc_authority_required",
                            subject="earlier derived evidence",
                            summary="Arc authority must inspect earlier derived evidence.",
                            evidence=[
                                "The reviewed Chapter evidence challenges its current Arc."
                            ],
                            observed_components=["observations", "canon"],
                        )
                    ],
                ),
            )
            async with engine.connect() as connection:
                request_id = await connection.scalar(
                    select(chapter_arc_change_requests.c.id).where(
                        chapter_arc_change_requests.c.chapter_id == source.chapter_id
                    )
                )
                arc_lock = await connection.scalar(
                    select(arc_workspaces.c.lock_version).where(
                        arc_workspaces.c.arc_id == first.foundation.arc_id
                    )
                )
            assert request_id is not None and arc_lock is not None
            fixture = ArcParentFixture(
                chapter=source,
                request_id=request_id,
                arc_workspace_lock_version=arc_lock,
            )
            lineage_id = "chapter-evidence-correction-lineage"
            parent_review_id = await _record_arc_parent(
                engine,
                fixture=fixture,
                suffix="evidence-parent",
                evaluation=ArcParentContractEvaluation(
                    arc_contract_judgment="remains_applicable",
                    book_review_concern="not_required",
                    chapter_evidence_concern="chapter_evidence_review_required",
                    chapter_evidence_target=ChapterEvidenceTarget(
                        chapter_book_ordinal=1,
                        correction_goal=(
                            "Clarify the observation already supported by the "
                            "approved prose."
                        ),
                    ),
                    summary="Chapter one needs evidence-only correction.",
                    issues=[_derived_evidence_issue()],
                ),
                lineage_id=lineage_id,
                correction_round=0,
            )
            async with engine.connect() as connection:
                workspace = (
                    await connection.execute(
                        select(chapter_workspaces).where(
                            chapter_workspaces.c.chapter_id == first.chapter_id
                        )
                    )
                ).mappings().one()
                original = (
                    await connection.execute(
                        select(chapter_baselines).where(
                            chapter_baselines.c.id
                            == first_commit.result.chapter_baseline_id
                        )
                    )
                ).mappings().one()
            assert workspace["plan_ref_id"] == original["plan_ref_id"]
            assert workspace["draft_ref_id"] == original["prose_ref_id"]
            assert workspace["revision_origin"] == "arc_evidence_correction"
            assert workspace["automatic_correction_round"] == 1
            correction_context = await HarnessContextBuilder(engine).build(
                task_kind="chapter.revise.observe",
                project_id=first.foundation.project_id,
                book_id=first.foundation.book_id,
                arc_id=first.foundation.arc_id,
                chapter_id=first.chapter_id,
                semantic_goal="Correct only the derived Chapter evidence.",
                source_arc_parent_review_id=parent_review_id,
            )
            correction_items = correction_context.manifest["items"]
            assert not any(
                item["group"] == "chapter_guidance"
                for item in correction_items
            )
            review_items = [
                item
                for item in correction_items
                if item["group"] == "arc_parent_review"
            ]
            assert len(review_items) == 1
            assert review_items[0]["role"] == "review_finding"
            assert review_items[0]["use"] == "repair_authorization"

            observations = ChapterObservationResult(
                summary=(
                    "Mara directly sees the written statement change while "
                    "preserving an analogue comparison."
                ),
                established_facts=[
                    {
                        "statement": (
                            "Mara preserves analogue copies before trusting later statements."
                        ),
                        "evidence_hint": (
                            "The frozen prose shows Mara preserving an analogue comparison."
                        ),
                    }
                ],
                canon_proposals=[
                    SemanticCanonProposal(
                        category="world_facts",
                        subject="Mara",
                        semantic_change=(
                            "Mara directly witnesses written memory evidence changing."
                        ),
                        resolved=False,
                        evidence_hint=(
                            "Mara directly watches the blue ink add a confession."
                        ),
                    )
                ],
            )
            observation_task, observation_attempt = await insert_successful_task(
                engine,
                project_id=first.foundation.project_id,
                run_id=first.foundation.run_id,
                task_id="evidence:revise-observations",
                attempt_id="evidence:revise-observations:attempt",
                role="chapter_writer",
                task_kind="chapter.revise.observe",
                scope_layer="chapter",
                book_id=first.foundation.book_id,
                book_baseline_id=first.foundation.book_baseline_id,
                arc_id=first.foundation.arc_id,
                arc_baseline_id=first.foundation.arc_baseline_id,
                chapter_id=first.chapter_id,
                chapter_baseline_id=first_commit.result.chapter_baseline_id,
                canon_baseline_id=second_commit.result.canon_after_id,
                workspace_lock_version=int(workspace["lock_version"]),
                correction_lineage_id=lineage_id,
                correction_lineage_origin="review_initiated",
                automatic_correction_round=1,
                source_arc_parent_review_id=parent_review_id,
                result=observations,
            )
            applied = await chapter_service.apply_revision_observation_result(
                ApplyChapterTaskRequest(
                    project_id=first.foundation.project_id,
                    chapter_id=first.chapter_id,
                    task_id=observation_task,
                    attempt_id=observation_attempt,
                    expected_workspace_lock_version=int(workspace["lock_version"]),
                ),
                idempotency_key="evidence:apply-observations",
            )
            submitted = await chapter_service.submit_for_review(
                SubmitChapterRequest(
                    project_id=first.foundation.project_id,
                    chapter_id=first.chapter_id,
                    expected_workspace_lock_version=(
                        applied.result.workspace_lock_version
                    ),
                ),
                idempotency_key="evidence:submit",
            )

            failed_task, failed_attempt = await insert_successful_task(
                engine,
                project_id=first.foundation.project_id,
                run_id=first.foundation.run_id,
                task_id="evidence:verify-descendant-conflict",
                attempt_id="evidence:verify-descendant-conflict:attempt",
                role="evaluator",
                task_kind="verify_evidence.chapter",
                scope_layer="chapter",
                book_id=first.foundation.book_id,
                book_baseline_id=first.foundation.book_baseline_id,
                arc_id=first.foundation.arc_id,
                arc_baseline_id=first.foundation.arc_baseline_id,
                chapter_id=first.chapter_id,
                chapter_baseline_id=first_commit.result.chapter_baseline_id,
                canon_baseline_id=second_commit.result.canon_after_id,
                workspace_lock_version=applied.result.workspace_lock_version,
                correction_lineage_id=lineage_id,
                correction_lineage_origin="review_initiated",
                automatic_correction_round=1,
                source_arc_parent_review_id=parent_review_id,
                result=ChapterEvidenceCorrectionEvaluation(
                    observations_supported_by_frozen_prose=True,
                    canon_intent_supported_by_frozen_prose=True,
                    descendant_facts_remain_consistent=False,
                    summary="The descendant consistency check failed.",
                    issues=[
                        EvaluationIssue(
                            kind="derived_evidence_mismatch",
                            code="descendant_fact_mismatch",
                            subject="corrected Chapter evidence",
                            summary=(
                                "The corrected derived evidence conflicts with a "
                                "formal descendant fact."
                            ),
                            evidence=[
                                "The corrected observation and descendant formal fact disagree."
                            ],
                            candidate_claim=(
                                "The corrected observation states the condition remained open."
                            ),
                            contrary_formal_statement=(
                                "A formal descendant Chapter records the condition as resolved."
                            ),
                        )
                    ],
                ),
            )
            evidence_strategy = DEFAULT_EVALUATION_STRATEGY_REGISTRY.for_task(
                "verify_evidence.chapter"
            )
            with pytest.raises(
                ChapterEvidenceVerificationFailure,
                match="did not pass",
            ):
                await chapter_service.record_evidence_review(
                    RecordChapterReviewRequest(
                        project_id=first.foundation.project_id,
                        chapter_id=first.chapter_id,
                        submission_id=submitted.result.submission_id,
                        evaluator_task_id=failed_task,
                        evaluator_attempt_id=failed_attempt,
                        rubric_id=evidence_strategy.rubric_id,
                        rubric_version=evidence_strategy.rubric_version,
                    ),
                    idempotency_key="evidence:reject-descendant-conflict",
                )

            evaluator_task, evaluator_attempt = await insert_successful_task(
                engine,
                project_id=first.foundation.project_id,
                run_id=first.foundation.run_id,
                task_id="evidence:verify",
                attempt_id="evidence:verify:attempt",
                role="evaluator",
                task_kind="verify_evidence.chapter",
                scope_layer="chapter",
                book_id=first.foundation.book_id,
                book_baseline_id=first.foundation.book_baseline_id,
                arc_id=first.foundation.arc_id,
                arc_baseline_id=first.foundation.arc_baseline_id,
                chapter_id=first.chapter_id,
                chapter_baseline_id=first_commit.result.chapter_baseline_id,
                canon_baseline_id=second_commit.result.canon_after_id,
                workspace_lock_version=applied.result.workspace_lock_version,
                correction_lineage_id=lineage_id,
                correction_lineage_origin="review_initiated",
                automatic_correction_round=1,
                source_arc_parent_review_id=parent_review_id,
                result=ChapterEvidenceCorrectionEvaluation(
                    observations_supported_by_frozen_prose=True,
                    canon_intent_supported_by_frozen_prose=True,
                    descendant_facts_remain_consistent=True,
                    summary="The correction is supported and descendants remain consistent.",
                ),
            )
            reviewed = await chapter_service.record_evidence_review(
                RecordChapterReviewRequest(
                    project_id=first.foundation.project_id,
                    chapter_id=first.chapter_id,
                    submission_id=submitted.result.submission_id,
                    evaluator_task_id=evaluator_task,
                    evaluator_attempt_id=evaluator_attempt,
                    rubric_id=evidence_strategy.rubric_id,
                    rubric_version=evidence_strategy.rubric_version,
                ),
                idempotency_key="evidence:record-review",
            )
            committed = await chapter_service.commit_chapter_and_canon(
                CommitChapterRequest(
                    project_id=first.foundation.project_id,
                    chapter_id=first.chapter_id,
                    submission_id=submitted.result.submission_id,
                    review_id=reviewed.result.review_id,
                    expected_current_chapter_baseline_id=(
                        first_commit.result.chapter_baseline_id
                    ),
                    expected_canon_baseline_id=second_commit.result.canon_after_id,
                ),
                idempotency_key="evidence:commit",
            )
            assert committed.result.chapter_baseline_version == 2
            assert committed.result.canon_changed

            async with engine.connect() as connection:
                revised = (
                    await connection.execute(
                        select(chapter_baselines).where(
                            chapter_baselines.c.id
                            == committed.result.chapter_baseline_id
                        )
                    )
                ).mappings().one()
                second_pointer = await connection.scalar(
                    select(chapters.c.current_baseline_id).where(
                        chapters.c.id == second.chapter_id
                    )
                )
                committed_chapter_count = await connection.scalar(
                    select(func.count())
                    .select_from(chapters)
                    .where(chapters.c.lifecycle_status == "committed")
                )
                arc_baseline_count = await connection.scalar(
                    select(func.count()).select_from(arc_baselines)
                )
                precheck_ref_id = await connection.scalar(
                    select(chapter_reviews.c.precheck_ref_id).where(
                        chapter_reviews.c.id == reviewed.result.review_id
                    )
                )
                assert precheck_ref_id is not None
                packed_precheck = await ContentRepository(connection).get_packed(
                    project_id=first.foundation.project_id,
                    ref_id=precheck_ref_id,
                )
                world_facts_ref_id = await connection.scalar(
                    select(canon_baselines.c.world_facts_ref_id).where(
                        canon_baselines.c.id == committed.result.canon_after_id
                    )
                )
                assert world_facts_ref_id is not None
                packed_world_facts = await ContentRepository(connection).get_packed(
                    project_id=first.foundation.project_id,
                    ref_id=world_facts_ref_id,
                )
            assert revised["plan_ref_id"] == original["plan_ref_id"]
            assert revised["prose_ref_id"] == original["prose_ref_id"]
            assert second_pointer == second_commit.result.chapter_baseline_id
            assert committed_chapter_count == 2
            assert arc_baseline_count == 1
            corrected_canon = [
                CanonEntry.model_validate(item)
                for item in json.loads(packed_world_facts.unpack_and_verify())
            ]
            mara_entry = next(item for item in corrected_canon if item.subject == "Mara")
            assert mara_entry.source_chapter_baseline_id == (
                committed.result.chapter_baseline_id
            )
            assert mara_entry.source_prose_ref_id == revised["prose_ref_id"]
            assert mara_entry.evidence.hint == (
                "Mara directly watches the blue ink add a confession."
            )
            precheck = json.loads(packed_precheck.unpack_and_verify())
            assert precheck["plan_bytes_unchanged"] is True
            assert precheck["prose_bytes_unchanged"] is True
            assert precheck["descendant_context_count"] == 1
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_second_automatic_arc_revision_waits_without_opening_workspace(
    tmp_path: Path,
) -> None:
    database = tmp_path / "arc-revision-limit.sqlite3"
    command.upgrade(alembic_config(database), "head")

    async def exercise() -> None:
        engine = create_sqlite_async_engine(database)
        try:
            fixture = await _prepare_arc_parent_fixture(
                engine,
                project_id="arc-revision-limit",
            )
            foundation = fixture.chapter.foundation
            # This test starts at the durable AR1 boundary: one automatic
            # recovery-origin baseline already exists for the active Arc.
            async with engine.begin() as connection:
                await connection.execute(
                    update(arc_baselines)
                    .where(arc_baselines.c.id == foundation.arc_baseline_id)
                    .values(revision_origin="automatic_arc_recovery")
                )
            review_id = await _record_arc_parent(
                engine,
                fixture=fixture,
                suffix="arc-revision-limit",
                evaluation=ArcParentContractEvaluation(
                    arc_contract_judgment="revision_warranted",
                    book_review_concern="not_required",
                    chapter_evidence_concern="not_required",
                    summary="Arc authority confirms another replacement is warranted.",
                ),
                lineage_id="arc-revision-limit-lineage",
                correction_round=0,
            )
            activated = await ChangeRequestCommandService(
                CommandBus(engine)
            ).activate(
                ActivateChangeRequest(
                    project_id=foundation.project_id,
                    change_request_id=fixture.request_id,
                    request_kind="chapter_to_arc",
                    expected_target_baseline_id=foundation.arc_baseline_id,
                    expected_workspace_lock_version=(
                        fixture.arc_workspace_lock_version
                    ),
                ),
                idempotency_key="arc-revision-limit:activate",
            )
            assert activated.result.action == "arc_revision_limit_reached"
            assert activated.result.workspace_lock_version is None
            async with engine.connect() as connection:
                workspace = (
                    await connection.execute(
                        select(
                            arc_workspaces.c.state,
                            arc_workspaces.c.lock_version,
                            arc_workspaces.c.base_arc_baseline_id,
                        ).where(arc_workspaces.c.arc_id == foundation.arc_id)
                    )
                ).one()
                run = (
                    await connection.execute(
                        select(
                            generation_runs.c.status,
                            generation_runs.c.wait_reason_code,
                            generation_runs.c.failure_source_kind,
                            generation_runs.c.blocking_task_id,
                            generation_runs.c.blocking_action_key,
                        ).where(generation_runs.c.id == foundation.run_id)
                    )
                ).one()
                opened_workspace = await connection.scalar(
                    select(arc_parent_reviews.c.opened_arc_workspace_id).where(
                        arc_parent_reviews.c.id == review_id
                    )
                )
            assert tuple(workspace) == (
                "idle",
                fixture.arc_workspace_lock_version,
                foundation.arc_baseline_id,
            )
            assert tuple(run) == (
                "waiting_for_user",
                "arc_revision_limit_reached",
                None,
                None,
                None,
            )
            assert opened_workspace is None
        finally:
            await engine.dispose()

    asyncio.run(exercise())
