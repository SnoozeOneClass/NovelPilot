from __future__ import annotations

import asyncio
from pathlib import Path
import pytest
from alembic import command
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.agents.contracts import (
    ArcChapterOutlineEntry,
    ArcClosureSignal,
    ArcPlanProposal,
    ArcStateTransition,
    ChapterEvaluationIssue,
    LayerEvaluationResult,
)
from app.agents.registry import (
    DEFAULT_EVALUATION_STRATEGY_REGISTRY,
    DEFAULT_TASK_REGISTRY,
)
from app.db.engine import create_sqlite_async_engine
from app.db.maintenance import alembic_config
from app.db.schema import (
    arc_baselines,
    arc_book_change_requests,
    arc_workspaces,
    book_baselines,
    book_workspaces,
    chapters,
    chapter_workspaces,
    chapter_arc_change_requests,
    metadata,
)
from app.domain.authority import (
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
from app.domain.arc.outline import ArcOutlineProjectionError
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
from app.domain.change_requests import (
    ActivateChangeRequest,
    ChangeRequestCommandService,
    RejectChangeRequest,
)
from app.domain.chapter.commands import ChapterCommandService
from app.domain.chapter.contracts import CommitChapterRequest
from app.domain.commands import CommandPreconditionError
from app.domain.evaluation import (
    ArcParentContractEvaluation,
    BookParentContractEvaluation,
)
from app.domain.project_state import ProjectStateQuery
from app.runtime.context import HarnessContextBuilder
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
    candidate = BookSuccessorCandidateProposal(
        direction="The investigation now permits the explicitly escalated reveal.",
        constraints=BookCreativeConstraints(
            genre_reader_promise="A fair-play speculative mystery.",
            premise_story_engine="Each verified contradiction exposes a deeper memory edit.",
            stable_world_invariants=["Physical evidence cannot be retroactively edited."],
            stable_character_invariants=["Mara requires verifiable evidence."],
            core_selling_points=["Escalating memory contradictions"],
            prohibited_outcomes=["Do not erase committed history as a dream."],
        ),
        selected_title="Echo Testimony",
        rolling_plan=BookRollingPlan(
            long_term_character_directions=[
                "Mara learns to trust evidence without surrendering judgment."
            ],
            whole_book_pacing_strategy="Escalate through evidence-bound Arc closures.",
            ending_tendency="Resolve the central edit while preserving earned consequences.",
            arc_planning_guidelines=["Each Arc must close an observable state transition."],
            whole_book_scale_guidance="Around twelve Chapters remains advisory.",
        ),
        completion_contract=CompletionContract(
            completion_requirements=[
                BookCompletionRequirement(
                    requirement_key="central_memory_conflict_resolved",
                    description="Resolve the central memory conflict.",
                    evidence_expectation="Committed Chapters prove the resolution.",
                )
            ],
        ),
        arc_topology_suffix=BookArcTopologySuffix(
            arcs=[
                BookArcContract(
                    whole_book_role="Resolve the revised memory mystery.",
                    core_goal="Permit the evidence-bound escalated reveal.",
                    handoff_from_previous="Continue the current active Arc.",
                    exit_conditions=[
                        "The escalated reveal is supported and resolved."
                    ],
                    is_final=True,
                )
            ]
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
            rubric_id=DEFAULT_EVALUATION_STRATEGY_REGISTRY.for_task(
                "evaluate.book"
            ).rubric_id,
            rubric_version=DEFAULT_EVALUATION_STRATEGY_REGISTRY.for_task(
                "evaluate.book"
            ).rubric_version,
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


async def _record_arc_revision_authorization(
    engine: AsyncEngine,
    *,
    project_id: str,
    run_id: str,
    book_id: str,
    book_baseline_id: str,
    arc_id: str,
    arc_baseline_id: str,
    canon_baseline_id: str,
    request_id: str,
    workspace_lock_version: int,
    suffix: str,
) -> str:
    task_id, attempt_id = await insert_successful_task(
        engine,
        project_id=project_id,
        run_id=run_id,
        task_id=f"{suffix}:arc-parent-review",
        attempt_id=f"{suffix}:arc-parent-review:attempt",
        role="evaluator",
        task_kind="evaluate.arc_parent_contract",
        scope_layer="arc",
        book_id=book_id,
        book_baseline_id=book_baseline_id,
        arc_id=arc_id,
        arc_baseline_id=arc_baseline_id,
        canon_baseline_id=canon_baseline_id,
        workspace_lock_version=workspace_lock_version,
        correction_lineage_id=f"{suffix}:arc-parent-lineage",
        correction_lineage_origin="review_initiated",
        automatic_correction_round=0,
        source_chapter_arc_request_id=request_id,
        result=ArcParentContractEvaluation(
            arc_contract_judgment="revision_warranted",
            book_review_concern="not_required",
            chapter_evidence_concern="not_required",
            summary="Arc authority confirms that its baseline requires revision.",
        ),
    )
    recorded = await LoopAuthorityCommandService(CommandBus(engine)).record_arc_parent_review(
        RecordArcParentReviewRequest(
            project_id=project_id,
            book_id=book_id,
            arc_id=arc_id,
            request_id=request_id,
            task_id=task_id,
            attempt_id=attempt_id,
        ),
        idempotency_key=f"{suffix}:record-arc-parent-review",
    )
    assert recorded.result.disposition == "arc_revision_warranted"
    return recorded.result.review_id


async def _record_book_revision_authorization(
    engine: AsyncEngine,
    *,
    project_id: str,
    run_id: str,
    book_id: str,
    book_baseline_id: str,
    arc_baseline_id: str | None,
    canon_baseline_id: str,
    request_id: str,
    workspace_lock_version: int,
    suffix: str,
) -> str:
    task_id, attempt_id = await insert_successful_task(
        engine,
        project_id=project_id,
        run_id=run_id,
        task_id=f"{suffix}:book-parent-review",
        attempt_id=f"{suffix}:book-parent-review:attempt",
        role="evaluator",
        task_kind="evaluate.book_parent_contract",
        scope_layer="book",
        book_id=book_id,
        book_baseline_id=book_baseline_id,
        arc_baseline_id=arc_baseline_id,
        canon_baseline_id=canon_baseline_id,
        workspace_lock_version=workspace_lock_version,
        correction_lineage_id=f"{suffix}:book-parent-lineage",
        correction_lineage_origin="review_initiated",
        automatic_correction_round=0,
        source_arc_book_request_id=request_id,
        result=BookParentContractEvaluation(
            book_contract_judgment="revision_warranted",
            arc_evidence_concern="not_required",
            summary="Book authority confirms that its baseline requires revision.",
        ),
    )
    recorded = await LoopAuthorityCommandService(CommandBus(engine)).record_book_parent_review(
        RecordBookParentReviewRequest(
            project_id=project_id,
            book_id=book_id,
            request_id=request_id,
            task_id=task_id,
            attempt_id=attempt_id,
        ),
        idempotency_key=f"{suffix}:record-book-parent-review",
    )
    assert recorded.result.disposition == "book_revision_warranted"
    return recorded.result.review_id


def test_chapter_to_arc_request_resolves_only_when_arc_v2_commits(
    tmp_path: Path,
) -> None:
    database = tmp_path / "chapter-to-arc.sqlite3"
    command.upgrade(alembic_config(database), "head")

    async def exercise() -> None:
        engine = create_sqlite_async_engine(database)
        try:
            first = await _prepare_reviewed_chapter(
                engine,
                project_id="change-project",
                target_chapter_count=2,
                canon_change=False,
            )
            await ChapterCommandService(CommandBus(engine)).commit_chapter_and_canon(
                CommitChapterRequest(
                    project_id=first.foundation.project_id,
                    chapter_id=first.chapter_id,
                    submission_id=first.submission_id,
                    review_id=first.review_id,
                    expected_canon_baseline_id=(
                        first.foundation.canon_baseline_id
                    ),
                ),
                idempotency_key="change:commit-first-chapter",
            )
            ready = await _prepare_reviewed_chapter(
                engine,
                project_id="change-project",
                target_chapter_count=2,
                canon_change=False,
                foundation=first.foundation,
                idempotency_suffix=":second",
                evaluation=LayerEvaluationResult(
                    decision="escalate_to_arc",
                    summary="The Arc contract must change before this Chapter can proceed.",
                    issues=[
                        ChapterEvaluationIssue(
                            code="arc_contract_concern",
                            subject="current Arc contract",
                            summary=(
                                "The Arc contract must change before this Chapter can proceed."
                            ),
                            affected_components=["plan"],
                        )
                    ],
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
            async with engine.connect() as connection:
                assert (
                    await connection.scalar(
                        select(chapter_workspaces.c.base_chapter_baseline_id).where(
                            chapter_workspaces.c.chapter_id == ready.chapter_id
                        )
                    )
                    is None
                )
            definition = DEFAULT_TASK_REGISTRY.get(
                role="evaluator",
                task_kind="evaluate.arc_parent_contract",
                contract_version=1,
            )
            frozen_context = await HarnessContextBuilder(engine).build(
                task_kind="evaluate.arc_parent_contract",
                project_id=ready.foundation.project_id,
                book_id=ready.foundation.book_id,
                arc_id=ready.foundation.arc_id,
                chapter_id=None,
                semantic_goal="Judge the exact rejected Chapter candidate against its Arc.",
                definition=definition,
                evaluation_strategy=DEFAULT_EVALUATION_STRATEGY_REGISTRY.for_task(
                    "evaluate.arc_parent_contract"
                ),
                source_chapter_arc_request_id=change_request_id,
                canon_baseline_id=ready.foundation.canon_baseline_id,
            )
            labels = {
                str(item["label"])
                for item in frozen_context.manifest["items"]
            }
            assert {
                "chapter_to_arc_request_evidence",
                "source_chapter_candidate_manifest",
                "source_chapter_candidate_plan",
                "source_chapter_candidate_prose",
                "source_chapter_candidate_observations",
                "source_chapter_candidate_canon_intent",
            } <= labels
            assert all("current" not in label for label in labels)
            bus = CommandBus(engine)
            with pytest.raises(
                CommandPreconditionError,
                match="Chapter-to-Arc request is stale",
            ):
                await ChangeRequestCommandService(bus).activate(
                    ActivateChangeRequest(
                        project_id=ready.foundation.project_id,
                        change_request_id=change_request_id,
                        request_kind="chapter_to_arc",
                        expected_target_baseline_id=ready.foundation.arc_baseline_id,
                        expected_workspace_lock_version=arc_lock,
                    ),
                    idempotency_key="change:activate-without-authority",
                )
            await _record_arc_revision_authorization(
                engine,
                project_id=ready.foundation.project_id,
                run_id=ready.foundation.run_id,
                book_id=ready.foundation.book_id,
                book_baseline_id=ready.foundation.book_baseline_id,
                arc_id=ready.foundation.arc_id,
                arc_baseline_id=ready.foundation.arc_baseline_id,
                canon_baseline_id=ready.foundation.canon_baseline_id,
                request_id=change_request_id,
                workspace_lock_version=arc_lock,
                suffix="change",
            )
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
                    == "reviewed"
                )

            plan = ArcPlanProposal(
                title="The First Contradiction, Revised",
                desired_state_transition=ArcStateTransition(
                    start_state="The memory contradiction remains unexplained.",
                    end_state="The edit source is identified through physical evidence.",
                ),
                conflict_trajectory=[
                    "Witnesses disagree",
                    "The revised evidence can now appear",
                ],
                pacing_trajectory=["Investigate", "Verify", "Close the stage"],
                character_obligations=["Mara changes one belief through evidence."],
                foreshadowing_obligations=["Leave one clue for the next Arc."],
                prohibitions=["Do not contradict committed Canon."],
                closure_signals=[
                    ArcClosureSignal(
                        signal_key="first_edit_identified",
                        description="The first edit source is identified.",
                        evidence_expectation="Committed observations identify the source.",
                    )
                ],
                chapter_outline=[
                    ArcChapterOutlineEntry(
                        title="The surviving trace",
                        core_event="The revised evidence can now appear",
                        hook="The verified trace is ready for Arc closure review.",
                        scenes=["Verify and commit the physical trace."],
                    ),
                ],
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
            candidate_context = await HarnessContextBuilder(engine).build(
                task_kind="evaluate.arc",
                project_id=ready.foundation.project_id,
                book_id=ready.foundation.book_id,
                arc_id=ready.foundation.arc_id,
                chapter_id=None,
                semantic_goal="Evaluate the coherent Arc successor candidate.",
            )
            assert '"title":"Chapter 1"' in candidate_context.prompt
            assert '"title":"The surviving trace"' in candidate_context.prompt
            assert "physical evidence at assignment 2" not in (
                candidate_context.prompt
            )
            assert any(
                item["label"] == "candidate_coherent_story_arc_outline"
                for item in candidate_context.manifest["items"]
                if isinstance(item, dict)
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
                    rubric_id=DEFAULT_EVALUATION_STRATEGY_REGISTRY.for_task(
                        "evaluate.arc"
                    ).rubric_id,
                    rubric_version=DEFAULT_EVALUATION_STRATEGY_REGISTRY.for_task(
                        "evaluate.arc"
                    ).rubric_version,
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
                outline_source = await connection.scalar(
                    select(chapters.c.outline_arc_baseline_id).where(
                        chapters.c.id == ready.chapter_id
                    )
                )
                assert tuple(change) == ("resolved", committed.result.baseline_id)
                assert tuple(chapter_workspace) == ("active", None)
                assert outline_source == committed.result.baseline_id
                assert (
                    await connection.scalar(select(func.count()).select_from(arc_baselines))
                    == 2
                )
            state = await ProjectStateQuery(engine).get_project(
                ready.foundation.project_id
            )
            assert state is not None
            assert state.current_arc is not None
            assert state.current_arc.outline is not None
            assert state.current_arc.outline.current_baseline_version == 2
            assert [
                (entry.arc_ordinal, entry.status, entry.source_arc_baseline_version)
                for entry in state.current_arc.outline.entries
            ] == [
                (1, "committed", 1),
                (2, "drafting", 2),
            ]
            arc_context = await HarnessContextBuilder(engine).build(
                task_kind="arc.revise",
                project_id=ready.foundation.project_id,
                book_id=ready.foundation.book_id,
                arc_id=ready.foundation.arc_id,
                chapter_id=None,
                semantic_goal="Revise the coherent current Story Arc.",
            )
            assert '"title":"Chapter 1"' in arc_context.prompt
            assert '"title":"The surviving trace"' in arc_context.prompt
            assert any(
                item["label"] == "formal_coherent_story_arc_outline"
                for item in arc_context.manifest["items"]
                if isinstance(item, dict)
            )
            async with engine.begin() as connection:
                await connection.execute(
                    chapters.update()
                    .where(chapters.c.id == ready.chapter_id)
                    .values(
                        outline_arc_baseline_id=(
                            ready.foundation.arc_baseline_id
                        )
                    )
                )
            with pytest.raises(ArcOutlineProjectionError) as corrupted:
                await ProjectStateQuery(engine).get_project(
                    ready.foundation.project_id
                )
            assert (
                corrupted.value.reason_code
                == "chapter_source_not_governing_interval"
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
                    decision="escalate_to_arc",
                    summary="The proposed reveal appears to require an Arc change.",
                    issues=[
                        ChapterEvaluationIssue(
                            code="arc_reveal_scope",
                            subject="proposed reveal",
                            summary="The proposed reveal appears to require an Arc change.",
                            affected_components=["plan"],
                        )
                    ],
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
                assert request_status == "superseded"
                assert workspace_state == "blocked_by_user"
                assert (
                    await connection.scalar(select(func.count()).select_from(arc_baselines))
                    == 1
                )
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_direct_chapter_to_book_authority_table_is_absent() -> None:
    assert "chapter_book_change_requests" not in metadata.tables


def test_arc_to_book_request_resolves_only_when_authorized_book_v2_is_approved(
    tmp_path: Path,
) -> None:
    database = tmp_path / "arc-to-book.sqlite3"
    command.upgrade(alembic_config(database), "head")

    async def exercise() -> None:
        engine = create_sqlite_async_engine(database)
        try:
            project_id = "arc-to-book-project"
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

            service = ChangeRequestCommandService(CommandBus(engine))
            with pytest.raises(
                CommandPreconditionError,
                match="Book change request is stale",
            ):
                await service.activate(
                    ActivateChangeRequest(
                        project_id=project_id,
                        change_request_id=request_id,
                        request_kind="arc_to_book",
                        expected_target_baseline_id=book_baseline_id,
                        expected_workspace_lock_version=book_lock,
                    ),
                    idempotency_key="arc-to-book:activate-without-authority",
                )
            await _record_book_revision_authorization(
                engine,
                project_id=project_id,
                run_id=run_id,
                book_id=book_id,
                book_baseline_id=book_baseline_id,
                arc_baseline_id=None,
                canon_baseline_id=canon_baseline_id,
                request_id=request_id,
                workspace_lock_version=book_lock,
                suffix="arc-to-book",
            )
            activated = await service.activate(
                ActivateChangeRequest(
                    project_id=project_id,
                    change_request_id=request_id,
                    request_kind="arc_to_book",
                    expected_target_baseline_id=book_baseline_id,
                    expected_workspace_lock_version=book_lock,
                ),
                idempotency_key="arc-to-book:activate",
            )
            assert activated.result.target_layer == "book"
            assert activated.result.workspace_lock_version is not None
            new_baseline_id = await _commit_book_v2(
                engine,
                project_id=project_id,
                run_id=run_id,
                book_id=book_id,
                book_baseline_id=book_baseline_id,
                canon_baseline_id=canon_baseline_id,
                workspace_lock_version=activated.result.workspace_lock_version,
                suffix="arc-to-book",
            )

            async with engine.connect() as connection:
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
