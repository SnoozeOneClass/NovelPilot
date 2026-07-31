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
    ArcClosureSignal,
    ArcPlanProposal,
    ArcStateTransition,
    EvaluationIssue,
)
from app.agents.registry import DEFAULT_EVALUATION_STRATEGY_REGISTRY
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
from app.runtime.context import HarnessContextBuilder
from app.store.command_bus import CommandBus
from tests.domain.test_chapter_lifecycle import (
    ReviewedChapter,
    _prepare_reviewed_chapter,
)
from tests.helpers.lifecycle_seed import ApprovedFoundation, insert_successful_task


def test_arc_closure_cannot_pass_while_ignoring_an_optional_signal_blocker() -> None:
    plan = ArcPlanProposal(
        title="Evidence Arc",
        desired_state_transition=ArcStateTransition(
            start_state="The source is unknown.",
            end_state="The source is identified.",
        ),
        conflict_trajectory=["The evidence first conflicts, then converges."],
        pacing_trajectory=["investigation", "closure"],
        character_obligations=["The investigator follows verifiable evidence."],
        prohibitions=["Do not replace evidence with a dream explanation."],
        closure_signals=[
            ArcClosureSignal(
                signal_key="source_identified",
                description="Identify the source.",
                evidence_expectation="A formal Chapter names the source.",
                required=True,
            ),
            ArcClosureSignal(
                signal_key="optional_cost",
                description="Optionally establish a secondary cost.",
                evidence_expectation="A formal Chapter may establish the cost.",
                required=False,
            ),
        ],
        chapter_outline=[
            ArcChapterOutlineEntry(
                title="The Source",
                core_event="Identify the source.",
                hook="Hand the identified source to closure.",
                scenes=["Compare the records.", "Name the source."],
            )
        ],
    )
    evaluation = ArcClosureEvaluation(
        signal_statuses=[
            ContractSignalStatus(
                signal_key="source_identified",
                status="satisfied",
                evidence=["The terminal Chapter names the source."],
                rationale="The required signal is satisfied.",
            ),
            ContractSignalStatus(
                signal_key="optional_cost",
                status="unresolved",
                evidence=["No committed Chapter establishes the optional cost."],
                rationale="The optional signal remains unresolved.",
            ),
        ],
        arc_contract_judgment="remains_applicable",
        book_review_concern="not_required",
        chapter_evidence_concern="not_required",
        summary="The required signal passes, but an issue ledger is still attached.",
        issues=[
            EvaluationIssue(
                kind="contract_unfulfilled",
                code="optional_cost_missing",
                subject="optional cost",
                summary="The issue ledger marks the optional signal as blocking.",
                evidence=["The optional signal remains unresolved."],
                contract_item="Establish the optional cost.",
            )
        ],
    )

    assert (
        LoopAuthorityCommandService._arc_closure_disposition(
            evaluation=evaluation,
            plan=plan,
        )
        == "no_legal_route"
    )


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
            desired_state_transition=ArcStateTransition(
                start_state="The first edit source has been identified.",
                end_state="The central memory conflict is resolved by physical evidence.",
            ),
            conflict_trajectory=["The final source resists", "The evidence converges"],
            pacing_trajectory=["Escalate", "Resolve"],
            character_obligations=["The investigator chooses evidence over memory."],
            prohibitions=["Do not erase the first Arc closure."],
            closure_signals=[
                ArcClosureSignal(
                    signal_key="central_conflict_resolved",
                    description="Resolve the central memory conflict.",
                    evidence_expectation="The formal Chapter records physical proof.",
                )
            ],
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
            rubric_id=DEFAULT_EVALUATION_STRATEGY_REGISTRY.for_task(
                "evaluate.arc"
            ).rubric_id,
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
            select(arc_workspaces.c.lock_version).where(
                arc_workspaces.c.arc_id == arc_id
            )
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
        result=ArcClosureEvaluation(
            signal_statuses=[
                ContractSignalStatus(
                    signal_key="central_conflict_resolved",
                    status="satisfied",
                    evidence=["The final Chapter records the physical proof."],
                    rationale="The required final signal is satisfied.",
                )
            ],
            arc_contract_judgment="remains_applicable",
            book_review_concern="not_required",
            chapter_evidence_concern="not_required",
            summary="The final Arc contract is complete.",
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
                        ).where(
                            book_completions.c.id
                            == completed.result.completion_id
                        )
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
                assert formal_completion.book_baseline_id == (
                    formal_closure.book_baseline_id
                )
                assert formal_completion.arc_closure_id == closure.arc_closure_id
                assert formal_completion.terminal_arc_id == (
                    closure.chapter.foundation.arc_id
                )
                assert formal_completion.terminal_arc_baseline_id == (
                    formal_closure.arc_baseline_id
                )
                assert formal_completion.terminal_chapter_id == (
                    formal_closure.terminal_chapter_id
                )
                assert formal_completion.terminal_chapter_baseline_id == (
                    formal_closure.terminal_chapter_baseline_id
                )
                assert formal_completion.canon_baseline_id == (
                    formal_closure.canon_baseline_id
                )
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
                    expected_workspace_lock_version=(
                        second.book_workspace_lock_version
                    ),
                ),
                idempotency_key="completion-second-arc:open-revision",
            )
            assert opened.result.workspace_lock_version == (
                second.book_workspace_lock_version + 1
            )
            async with engine.connect() as connection:
                workspace = (
                    await connection.execute(
                        select(
                            book_workspaces.c.state,
                            book_workspaces.c.source_book_completion_review_id,
                            book_workspaces.c.source_book_progress_handoff_id,
                            book_workspaces.c.work_cycle_id,
                        ).where(
                            book_workspaces.c.book_id
                            == second.chapter.foundation.book_id
                        )
                    )
                ).one()
                current_handoff_id = await connection.scalar(
                    select(books.c.current_progress_handoff_id).where(
                        books.c.id == second.chapter.foundation.book_id
                    )
                )
            assert workspace.state == "active"
            assert workspace.source_book_completion_review_id == review_id
            assert (
                workspace.source_book_progress_handoff_id
                == handoff.result.handoff_id
            )
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
            assert (
                completion_review_items[0]["label"]
                == "source_book_completion_review"
            )
            assert completion_review_items[0]["use"] == "verification"
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
