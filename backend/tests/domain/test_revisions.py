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
from app.agents.registry import DEFAULT_EVALUATION_STRATEGY_REGISTRY
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
    BookArcContract,
    BookArcTopologySuffix,
    BookCompletionRequirement,
    BookCreativeConstraints,
    BookEvaluation,
    BookRollingPlan,
    CompletionContract,
    BookSuccessorCandidateProposal,
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
    QueueFeedbackRequest,
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
            feedback = await feedback_service.queue(
                QueueFeedbackRequest(
                    project_id=foundation.project_id,
                    content="Clarify the future-only Book constraint before more Chapters.",
                    route_layer="book",
                    book_id=foundation.book_id,
                ),
                idempotency_key="revision:queue-book-feedback",
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
            candidate = BookSuccessorCandidateProposal(
                direction="Conflicting testimony reveals memory editing without rewriting history.",
                constraints=BookCreativeConstraints(
                    genre_reader_promise="A fair-play memory mystery.",
                    premise_story_engine="Physical evidence reveals memory editing.",
                    stable_world_invariants=[
                        "Physical evidence cannot be retroactively edited."
                    ],
                    stable_character_invariants=["Mara requires verifiable evidence."],
                    core_selling_points=["Evidence-bound reversals"],
                    prohibited_outcomes=["Do not erase committed history."],
                ),
                selected_title="Echo Testimony",
                rolling_plan=BookRollingPlan(
                    long_term_character_directions=[
                        "Mara learns to distinguish trust from certainty."
                    ],
                    whole_book_pacing_strategy="Escalate through bounded Arcs.",
                    ending_tendency="Resolve the central edit at a personal cost.",
                    arc_planning_guidelines=[
                        "Every Arc must close an observable state transition."
                    ],
                    whole_book_scale_guidance="Around twelve Chapters is advisory.",
                ),
                completion_contract=CompletionContract(
                    completion_requirements=[
                        BookCompletionRequirement(
                            requirement_key="memory_conflict_resolved",
                            description="Resolve the central memory conflict.",
                            evidence_expectation="Committed Chapters prove the resolution.",
                        )
                    ],
                ),
                arc_topology_suffix=BookArcTopologySuffix(
                    arcs=[
                        BookArcContract(
                            whole_book_role="Resolve the memory mystery.",
                            core_goal="Confront the operator with verified evidence.",
                            handoff_from_previous="Continue the current active Arc.",
                            exit_conditions=["The central edit is resolved."],
                            completion_requirement_keys=["memory_conflict_resolved"],
                            is_final=True,
                        )
                    ]
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
                    requirement_coverage=[
                        {
                            "requirement_key": "memory_conflict_resolved",
                            "judgment": "aligned",
                            "rationale": "The revised final Arc resolves the requirement.",
                        }
                    ],
                ),
            )
            reviewed = await book_service.record_review(
                RecordBookReviewRequest(
                    project_id=foundation.project_id,
                    book_id=foundation.book_id,
                    submission_id=submitted.result.submission_id,
                    evaluator_task_id=evaluator_task,
                    evaluator_attempt_id=evaluator_attempt,
                        rubric_id=DEFAULT_EVALUATION_STRATEGY_REGISTRY.for_task(
                            "evaluate.book"
                        ).rubric_id,
                        rubric_version=DEFAULT_EVALUATION_STRATEGY_REGISTRY.for_task(
                            "evaluate.book"
                        ).rubric_version,
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
                historical_book_binding = (
                    await connection.execute(
                        select(
                            book_baselines.c.submission_id,
                            book_baselines.c.review_id,
                            book_baselines.c.approval_id,
                            book_baselines.c.direction_ref_id,
                            book_baselines.c.constraints_ref_id,
                            book_baselines.c.rolling_plan_ref_id,
                            book_baselines.c.completion_contract_ref_id,
                            book_baselines.c.arc_topology_ref_id,
                        ).where(
                            book_baselines.c.id == foundation.book_baseline_id
                        )
                    )
                ).one()
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
                assert tuple(arc) == (None, "planning")
                assert await connection.scalar(select(func.count()).select_from(book_baselines)) == 2
                assert await connection.scalar(select(func.count()).select_from(arc_baselines)) == 1
                assert (
                    await connection.scalar(
                        select(books.c.current_baseline_id).where(
                            books.c.id == foundation.book_id
                        )
                    )
                    == committed.result.baseline_id
                )
                assert (
                    await connection.execute(
                        select(
                            book_baselines.c.submission_id,
                            book_baselines.c.review_id,
                            book_baselines.c.approval_id,
                            book_baselines.c.direction_ref_id,
                            book_baselines.c.constraints_ref_id,
                            book_baselines.c.rolling_plan_ref_id,
                            book_baselines.c.completion_contract_ref_id,
                            book_baselines.c.arc_topology_ref_id,
                        ).where(
                            book_baselines.c.id == foundation.book_baseline_id
                        )
                    )
                ).one() == historical_book_binding
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
            feedback = await feedback_service.queue(
                QueueFeedbackRequest(
                    project_id=ready.foundation.project_id,
                    content="Tighten this Chapter's reveal while preserving its Canon outcome.",
                    route_layer="chapter",
                    book_id=ready.foundation.book_id,
                    arc_id=ready.foundation.arc_id,
                    chapter_id=ready.chapter_id,
                ),
                idempotency_key="chapter-revision:queue-feedback",
            )
            async with engine.connect() as connection:
                workspace_lock = await connection.scalar(
                    select(chapter_workspaces.c.lock_version).where(
                        chapter_workspaces.c.chapter_id == ready.chapter_id
                    )
                )
                historical_chapter_binding = (
                    await connection.execute(
                        select(
                            chapter_baselines.c.book_baseline_id,
                            chapter_baselines.c.arc_baseline_id,
                            chapter_baselines.c.canon_before_id,
                            chapter_baselines.c.canon_after_id,
                            chapter_baselines.c.plan_ref_id,
                            chapter_baselines.c.prose_ref_id,
                            chapter_baselines.c.observations_ref_id,
                            chapter_baselines.c.accepted_canon_patch_ref_id,
                        ).where(
                            chapter_baselines.c.id
                            == first.result.chapter_baseline_id
                        )
                    )
                ).one()
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
                source_feedback_id=feedback.result.feedback_id,
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
                source_feedback_id=feedback.result.feedback_id,
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
                source_feedback_id=feedback.result.feedback_id,
                result=ChapterObservationResult(
                    summary="The tighter reveal preserves the established outcome.",
                    established_facts=[
                        {
                            "statement": "Mara still distrusts her written notes.",
                            "evidence_hint": (
                                "The Chapter shows Mara verifying notes before relying on them."
                            ),
                        }
                    ],
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
                source_feedback_id=feedback.result.feedback_id,
                result=LayerEvaluationResult(
                    guidance_authority_judgment=(
                        "compatible_with_current_authority"
                    ),
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
                    rubric_id=DEFAULT_EVALUATION_STRATEGY_REGISTRY.for_task(
                        "evaluate.chapter"
                    ).rubric_id,
                    rubric_version=DEFAULT_EVALUATION_STRATEGY_REGISTRY.for_task(
                        "evaluate.chapter"
                    ).rubric_version,
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
                assert (
                    await connection.scalar(
                        select(chapters.c.current_baseline_id).where(
                            chapters.c.id == ready.chapter_id
                        )
                    )
                    == second.result.chapter_baseline_id
                )
                assert (
                    await connection.execute(
                        select(
                            chapter_baselines.c.book_baseline_id,
                            chapter_baselines.c.arc_baseline_id,
                            chapter_baselines.c.canon_before_id,
                            chapter_baselines.c.canon_after_id,
                            chapter_baselines.c.plan_ref_id,
                            chapter_baselines.c.prose_ref_id,
                            chapter_baselines.c.observations_ref_id,
                            chapter_baselines.c.accepted_canon_patch_ref_id,
                        ).where(
                            chapter_baselines.c.id
                            == first.result.chapter_baseline_id
                        )
                    )
                ).one() == historical_chapter_binding
        finally:
            await engine.dispose()

    asyncio.run(exercise())
