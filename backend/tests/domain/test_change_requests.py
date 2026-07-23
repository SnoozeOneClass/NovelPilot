from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Literal

import pytest
from alembic import command
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.agents.contracts import ArcPlanProposal, LayerEvaluationResult
from app.db.engine import create_sqlite_async_engine
from app.db.maintenance import alembic_config
from app.db.schema import (
    arc_baselines,
    arc_book_change_requests,
    arc_workspaces,
    book_baselines,
    book_workspaces,
    chapter_arc_change_requests,
    chapter_book_change_requests,
    chapter_workspaces,
)
from app.domain.arc.commands import ArcCommandService
from app.domain.arc.contracts import (
    ApplyArcTaskRequest,
    ArcEvaluation,
    CommitArcAutoRequest,
    RecordArcReviewRequest,
    SubmitArcRequest,
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
from app.domain.change_requests import (
    ActivateChangeRequest,
    ChangeRequestCommandService,
    RejectChangeRequest,
)
from app.store.command_bus import CommandBus
from tests.domain.test_arc_lifecycle import _prepare_reviewed_arc
from tests.domain.test_chapter_lifecycle import _prepare_reviewed_chapter
from tests.helpers.lifecycle_seed import insert_successful_task


async def _commit_book_v2(
    engine: AsyncEngine,
    *,
    project_id: str,
    run_id: str,
    book_id: str,
    book_baseline_id: str,
    canon_baseline_id: str,
    workspace_lock_version: int,
    suffix: str,
) -> str:
    candidate = BookCandidatePack(
        direction="The investigation now permits the explicitly escalated reveal.",
        constraints={"pov": "limited-third", "history": "preserve-committed"},
        selected_title="Echo Testimony",
        rolling_plan={"strategy": "one-arc-at-a-time", "revision": suffix},
        completion_contract=CompletionContract(
            minimum_chapter_count=1,
            maximum_chapter_count=12,
            completion_requirements=["Resolve the central memory conflict"],
        ),
    )
    task_id, attempt_id = await insert_successful_task(
        engine,
        project_id=project_id,
        run_id=run_id,
        task_id=f"{suffix}:book-revise",
        attempt_id=f"{suffix}:book-revise:attempt",
        role="book_strategist",
        task_kind="book.revise",
        scope_layer="book",
        book_id=book_id,
        book_baseline_id=book_baseline_id,
        canon_baseline_id=canon_baseline_id,
        workspace_lock_version=workspace_lock_version,
        result=candidate,
    )
    service = BookCommandService(CommandBus(engine))
    applied = await service.apply_candidate_result(
        ApplyBookCandidateTaskRequest(
            project_id=project_id,
            book_id=book_id,
            task_id=task_id,
            attempt_id=attempt_id,
            expected_workspace_lock_version=workspace_lock_version,
        ),
        idempotency_key=f"{suffix}:apply-book-revise",
    )
    submitted = await service.submit_for_review(
        SubmitBookRequest(
            project_id=project_id,
            book_id=book_id,
            expected_workspace_lock_version=applied.result.workspace_lock_version,
        ),
        idempotency_key=f"{suffix}:submit-book-revise",
    )
    evaluator_task, evaluator_attempt = await insert_successful_task(
        engine,
        project_id=project_id,
        run_id=run_id,
        task_id=f"{suffix}:evaluate-book-revise",
        attempt_id=f"{suffix}:evaluate-book-revise:attempt",
        role="evaluator",
        task_kind="evaluate.book",
        scope_layer="book",
        book_id=book_id,
        book_baseline_id=book_baseline_id,
        canon_baseline_id=canon_baseline_id,
        workspace_lock_version=applied.result.workspace_lock_version,
        result=BookEvaluation(
            decision="pass",
            summary="The Book revision resolves the explicit lower-layer request.",
        ),
    )
    reviewed = await service.record_review(
        RecordBookReviewRequest(
            project_id=project_id,
            book_id=book_id,
            submission_id=submitted.result.submission_id,
            evaluator_task_id=evaluator_task,
            evaluator_attempt_id=evaluator_attempt,
            rubric_id="book-rubric",
            rubric_version=1,
            deterministic_precheck={"passed": True},
        ),
        idempotency_key=f"{suffix}:review-book-revise",
    )
    committed = await service.approve_and_commit(
        ApproveBookRequest(
            project_id=project_id,
            book_id=book_id,
            submission_id=submitted.result.submission_id,
            review_id=reviewed.result.review_id,
            expected_current_baseline_id=book_baseline_id,
        ),
        idempotency_key=f"{suffix}:approve-book-revise",
    )
    assert committed.result.baseline_version == 2
    return committed.result.baseline_id


def test_chapter_to_arc_request_resolves_only_when_arc_v2_commits(
    tmp_path: Path,
) -> None:
    database = tmp_path / "chapter-to-arc.sqlite3"
    command.upgrade(alembic_config(database), "head")

    async def exercise() -> None:
        engine = create_sqlite_async_engine(database)
        try:
            ready = await _prepare_reviewed_chapter(
                engine,
                project_id="change-project",
                target_chapter_count=2,
                canon_change=False,
                evaluation=LayerEvaluationResult(
                    decision="cross_loop_escalation",
                    summary="The Arc contract must change before this Chapter can proceed.",
                    escalation_target="arc",
                ),
            )
            async with engine.connect() as connection:
                change_request_id = await connection.scalar(
                    select(chapter_arc_change_requests.c.id).where(
                        chapter_arc_change_requests.c.chapter_id == ready.chapter_id
                    )
                )
                arc_lock = await connection.scalar(
                    select(arc_workspaces.c.lock_version).where(
                        arc_workspaces.c.arc_id == ready.foundation.arc_id
                    )
                )
            assert change_request_id is not None and arc_lock is not None
            bus = CommandBus(engine)
            activated = await ChangeRequestCommandService(bus).activate(
                ActivateChangeRequest(
                    project_id=ready.foundation.project_id,
                    change_request_id=change_request_id,
                    request_kind="chapter_to_arc",
                    expected_target_baseline_id=ready.foundation.arc_baseline_id,
                    expected_workspace_lock_version=arc_lock,
                ),
                idempotency_key="change:activate",
            )
            assert activated.result.target_layer == "arc"
            async with engine.connect() as connection:
                assert (
                    await connection.scalar(
                        select(chapter_arc_change_requests.c.status).where(
                            chapter_arc_change_requests.c.id == change_request_id
                        )
                    )
                    == "open"
                )

            plan = ArcPlanProposal(
                title="The First Contradiction, Revised",
                purpose="Allow the Chapter to reveal a physical memory-edit trace.",
                beats=["Witnesses disagree", "The revised evidence can now appear"],
                target_chapter_count=2,
                completion_signals=["The first edit source is identified"],
            )
            task_id, attempt_id = await insert_successful_task(
                engine,
                project_id=ready.foundation.project_id,
                run_id=ready.foundation.run_id,
                task_id="arc-revise-task",
                attempt_id="arc-revise-attempt",
                role="arc_planner",
                task_kind="arc.revise",
                scope_layer="arc",
                book_id=ready.foundation.book_id,
                book_baseline_id=ready.foundation.book_baseline_id,
                arc_id=ready.foundation.arc_id,
                arc_baseline_id=ready.foundation.arc_baseline_id,
                canon_baseline_id=ready.foundation.canon_baseline_id,
                workspace_lock_version=activated.result.workspace_lock_version,
                result=plan,
            )
            arc_service = ArcCommandService(bus)
            applied = await arc_service.apply_task_result(
                ApplyArcTaskRequest(
                    project_id=ready.foundation.project_id,
                    book_id=ready.foundation.book_id,
                    arc_id=ready.foundation.arc_id,
                    task_id=task_id,
                    attempt_id=attempt_id,
                    expected_workspace_lock_version=activated.result.workspace_lock_version,
                ),
                idempotency_key="change:apply-arc-revision",
            )
            submitted = await arc_service.submit_for_review(
                SubmitArcRequest(
                    project_id=ready.foundation.project_id,
                    book_id=ready.foundation.book_id,
                    arc_id=ready.foundation.arc_id,
                    expected_workspace_lock_version=applied.result.workspace_lock_version,
                ),
                idempotency_key="change:submit-arc-revision",
            )
            evaluator_task, evaluator_attempt = await insert_successful_task(
                engine,
                project_id=ready.foundation.project_id,
                run_id=ready.foundation.run_id,
                task_id="evaluate-arc-revision",
                attempt_id="evaluate-arc-revision-attempt",
                role="evaluator",
                task_kind="evaluate.arc",
                scope_layer="arc",
                book_id=ready.foundation.book_id,
                book_baseline_id=ready.foundation.book_baseline_id,
                arc_id=ready.foundation.arc_id,
                arc_baseline_id=ready.foundation.arc_baseline_id,
                canon_baseline_id=ready.foundation.canon_baseline_id,
                workspace_lock_version=applied.result.workspace_lock_version,
                result=ArcEvaluation(
                    decision="pass",
                    summary="The revised Arc resolves the explicit Chapter escalation.",
                ),
            )
            reviewed = await arc_service.record_review(
                RecordArcReviewRequest(
                    project_id=ready.foundation.project_id,
                    book_id=ready.foundation.book_id,
                    arc_id=ready.foundation.arc_id,
                    submission_id=submitted.result.submission_id,
                    evaluator_task_id=evaluator_task,
                    evaluator_attempt_id=evaluator_attempt,
                    rubric_id="arc-rubric",
                    rubric_version=1,
                    deterministic_precheck={"passed": True},
                ),
                idempotency_key="change:review-arc-revision",
            )
            committed = await arc_service.commit_baseline_auto(
                CommitArcAutoRequest(
                    project_id=ready.foundation.project_id,
                    book_id=ready.foundation.book_id,
                    arc_id=ready.foundation.arc_id,
                    submission_id=submitted.result.submission_id,
                    review_id=reviewed.result.review_id,
                    expected_current_baseline_id=ready.foundation.arc_baseline_id,
                ),
                idempotency_key="change:commit-arc-revision",
            )
            assert committed.result.baseline_version == 2
            async with engine.connect() as connection:
                change = (
                    await connection.execute(
                        select(
                            chapter_arc_change_requests.c.status,
                            chapter_arc_change_requests.c.resolved_by_arc_baseline_id,
                        ).where(chapter_arc_change_requests.c.id == change_request_id)
                    )
                ).one()
                chapter_workspace = (
                    await connection.execute(
                        select(
                            chapter_workspaces.c.state,
                            chapter_workspaces.c.stale_reason_code,
                        ).where(chapter_workspaces.c.chapter_id == ready.chapter_id)
                    )
                ).one()
                assert tuple(change) == ("resolved", committed.result.baseline_id)
                assert tuple(chapter_workspace) == ("stale", "upstream_arc_revised")
                assert (
                    await connection.scalar(select(func.count()).select_from(arc_baselines))
                    == 2
                )
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_rejected_change_request_keeps_formal_baselines_and_blocks_source_for_user(
    tmp_path: Path,
) -> None:
    database = tmp_path / "reject-change.sqlite3"
    command.upgrade(alembic_config(database), "head")

    async def exercise() -> None:
        engine = create_sqlite_async_engine(database)
        try:
            ready = await _prepare_reviewed_chapter(
                engine,
                project_id="reject-change-project",
                target_chapter_count=2,
                canon_change=False,
                evaluation=LayerEvaluationResult(
                    decision="cross_loop_escalation",
                    summary="The proposed reveal appears to require an Arc change.",
                    escalation_target="arc",
                ),
            )
            async with engine.connect() as connection:
                request_id = await connection.scalar(
                    select(chapter_arc_change_requests.c.id).where(
                        chapter_arc_change_requests.c.chapter_id == ready.chapter_id
                    )
                )
            assert request_id is not None
            rejected = await ChangeRequestCommandService(CommandBus(engine)).reject(
                RejectChangeRequest(
                    project_id=ready.foundation.project_id,
                    change_request_id=request_id,
                    request_kind="chapter_to_arc",
                    reason="The escalation is not justified by the approved Arc.",
                ),
                idempotency_key="change:reject",
            )
            assert rejected.result.rejected
            async with engine.connect() as connection:
                request_status = await connection.scalar(
                    select(chapter_arc_change_requests.c.status).where(
                        chapter_arc_change_requests.c.id == request_id
                    )
                )
                workspace_state = await connection.scalar(
                    select(chapter_workspaces.c.state).where(
                        chapter_workspaces.c.chapter_id == ready.chapter_id
                    )
                )
                assert request_status == "rejected"
                assert workspace_state == "blocked_by_user"
                assert (
                    await connection.scalar(select(func.count()).select_from(arc_baselines))
                    == 1
                )
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.parametrize("request_kind", ["chapter_to_book", "arc_to_book"])
def test_lower_to_book_request_resolves_only_when_book_v2_is_approved(
    tmp_path: Path,
    request_kind: Literal["chapter_to_book", "arc_to_book"],
) -> None:
    database = tmp_path / f"{request_kind}.sqlite3"
    command.upgrade(alembic_config(database), "head")

    async def exercise() -> None:
        engine = create_sqlite_async_engine(database)
        try:
            project_id = f"{request_kind}-project"
            if request_kind == "chapter_to_book":
                ready = await _prepare_reviewed_chapter(
                    engine,
                    project_id=project_id,
                    target_chapter_count=2,
                    canon_change=False,
                    evaluation=LayerEvaluationResult(
                        decision="cross_loop_escalation",
                        summary="The reveal requires a Book-level direction change.",
                        escalation_target="book",
                    ),
                )
                run_id = ready.foundation.run_id
                book_id = ready.foundation.book_id
                book_baseline_id = ready.foundation.book_baseline_id
                canon_baseline_id = ready.foundation.canon_baseline_id
                async with engine.connect() as connection:
                    request_id = await connection.scalar(
                        select(chapter_book_change_requests.c.id).where(
                            chapter_book_change_requests.c.chapter_id == ready.chapter_id
                        )
                    )
            else:
                reviewed_arc = await _prepare_reviewed_arc(
                    engine,
                    project_id=project_id,
                    operation_mode="full_auto",
                    evaluation=ArcEvaluation(
                        decision="escalate_to_book",
                        summary="The Arc requires a Book-level direction change.",
                    ),
                )
                run_id = reviewed_arc.book.run_id
                book_id = reviewed_arc.book.book_id
                book_baseline_id = reviewed_arc.book.book_baseline_id
                canon_baseline_id = reviewed_arc.book.canon_baseline_id
                async with engine.connect() as connection:
                    request_id = await connection.scalar(
                        select(arc_book_change_requests.c.id).where(
                            arc_book_change_requests.c.arc_id == reviewed_arc.arc_id
                        )
                    )
            assert request_id is not None
            async with engine.connect() as connection:
                book_lock = await connection.scalar(
                    select(book_workspaces.c.lock_version).where(
                        book_workspaces.c.book_id == book_id
                    )
                )
            assert book_lock is not None

            activated = await ChangeRequestCommandService(CommandBus(engine)).activate(
                ActivateChangeRequest(
                    project_id=project_id,
                    change_request_id=request_id,
                    request_kind=request_kind,
                    expected_target_baseline_id=book_baseline_id,
                    expected_workspace_lock_version=book_lock,
                ),
                idempotency_key=f"{request_kind}:activate",
            )
            assert activated.result.target_layer == "book"
            new_baseline_id = await _commit_book_v2(
                engine,
                project_id=project_id,
                run_id=run_id,
                book_id=book_id,
                book_baseline_id=book_baseline_id,
                canon_baseline_id=canon_baseline_id,
                workspace_lock_version=activated.result.workspace_lock_version,
                suffix=request_kind,
            )

            async with engine.connect() as connection:
                if request_kind == "chapter_to_book":
                    request_row = (
                        await connection.execute(
                            select(
                                chapter_book_change_requests.c.status,
                                chapter_book_change_requests.c.resolved_by_book_baseline_id,
                            ).where(chapter_book_change_requests.c.id == request_id)
                        )
                    ).one()
                    source_row = (
                        await connection.execute(
                            select(
                                chapter_workspaces.c.state,
                                chapter_workspaces.c.stale_reason_code,
                            ).where(chapter_workspaces.c.chapter_id == ready.chapter_id)
                        )
                    ).one()
                else:
                    request_row = (
                        await connection.execute(
                            select(
                                arc_book_change_requests.c.status,
                                arc_book_change_requests.c.resolved_by_book_baseline_id,
                            ).where(arc_book_change_requests.c.id == request_id)
                        )
                    ).one()
                    source_row = (
                        await connection.execute(
                            select(
                                arc_workspaces.c.state,
                                arc_workspaces.c.stale_reason_code,
                            ).where(arc_workspaces.c.arc_id == reviewed_arc.arc_id)
                        )
                    ).one()
                assert tuple(request_row) == ("resolved", new_baseline_id)
                assert tuple(source_row) == ("stale", "upstream_book_revised")
                assert (
                    await connection.scalar(select(func.count()).select_from(book_baselines))
                    == 2
                )
        finally:
            await engine.dispose()

    asyncio.run(exercise())
