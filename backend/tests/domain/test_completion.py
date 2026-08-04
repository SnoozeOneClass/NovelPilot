from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
import pytest
from alembic import command
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.agents.contracts import (
    ArcChapterOutlineEntry,
    ArcPlanProposal,
    EvaluationIssue,
)
from app.agents.registry import DEFAULT_EVALUATION_STRATEGY_REGISTRY
from app.db.engine import create_sqlite_async_engine
from app.db.maintenance import alembic_config
from app.db.schema import (
    arc_book_change_requests,
    arc_closure_reviews,
    arc_closures,
    arc_workspaces,
    book_completions,
    book_progress_handoffs,
    book_workspaces,
    books,
    chapter_workspaces,
    generation_runs,
    projects,
    story_arcs,
)
from app.db.uow import UnitOfWork
from app.domain.arc.commands import ArcCommandService
from app.domain.arc.contracts import (
    ApplyArcTaskRequest,
    ArcEvaluation,
    CommitArcAutoRequest,
    CreateStoryArcRequest,
    RecordArcReviewRequest,
    SubmitArcRequest,
)
from app.domain.authority import (
    CommitBookCompletionRequest,
    CommitBookProgressHandoffRequest,
    LoopAuthorityCommandService,
    OpenBookCompletionRevisionRequest,
    RecordArcClosureReviewRequest,
    RecordBookCompletionReviewRequest,
)
from app.domain.book.commands import BookCommandService
from app.domain.book.contracts import (
    ApplyBookCandidateTaskRequest,
    BookArcContract,
    BookArcTopologySuffix,
    BookCompletionRequirement,
    BookCreativeConstraints,
    BookRollingPlan,
    BookSuccessorCandidateProposal,
    CompletionContract,
)
from app.domain.chapter.commands import ChapterCommandService
from app.domain.chapter.contracts import CommitChapterRequest
from app.domain.commands import CommandPreconditionError
from app.domain.evaluation import (
    ArcClosureEvaluation,
    BookCompletionEvaluation,
    CompletionRequirementStatus,
)
from app.domain.feedback import FeedbackCommandService, SubmitFeedbackRequest
from app.runtime.context import HarnessContextBuilder
from app.runtime.driver import DomainRunDriver
from app.store.command_bus import CommandBus
from app.store.content import ContentRepository
from tests.domain.test_chapter_lifecycle import (
    ReviewedChapter,
    _prepare_reviewed_chapter,
)
from tests.helpers.lifecycle_seed import ApprovedFoundation, insert_successful_task


def test_arc_closure_disposition_uses_one_discriminated_outcome() -> None:
    closed = ArcClosureEvaluation.model_validate(
        {
            "outcome": {
                "kind": "closed",
                "summary": "Every assigned Book exit condition has committed evidence.",
                "evidence": ["The terminal Chapter identifies the source."],
            }
        }
    )
    revise = ArcClosureEvaluation.model_validate(
        {
            "outcome": {
                "kind": "revise_arc",
                "summary": "The Book assignment remains valid but one exit is absent.",
                "issues": [
                    EvaluationIssue(
                        kind="contract_unfulfilled",
                        code="arc_exit_missing",
                        subject="assigned Book Arc exit",
                        summary="The committed Arc does not establish the exit.",
                        evidence=["The terminal Chapter leaves the source unresolved."],
                        contract_item="Identify the source through physical evidence.",
                    ).model_dump(mode="json")
                ],
            }
        }
    )

    assert LoopAuthorityCommandService._arc_closure_disposition(evaluation=closed) == "pass"
    assert (
        LoopAuthorityCommandService._arc_closure_disposition(evaluation=revise)
        == "arc_revision_warranted"
    )


def _arc_closure_evaluation(outcome_kind: str) -> ArcClosureEvaluation:
    if outcome_kind == "closed":
        outcome = {
            "kind": "closed",
            "summary": "Every assigned Book Arc exit has committed evidence.",
            "evidence": ["Chapter 1 records the physical trace and its source."],
        }
    elif outcome_kind == "revise_arc":
        outcome = {
            "kind": "revise_arc",
            "summary": "The Book contract remains valid but the current Arc exit is absent.",
            "issues": [
                {
                    "kind": "contract_unfulfilled",
                    "code": "arc_exit_not_established",
                    "subject": "assigned Book Arc exit",
                    "summary": "The committed Chapter does not establish the required exit.",
                    "evidence": ["The terminal observation leaves the source unresolved."],
                    "contract_item": "Committed evidence identifies the source of the edit.",
                }
            ],
        }
    elif outcome_kind == "escalate_to_book":
        outcome = {
            "kind": "escalate_to_book",
            "summary": "Committed evidence challenges a Book-owned promise.",
            "issues": [
                {
                    "kind": "parent_authority_concern",
                    "code": "book_promise_review_required",
                    "subject": "approved Book promise",
                    "summary": "Only Book may judge whether its promise still applies.",
                    "evidence": ["The Arc evidence reaches the Book-owned outcome."],
                }
            ],
        }
    elif outcome_kind == "correct_chapter_evidence":
        outcome = {
            "kind": "correct_chapter_evidence",
            "summary": "The formal prose is sound but its derived evidence is inverted.",
            "target": {
                "chapter_book_ordinal": 1,
                "correction_goal": (
                    "Correct observations and Canon intent from the frozen Chapter prose."
                ),
            },
            "issues": [
                {
                    "kind": "derived_evidence_mismatch",
                    "code": "chapter_observation_inverts_prose",
                    "subject": "Chapter 1 derived observation",
                    "summary": "The observation contradicts the formal prose it summarizes.",
                    "evidence": ["Formal prose records the trace on the desk."],
                    "candidate_claim": "The observation says no physical trace exists.",
                    "contrary_formal_statement": (
                        "The approved prose records the physical trace on the desk."
                    ),
                }
            ],
        }
    elif outcome_kind == "needs_user":
        question = "Should the ambiguous witness identity remain deliberately unresolved?"
        outcome = {
            "kind": "needs_user",
            "summary": "Only the creator controls the intended ambiguity.",
            "creator_input_need": {
                "controlled_fact": "Whether the witness identity is intentionally ambiguous.",
                "question": question,
                "evidence": ["Committed prose and Canon do not encode this creator intent."],
            },
            "issues": [
                {
                    "kind": "creator_owned_unknown",
                    "code": "witness_ambiguity_intent_unknown",
                    "subject": "creator-owned witness intent",
                    "summary": "Formal evidence cannot determine the intended ambiguity.",
                    "evidence": ["No approved creator decision resolves this intent."],
                    "creator_question": question,
                }
            ],
        }
    else:  # pragma: no cover - guards the local test matrix.
        raise AssertionError(f"Unknown test Arc closure outcome: {outcome_kind}")
    return ArcClosureEvaluation.model_validate({"outcome": outcome})


@pytest.mark.parametrize(
    (
        "outcome_kind",
        "expected_disposition",
        "expected_owner",
        "expected_arc_judgment",
        "expected_parent_judgment",
        "expected_next_attribute",
        "expected_next_value",
    ),
    [
        (
            "closed",
            "pass",
            "none",
            "remains_applicable",
            "not_required",
            "task_kind",
            "evaluate.book_completion",
        ),
        (
            "revise_arc",
            "arc_revision_warranted",
            "arc",
            "revision_warranted",
            "not_required",
            "kind",
            "open_arc_closure_revision",
        ),
        (
            "escalate_to_book",
            "book_review_required",
            "book",
            "remains_applicable",
            "book_review_required",
            "task_kind",
            "evaluate.book_parent_contract",
        ),
        (
            "correct_chapter_evidence",
            "chapter_evidence_review_required",
            "chapter",
            "remains_applicable",
            "not_required",
            "task_kind",
            "chapter.revise.observe",
        ),
        (
            "needs_user",
            "waiting_for_user",
            "creator",
            "unable_to_judge",
            "not_required",
            None,
            None,
        ),
    ],
)
def test_every_arc_closure_outcome_persists_and_resumes_its_exact_route(
    tmp_path: Path,
    outcome_kind: str,
    expected_disposition: str,
    expected_owner: str,
    expected_arc_judgment: str,
    expected_parent_judgment: str,
    expected_next_attribute: str | None,
    expected_next_value: str | None,
) -> None:
    database = tmp_path / f"arc-closure-{outcome_kind}.sqlite3"
    command.upgrade(alembic_config(database), "head")

    async def exercise() -> None:
        engine = create_sqlite_async_engine(database)
        try:
            ready = await _prepare_reviewed_chapter(
                engine,
                project_id=f"arc-closure-{outcome_kind}",
                target_chapter_count=1,
                canon_change=False,
            )
            committed = await ChapterCommandService(CommandBus(engine)).commit_chapter_and_canon(
                CommitChapterRequest(
                    project_id=ready.foundation.project_id,
                    chapter_id=ready.chapter_id,
                    submission_id=ready.submission_id,
                    review_id=ready.review_id,
                    expected_canon_baseline_id=ready.foundation.canon_baseline_id,
                ),
                idempotency_key=f"{outcome_kind}:commit-terminal-chapter",
            )
            assert committed.result.arc_closure_due
            async with engine.connect() as connection:
                arc_workspace_lock = await connection.scalar(
                    select(arc_workspaces.c.lock_version).where(
                        arc_workspaces.c.arc_id == ready.foundation.arc_id
                    )
                )
            assert arc_workspace_lock is not None
            task_id, attempt_id = await insert_successful_task(
                engine,
                project_id=ready.foundation.project_id,
                run_id=ready.foundation.run_id,
                task_id=f"{outcome_kind}:evaluate-arc-closure",
                attempt_id=f"{outcome_kind}:evaluate-arc-closure:attempt",
                role="evaluator",
                task_kind="evaluate.arc_closure",
                scope_layer="arc",
                book_id=ready.foundation.book_id,
                book_baseline_id=ready.foundation.book_baseline_id,
                arc_id=ready.foundation.arc_id,
                arc_baseline_id=ready.foundation.arc_baseline_id,
                canon_baseline_id=committed.result.canon_after_id,
                workspace_lock_version=arc_workspace_lock,
                correction_lineage_id=f"{outcome_kind}:closure-lineage",
                correction_lineage_origin="review_initiated",
                automatic_correction_round=0,
                result=_arc_closure_evaluation(outcome_kind),
            )
            recorded = await LoopAuthorityCommandService(
                CommandBus(engine)
            ).record_arc_closure_review(
                RecordArcClosureReviewRequest(
                    project_id=ready.foundation.project_id,
                    book_id=ready.foundation.book_id,
                    arc_id=ready.foundation.arc_id,
                    task_id=task_id,
                    attempt_id=attempt_id,
                ),
                idempotency_key=f"{outcome_kind}:record-arc-closure",
            )
            assert recorded.result.disposition == expected_disposition
            assert (recorded.result.formal_closure_id is not None) == (outcome_kind == "closed")
            assert (recorded.result.change_request_id is not None) == (
                outcome_kind == "escalate_to_book"
            )
            assert recorded.result.downstream_action == (
                "chapter_evidence_correction_opened"
                if outcome_kind == "correct_chapter_evidence"
                else "none"
            )
        finally:
            await engine.dispose()

        restarted = create_sqlite_async_engine(database)
        try:
            async with restarted.connect() as connection:
                review = (
                    await connection.execute(
                        select(
                            arc_closure_reviews.c.disposition,
                            arc_closure_reviews.c.resolution_owner,
                            arc_closure_reviews.c.arc_contract_judgment,
                            arc_closure_reviews.c.parent_review_judgment,
                            arc_closure_reviews.c.detail_ref_id,
                            arc_closure_reviews.c.user_question_ref_id,
                        ).where(arc_closure_reviews.c.id == recorded.result.review_id)
                    )
                ).one()
                persisted_result = ArcClosureEvaluation.model_validate_json(
                    (
                        await ContentRepository(connection).get_packed(
                            project_id=ready.foundation.project_id,
                            ref_id=review.detail_ref_id,
                        )
                    ).unpack_and_verify()
                )
                arc_state = (
                    await connection.execute(
                        select(
                            story_arcs.c.lifecycle_status,
                            story_arcs.c.current_closure_id,
                            story_arcs.c.latest_closure_review_id,
                        ).where(story_arcs.c.id == ready.foundation.arc_id)
                    )
                ).one()
                run_state = (
                    await connection.execute(
                        select(
                            generation_runs.c.status,
                            generation_runs.c.wait_reason_code,
                        ).where(generation_runs.c.id == ready.foundation.run_id)
                    )
                ).one()
                formal_closure_count = await connection.scalar(
                    select(func.count()).select_from(arc_closures)
                )
                book_request_count = await connection.scalar(
                    select(func.count()).select_from(arc_book_change_requests)
                )
                chapter_workspace = (
                    await connection.execute(
                        select(
                            chapter_workspaces.c.state,
                            chapter_workspaces.c.revision_origin,
                            chapter_workspaces.c.source_arc_closure_review_id,
                        ).where(chapter_workspaces.c.chapter_id == ready.chapter_id)
                    )
                ).one()

            assert persisted_result.outcome.kind == outcome_kind
            assert tuple(review[:4]) == (
                expected_disposition,
                expected_owner,
                expected_arc_judgment,
                expected_parent_judgment,
            )
            assert (review.user_question_ref_id is not None) == (outcome_kind == "needs_user")
            assert arc_state.latest_closure_review_id == recorded.result.review_id
            assert arc_state.lifecycle_status == (
                "completed" if outcome_kind == "closed" else "closing"
            )
            assert (arc_state.current_closure_id is not None) == (outcome_kind == "closed")
            assert formal_closure_count == (1 if outcome_kind == "closed" else 0)
            assert book_request_count == (1 if outcome_kind == "escalate_to_book" else 0)
            assert tuple(run_state) == (
                ("waiting_for_user", "arc_closure_needs_user")
                if outcome_kind == "needs_user"
                else ("running", None)
            )
            if outcome_kind == "correct_chapter_evidence":
                assert tuple(chapter_workspace) == (
                    "active",
                    "arc_evidence_correction",
                    recorded.result.review_id,
                )

            if expected_next_attribute is not None:
                async with UnitOfWork(restarted) as store:
                    run = await store.runs.get(
                        project_id=ready.foundation.project_id,
                        run_id=ready.foundation.run_id,
                    )
                assert run is not None
                driver = object.__new__(DomainRunDriver)
                driver._engine = restarted
                instruction = await driver._decide_next(run)
                assert instruction is not None
                assert getattr(instruction, expected_next_attribute) == expected_next_value
        finally:
            await restarted.dispose()

    asyncio.run(exercise())


@dataclass(frozen=True, slots=True)
class FormalArcClosure:
    chapter: ReviewedChapter
    canon_baseline_id: str
    arc_closure_id: str
    book_workspace_lock_version: int


async def _prepare_formal_arc_closure(
    engine: AsyncEngine,
    *,
    project_id: str,
    arc_contract_count: int = 1,
) -> FormalArcClosure:
    ready = await _prepare_reviewed_chapter(
        engine,
        project_id=project_id,
        target_chapter_count=1,
        canon_change=False,
        arc_contract_count=arc_contract_count,
    )
    committed = await ChapterCommandService(CommandBus(engine)).commit_chapter_and_canon(
        CommitChapterRequest(
            project_id=ready.foundation.project_id,
            chapter_id=ready.chapter_id,
            submission_id=ready.submission_id,
            review_id=ready.review_id,
            expected_canon_baseline_id=ready.foundation.canon_baseline_id,
        ),
        idempotency_key=f"{project_id}:commit-terminal-chapter",
    )
    assert committed.result.arc_closure_due
    async with engine.connect() as connection:
        arc_workspace_lock = await connection.scalar(
            select(arc_workspaces.c.lock_version).where(
                arc_workspaces.c.arc_id == ready.foundation.arc_id
            )
        )
    assert arc_workspace_lock is not None
    task_id, attempt_id = await insert_successful_task(
        engine,
        project_id=ready.foundation.project_id,
        run_id=ready.foundation.run_id,
        task_id=f"{project_id}:evaluate-arc-closure",
        attempt_id=f"{project_id}:evaluate-arc-closure:attempt",
        role="evaluator",
        task_kind="evaluate.arc_closure",
        scope_layer="arc",
        book_id=ready.foundation.book_id,
        book_baseline_id=ready.foundation.book_baseline_id,
        arc_id=ready.foundation.arc_id,
        arc_baseline_id=ready.foundation.arc_baseline_id,
        canon_baseline_id=committed.result.canon_after_id,
        workspace_lock_version=arc_workspace_lock,
        correction_lineage_id=f"{project_id}:arc-closure-lineage",
        correction_lineage_origin="review_initiated",
        automatic_correction_round=0,
        result=ArcClosureEvaluation.model_validate(
            {
                "outcome": {
                    "kind": "closed",
                    "summary": "The assigned Book Arc exits are semantically complete.",
                    "evidence": ["Chapter 1 observations identify the first edit source."],
                }
            }
        ),
    )
    authority = LoopAuthorityCommandService(CommandBus(engine))
    closure = await authority.record_arc_closure_review(
        RecordArcClosureReviewRequest(
            project_id=ready.foundation.project_id,
            book_id=ready.foundation.book_id,
            arc_id=ready.foundation.arc_id,
            task_id=task_id,
            attempt_id=attempt_id,
        ),
        idempotency_key=f"{project_id}:record-arc-closure",
    )
    assert closure.result.disposition == "pass"
    assert closure.result.formal_closure_id is not None
    async with engine.connect() as connection:
        book_workspace_lock = await connection.scalar(
            select(book_workspaces.c.lock_version).where(
                book_workspaces.c.book_id == ready.foundation.book_id
            )
        )
    assert book_workspace_lock is not None
    return FormalArcClosure(
        chapter=ready,
        canon_baseline_id=committed.result.canon_after_id,
        arc_closure_id=closure.result.formal_closure_id,
        book_workspace_lock_version=book_workspace_lock,
    )


async def _record_book_completion(
    engine: AsyncEngine,
    *,
    closure: FormalArcClosure,
    requirement_status: str,
    suffix: str,
) -> tuple[LoopAuthorityCommandService, str, str]:
    ready = closure.chapter
    async with engine.connect() as connection:
        source_progress_handoff_id = await connection.scalar(
            select(books.c.current_progress_handoff_id).where(
                books.c.id == ready.foundation.book_id
            )
        )
    task_id, attempt_id = await insert_successful_task(
        engine,
        project_id=ready.foundation.project_id,
        run_id=ready.foundation.run_id,
        task_id=f"{suffix}:evaluate-book-completion",
        attempt_id=f"{suffix}:evaluate-book-completion:attempt",
        role="evaluator",
        task_kind="evaluate.book_completion",
        scope_layer="book",
        book_id=ready.foundation.book_id,
        book_baseline_id=ready.foundation.book_baseline_id,
        canon_baseline_id=closure.canon_baseline_id,
        workspace_lock_version=closure.book_workspace_lock_version,
        correction_lineage_id=f"{suffix}:book-completion-lineage",
        correction_lineage_origin="review_initiated",
        automatic_correction_round=0,
        source_arc_closure_id=closure.arc_closure_id,
        source_book_progress_handoff_id=source_progress_handoff_id,
        result=BookCompletionEvaluation(
            requirement_statuses=[
                CompletionRequirementStatus(
                    requirement_key="memory_conflict_resolved",
                    status=requirement_status,
                    evidence=["The terminal Chapter supplies the current evidence."],
                    rationale="The status is frozen from committed evidence.",
                )
            ],
            book_contract_judgment="remains_applicable",
            summary="The planned final Arc supports a Book completion decision.",
        ),
    )
    authority = LoopAuthorityCommandService(CommandBus(engine))
    review = await authority.record_book_completion_review(
        RecordBookCompletionReviewRequest(
            project_id=ready.foundation.project_id,
            book_id=ready.foundation.book_id,
            task_id=task_id,
            attempt_id=attempt_id,
        ),
        idempotency_key=f"{suffix}:record-book-completion",
    )
    return authority, review.result.review_id, review.result.disposition


async def _prepare_second_formal_arc_closure(
    engine: AsyncEngine,
    *,
    first: FormalArcClosure,
    handoff_id: str,
) -> FormalArcClosure:
    foundation = first.chapter.foundation
    bus = CommandBus(engine)
    arc_service = ArcCommandService(bus)
    created = await arc_service.create_story_arc(
        CreateStoryArcRequest(
            project_id=foundation.project_id,
            book_id=foundation.book_id,
            expected_book_baseline_id=foundation.book_baseline_id,
            expected_canon_baseline_id=first.canon_baseline_id,
            expected_ordinal=2,
            source_progress_handoff_id=handoff_id,
        ),
        idempotency_key=f"{foundation.project_id}:create-second-arc",
    )
    arc_id = created.result.arc_id
    plan_task_id, plan_attempt_id = await insert_successful_task(
        engine,
        project_id=foundation.project_id,
        run_id=foundation.run_id,
        task_id=f"{arc_id}:plan",
        attempt_id=f"{arc_id}:plan:attempt",
        role="arc_planner",
        task_kind="arc.plan",
        scope_layer="arc",
        book_id=foundation.book_id,
        book_baseline_id=foundation.book_baseline_id,
        arc_id=arc_id,
        canon_baseline_id=first.canon_baseline_id,
        source_book_progress_handoff_id=handoff_id,
        workspace_lock_version=created.result.workspace_lock_version,
        result=ArcPlanProposal(
            title="The Final Evidence",
            chapter_outline=[
                ArcChapterOutlineEntry(
                    title="The Final Proof",
                    core_event="Physical proof resolves the central conflict.",
                    hook="Hand the completed evidence to Book closure.",
                    scenes=["Test the final source.", "Commit the physical proof."],
                )
            ],
        ),
    )
    applied = await arc_service.apply_task_result(
        ApplyArcTaskRequest(
            project_id=foundation.project_id,
            book_id=foundation.book_id,
            arc_id=arc_id,
            task_id=plan_task_id,
            attempt_id=plan_attempt_id,
            expected_workspace_lock_version=created.result.workspace_lock_version,
        ),
        idempotency_key=f"{arc_id}:apply-plan",
    )
    submitted = await arc_service.submit_for_review(
        SubmitArcRequest(
            project_id=foundation.project_id,
            book_id=foundation.book_id,
            arc_id=arc_id,
            expected_workspace_lock_version=applied.result.workspace_lock_version,
        ),
        idempotency_key=f"{arc_id}:submit",
    )
    review_task_id, review_attempt_id = await insert_successful_task(
        engine,
        project_id=foundation.project_id,
        run_id=foundation.run_id,
        task_id=f"{arc_id}:evaluate",
        attempt_id=f"{arc_id}:evaluate:attempt",
        role="evaluator",
        task_kind="evaluate.arc",
        scope_layer="arc",
        book_id=foundation.book_id,
        book_baseline_id=foundation.book_baseline_id,
        arc_id=arc_id,
        canon_baseline_id=first.canon_baseline_id,
        source_book_progress_handoff_id=handoff_id,
        workspace_lock_version=applied.result.workspace_lock_version,
        result=ArcEvaluation(
            guidance_authority_judgment="not_present",
            decision="pass",
            summary="The final Arc plan fits the approved Book topology.",
        ),
    )
    reviewed = await arc_service.record_review(
        RecordArcReviewRequest(
            project_id=foundation.project_id,
            book_id=foundation.book_id,
            arc_id=arc_id,
            submission_id=submitted.result.submission_id,
            evaluator_task_id=review_task_id,
            evaluator_attempt_id=review_attempt_id,
            rubric_id=DEFAULT_EVALUATION_STRATEGY_REGISTRY.for_task("evaluate.arc").rubric_id,
            rubric_version=DEFAULT_EVALUATION_STRATEGY_REGISTRY.for_task(
                "evaluate.arc"
            ).rubric_version,
            deterministic_precheck={"passed": True},
        ),
        idempotency_key=f"{arc_id}:record-review",
    )
    committed_arc = await arc_service.commit_baseline_auto(
        CommitArcAutoRequest(
            project_id=foundation.project_id,
            book_id=foundation.book_id,
            arc_id=arc_id,
            submission_id=submitted.result.submission_id,
            review_id=reviewed.result.review_id,
        ),
        idempotency_key=f"{arc_id}:commit",
    )
    second_foundation = ApprovedFoundation(
        project_id=foundation.project_id,
        run_id=foundation.run_id,
        book_id=foundation.book_id,
        book_baseline_id=foundation.book_baseline_id,
        arc_id=arc_id,
        arc_baseline_id=committed_arc.result.baseline_id,
        canon_baseline_id=first.canon_baseline_id,
        target_chapter_count=1,
    )
    ready = await _prepare_reviewed_chapter(
        engine,
        project_id=foundation.project_id,
        target_chapter_count=1,
        canon_change=False,
        foundation=second_foundation,
        idempotency_suffix=":second-arc",
    )
    committed_chapter = await ChapterCommandService(bus).commit_chapter_and_canon(
        CommitChapterRequest(
            project_id=foundation.project_id,
            chapter_id=ready.chapter_id,
            submission_id=ready.submission_id,
            review_id=ready.review_id,
            expected_canon_baseline_id=first.canon_baseline_id,
        ),
        idempotency_key=f"{ready.chapter_id}:commit",
    )
    assert committed_chapter.result.arc_closure_due
    async with engine.connect() as connection:
        arc_workspace_lock = await connection.scalar(
            select(arc_workspaces.c.lock_version).where(arc_workspaces.c.arc_id == arc_id)
        )
    assert arc_workspace_lock is not None
    closure_task_id, closure_attempt_id = await insert_successful_task(
        engine,
        project_id=foundation.project_id,
        run_id=foundation.run_id,
        task_id=f"{arc_id}:evaluate-closure",
        attempt_id=f"{arc_id}:evaluate-closure:attempt",
        role="evaluator",
        task_kind="evaluate.arc_closure",
        scope_layer="arc",
        book_id=foundation.book_id,
        book_baseline_id=foundation.book_baseline_id,
        arc_id=arc_id,
        arc_baseline_id=committed_arc.result.baseline_id,
        canon_baseline_id=committed_chapter.result.canon_after_id,
        workspace_lock_version=arc_workspace_lock,
        correction_lineage_id=f"{arc_id}:closure-lineage",
        correction_lineage_origin="review_initiated",
        automatic_correction_round=0,
        result=ArcClosureEvaluation.model_validate(
            {
                "outcome": {
                    "kind": "closed",
                    "summary": "The final assigned Book Arc exits are complete.",
                    "evidence": ["The final Chapter records the physical proof."],
                }
            }
        ),
    )
    closure = await LoopAuthorityCommandService(bus).record_arc_closure_review(
        RecordArcClosureReviewRequest(
            project_id=foundation.project_id,
            book_id=foundation.book_id,
            arc_id=arc_id,
            task_id=closure_task_id,
            attempt_id=closure_attempt_id,
        ),
        idempotency_key=f"{arc_id}:record-closure",
    )
    assert closure.result.disposition == "pass"
    assert closure.result.formal_closure_id is not None
    async with engine.connect() as connection:
        book_workspace_lock = await connection.scalar(
            select(book_workspaces.c.lock_version).where(
                book_workspaces.c.book_id == foundation.book_id
            )
        )
    assert book_workspace_lock is not None
    return FormalArcClosure(
        chapter=ready,
        canon_baseline_id=committed_chapter.result.canon_after_id,
        arc_closure_id=closure.result.formal_closure_id,
        book_workspace_lock_version=book_workspace_lock,
    )


def test_formal_final_arc_and_book_completion_atomically_complete_run(
    tmp_path: Path,
) -> None:
    database = tmp_path / "completion.sqlite3"
    command.upgrade(alembic_config(database), "head")

    async def exercise() -> None:
        engine = create_sqlite_async_engine(database)
        try:
            closure = await _prepare_formal_arc_closure(
                engine,
                project_id="completion-project",
            )
            authority, review_id, disposition = await _record_book_completion(
                engine,
                closure=closure,
                requirement_status="satisfied",
                suffix="completion",
            )
            assert disposition == "complete_book"
            completed = await authority.commit_book_completion(
                CommitBookCompletionRequest(
                    project_id=closure.chapter.foundation.project_id,
                    book_id=closure.chapter.foundation.book_id,
                    completion_review_id=review_id,
                ),
                idempotency_key="completion:commit",
            )
            with pytest.raises(
                CommandPreconditionError,
                match="dependencies are not current",
            ):
                await ArcCommandService(CommandBus(engine)).create_story_arc(
                    CreateStoryArcRequest(
                        project_id=closure.chapter.foundation.project_id,
                        book_id=closure.chapter.foundation.book_id,
                        expected_book_baseline_id=(closure.chapter.foundation.book_baseline_id),
                        expected_canon_baseline_id=closure.canon_baseline_id,
                        expected_ordinal=2,
                    ),
                    idempotency_key="completion:no-next-arc",
                )
            async with engine.connect() as connection:
                statuses = (
                    await connection.execute(
                        select(
                            projects.c.lifecycle_status,
                            books.c.lifecycle_status,
                            books.c.current_completion_id,
                            generation_runs.c.status,
                            generation_runs.c.finished_at_ms,
                        )
                        .join(books, books.c.project_id == projects.c.id)
                        .join(
                            generation_runs,
                            generation_runs.c.project_id == projects.c.id,
                        )
                        .where(projects.c.id == closure.chapter.foundation.project_id)
                    )
                ).one()
                assert tuple(statuses[:4]) == (
                    "completed",
                    "completed",
                    completed.result.completion_id,
                    "completed",
                )
                assert statuses.finished_at_ms is not None
                assert (
                    await connection.scalar(select(func.count()).select_from(book_completions)) == 1
                )
                assert await connection.scalar(select(func.count()).select_from(arc_closures)) == 1
                formal_closure = (
                    await connection.execute(
                        select(
                            arc_closures.c.book_baseline_id,
                            arc_closures.c.arc_baseline_id,
                            arc_closures.c.canon_baseline_id,
                            arc_closures.c.terminal_chapter_id,
                            arc_closures.c.terminal_chapter_baseline_id,
                            arc_closures.c.chapter_set_manifest_ref_id,
                        ).where(arc_closures.c.id == closure.arc_closure_id)
                    )
                ).one()
                formal_completion = (
                    await connection.execute(
                        select(
                            book_completions.c.book_baseline_id,
                            book_completions.c.arc_closure_id,
                            book_completions.c.terminal_arc_id,
                            book_completions.c.terminal_arc_baseline_id,
                            book_completions.c.terminal_chapter_id,
                            book_completions.c.terminal_chapter_baseline_id,
                            book_completions.c.canon_baseline_id,
                            book_completions.c.gate_manifest_ref_id,
                        ).where(book_completions.c.id == completed.result.completion_id)
                    )
                ).one()
                assert formal_closure.book_baseline_id == (
                    closure.chapter.foundation.book_baseline_id
                )
                assert formal_closure.arc_baseline_id == (
                    closure.chapter.foundation.arc_baseline_id
                )
                assert formal_closure.canon_baseline_id == closure.canon_baseline_id
                assert formal_closure.chapter_set_manifest_ref_id
                assert formal_completion.book_baseline_id == (formal_closure.book_baseline_id)
                assert formal_completion.arc_closure_id == closure.arc_closure_id
                assert formal_completion.terminal_arc_id == (closure.chapter.foundation.arc_id)
                assert formal_completion.terminal_arc_baseline_id == (
                    formal_closure.arc_baseline_id
                )
                assert formal_completion.terminal_chapter_id == (formal_closure.terminal_chapter_id)
                assert formal_completion.terminal_chapter_baseline_id == (
                    formal_closure.terminal_chapter_baseline_id
                )
                assert formal_completion.canon_baseline_id == (formal_closure.canon_baseline_id)
                assert formal_completion.gate_manifest_ref_id
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_nonfinal_arc_closure_deterministically_commits_next_topology_handoff(
    tmp_path: Path,
) -> None:
    database = tmp_path / "handoff-next-ordinal.sqlite3"
    command.upgrade(alembic_config(database), "head")

    async def exercise() -> None:
        engine = create_sqlite_async_engine(database)
        try:
            closure = await _prepare_formal_arc_closure(
                engine,
                project_id="handoff-project",
                arc_contract_count=2,
            )
            authority = LoopAuthorityCommandService(CommandBus(engine))
            with pytest.raises(
                CommandPreconditionError,
                match="requires the current Book progress handoff",
            ):
                await ArcCommandService(CommandBus(engine)).create_story_arc(
                    CreateStoryArcRequest(
                        project_id=closure.chapter.foundation.project_id,
                        book_id=closure.chapter.foundation.book_id,
                        expected_book_baseline_id=(closure.chapter.foundation.book_baseline_id),
                        expected_canon_baseline_id=closure.canon_baseline_id,
                        expected_ordinal=2,
                    ),
                    idempotency_key="handoff:missing",
                )
            handoff = await authority.commit_book_progress_handoff(
                CommitBookProgressHandoffRequest(
                    project_id=closure.chapter.foundation.project_id,
                    book_id=closure.chapter.foundation.book_id,
                    source_arc_closure_id=closure.arc_closure_id,
                ),
                idempotency_key="handoff:commit",
            )
            assert handoff.result.next_arc_ordinal == 2
            with pytest.raises(
                CommandPreconditionError,
                match="requires the current Book progress handoff",
            ):
                await ArcCommandService(CommandBus(engine)).create_story_arc(
                    CreateStoryArcRequest(
                        project_id=closure.chapter.foundation.project_id,
                        book_id=closure.chapter.foundation.book_id,
                        expected_book_baseline_id=(closure.chapter.foundation.book_baseline_id),
                        expected_canon_baseline_id=closure.canon_baseline_id,
                        expected_ordinal=2,
                        source_progress_handoff_id="wrong-handoff",
                    ),
                    idempotency_key="handoff:wrong-id",
                )
            await ArcCommandService(CommandBus(engine)).create_story_arc(
                CreateStoryArcRequest(
                    project_id=closure.chapter.foundation.project_id,
                    book_id=closure.chapter.foundation.book_id,
                    expected_book_baseline_id=(closure.chapter.foundation.book_baseline_id),
                    expected_canon_baseline_id=closure.canon_baseline_id,
                    expected_ordinal=handoff.result.next_arc_ordinal,
                    source_progress_handoff_id=handoff.result.handoff_id,
                ),
                idempotency_key="handoff:create-next",
            )
            with pytest.raises(
                CommandPreconditionError,
                match="exceeds the approved Book topology",
            ):
                await ArcCommandService(CommandBus(engine)).create_story_arc(
                    CreateStoryArcRequest(
                        project_id=closure.chapter.foundation.project_id,
                        book_id=closure.chapter.foundation.book_id,
                        expected_book_baseline_id=(closure.chapter.foundation.book_baseline_id),
                        expected_canon_baseline_id=closure.canon_baseline_id,
                        expected_ordinal=3,
                        source_progress_handoff_id=handoff.result.handoff_id,
                    ),
                    idempotency_key="handoff:reject-unplanned-third-arc",
                )
            async with engine.connect() as connection:
                arcs = (
                    await connection.execute(
                        select(
                            story_arcs.c.ordinal,
                            story_arcs.c.lifecycle_status,
                        ).order_by(story_arcs.c.ordinal)
                    )
                ).all()
                assert [tuple(row) for row in arcs] == [
                    (1, "completed"),
                    (2, "planning"),
                ]
                assert (
                    await connection.scalar(
                        select(func.count()).select_from(book_progress_handoffs)
                    )
                    == 1
                )
                assert (
                    await connection.scalar(select(func.count()).select_from(book_completions)) == 0
                )
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_final_second_arc_completion_revision_keeps_the_exact_prior_handoff(
    tmp_path: Path,
) -> None:
    database = tmp_path / "completion-second-arc-revision.sqlite3"
    command.upgrade(alembic_config(database), "head")

    async def exercise() -> None:
        engine = create_sqlite_async_engine(database)
        try:
            first = await _prepare_formal_arc_closure(
                engine,
                project_id="completion-second-arc-project",
                arc_contract_count=2,
            )
            authority = LoopAuthorityCommandService(CommandBus(engine))
            handoff = await authority.commit_book_progress_handoff(
                CommitBookProgressHandoffRequest(
                    project_id=first.chapter.foundation.project_id,
                    book_id=first.chapter.foundation.book_id,
                    source_arc_closure_id=first.arc_closure_id,
                ),
                idempotency_key="completion-second-arc:handoff",
            )
            second = await _prepare_second_formal_arc_closure(
                engine,
                first=first,
                handoff_id=handoff.result.handoff_id,
            )
            authority, review_id, disposition = await _record_book_completion(
                engine,
                closure=second,
                requirement_status="unresolved",
                suffix="completion-second-arc",
            )
            assert disposition == "book_revision_warranted"
            opened = await authority.open_book_completion_revision(
                OpenBookCompletionRevisionRequest(
                    project_id=second.chapter.foundation.project_id,
                    book_id=second.chapter.foundation.book_id,
                    completion_review_id=review_id,
                    expected_workspace_lock_version=(second.book_workspace_lock_version),
                ),
                idempotency_key="completion-second-arc:open-revision",
            )
            assert opened.result.workspace_lock_version == (second.book_workspace_lock_version + 1)
            async with engine.connect() as connection:
                workspace = (
                    await connection.execute(
                        select(
                            book_workspaces.c.state,
                            book_workspaces.c.source_book_completion_review_id,
                            book_workspaces.c.source_book_progress_handoff_id,
                            book_workspaces.c.work_cycle_id,
                        ).where(book_workspaces.c.book_id == second.chapter.foundation.book_id)
                    )
                ).one()
                current_handoff_id = await connection.scalar(
                    select(books.c.current_progress_handoff_id).where(
                        books.c.id == second.chapter.foundation.book_id
                    )
                )
            assert workspace.state == "active"
            assert workspace.source_book_completion_review_id == review_id
            assert workspace.source_book_progress_handoff_id == handoff.result.handoff_id
            assert current_handoff_id == handoff.result.handoff_id
            assert workspace.work_cycle_id
            revision_context = await HarnessContextBuilder(engine).build(
                task_kind="book.revise",
                project_id=second.chapter.foundation.project_id,
                book_id=second.chapter.foundation.book_id,
                arc_id=None,
                chapter_id=None,
                semantic_goal="Revise the Book from its exact completion review.",
                source_book_completion_review_id=review_id,
                source_book_progress_handoff_id=handoff.result.handoff_id,
            )
            completion_review_items = [
                item
                for item in revision_context.manifest["items"]
                if item["group"] == "book_completion_review"
            ]
            assert len(completion_review_items) == 1
            assert completion_review_items[0]["label"] == "source_book_completion_review"
            assert completion_review_items[0]["use"] == "verification"

            successor = BookSuccessorCandidateProposal(
                direction=(
                    "The verified history remains valid while one future Arc resolves "
                    "the still-open central memory conflict."
                ),
                constraints=BookCreativeConstraints(
                    genre_reader_promise="A fair-play memory mystery.",
                    premise_story_engine=("Physical evidence exposes each remaining memory edit."),
                    stable_world_invariants=["Physical evidence cannot be retroactively edited."],
                    stable_character_invariants=["The investigator follows verifiable evidence."],
                    core_selling_points=["Evidence-bound reversals"],
                    prohibited_outcomes=["Do not erase committed Arc history."],
                ),
                selected_title="Echo Testimony",
                rolling_plan=BookRollingPlan(
                    long_term_character_directions=[
                        "The investigator accepts the final cost of verified truth."
                    ],
                    whole_book_pacing_strategy=(
                        "Add only the future Arc needed by the unresolved requirement."
                    ),
                    ending_tendency="Resolve the central conflict without rewriting history.",
                    arc_planning_guidelines=[
                        "The successor Arc must establish the unresolved completion evidence."
                    ],
                    whole_book_scale_guidance="The prior Chapter estimate remains advisory.",
                ),
                completion_contract=CompletionContract(
                    completion_requirements=[
                        BookCompletionRequirement(
                            requirement_key="memory_conflict_resolved",
                            description="Resolve the central memory conflict.",
                            evidence_expectation=(
                                "A future committed Chapter proves the final resolution."
                            ),
                        )
                    ]
                ),
                arc_topology_suffix=BookArcTopologySuffix(
                    arcs=[
                        BookArcContract(
                            whole_book_role="Resolve the still-open completion requirement.",
                            core_goal=(
                                "Establish the final physical proof of the memory conflict."
                            ),
                            handoff_from_previous=(
                                "Continue from both immutable formal Arc closures."
                            ),
                            exit_conditions=[
                                "Committed evidence resolves the central memory conflict."
                            ],
                            completion_requirement_keys=["memory_conflict_resolved"],
                            is_final=True,
                        )
                    ]
                ),
            )
            invalid_successor = successor.model_copy(
                update={
                    "arc_topology_suffix": BookArcTopologySuffix(
                        arcs=[
                            successor.arc_topology_suffix.arcs[0].model_copy(
                                update={"completion_requirement_keys": []}
                            )
                        ]
                    )
                }
            )
            invalid_task, invalid_attempt = await insert_successful_task(
                engine,
                project_id=second.chapter.foundation.project_id,
                run_id=second.chapter.foundation.run_id,
                task_id="completion-second-arc:invalid-book-revise",
                attempt_id="completion-second-arc:invalid-book-revise:attempt",
                role="book_strategist",
                task_kind="book.revise",
                scope_layer="book",
                book_id=second.chapter.foundation.book_id,
                book_baseline_id=second.chapter.foundation.book_baseline_id,
                canon_baseline_id=second.canon_baseline_id,
                workspace_lock_version=opened.result.workspace_lock_version,
                source_book_completion_review_id=review_id,
                source_book_progress_handoff_id=handoff.result.handoff_id,
                result=invalid_successor,
            )
            book_service = BookCommandService(CommandBus(engine))
            with pytest.raises(
                CommandPreconditionError,
                match="must be owned by a mutable future Arc",
            ):
                await book_service.apply_candidate_result(
                    ApplyBookCandidateTaskRequest(
                        project_id=second.chapter.foundation.project_id,
                        book_id=second.chapter.foundation.book_id,
                        task_id=invalid_task,
                        attempt_id=invalid_attempt,
                        expected_workspace_lock_version=(opened.result.workspace_lock_version),
                    ),
                    idempotency_key="completion-second-arc:reject-unowned-successor",
                )

            revise_task, revise_attempt = await insert_successful_task(
                engine,
                project_id=second.chapter.foundation.project_id,
                run_id=second.chapter.foundation.run_id,
                task_id="completion-second-arc:book-revise",
                attempt_id="completion-second-arc:book-revise:attempt",
                role="book_strategist",
                task_kind="book.revise",
                scope_layer="book",
                book_id=second.chapter.foundation.book_id,
                book_baseline_id=second.chapter.foundation.book_baseline_id,
                canon_baseline_id=second.canon_baseline_id,
                workspace_lock_version=opened.result.workspace_lock_version,
                source_book_completion_review_id=review_id,
                source_book_progress_handoff_id=handoff.result.handoff_id,
                result=successor,
            )
            applied = await book_service.apply_candidate_result(
                ApplyBookCandidateTaskRequest(
                    project_id=second.chapter.foundation.project_id,
                    book_id=second.chapter.foundation.book_id,
                    task_id=revise_task,
                    attempt_id=revise_attempt,
                    expected_workspace_lock_version=opened.result.workspace_lock_version,
                ),
                idempotency_key="completion-second-arc:apply-successor",
            )
            evaluation_context = await HarnessContextBuilder(engine).build(
                task_kind="evaluate.book",
                project_id=second.chapter.foundation.project_id,
                book_id=second.chapter.foundation.book_id,
                arc_id=None,
                chapter_id=None,
                semantic_goal="Evaluate the completion-driven Book successor.",
                source_book_completion_review_id=review_id,
                source_book_progress_handoff_id=handoff.result.handoff_id,
            )
            state_marker = "Model-visible semantic state and counters:\n"
            visible_state = next(
                line
                for line in evaluation_context.prompt.split(state_marker, maxsplit=1)[
                    1
                ].splitlines()
                if line.strip()
            )
            assert '"candidate_kind":"successor"' in visible_state
            assert '"historical_prefix_arc_count":2' in visible_state
            assert '"candidate_arc_contract_count":3' in visible_state
            assert '"candidate_final_arc_ordinal":3' in visible_state
            assert '"book_arc_contract_count"' not in visible_state
            assert applied.result.workspace_lock_version == (
                opened.result.workspace_lock_version + 1
            )
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_completion_gate_rejects_queued_feedback_after_completion_review(
    tmp_path: Path,
) -> None:
    database = tmp_path / "completion-blocked.sqlite3"
    command.upgrade(alembic_config(database), "head")

    async def exercise() -> None:
        engine = create_sqlite_async_engine(database)
        try:
            closure = await _prepare_formal_arc_closure(
                engine,
                project_id="completion-blocked-project",
            )
            authority, review_id, disposition = await _record_book_completion(
                engine,
                closure=closure,
                requirement_status="satisfied",
                suffix="completion-blocked",
            )
            assert disposition == "complete_book"
            await FeedbackCommandService(CommandBus(engine)).submit(
                SubmitFeedbackRequest(
                    project_id=closure.chapter.foundation.project_id,
                    content="Resolve this queued creator input before completion.",
                ),
                idempotency_key="completion-blocked:feedback",
            )
            with pytest.raises(
                CommandPreconditionError,
                match="completion gate facts are stale or incomplete",
            ):
                await authority.commit_book_completion(
                    CommitBookCompletionRequest(
                        project_id=closure.chapter.foundation.project_id,
                        book_id=closure.chapter.foundation.book_id,
                        completion_review_id=review_id,
                    ),
                    idempotency_key="completion-blocked:commit",
                )
            async with engine.connect() as connection:
                assert (
                    await connection.scalar(select(func.count()).select_from(book_completions)) == 0
                )
        finally:
            await engine.dispose()

    asyncio.run(exercise())
