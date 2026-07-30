from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import func, select

from app.db.engine import create_sqlite_async_engine
from app.db.maintenance import alembic_config
from app.db.schema import (
    arc_review_submissions,
    arc_workspaces,
    book_workspaces,
    books,
    chapter_baselines,
    chapter_workspaces,
    chapters,
    generation_runs,
    story_arcs,
    user_feedback,
)
from app.domain.arc.commands import ArcCommandService
from app.domain.arc.contracts import SubmitArcRequest
from app.domain.book.contracts import BookEvaluation
from app.domain.chapter.commands import ChapterCommandService
from app.domain.chapter.contracts import CommitChapterRequest
from app.domain.commands import CommandPreconditionError
from app.domain.feedback import (
    ApplyFeedbackRequest,
    DismissFeedbackRequest,
    FeedbackCommandService,
    QueueFeedbackRequest,
    RouteFeedbackRequest,
    SubmitFeedbackRequest,
)
from app.runtime.context import HarnessContextBuilder
from app.store.command_bus import CommandBus
from app.store.content import ContentRepository
from app.db.uow import UnitOfWork
from tests.domain.test_chapter_lifecycle import _prepare_reviewed_chapter
from tests.helpers.lifecycle_seed import (
    insert_successful_task,
    seed_approved_book_and_arc,
)


def test_feedback_is_routed_then_activates_workspace_without_changing_baseline(
    tmp_path: Path,
) -> None:
    database = tmp_path / "feedback.sqlite3"
    command.upgrade(alembic_config(database), "head")

    async def exercise() -> None:
        engine = create_sqlite_async_engine(database)
        try:
            foundation = await seed_approved_book_and_arc(
                engine,
                project_id="feedback-project",
            )
            service = FeedbackCommandService(CommandBus(engine))
            queued = await service.queue(
                QueueFeedbackRequest(
                    project_id=foundation.project_id,
                    content="Strengthen the conflict without rewriting committed facts.",
                    route_layer="arc",
                    book_id=foundation.book_id,
                    arc_id=foundation.arc_id,
                ),
                idempotency_key="feedback:queue",
            )
            assert queued.result.status == "routed"
            async with engine.connect() as connection:
                lock_version = await connection.scalar(
                    select(arc_workspaces.c.lock_version).where(
                        arc_workspaces.c.arc_id == foundation.arc_id
                    )
                )
            assert lock_version is not None
            applied = await service.apply(
                ApplyFeedbackRequest(
                    project_id=foundation.project_id,
                    feedback_id=queued.result.feedback_id,
                    expected_workspace_lock_version=lock_version,
                ),
                idempotency_key="feedback:apply",
            )
            assert applied.result.route_layer == "arc"
            assert applied.result.workspace_lock_version == lock_version + 1

            async with engine.connect() as connection:
                feedback = (
                    await connection.execute(
                        select(
                            user_feedback.c.status,
                            user_feedback.c.applied_command_id,
                            user_feedback.c.content_ref_id,
                        ).where(user_feedback.c.id == queued.result.feedback_id)
                    )
                ).one()
                workspace = (
                    await connection.execute(
                        select(
                            arc_workspaces.c.state,
                            arc_workspaces.c.base_arc_baseline_id,
                            arc_workspaces.c.lock_version,
                            arc_workspaces.c.guidance_ref_id,
                        ).where(arc_workspaces.c.arc_id == foundation.arc_id)
                    )
                ).one()
                current_arc = await connection.scalar(
                    select(story_arcs.c.current_baseline_id).where(
                        story_arcs.c.id == foundation.arc_id
                    )
                )
                assert tuple(feedback[:2]) == ("applied", applied.receipt_id)
                assert tuple(workspace) == (
                    "active",
                    foundation.arc_baseline_id,
                    lock_version + 1,
                    feedback.content_ref_id,
                )
                assert current_arc == foundation.arc_baseline_id
            context = await HarnessContextBuilder(engine).build(
                task_kind="arc.revise",
                project_id=foundation.project_id,
                book_id=foundation.book_id,
                arc_id=foundation.arc_id,
                chapter_id=None,
                semantic_goal="Revise only from explicit user guidance.",
            )
            assert "Strengthen the conflict" in context.prompt
            guidance_items = [
                item
                for item in context.manifest["items"]
                if item["group"] == "arc_guidance"
            ]
            assert [item["label"] for item in guidance_items] == [
                "story_arc_user_guidance"
            ]
            assert guidance_items[0]["role"] == "creator_guidance"
            assert guidance_items[0]["use"] == "advisory"

            second = await service.queue(
                QueueFeedbackRequest(
                    project_id=foundation.project_id,
                    content="Keep the current conflict restrained and evidence-led.",
                    route_layer="arc",
                    book_id=foundation.book_id,
                    arc_id=foundation.arc_id,
                ),
                idempotency_key="feedback:queue-second",
            )
            second_applied = await service.apply(
                ApplyFeedbackRequest(
                    project_id=foundation.project_id,
                    feedback_id=second.result.feedback_id,
                    expected_workspace_lock_version=(
                        applied.result.workspace_lock_version
                    ),
                ),
                idempotency_key="feedback:apply-second",
            )
            current_context = await HarnessContextBuilder(engine).build(
                task_kind="arc.revise",
                project_id=foundation.project_id,
                book_id=foundation.book_id,
                arc_id=foundation.arc_id,
                chapter_id=None,
                semantic_goal="Consume only current Arc guidance.",
            )
            assert "Keep the current conflict restrained" in current_context.prompt
            assert "Strengthen the conflict" not in current_context.prompt
            assert len(
                [
                    item
                    for item in current_context.manifest["items"]
                    if item["group"] == "arc_guidance"
                ]
            ) == 1

            submitted = await ArcCommandService(CommandBus(engine)).submit_for_review(
                SubmitArcRequest(
                    project_id=foundation.project_id,
                    book_id=foundation.book_id,
                    arc_id=foundation.arc_id,
                    expected_workspace_lock_version=(
                        second_applied.result.workspace_lock_version
                    ),
                ),
                idempotency_key="feedback:submit-current-guidance",
            )
            async with engine.connect() as connection:
                manifest_ref_id = await connection.scalar(
                    select(
                        arc_review_submissions.c.content_manifest_ref_id
                    ).where(
                        arc_review_submissions.c.id
                        == submitted.result.submission_id
                    )
                )
                assert manifest_ref_id is not None
                manifest = json.loads(
                    (
                        await ContentRepository(connection).get_packed(
                            project_id=foundation.project_id,
                            ref_id=manifest_ref_id,
                        )
                    ).unpack_and_verify()
                )
                second_feedback = (
                    await connection.execute(
                        select(
                            user_feedback.c.id,
                            user_feedback.c.content_ref_id,
                        ).where(
                            user_feedback.c.id == second.result.feedback_id
                        )
                    )
                ).one()
            assert manifest["schema"] == "arc-review-manifest-v6"
            assert manifest["source_feedback_id"] == second_feedback.id
            assert manifest["guidance_ref_id"] == second_feedback.content_ref_id
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_identical_feedback_bytes_bind_the_exact_new_work_cycle(
    tmp_path: Path,
) -> None:
    database = tmp_path / "feedback-identical-bytes.sqlite3"
    command.upgrade(alembic_config(database), "head")

    async def exercise() -> None:
        engine = create_sqlite_async_engine(database)
        try:
            foundation = await seed_approved_book_and_arc(
                engine,
                project_id="feedback-identical-project",
            )
            service = FeedbackCommandService(CommandBus(engine))
            content = "Keep the investigation restrained and evidence-led."

            async with engine.connect() as connection:
                initial = (
                    await connection.execute(
                        select(
                            arc_workspaces.c.lock_version,
                            arc_workspaces.c.work_cycle_id,
                        ).where(arc_workspaces.c.arc_id == foundation.arc_id)
                    )
                ).one()

            first = await service.queue(
                QueueFeedbackRequest(
                    project_id=foundation.project_id,
                    content=content,
                    route_layer="arc",
                    book_id=foundation.book_id,
                    arc_id=foundation.arc_id,
                ),
                idempotency_key="identical-feedback:first:queue",
            )
            first_applied = await service.apply(
                ApplyFeedbackRequest(
                    project_id=foundation.project_id,
                    feedback_id=first.result.feedback_id,
                    expected_workspace_lock_version=initial.lock_version,
                ),
                idempotency_key="identical-feedback:first:apply",
            )
            async with engine.connect() as connection:
                after_first = (
                    await connection.execute(
                        select(
                            arc_workspaces.c.lock_version,
                            arc_workspaces.c.work_cycle_id,
                        ).where(arc_workspaces.c.arc_id == foundation.arc_id)
                    )
                ).one()
            task_id, attempt_id = await insert_successful_task(
                engine,
                project_id=foundation.project_id,
                run_id=foundation.run_id,
                task_id="identical-feedback:first-revision-task",
                attempt_id="identical-feedback:first-revision-attempt",
                role="arc_planner",
                task_kind="arc.revise",
                scope_layer="arc",
                book_id=foundation.book_id,
                book_baseline_id=foundation.book_baseline_id,
                arc_id=foundation.arc_id,
                arc_baseline_id=foundation.arc_baseline_id,
                canon_baseline_id=foundation.canon_baseline_id,
                workspace_lock_version=after_first.lock_version,
                workspace_work_cycle_id=after_first.work_cycle_id,
                source_feedback_id=first.result.feedback_id,
                result=BookEvaluation(
                    decision="pass",
                    summary="Fixture payload for exact applied-task identity.",
                ),
            )
            async with UnitOfWork(engine, begin_mode="IMMEDIATE") as store:
                assert await store.execution.mark_delivery_applied(
                    project_id=foundation.project_id,
                    task_id=task_id,
                    attempt_id=attempt_id,
                    command_id=first_applied.receipt_id,
                    updated_at_ms=100,
                )

            second = await service.queue(
                QueueFeedbackRequest(
                    project_id=foundation.project_id,
                    content=content,
                    route_layer="arc",
                    book_id=foundation.book_id,
                    arc_id=foundation.arc_id,
                ),
                idempotency_key="identical-feedback:second:queue",
            )
            second_applied = await service.apply(
                ApplyFeedbackRequest(
                    project_id=foundation.project_id,
                    feedback_id=second.result.feedback_id,
                    expected_workspace_lock_version=after_first.lock_version,
                ),
                idempotency_key="identical-feedback:second:apply",
            )
            assert first_applied.result.workspace_lock_version < (
                second_applied.result.workspace_lock_version
            )

            async with engine.connect() as connection:
                current = (
                    await connection.execute(
                        select(
                            arc_workspaces.c.work_cycle_id,
                            arc_workspaces.c.source_feedback_id,
                            arc_workspaces.c.guidance_ref_id,
                        ).where(arc_workspaces.c.arc_id == foundation.arc_id)
                    )
                ).one()
                first_feedback = (
                    await connection.execute(
                        select(user_feedback.c.content_ref_id).where(
                            user_feedback.c.id == first.result.feedback_id
                        )
                    )
                ).scalar_one()
                second_feedback = (
                    await connection.execute(
                        select(user_feedback.c.content_ref_id).where(
                            user_feedback.c.id == second.result.feedback_id
                        )
                    )
                ).scalar_one()
                first_packed = await ContentRepository(connection).get_packed(
                    project_id=foundation.project_id,
                    ref_id=first_feedback,
                )
                second_packed = await ContentRepository(connection).get_packed(
                    project_id=foundation.project_id,
                    ref_id=second_feedback,
                )

            assert initial.work_cycle_id != after_first.work_cycle_id
            assert after_first.work_cycle_id != current.work_cycle_id
            assert current.source_feedback_id == second.result.feedback_id
            assert current.guidance_ref_id == second_feedback
            assert first_feedback != second_feedback
            assert (
                first_packed.reference.blob_sha256
                == second_packed.reference.blob_sha256
            )
            async with UnitOfWork(engine) as store:
                assert await store.execution.has_applied_task(
                    project_id=foundation.project_id,
                    run_id=foundation.run_id,
                    task_kind="arc.revise",
                    book_id=foundation.book_id,
                    arc_id=foundation.arc_id,
                    book_baseline_id=foundation.book_baseline_id,
                    arc_baseline_id=foundation.arc_baseline_id,
                    workspace_work_cycle_id=after_first.work_cycle_id,
                    source_feedback_id=first.result.feedback_id,
                )
                assert not await store.execution.has_applied_task(
                    project_id=foundation.project_id,
                    run_id=foundation.run_id,
                    task_kind="arc.revise",
                    book_id=foundation.book_id,
                    arc_id=foundation.arc_id,
                    book_baseline_id=foundation.book_baseline_id,
                    arc_baseline_id=foundation.arc_baseline_id,
                    workspace_work_cycle_id=current.work_cycle_id,
                    source_feedback_id=second.result.feedback_id,
                )

            context = await HarnessContextBuilder(engine).build(
                task_kind="arc.revise",
                project_id=foundation.project_id,
                book_id=foundation.book_id,
                arc_id=foundation.arc_id,
                chapter_id=None,
                semantic_goal="Consume the exact current feedback record.",
            )
            guidance = [
                item
                for item in context.manifest["items"]
                if item["group"] == "arc_guidance"
            ]
            assert len(guidance) == 1
            assert content in context.prompt
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_feedback_route_validates_hierarchy_and_dismiss_is_terminal(tmp_path: Path) -> None:
    database = tmp_path / "feedback-dismiss.sqlite3"
    command.upgrade(alembic_config(database), "head")

    async def exercise() -> None:
        engine = create_sqlite_async_engine(database)
        try:
            foundation = await seed_approved_book_and_arc(
                engine,
                project_id="feedback-dismiss-project",
            )
            service = FeedbackCommandService(CommandBus(engine))
            submitted = await service.submit(
                SubmitFeedbackRequest(
                    project_id=foundation.project_id,
                    content="A note that may be dismissed.",
                ),
                idempotency_key="dismiss:submit",
            )
            with pytest.raises(CommandPreconditionError, match="Arc target"):
                await service.route(
                    RouteFeedbackRequest(
                        project_id=foundation.project_id,
                        feedback_id=submitted.result.feedback_id,
                        route_layer="arc",
                        book_id=foundation.book_id,
                        arc_id="not-this-project-arc",
                    ),
                    idempotency_key="dismiss:invalid-route",
                )
            dismissed = await service.dismiss(
                DismissFeedbackRequest(
                    project_id=foundation.project_id,
                    feedback_id=submitted.result.feedback_id,
                    reason="The user withdrew it.",
                ),
                idempotency_key="dismiss:terminal",
            )
            assert dismissed.result.status == "dismissed"
            with pytest.raises(CommandPreconditionError, match="not pending"):
                await service.route(
                    RouteFeedbackRequest(
                        project_id=foundation.project_id,
                        feedback_id=submitted.result.feedback_id,
                        route_layer="book",
                        book_id=foundation.book_id,
                    ),
                    idempotency_key="dismiss:route-after-terminal",
                )
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_book_feedback_activation_keeps_approved_book_pointer(tmp_path: Path) -> None:
    database = tmp_path / "feedback-book.sqlite3"
    command.upgrade(alembic_config(database), "head")

    async def exercise() -> None:
        engine = create_sqlite_async_engine(database)
        try:
            foundation = await seed_approved_book_and_arc(
                engine,
                project_id="feedback-book-project",
            )
            service = FeedbackCommandService(CommandBus(engine))
            queued = await service.queue(
                QueueFeedbackRequest(
                    project_id=foundation.project_id,
                    content="Refine the future-only Book direction.",
                    route_layer="book",
                    book_id=foundation.book_id,
                ),
                idempotency_key="book-feedback:queue",
            )
            async with engine.connect() as connection:
                lock_version = await connection.scalar(
                    select(book_workspaces.c.lock_version).where(
                        book_workspaces.c.book_id == foundation.book_id
                    )
                )
            assert lock_version is not None
            await service.apply(
                ApplyFeedbackRequest(
                    project_id=foundation.project_id,
                    feedback_id=queued.result.feedback_id,
                    expected_workspace_lock_version=lock_version,
                ),
                idempotency_key="book-feedback:apply",
            )
            async with engine.connect() as connection:
                row = (
                    await connection.execute(
                        select(
                            books.c.current_baseline_id,
                            book_workspaces.c.base_book_baseline_id,
                            book_workspaces.c.state,
                        )
                        .join(book_workspaces, book_workspaces.c.book_id == books.c.id)
                        .where(books.c.id == foundation.book_id)
                    )
                ).one()
                assert tuple(row) == (
                    foundation.book_baseline_id,
                    foundation.book_baseline_id,
                    "active",
                )
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_feedback_cannot_rewrite_a_chapter_with_committed_descendants(
    tmp_path: Path,
) -> None:
    database = tmp_path / "feedback-historical-chapter.sqlite3"
    command.upgrade(alembic_config(database), "head")

    async def exercise() -> None:
        engine = create_sqlite_async_engine(database)
        try:
            first = await _prepare_reviewed_chapter(
                engine,
                project_id="feedback-historical-project",
                target_chapter_count=3,
                canon_change=False,
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
                idempotency_key="historical:first-commit",
            )
            second = await _prepare_reviewed_chapter(
                engine,
                project_id=first.foundation.project_id,
                target_chapter_count=3,
                canon_change=False,
                foundation=first.foundation,
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
                idempotency_key="historical:second-commit",
            )
            async with engine.connect() as connection:
                before_workspace = (
                    await connection.execute(
                        select(
                            chapter_workspaces.c.state,
                            chapter_workspaces.c.lock_version,
                            chapter_workspaces.c.base_chapter_baseline_id,
                            chapter_workspaces.c.guidance_ref_id,
                        ).where(chapter_workspaces.c.chapter_id == first.chapter_id)
                    )
                ).one()
                before_pointers = (
                    await connection.execute(
                        select(
                            books.c.current_baseline_id.label("book_baseline_id"),
                            story_arcs.c.current_baseline_id.label("arc_baseline_id"),
                            chapters.c.current_baseline_id.label(
                                "chapter_baseline_id"
                            ),
                        )
                        .join(story_arcs, story_arcs.c.book_id == books.c.id)
                        .join(chapters, chapters.c.arc_id == story_arcs.c.id)
                        .where(chapters.c.id == first.chapter_id)
                    )
                ).one()

            feedback_service = FeedbackCommandService(CommandBus(engine))
            queued = await feedback_service.queue(
                QueueFeedbackRequest(
                    project_id=first.foundation.project_id,
                    content="Rewrite the first Chapter so the witness confesses immediately.",
                    route_layer="chapter",
                    book_id=first.foundation.book_id,
                    arc_id=first.foundation.arc_id,
                    chapter_id=first.chapter_id,
                ),
                idempotency_key="historical:queue",
            )
            applied = await feedback_service.apply(
                ApplyFeedbackRequest(
                    project_id=first.foundation.project_id,
                    feedback_id=queued.result.feedback_id,
                ),
                idempotency_key="historical:apply",
            )
            assert applied.result.workspace_lock_version == before_workspace.lock_version

            async with engine.connect() as connection:
                after_workspace = (
                    await connection.execute(
                        select(
                            chapter_workspaces.c.state,
                            chapter_workspaces.c.lock_version,
                            chapter_workspaces.c.base_chapter_baseline_id,
                            chapter_workspaces.c.guidance_ref_id,
                        ).where(chapter_workspaces.c.chapter_id == first.chapter_id)
                    )
                ).one()
                after_pointers = (
                    await connection.execute(
                        select(
                            books.c.current_baseline_id.label("book_baseline_id"),
                            story_arcs.c.current_baseline_id.label("arc_baseline_id"),
                            chapters.c.current_baseline_id.label(
                                "chapter_baseline_id"
                            ),
                        )
                        .join(story_arcs, story_arcs.c.book_id == books.c.id)
                        .join(chapters, chapters.c.arc_id == story_arcs.c.id)
                        .where(chapters.c.id == first.chapter_id)
                    )
                ).one()
                run = (
                    await connection.execute(
                        select(
                            generation_runs.c.status,
                            generation_runs.c.wait_reason_code,
                            generation_runs.c.failure_source_kind,
                        ).where(generation_runs.c.id == first.foundation.run_id)
                    )
                ).one()
                baselines = await connection.scalar(
                    select(func.count()).select_from(chapter_baselines)
                )
            assert tuple(after_workspace) == tuple(before_workspace)
            assert tuple(after_pointers) == tuple(before_pointers)
            assert before_pointers.chapter_baseline_id == (
                first_commit.result.chapter_baseline_id
            )
            assert second_commit.result.chapter_baseline_id != (
                first_commit.result.chapter_baseline_id
            )
            assert tuple(run) == (
                "waiting_for_user",
                "historical_rewrite_unsupported",
                None,
            )
            assert baselines == 2
        finally:
            await engine.dispose()

    asyncio.run(exercise())
