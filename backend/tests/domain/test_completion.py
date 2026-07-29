from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
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
    RecordBookCompletionReviewRequest,
)
from app.domain.chapter.commands import ChapterCommandService
from app.domain.chapter.contracts import CommitChapterRequest
from app.domain.commands import CommandPreconditionError
from app.domain.evaluation import (
    ArcClosureEvaluation,
    BookCompletionEvaluation,
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
                        expected_book_baseline_id=(
                            closure.chapter.foundation.book_baseline_id
                        ),
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
                        .where(
                            projects.c.id
                            == closure.chapter.foundation.project_id
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
                        expected_book_baseline_id=(
                            closure.chapter.foundation.book_baseline_id
                        ),
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
                        expected_book_baseline_id=(
                            closure.chapter.foundation.book_baseline_id
                        ),
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
                    expected_book_baseline_id=(
                        closure.chapter.foundation.book_baseline_id
                    ),
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
                        expected_book_baseline_id=(
                            closure.chapter.foundation.book_baseline_id
                        ),
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
                    await connection.scalar(
                        select(func.count()).select_from(book_completions)
                    )
                    == 0
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
                    await connection.scalar(
                        select(func.count()).select_from(book_completions)
                    )
                    == 0
                )
        finally:
            await engine.dispose()

    asyncio.run(exercise())
