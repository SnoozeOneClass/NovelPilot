from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import select

from app.db.engine import create_sqlite_async_engine
from app.db.maintenance import alembic_config
from app.db.schema import (
    arc_workspaces,
    book_workspaces,
    books,
    story_arcs,
    user_feedback,
)
from app.domain.commands import CommandPreconditionError
from app.domain.feedback import (
    ApplyFeedbackRequest,
    DismissFeedbackRequest,
    FeedbackCommandService,
    RouteFeedbackRequest,
    SubmitFeedbackRequest,
)
from app.runtime.context import HarnessContextBuilder
from app.store.command_bus import CommandBus
from tests.helpers.lifecycle_seed import seed_approved_book_and_arc


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
            submitted = await service.submit(
                SubmitFeedbackRequest(
                    project_id=foundation.project_id,
                    content="Strengthen the conflict without rewriting committed facts.",
                ),
                idempotency_key="feedback:submit",
            )
            routed = await service.route(
                RouteFeedbackRequest(
                    project_id=foundation.project_id,
                    feedback_id=submitted.result.feedback_id,
                    route_layer="arc",
                    book_id=foundation.book_id,
                    arc_id=foundation.arc_id,
                ),
                idempotency_key="feedback:route",
            )
            assert routed.result.status == "routed"
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
                    feedback_id=submitted.result.feedback_id,
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
                        ).where(user_feedback.c.id == submitted.result.feedback_id)
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
            assert "story_arc_user_guidance" in context.prompt
            assert "Strengthen the conflict" in context.prompt
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
            submitted = await service.submit(
                SubmitFeedbackRequest(
                    project_id=foundation.project_id,
                    content="Refine the future-only Book direction.",
                ),
                idempotency_key="book-feedback:submit",
            )
            await service.route(
                RouteFeedbackRequest(
                    project_id=foundation.project_id,
                    feedback_id=submitted.result.feedback_id,
                    route_layer="book",
                    book_id=foundation.book_id,
                ),
                idempotency_key="book-feedback:route",
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
                    feedback_id=submitted.result.feedback_id,
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
