from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import pytest
from alembic import command
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db.engine import create_sqlite_async_engine
from app.db.maintenance import alembic_config
from app.db.schema import (
    arc_closures,
    arc_workspaces,
    book_completions,
    book_progress_handoffs,
    book_workspaces,
    books,
    generation_runs,
    projects,
    story_arcs,
)
from app.domain.arc.commands import ArcCommandService
from app.domain.arc.contracts import CreateStoryArcRequest
from app.domain.authority import (
    CommitBookCompletionRequest,
    CommitBookProgressHandoffRequest,
    LoopAuthorityCommandService,
    RecordArcClosureReviewRequest,
    RecordBookBoundaryReviewRequest,
)
from app.domain.chapter.commands import ChapterCommandService
from app.domain.chapter.contracts import CommitChapterRequest
from app.domain.commands import CommandPreconditionError
from app.domain.evaluation import (
    ArcClosureEvaluation,
    BookBoundaryEvaluation,
    CompletionRequirementStatus,
    ContractSignalStatus,
)
from app.domain.feedback import FeedbackCommandService, SubmitFeedbackRequest
from app.store.command_bus import CommandBus
from tests.domain.test_chapter_lifecycle import (
    ReviewedChapter,
    _prepare_reviewed_chapter,
)
from tests.helpers.lifecycle_seed import insert_successful_task


@dataclass(frozen=True, slots=True)
class FormalArcBoundary:
    chapter: ReviewedChapter
    canon_baseline_id: str
    arc_closure_id: str
    book_workspace_lock_version: int


async def _prepare_formal_arc_boundary(
    engine: AsyncEngine,
    *,
    project_id: str,
    arc_purpose: Literal["regular", "final"],
) -> FormalArcBoundary:
    ready = await _prepare_reviewed_chapter(
        engine,
        project_id=project_id,
        target_chapter_count=1,
        canon_change=False,
        arc_purpose=arc_purpose,
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
        result=ArcClosureEvaluation(
            signal_statuses=[
                ContractSignalStatus(
                    signal_key="first_edit_identified",
                    status="satisfied",
                    evidence=["Chapter 1 observations identify the first edit source."],
                    rationale="The committed Chapter supplies the required evidence.",
                )
            ],
            arc_contract_judgment="remains_applicable",
            book_review_concern="not_required",
            chapter_evidence_concern="not_required",
            summary="The Arc contract is semantically complete.",
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
    return FormalArcBoundary(
        chapter=ready,
        canon_baseline_id=committed.result.canon_after_id,
        arc_closure_id=closure.result.formal_closure_id,
        book_workspace_lock_version=book_workspace_lock,
    )


async def _record_book_boundary(
    engine: AsyncEngine,
    *,
    boundary: FormalArcBoundary,
    ending_judgment: Literal[
        "regular_arc_needed",
        "final_arc_ready",
        "completion_ready",
    ],
    requirement_status: Literal["satisfied", "unresolved"],
    suffix: str,
) -> tuple[LoopAuthorityCommandService, str, str]:
    ready = boundary.chapter
    task_id, attempt_id = await insert_successful_task(
        engine,
        project_id=ready.foundation.project_id,
        run_id=ready.foundation.run_id,
        task_id=f"{suffix}:evaluate-book-boundary",
        attempt_id=f"{suffix}:evaluate-book-boundary:attempt",
        role="evaluator",
        task_kind="evaluate.book_boundary",
        scope_layer="book",
        book_id=ready.foundation.book_id,
        book_baseline_id=ready.foundation.book_baseline_id,
        canon_baseline_id=boundary.canon_baseline_id,
        workspace_lock_version=boundary.book_workspace_lock_version,
        correction_lineage_id=f"{suffix}:book-boundary-lineage",
        correction_lineage_origin="review_initiated",
        automatic_correction_round=0,
        source_arc_closure_id=boundary.arc_closure_id,
        result=BookBoundaryEvaluation(
            requirement_statuses=[
                CompletionRequirementStatus(
                    requirement_key="memory_conflict_resolved",
                    status=requirement_status,
                    evidence=["The terminal Chapter supplies the current evidence."],
                    rationale="The status is frozen from committed evidence.",
                )
            ],
            ending_trajectory_judgment=ending_judgment,
            book_contract_judgment="remains_applicable",
            summary="The Book boundary has an explicit semantic disposition.",
        ),
    )
    authority = LoopAuthorityCommandService(CommandBus(engine))
    review = await authority.record_book_boundary_review(
        RecordBookBoundaryReviewRequest(
            project_id=ready.foundation.project_id,
            book_id=ready.foundation.book_id,
            task_id=task_id,
            attempt_id=attempt_id,
        ),
        idempotency_key=f"{suffix}:record-book-boundary",
    )
    return authority, review.result.review_id, review.result.disposition


def test_formal_final_arc_and_book_boundary_atomically_complete_run(
    tmp_path: Path,
) -> None:
    database = tmp_path / "completion.sqlite3"
    command.upgrade(alembic_config(database), "head")

    async def exercise() -> None:
        engine = create_sqlite_async_engine(database)
        try:
            boundary = await _prepare_formal_arc_boundary(
                engine,
                project_id="completion-project",
                arc_purpose="final",
            )
            authority, review_id, disposition = await _record_book_boundary(
                engine,
                boundary=boundary,
                ending_judgment="completion_ready",
                requirement_status="satisfied",
                suffix="completion",
            )
            assert disposition == "complete_book"
            completed = await authority.commit_book_completion(
                CommitBookCompletionRequest(
                    project_id=boundary.chapter.foundation.project_id,
                    book_id=boundary.chapter.foundation.book_id,
                    boundary_review_id=review_id,
                ),
                idempotency_key="completion:commit",
            )
            with pytest.raises(
                CommandPreconditionError,
                match="dependencies are not current",
            ):
                await ArcCommandService(CommandBus(engine)).create_story_arc(
                    CreateStoryArcRequest(
                        project_id=boundary.chapter.foundation.project_id,
                        book_id=boundary.chapter.foundation.book_id,
                        expected_book_baseline_id=(
                            boundary.chapter.foundation.book_baseline_id
                        ),
                        expected_canon_baseline_id=boundary.canon_baseline_id,
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
                        .join(
                            generation_runs,
                            generation_runs.c.project_id == projects.c.id,
                        )
                        .where(
                            projects.c.id
                            == boundary.chapter.foundation.project_id
                        )
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
                    await connection.scalar(
                        select(func.count()).select_from(book_completions)
                    )
                    == 1
                )
                assert (
                    await connection.scalar(
                        select(func.count()).select_from(arc_closures)
                    )
                    == 1
                )
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("ending_judgment", "expected_disposition", "expected_purpose"),
    [
        ("regular_arc_needed", "continue_regular_arc", "regular"),
        ("final_arc_ready", "plan_final_arc", "final"),
    ],
)
def test_nonterminal_book_boundary_commits_handoff_before_next_arc(
    tmp_path: Path,
    ending_judgment: Literal["regular_arc_needed", "final_arc_ready"],
    expected_disposition: Literal["continue_regular_arc", "plan_final_arc"],
    expected_purpose: Literal["regular", "final"],
) -> None:
    database = tmp_path / f"handoff-{expected_purpose}.sqlite3"
    command.upgrade(alembic_config(database), "head")

    async def exercise() -> None:
        engine = create_sqlite_async_engine(database)
        try:
            boundary = await _prepare_formal_arc_boundary(
                engine,
                project_id=f"handoff-{expected_purpose}-project",
                arc_purpose="regular",
            )
            authority, review_id, disposition = await _record_book_boundary(
                engine,
                boundary=boundary,
                ending_judgment=ending_judgment,
                requirement_status="unresolved",
                suffix=f"handoff-{expected_purpose}",
            )
            assert disposition == expected_disposition
            with pytest.raises(
                CommandPreconditionError,
                match="requires the current Book progress handoff",
            ):
                await ArcCommandService(CommandBus(engine)).create_story_arc(
                    CreateStoryArcRequest(
                        project_id=boundary.chapter.foundation.project_id,
                        book_id=boundary.chapter.foundation.book_id,
                        expected_book_baseline_id=(
                            boundary.chapter.foundation.book_baseline_id
                        ),
                        expected_canon_baseline_id=boundary.canon_baseline_id,
                        purpose=expected_purpose,
                    ),
                    idempotency_key=f"handoff-{expected_purpose}:missing-handoff",
                )
            handoff = await authority.commit_book_progress_handoff(
                CommitBookProgressHandoffRequest(
                    project_id=boundary.chapter.foundation.project_id,
                    book_id=boundary.chapter.foundation.book_id,
                    boundary_review_id=review_id,
                ),
                idempotency_key=f"handoff-{expected_purpose}:commit",
            )
            assert handoff.result.next_arc_purpose == expected_purpose
            wrong_purpose: Literal["regular", "final"] = (
                "final" if expected_purpose == "regular" else "regular"
            )
            with pytest.raises(
                CommandPreconditionError,
                match="does not match the current Book handoff",
            ):
                await ArcCommandService(CommandBus(engine)).create_story_arc(
                    CreateStoryArcRequest(
                        project_id=boundary.chapter.foundation.project_id,
                        book_id=boundary.chapter.foundation.book_id,
                        expected_book_baseline_id=(
                            boundary.chapter.foundation.book_baseline_id
                        ),
                        expected_canon_baseline_id=boundary.canon_baseline_id,
                        purpose=wrong_purpose,
                    ),
                    idempotency_key=f"handoff-{expected_purpose}:wrong-purpose",
                )
            await ArcCommandService(CommandBus(engine)).create_story_arc(
                CreateStoryArcRequest(
                    project_id=boundary.chapter.foundation.project_id,
                    book_id=boundary.chapter.foundation.book_id,
                    expected_book_baseline_id=(
                        boundary.chapter.foundation.book_baseline_id
                    ),
                    expected_canon_baseline_id=boundary.canon_baseline_id,
                    purpose=handoff.result.next_arc_purpose,
                ),
                idempotency_key=f"handoff-{expected_purpose}:create-next-arc",
            )
            async with engine.connect() as connection:
                arcs = (
                    await connection.execute(
                        select(
                            story_arcs.c.ordinal,
                            story_arcs.c.purpose,
                            story_arcs.c.lifecycle_status,
                        ).order_by(story_arcs.c.ordinal)
                    )
                ).all()
                assert [tuple(row) for row in arcs] == [
                    (1, "regular", "completed"),
                    (2, expected_purpose, "planning"),
                ]
                assert (
                    await connection.scalar(
                        select(func.count()).select_from(book_progress_handoffs)
                    )
                    == 1
                )
                assert (
                    await connection.scalar(
                        select(func.count()).select_from(book_completions)
                    )
                    == 0
                )
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_completion_gate_rejects_queued_feedback_after_boundary_review(
    tmp_path: Path,
) -> None:
    database = tmp_path / "completion-blocked.sqlite3"
    command.upgrade(alembic_config(database), "head")

    async def exercise() -> None:
        engine = create_sqlite_async_engine(database)
        try:
            boundary = await _prepare_formal_arc_boundary(
                engine,
                project_id="completion-blocked-project",
                arc_purpose="final",
            )
            authority, review_id, disposition = await _record_book_boundary(
                engine,
                boundary=boundary,
                ending_judgment="completion_ready",
                requirement_status="satisfied",
                suffix="completion-blocked",
            )
            assert disposition == "complete_book"
            await FeedbackCommandService(CommandBus(engine)).submit(
                SubmitFeedbackRequest(
                    project_id=boundary.chapter.foundation.project_id,
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
                        project_id=boundary.chapter.foundation.project_id,
                        book_id=boundary.chapter.foundation.book_id,
                        boundary_review_id=review_id,
                    ),
                    idempotency_key="completion-blocked:commit",
                )
            async with engine.connect() as connection:
                assert (
                    await connection.scalar(
                        select(func.count()).select_from(book_completions)
                    )
                    == 0
                )
        finally:
            await engine.dispose()

    asyncio.run(exercise())
