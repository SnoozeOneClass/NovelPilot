from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import func, select

from app.agents.contracts import BookProgressAssessment
from app.db.engine import create_sqlite_async_engine
from app.db.maintenance import alembic_config
from app.db.schema import (
    agent_tasks,
    book_completions,
    book_workspaces,
    books,
    generation_runs,
    projects,
    story_arcs,
)
from app.domain.arc.commands import ArcCommandService
from app.domain.arc.contracts import CreateStoryArcRequest
from app.domain.chapter.commands import ChapterCommandService
from app.domain.chapter.contracts import CommitChapterRequest
from app.domain.commands import CommandPreconditionError
from app.domain.completion import (
    ApplyBookProgressRequest,
    CompletionCommandService,
    ReopenBookRequest,
)
from app.domain.feedback import FeedbackCommandService, SubmitFeedbackRequest
from app.store.command_bus import CommandBus
from tests.domain.test_chapter_lifecycle import _prepare_reviewed_chapter
from tests.helpers.lifecycle_seed import insert_successful_task


async def _prepare_completed_arc(engine, *, project_id: str):
    ready = await _prepare_reviewed_chapter(
        engine,
        project_id=project_id,
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
        idempotency_key=f"{project_id}:commit-terminal-chapter",
    )
    assert committed.result.arc_completed
    async with engine.connect() as connection:
        workspace_lock = await connection.scalar(
            select(book_workspaces.c.lock_version).where(
                book_workspaces.c.book_id == ready.foundation.book_id
            )
        )
    assert workspace_lock is not None
    return ready, committed, workspace_lock


async def _insert_assessment(
    engine,
    *,
    ready,
    committed,
    workspace_lock: int,
    decision: str,
    suffix: str,
) -> tuple[str, str]:
    return await insert_successful_task(
        engine,
        project_id=ready.foundation.project_id,
        run_id=ready.foundation.run_id,
        task_id=f"assessment-{suffix}",
        attempt_id=f"assessment-{suffix}-attempt",
        role="book_strategist",
        task_kind="book.assess_progress_or_completion",
        scope_layer="book",
        book_id=ready.foundation.book_id,
        book_baseline_id=ready.foundation.book_baseline_id,
        canon_baseline_id=committed.result.canon_after_id,
        workspace_lock_version=workspace_lock,
        result=BookProgressAssessment(
            decision=decision,
            rationale=f"Deterministic fixture assessment: {decision}.",
            unresolved_requirements=([] if decision == "complete" else ["Continue the story"]),
        ),
    )


def test_complete_decision_atomically_completes_book_project_and_run(tmp_path: Path) -> None:
    database = tmp_path / "completion.sqlite3"
    command.upgrade(alembic_config(database), "head")

    async def exercise() -> None:
        engine = create_sqlite_async_engine(database)
        try:
            ready, committed, workspace_lock = await _prepare_completed_arc(
                engine,
                project_id="completion-project",
            )
            task_id, attempt_id = await _insert_assessment(
                engine,
                ready=ready,
                committed=committed,
                workspace_lock=workspace_lock,
                decision="complete",
                suffix="complete",
            )
            completed = await CompletionCommandService(CommandBus(engine)).apply_assessment(
                ApplyBookProgressRequest(
                    project_id=ready.foundation.project_id,
                    book_id=ready.foundation.book_id,
                    task_id=task_id,
                    attempt_id=attempt_id,
                    expected_book_baseline_id=ready.foundation.book_baseline_id,
                    expected_canon_baseline_id=committed.result.canon_after_id,
                    expected_book_workspace_lock_version=workspace_lock,
                    terminal_arc_id=ready.foundation.arc_id,
                    terminal_arc_baseline_id=ready.foundation.arc_baseline_id,
                    terminal_chapter_id=ready.chapter_id,
                    terminal_chapter_baseline_id=committed.result.chapter_baseline_id,
                ),
                idempotency_key="completion:apply",
            )
            assert completed.result.action == "completed"
            assert completed.result.completion_id is not None
            with pytest.raises(CommandPreconditionError, match="dependencies are not current"):
                await ArcCommandService(CommandBus(engine)).create_story_arc(
                    CreateStoryArcRequest(
                        project_id=ready.foundation.project_id,
                        book_id=ready.foundation.book_id,
                        expected_book_baseline_id=ready.foundation.book_baseline_id,
                        expected_canon_baseline_id=committed.result.canon_after_id,
                        purpose="regular",
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
                        .join(generation_runs, generation_runs.c.project_id == projects.c.id)
                        .where(projects.c.id == ready.foundation.project_id)
                    )
                ).one()
                assert statuses[0] == "completed"
                assert statuses[1] == "completed"
                assert statuses[2] == completed.result.completion_id
                assert statuses[3] == "completed"
                assert statuses[4] is not None
                assert (
                    await connection.scalar(select(func.count()).select_from(book_completions))
                    == 1
                )
                assert (
                    await connection.scalar(
                        select(agent_tasks.c.delivery_state).where(agent_tasks.c.id == task_id)
                    )
                    == "applied"
                )
            reopened = await CompletionCommandService(CommandBus(engine)).reopen_book(
                ReopenBookRequest(
                    project_id=ready.foundation.project_id,
                    book_id=ready.foundation.book_id,
                    expected_completion_id=completed.result.completion_id,
                ),
                idempotency_key="completion:reopen",
            )
            assert reopened.result.run_number == 2
            async with engine.connect() as connection:
                lifecycle = (
                    await connection.execute(
                        select(
                            projects.c.lifecycle_status,
                            books.c.lifecycle_status,
                            books.c.current_completion_id,
                        )
                        .join(books, books.c.project_id == projects.c.id)
                        .where(projects.c.id == ready.foundation.project_id)
                    )
                ).one()
                runs = (
                    await connection.execute(
                        select(generation_runs.c.run_number, generation_runs.c.status).order_by(
                            generation_runs.c.run_number
                        )
                    )
                ).all()
                assert tuple(lifecycle) == ("active", "active", None)
                assert [tuple(row) for row in runs] == [
                    (1, "completed"),
                    (2, "waiting_for_user"),
                ]
                assert (
                    await connection.scalar(select(func.count()).select_from(book_completions))
                    == 1
                )
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("decision", "expected_action", "expected_purpose"),
    [
        ("continue", "created_regular_arc", "regular"),
        ("plan_final_arc", "created_final_arc", "final"),
    ],
)
def test_nonterminal_assessment_creates_exactly_one_next_arc(
    tmp_path: Path,
    decision: str,
    expected_action: str,
    expected_purpose: str,
) -> None:
    database = tmp_path / f"completion-{decision}.sqlite3"
    command.upgrade(alembic_config(database), "head")

    async def exercise() -> None:
        engine = create_sqlite_async_engine(database)
        try:
            ready, committed, workspace_lock = await _prepare_completed_arc(
                engine,
                project_id=f"progress-{decision}",
            )
            task_id, attempt_id = await _insert_assessment(
                engine,
                ready=ready,
                committed=committed,
                workspace_lock=workspace_lock,
                decision=decision,
                suffix=decision,
            )
            applied = await CompletionCommandService(CommandBus(engine)).apply_assessment(
                ApplyBookProgressRequest(
                    project_id=ready.foundation.project_id,
                    book_id=ready.foundation.book_id,
                    task_id=task_id,
                    attempt_id=attempt_id,
                    expected_book_baseline_id=ready.foundation.book_baseline_id,
                    expected_canon_baseline_id=committed.result.canon_after_id,
                    expected_book_workspace_lock_version=workspace_lock,
                    terminal_arc_id=ready.foundation.arc_id,
                    terminal_arc_baseline_id=ready.foundation.arc_baseline_id,
                    terminal_chapter_id=ready.chapter_id,
                    terminal_chapter_baseline_id=committed.result.chapter_baseline_id,
                ),
                idempotency_key=f"progress:{decision}",
            )
            assert applied.result.action == expected_action
            async with engine.connect() as connection:
                arcs = (
                    await connection.execute(
                        select(story_arcs.c.ordinal, story_arcs.c.purpose).order_by(
                            story_arcs.c.ordinal
                        )
                    )
                ).all()
                assert [tuple(row) for row in arcs] == [
                    (1, "regular"),
                    (2, expected_purpose),
                ]
                assert await connection.scalar(select(func.count()).select_from(book_completions)) == 0
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_completion_gate_rejects_unapplied_feedback_without_consuming_assessment(
    tmp_path: Path,
) -> None:
    database = tmp_path / "completion-blocked.sqlite3"
    command.upgrade(alembic_config(database), "head")

    async def exercise() -> None:
        engine = create_sqlite_async_engine(database)
        try:
            ready, committed, workspace_lock = await _prepare_completed_arc(
                engine,
                project_id="completion-blocked-project",
            )
            await FeedbackCommandService(CommandBus(engine)).submit(
                SubmitFeedbackRequest(
                    project_id=ready.foundation.project_id,
                    content="Resolve this feedback before completing the Book.",
                ),
                idempotency_key="completion-blocked:feedback",
            )
            task_id, attempt_id = await _insert_assessment(
                engine,
                ready=ready,
                committed=committed,
                workspace_lock=workspace_lock,
                decision="complete",
                suffix="blocked",
            )
            with pytest.raises(CommandPreconditionError, match="unapplied user feedback"):
                await CompletionCommandService(CommandBus(engine)).apply_assessment(
                    ApplyBookProgressRequest(
                        project_id=ready.foundation.project_id,
                        book_id=ready.foundation.book_id,
                        task_id=task_id,
                        attempt_id=attempt_id,
                        expected_book_baseline_id=ready.foundation.book_baseline_id,
                        expected_canon_baseline_id=committed.result.canon_after_id,
                        expected_book_workspace_lock_version=workspace_lock,
                        terminal_arc_id=ready.foundation.arc_id,
                        terminal_arc_baseline_id=ready.foundation.arc_baseline_id,
                        terminal_chapter_id=ready.chapter_id,
                        terminal_chapter_baseline_id=committed.result.chapter_baseline_id,
                    ),
                    idempotency_key="completion-blocked:apply",
                )
            async with engine.connect() as connection:
                assert await connection.scalar(select(func.count()).select_from(book_completions)) == 0
                assert (
                    await connection.scalar(
                        select(agent_tasks.c.delivery_state).where(agent_tasks.c.id == task_id)
                    )
                    == "pending"
                )
        finally:
            await engine.dispose()

    asyncio.run(exercise())
