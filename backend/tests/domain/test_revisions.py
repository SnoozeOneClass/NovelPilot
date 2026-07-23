from __future__ import annotations

import asyncio
from pathlib import Path

from alembic import command
from sqlalchemy import func, select

from app.agents.contracts import (
    ChapterDraftResult,
    ChapterObservationResult,
    ChapterPlanProposal,
    LayerEvaluationResult,
)
from app.db.engine import create_sqlite_async_engine
from app.db.maintenance import alembic_config
from app.db.schema import (
    arc_baselines,
    arc_workspaces,
    book_baselines,
    book_workspaces,
    books,
    chapter_baselines,
    chapter_workspaces,
    chapters,
    story_arcs,
)
from app.domain.book.commands import BookCommandService
from app.domain.book.contracts import (
    ApplyBookCandidateTaskRequest,
    ApproveBookRequest,
    BookCandidatePack,
    BookEvaluation,
    CompletionContract,
    RecordBookReviewRequest,
    SubmitBookRequest,
)
from app.domain.chapter.commands import ChapterCommandService
from app.domain.chapter.contracts import (
    ApplyChapterTaskRequest,
    CommitChapterRequest,
    RecordChapterReviewRequest,
    SubmitChapterRequest,
)
from app.domain.feedback import (
    ApplyFeedbackRequest,
    FeedbackCommandService,
    RouteFeedbackRequest,
    SubmitFeedbackRequest,
)
from app.store.command_bus import CommandBus
from tests.domain.test_chapter_lifecycle import _prepare_reviewed_chapter
from tests.helpers.lifecycle_seed import insert_successful_task, seed_approved_book_and_arc


def test_book_revision_requires_review_and_user_approval_then_stales_active_arc_work(
    tmp_path: Path,
) -> None:
    database = tmp_path / "book-revision.sqlite3"
    command.upgrade(alembic_config(database), "head")

    async def exercise() -> None:
        engine = create_sqlite_async_engine(database)
        try:
            foundation = await seed_approved_book_and_arc(
                engine,
                project_id="book-revision-project",
                target_chapter_count=2,
            )
            bus = CommandBus(engine)
            feedback_service = FeedbackCommandService(bus)
            feedback = await feedback_service.submit(
                SubmitFeedbackRequest(
                    project_id=foundation.project_id,
                    content="Clarify the future-only Book constraint before more Chapters.",
                ),
                idempotency_key="revision:feedback",
            )
            await feedback_service.route(
                RouteFeedbackRequest(
                    project_id=foundation.project_id,
                    feedback_id=feedback.result.feedback_id,
                    route_layer="book",
                    book_id=foundation.book_id,
                ),
                idempotency_key="revision:route",
            )
            async with engine.connect() as connection:
                workspace_lock = await connection.scalar(
                    select(book_workspaces.c.lock_version).where(
                        book_workspaces.c.book_id == foundation.book_id
                    )
                )
            assert workspace_lock is not None
            activated = await feedback_service.apply(
                ApplyFeedbackRequest(
                    project_id=foundation.project_id,
                    feedback_id=feedback.result.feedback_id,
                    expected_workspace_lock_version=workspace_lock,
                ),
                idempotency_key="revision:activate-book",
            )
            candidate = BookCandidatePack(
                direction="Conflicting testimony reveals memory editing without rewriting history.",
                constraints={"pov": "limited-third", "history": "preserve-committed"},
                selected_title="Echo Testimony",
                rolling_plan={"strategy": "one-arc-at-a-time", "revision": "future-only"},
                completion_contract=CompletionContract(
                    minimum_chapter_count=1,
                    maximum_chapter_count=12,
                    completion_requirements=["Resolve the central memory conflict"],
                ),
            )
            revise_task, revise_attempt = await insert_successful_task(
                engine,
                project_id=foundation.project_id,
                run_id=foundation.run_id,
                task_id="book-revise-task",
                attempt_id="book-revise-attempt",
                role="book_strategist",
                task_kind="book.revise",
                scope_layer="book",
                book_id=foundation.book_id,
                book_baseline_id=foundation.book_baseline_id,
                canon_baseline_id=foundation.canon_baseline_id,
                workspace_lock_version=activated.result.workspace_lock_version,
                result=candidate,
            )
            book_service = BookCommandService(bus)
            applied = await book_service.apply_candidate_result(
                ApplyBookCandidateTaskRequest(
                    project_id=foundation.project_id,
                    book_id=foundation.book_id,
                    task_id=revise_task,
                    attempt_id=revise_attempt,
                    expected_workspace_lock_version=activated.result.workspace_lock_version,
                ),
                idempotency_key="revision:apply-book",
            )
            submitted = await book_service.submit_for_review(
                SubmitBookRequest(
                    project_id=foundation.project_id,
                    book_id=foundation.book_id,
                    expected_workspace_lock_version=applied.result.workspace_lock_version,
                ),
                idempotency_key="revision:submit-book",
            )
            evaluator_task, evaluator_attempt = await insert_successful_task(
                engine,
                project_id=foundation.project_id,
                run_id=foundation.run_id,
                task_id="evaluate-book-revision",
                attempt_id="evaluate-book-revision-attempt",
                role="evaluator",
                task_kind="evaluate.book",
                scope_layer="book",
                book_id=foundation.book_id,
                book_baseline_id=foundation.book_baseline_id,
                canon_baseline_id=foundation.canon_baseline_id,
                workspace_lock_version=applied.result.workspace_lock_version,
                result=BookEvaluation(
                    decision="pass",
                    summary="The future-only revision preserves committed history.",
                ),
            )
            reviewed = await book_service.record_review(
                RecordBookReviewRequest(
                    project_id=foundation.project_id,
                    book_id=foundation.book_id,
                    submission_id=submitted.result.submission_id,
                    evaluator_task_id=evaluator_task,
                    evaluator_attempt_id=evaluator_attempt,
                    rubric_id="book-rubric",
                    rubric_version=1,
                    deterministic_precheck={"passed": True},
                ),
                idempotency_key="revision:review-book",
            )
            async with engine.connect() as connection:
                assert await connection.scalar(select(func.count()).select_from(book_baselines)) == 1
                assert (
                    await connection.scalar(
                        select(books.c.current_baseline_id).where(
                            books.c.id == foundation.book_id
                        )
                    )
                    == foundation.book_baseline_id
                )
            committed = await book_service.approve_and_commit(
                ApproveBookRequest(
                    project_id=foundation.project_id,
                    book_id=foundation.book_id,
                    submission_id=submitted.result.submission_id,
                    review_id=reviewed.result.review_id,
                    expected_current_baseline_id=foundation.book_baseline_id,
                ),
                idempotency_key="revision:approve-book",
            )
            assert committed.result.baseline_version == 2
            async with engine.connect() as connection:
                arc_workspace = (
                    await connection.execute(
                        select(
                            arc_workspaces.c.state,
                            arc_workspaces.c.stale_reason_code,
                        ).where(arc_workspaces.c.arc_id == foundation.arc_id)
                    )
                ).one()
                arc = (
                    await connection.execute(
                        select(
                            story_arcs.c.current_baseline_id,
                            story_arcs.c.lifecycle_status,
                        ).where(story_arcs.c.id == foundation.arc_id)
                    )
                ).one()
                assert tuple(arc_workspace) == ("stale", "upstream_book_revised")
                assert tuple(arc) == (foundation.arc_baseline_id, "active")
                assert await connection.scalar(select(func.count()).select_from(book_baselines)) == 2
                assert await connection.scalar(select(func.count()).select_from(arc_baselines)) == 1
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_chapter_revision_creates_v2_without_increasing_committed_chapter_count(
    tmp_path: Path,
) -> None:
    database = tmp_path / "chapter-revision.sqlite3"
    command.upgrade(alembic_config(database), "head")

    async def exercise() -> None:
        engine = create_sqlite_async_engine(database)
        try:
            ready = await _prepare_reviewed_chapter(
                engine,
                project_id="chapter-revision-project",
                target_chapter_count=2,
                canon_change=False,
            )
            bus = CommandBus(engine)
            chapter_service = ChapterCommandService(bus)
            first = await chapter_service.commit_chapter_and_canon(
                CommitChapterRequest(
                    project_id=ready.foundation.project_id,
                    chapter_id=ready.chapter_id,
                    submission_id=ready.submission_id,
                    review_id=ready.review_id,
                    expected_canon_baseline_id=ready.foundation.canon_baseline_id,
                ),
                idempotency_key="chapter-revision:commit-v1",
            )
            feedback_service = FeedbackCommandService(bus)
            feedback = await feedback_service.submit(
                SubmitFeedbackRequest(
                    project_id=ready.foundation.project_id,
                    content="Tighten this Chapter's reveal while preserving its Canon outcome.",
                ),
                idempotency_key="chapter-revision:feedback",
            )
            await feedback_service.route(
                RouteFeedbackRequest(
                    project_id=ready.foundation.project_id,
                    feedback_id=feedback.result.feedback_id,
                    route_layer="chapter",
                    book_id=ready.foundation.book_id,
                    arc_id=ready.foundation.arc_id,
                    chapter_id=ready.chapter_id,
                ),
                idempotency_key="chapter-revision:route",
            )
            async with engine.connect() as connection:
                workspace_lock = await connection.scalar(
                    select(chapter_workspaces.c.lock_version).where(
                        chapter_workspaces.c.chapter_id == ready.chapter_id
                    )
                )
            assert workspace_lock is not None
            activated = await feedback_service.apply(
                ApplyFeedbackRequest(
                    project_id=ready.foundation.project_id,
                    feedback_id=feedback.result.feedback_id,
                    expected_workspace_lock_version=workspace_lock,
                ),
                idempotency_key="chapter-revision:activate",
            )
            plan_task, plan_attempt = await insert_successful_task(
                engine,
                project_id=ready.foundation.project_id,
                run_id=ready.foundation.run_id,
                task_id="chapter-revise-plan",
                attempt_id="chapter-revise-plan-attempt",
                role="chapter_writer",
                task_kind="chapter.revise.plan",
                scope_layer="chapter",
                book_id=ready.foundation.book_id,
                book_baseline_id=ready.foundation.book_baseline_id,
                arc_id=ready.foundation.arc_id,
                arc_baseline_id=ready.foundation.arc_baseline_id,
                chapter_id=ready.chapter_id,
                chapter_baseline_id=first.result.chapter_baseline_id,
                canon_baseline_id=first.result.canon_after_id,
                workspace_lock_version=activated.result.workspace_lock_version,
                result=ChapterPlanProposal(
                    title="The Witness Who Remembered Twice",
                    purpose="Tighten the reveal of the first physical trace.",
                    scene_beats=["Mara compares statements", "The altered ink exposes itself"],
                    required_continuity=["Mara distrusts her notes"],
                ),
            )
            plan_applied = await chapter_service.apply_revision_plan_result(
                ApplyChapterTaskRequest(
                    project_id=ready.foundation.project_id,
                    chapter_id=ready.chapter_id,
                    task_id=plan_task,
                    attempt_id=plan_attempt,
                    expected_workspace_lock_version=activated.result.workspace_lock_version,
                ),
                idempotency_key="chapter-revision:apply-plan",
            )
            draft_task, draft_attempt = await insert_successful_task(
                engine,
                project_id=ready.foundation.project_id,
                run_id=ready.foundation.run_id,
                task_id="chapter-revise-draft",
                attempt_id="chapter-revise-draft-attempt",
                role="chapter_writer",
                task_kind="chapter.revise.draft",
                scope_layer="chapter",
                book_id=ready.foundation.book_id,
                book_baseline_id=ready.foundation.book_baseline_id,
                arc_id=ready.foundation.arc_id,
                arc_baseline_id=ready.foundation.arc_baseline_id,
                chapter_id=ready.chapter_id,
                chapter_baseline_id=first.result.chapter_baseline_id,
                canon_baseline_id=first.result.canon_after_id,
                workspace_lock_version=plan_applied.result.workspace_lock_version,
                output_mode="text_streaming",
                result=ChapterDraftResult(
                    prose=(
                        "Mara aligned the statements beneath the lamp. "
                        "The blue ink shifted, exposing a confession no witness had spoken."
                    )
                ),
            )
            draft_applied = await chapter_service.apply_revision_draft_result(
                ApplyChapterTaskRequest(
                    project_id=ready.foundation.project_id,
                    chapter_id=ready.chapter_id,
                    task_id=draft_task,
                    attempt_id=draft_attempt,
                    expected_workspace_lock_version=plan_applied.result.workspace_lock_version,
                ),
                idempotency_key="chapter-revision:apply-draft",
            )
            observe_task, observe_attempt = await insert_successful_task(
                engine,
                project_id=ready.foundation.project_id,
                run_id=ready.foundation.run_id,
                task_id="chapter-revise-observe",
                attempt_id="chapter-revise-observe-attempt",
                role="chapter_writer",
                task_kind="chapter.revise.observe",
                scope_layer="chapter",
                book_id=ready.foundation.book_id,
                book_baseline_id=ready.foundation.book_baseline_id,
                arc_id=ready.foundation.arc_id,
                arc_baseline_id=ready.foundation.arc_baseline_id,
                chapter_id=ready.chapter_id,
                chapter_baseline_id=first.result.chapter_baseline_id,
                canon_baseline_id=first.result.canon_after_id,
                workspace_lock_version=draft_applied.result.workspace_lock_version,
                result=ChapterObservationResult(
                    summary="The tighter reveal preserves the established outcome.",
                    continuity_observations=["Mara still distrusts her written notes."],
                    canon_proposals=[],
                ),
            )
            observed = await chapter_service.apply_revision_observation_result(
                ApplyChapterTaskRequest(
                    project_id=ready.foundation.project_id,
                    chapter_id=ready.chapter_id,
                    task_id=observe_task,
                    attempt_id=observe_attempt,
                    expected_workspace_lock_version=draft_applied.result.workspace_lock_version,
                ),
                idempotency_key="chapter-revision:apply-observation",
            )
            submitted = await chapter_service.submit_for_review(
                SubmitChapterRequest(
                    project_id=ready.foundation.project_id,
                    chapter_id=ready.chapter_id,
                    expected_workspace_lock_version=observed.result.workspace_lock_version,
                ),
                idempotency_key="chapter-revision:submit",
            )
            evaluator_task, evaluator_attempt = await insert_successful_task(
                engine,
                project_id=ready.foundation.project_id,
                run_id=ready.foundation.run_id,
                task_id="evaluate-chapter-revision",
                attempt_id="evaluate-chapter-revision-attempt",
                role="evaluator",
                task_kind="evaluate.chapter",
                scope_layer="chapter",
                book_id=ready.foundation.book_id,
                book_baseline_id=ready.foundation.book_baseline_id,
                arc_id=ready.foundation.arc_id,
                arc_baseline_id=ready.foundation.arc_baseline_id,
                chapter_id=ready.chapter_id,
                chapter_baseline_id=first.result.chapter_baseline_id,
                canon_baseline_id=first.result.canon_after_id,
                workspace_lock_version=observed.result.workspace_lock_version,
                result=LayerEvaluationResult(
                    decision="pass",
                    summary="The Chapter-only revision is coherent and Canon-neutral.",
                ),
            )
            reviewed = await chapter_service.record_review(
                RecordChapterReviewRequest(
                    project_id=ready.foundation.project_id,
                    chapter_id=ready.chapter_id,
                    submission_id=submitted.result.submission_id,
                    evaluator_task_id=evaluator_task,
                    evaluator_attempt_id=evaluator_attempt,
                    rubric_id="chapter-rubric",
                    rubric_version=1,
                    deterministic_precheck={"passed": True},
                ),
                idempotency_key="chapter-revision:review",
            )
            second = await chapter_service.commit_chapter_and_canon(
                CommitChapterRequest(
                    project_id=ready.foundation.project_id,
                    chapter_id=ready.chapter_id,
                    submission_id=submitted.result.submission_id,
                    review_id=reviewed.result.review_id,
                    expected_current_chapter_baseline_id=first.result.chapter_baseline_id,
                    expected_canon_baseline_id=first.result.canon_after_id,
                ),
                idempotency_key="chapter-revision:commit-v2",
            )
            assert second.result.chapter_baseline_version == 2
            assert second.result.canon_after_id == first.result.canon_after_id
            async with engine.connect() as connection:
                baseline = (
                    await connection.execute(
                        select(
                            chapter_baselines.c.parent_baseline_id,
                            chapter_baselines.c.baseline_version,
                        ).where(
                            chapter_baselines.c.id == second.result.chapter_baseline_id
                        )
                    )
                ).one()
                assert tuple(baseline) == (first.result.chapter_baseline_id, 2)
                assert (
                    await connection.scalar(
                        select(func.count())
                        .select_from(chapters)
                        .where(chapters.c.lifecycle_status == "committed")
                    )
                    == 1
                )
                assert (
                    await connection.scalar(select(func.count()).select_from(chapter_baselines))
                    == 2
                )
        finally:
            await engine.dispose()

    asyncio.run(exercise())
