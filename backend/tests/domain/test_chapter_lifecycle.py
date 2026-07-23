from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import func, select
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine

from app.agents.contracts import (
    ChapterDraftResult,
    ChapterObservationResult,
    ChapterPlanProposal,
    LayerEvaluationResult,
    SemanticCanonProposal,
)
from app.db.engine import create_sqlite_async_engine
from app.db.maintenance import alembic_config
from app.db.schema import (
    agent_tasks,
    canon_baselines,
    chapter_baselines,
    chapter_arc_change_requests,
    chapter_review_submissions,
    chapter_workspaces,
    chapters,
    content_refs,
    projects,
    story_arcs,
    generation_runs,
)
from app.domain.chapter.commands import ChapterCommandService
from app.domain.chapter.contracts import (
    ApplyChapterTaskRequest,
    CommitChapterRequest,
    CreateChapterRequest,
    RecordChapterReviewRequest,
    SubmitChapterRequest,
)
from app.domain.chapter.queries import ChapterQueryService
from app.store.canon import CanonRepository
from app.store.command_bus import CommandBus
from tests.helpers.lifecycle_seed import (
    ApprovedFoundation,
    insert_successful_task,
    seed_approved_book_and_arc,
)


@dataclass(frozen=True, slots=True)
class ReviewedChapter:
    foundation: ApprovedFoundation
    chapter_id: str
    submission_id: str
    review_id: str
    workspace_lock_version: int


async def _prepare_reviewed_chapter(
    engine: AsyncEngine,
    *,
    project_id: str,
    target_chapter_count: int,
    canon_change: bool,
    evaluation: LayerEvaluationResult | None = None,
    repair_count_before_review: int | None = None,
) -> ReviewedChapter:
    foundation = await seed_approved_book_and_arc(
        engine,
        project_id=project_id,
        target_chapter_count=target_chapter_count,
    )
    service = ChapterCommandService(CommandBus(engine))
    created = await service.create_chapter(
        CreateChapterRequest(
            project_id=project_id,
            book_id=foundation.book_id,
            arc_id=foundation.arc_id,
            expected_book_baseline_id=foundation.book_baseline_id,
            expected_arc_baseline_id=foundation.arc_baseline_id,
            expected_canon_baseline_id=foundation.canon_baseline_id,
        ),
        idempotency_key=f"{project_id}:chapter-create",
    )
    chapter_id = created.result.chapter_id
    plan_task, plan_attempt = await insert_successful_task(
        engine,
        project_id=project_id,
        run_id=foundation.run_id,
        task_id=f"{chapter_id}:plan",
        attempt_id=f"{chapter_id}:plan:attempt",
        role="chapter_writer",
        task_kind="chapter.plan",
        scope_layer="chapter",
        book_id=foundation.book_id,
        book_baseline_id=foundation.book_baseline_id,
        arc_id=foundation.arc_id,
        arc_baseline_id=foundation.arc_baseline_id,
        chapter_id=chapter_id,
        chapter_baseline_id=None,
        canon_baseline_id=foundation.canon_baseline_id,
        workspace_lock_version=1,
        result=ChapterPlanProposal(
            title="The Witness Who Remembered Twice",
            purpose="Reveal the first physical trace of memory editing.",
            scene_beats=["Mara compares two incompatible statements", "The ink changes"],
            required_continuity=["Mara distrusts her own notes"],
        ),
    )
    applied_plan = await service.apply_plan_result(
        ApplyChapterTaskRequest(
            project_id=project_id,
            chapter_id=chapter_id,
            task_id=plan_task,
            attempt_id=plan_attempt,
            expected_workspace_lock_version=1,
        ),
        idempotency_key=f"{chapter_id}:apply-plan",
    )
    draft_task, draft_attempt = await insert_successful_task(
        engine,
        project_id=project_id,
        run_id=foundation.run_id,
        task_id=f"{chapter_id}:draft",
        attempt_id=f"{chapter_id}:draft:attempt",
        role="chapter_writer",
        task_kind="chapter.draft",
        scope_layer="chapter",
        book_id=foundation.book_id,
        book_baseline_id=foundation.book_baseline_id,
        arc_id=foundation.arc_id,
        arc_baseline_id=foundation.arc_baseline_id,
        chapter_id=chapter_id,
        chapter_baseline_id=None,
        canon_baseline_id=foundation.canon_baseline_id,
        workspace_lock_version=applied_plan.result.workspace_lock_version,
        output_mode="text_streaming",
        result=ChapterDraftResult(
            prose=(
                "Mara laid the two statements side by side. "
                "The blue ink changed while she watched, adding a confession she had never heard."
            )
        ),
    )
    applied_draft = await service.apply_draft_result(
        ApplyChapterTaskRequest(
            project_id=project_id,
            chapter_id=chapter_id,
            task_id=draft_task,
            attempt_id=draft_attempt,
            expected_workspace_lock_version=applied_plan.result.workspace_lock_version,
        ),
        idempotency_key=f"{chapter_id}:apply-draft",
    )
    proposals = (
        [
            SemanticCanonProposal(
                category="characters",
                operation="add",
                subject="Mara",
                semantic_change="Mara directly witnesses written memory evidence changing.",
                evidence_hint="The blue ink changed while she watched",
            )
        ]
        if canon_change
        else []
    )
    observation_task, observation_attempt = await insert_successful_task(
        engine,
        project_id=project_id,
        run_id=foundation.run_id,
        task_id=f"{chapter_id}:observe",
        attempt_id=f"{chapter_id}:observe:attempt",
        role="chapter_writer",
        task_kind="chapter.observe",
        scope_layer="chapter",
        book_id=foundation.book_id,
        book_baseline_id=foundation.book_baseline_id,
        arc_id=foundation.arc_id,
        arc_baseline_id=foundation.arc_baseline_id,
        chapter_id=chapter_id,
        chapter_baseline_id=None,
        canon_baseline_id=foundation.canon_baseline_id,
        workspace_lock_version=applied_draft.result.workspace_lock_version,
        result=ChapterObservationResult(
            summary="Mara obtains physical evidence that memory edits affect documents.",
            continuity_observations=["Mara now has a reason to preserve analogue copies."],
            canon_proposals=proposals,
        ),
    )
    applied_observation = await service.apply_observation_result(
        ApplyChapterTaskRequest(
            project_id=project_id,
            chapter_id=chapter_id,
            task_id=observation_task,
            attempt_id=observation_attempt,
            expected_workspace_lock_version=applied_draft.result.workspace_lock_version,
        ),
        idempotency_key=f"{chapter_id}:apply-observation",
    )
    submitted = await service.submit_for_review(
        SubmitChapterRequest(
            project_id=project_id,
            chapter_id=chapter_id,
            expected_workspace_lock_version=applied_observation.result.workspace_lock_version,
        ),
        idempotency_key=f"{chapter_id}:submit",
    )
    evaluator_task, evaluator_attempt = await insert_successful_task(
        engine,
        project_id=project_id,
        run_id=foundation.run_id,
        task_id=f"{chapter_id}:evaluate",
        attempt_id=f"{chapter_id}:evaluate:attempt",
        role="evaluator",
        task_kind=(
            "verify_repair.chapter"
            if repair_count_before_review is not None
            and repair_count_before_review > 0
            else "evaluate.chapter"
        ),
        scope_layer="chapter",
        book_id=foundation.book_id,
        book_baseline_id=foundation.book_baseline_id,
        arc_id=foundation.arc_id,
        arc_baseline_id=foundation.arc_baseline_id,
        chapter_id=chapter_id,
        chapter_baseline_id=None,
        canon_baseline_id=foundation.canon_baseline_id,
        workspace_lock_version=applied_observation.result.workspace_lock_version,
        result=(
            evaluation
            or LayerEvaluationResult(
                decision="pass",
                summary=(
                    "The Chapter is coherent and the evidence span supports the Canon proposal."
                ),
            )
        ),
    )
    if repair_count_before_review is not None:
        async with engine.begin() as connection:
            await connection.execute(
                update(chapter_workspaces)
                .where(chapter_workspaces.c.chapter_id == chapter_id)
                .values(semantic_repair_count=repair_count_before_review)
            )
    reviewed = await service.record_review(
        RecordChapterReviewRequest(
            project_id=project_id,
            chapter_id=chapter_id,
            submission_id=submitted.result.submission_id,
            evaluator_task_id=evaluator_task,
            evaluator_attempt_id=evaluator_attempt,
            rubric_id="chapter-rubric",
            rubric_version=1,
            deterministic_precheck={"passed": True, "checks": ["exact_evidence"]},
        ),
        idempotency_key=f"{chapter_id}:review",
    )
    async with engine.connect() as connection:
        final_workspace_lock = await connection.scalar(
            select(chapter_workspaces.c.lock_version).where(
                chapter_workspaces.c.chapter_id == chapter_id
            )
        )
    assert final_workspace_lock is not None
    return ReviewedChapter(
        foundation=foundation,
        chapter_id=chapter_id,
        submission_id=submitted.result.submission_id,
        review_id=reviewed.result.review_id,
        workspace_lock_version=final_workspace_lock,
    )


def test_chapter_and_changed_canon_commit_atomically_and_complete_arc(
    tmp_path: Path,
) -> None:
    database = tmp_path / "chapter-canon.sqlite3"
    command.upgrade(alembic_config(database), "head")

    async def exercise() -> None:
        engine = create_sqlite_async_engine(database)
        try:
            ready = await _prepare_reviewed_chapter(
                engine,
                project_id="project-changed",
                target_chapter_count=1,
                canon_change=True,
            )
            service = ChapterCommandService(CommandBus(engine))
            request = CommitChapterRequest(
                project_id=ready.foundation.project_id,
                chapter_id=ready.chapter_id,
                submission_id=ready.submission_id,
                review_id=ready.review_id,
                expected_canon_baseline_id=ready.foundation.canon_baseline_id,
            )
            committed = await service.commit_chapter_and_canon(
                request,
                idempotency_key=f"{ready.chapter_id}:commit",
            )
            replayed = await service.commit_chapter_and_canon(
                request,
                idempotency_key=f"{ready.chapter_id}:commit",
            )
            assert replayed.replayed
            assert replayed.result == committed.result
            assert committed.result.canon_changed
            assert committed.result.canon_after_id != committed.result.canon_before_id
            assert committed.result.arc_completed

            async with engine.connect() as connection:
                chapter = (
                    await connection.execute(
                        select(
                            chapters.c.lifecycle_status,
                            chapters.c.current_baseline_id,
                        ).where(chapters.c.id == ready.chapter_id)
                    )
                ).one()
                arc_status = await connection.scalar(
                    select(story_arcs.c.lifecycle_status).where(
                        story_arcs.c.id == ready.foundation.arc_id
                    )
                )
                current_canon = await connection.scalar(
                    select(projects.c.current_canon_baseline_id).where(
                        projects.c.id == ready.foundation.project_id
                    )
                )
                assert (
                    await connection.scalar(
                        select(func.count()).select_from(chapter_baselines)
                    )
                    == 1
                )
                assert (
                    await connection.scalar(select(func.count()).select_from(canon_baselines))
                    == 2
                )
                assert (
                    await connection.scalar(
                        select(chapter_review_submissions.c.disposition).where(
                            chapter_review_submissions.c.id == ready.submission_id
                        )
                    )
                    == "promoted"
                )
            assert tuple(chapter) == (
                "committed",
                committed.result.chapter_baseline_id,
            )
            assert arc_status == "completed"
            assert current_canon == committed.result.canon_after_id
            text = await ChapterQueryService(engine).get_current_text(
                project_id=ready.foundation.project_id,
                chapter_id=ready.chapter_id,
            )
            assert text.chapter_title == "The Witness Who Remembered Twice"
            assert "The blue ink changed" in text.prose
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_noop_canon_patch_reuses_current_pointer(tmp_path: Path) -> None:
    database = tmp_path / "chapter-noop.sqlite3"
    command.upgrade(alembic_config(database), "head")

    async def exercise() -> None:
        engine = create_sqlite_async_engine(database)
        try:
            ready = await _prepare_reviewed_chapter(
                engine,
                project_id="project-noop",
                target_chapter_count=2,
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
                idempotency_key=f"{ready.chapter_id}:commit",
            )
            assert not committed.result.canon_changed
            assert committed.result.canon_before_id == committed.result.canon_after_id
            assert not committed.result.arc_completed
            async with engine.connect() as connection:
                assert (
                    await connection.scalar(select(func.count()).select_from(canon_baselines))
                    == 1
                )
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_stale_chapter_task_is_discarded_without_overwriting_workspace(
    tmp_path: Path,
) -> None:
    database = tmp_path / "chapter-stale.sqlite3"
    command.upgrade(alembic_config(database), "head")

    async def exercise() -> None:
        engine = create_sqlite_async_engine(database)
        try:
            foundation = await seed_approved_book_and_arc(
                engine,
                project_id="project-stale",
            )
            service = ChapterCommandService(CommandBus(engine))
            created = await service.create_chapter(
                CreateChapterRequest(
                    project_id=foundation.project_id,
                    book_id=foundation.book_id,
                    arc_id=foundation.arc_id,
                    expected_book_baseline_id=foundation.book_baseline_id,
                    expected_arc_baseline_id=foundation.arc_baseline_id,
                    expected_canon_baseline_id=foundation.canon_baseline_id,
                ),
                idempotency_key="create-chapter",
            )
            chapter_id = created.result.chapter_id
            tasks: list[tuple[str, str]] = []
            for suffix, title in (("new", "Current Plan"), ("old", "Stale Plan")):
                tasks.append(
                    await insert_successful_task(
                        engine,
                        project_id=foundation.project_id,
                        run_id=foundation.run_id,
                        task_id=f"{chapter_id}:plan:{suffix}",
                        attempt_id=f"{chapter_id}:plan:{suffix}:attempt",
                        role="chapter_writer",
                        task_kind="chapter.plan",
                        scope_layer="chapter",
                        book_id=foundation.book_id,
                        book_baseline_id=foundation.book_baseline_id,
                        arc_id=foundation.arc_id,
                        arc_baseline_id=foundation.arc_baseline_id,
                        chapter_id=chapter_id,
                        canon_baseline_id=foundation.canon_baseline_id,
                        workspace_lock_version=1,
                        result=ChapterPlanProposal(
                            title=title,
                            purpose="Test stale delivery.",
                            scene_beats=["One beat"],
                        ),
                    )
                )
            await service.apply_plan_result(
                ApplyChapterTaskRequest(
                    project_id=foundation.project_id,
                    chapter_id=chapter_id,
                    task_id=tasks[0][0],
                    attempt_id=tasks[0][1],
                    expected_workspace_lock_version=1,
                ),
                idempotency_key="apply-current",
            )
            stale = await service.apply_plan_result(
                ApplyChapterTaskRequest(
                    project_id=foundation.project_id,
                    chapter_id=chapter_id,
                    task_id=tasks[1][0],
                    attempt_id=tasks[1][1],
                    expected_workspace_lock_version=1,
                ),
                idempotency_key="apply-stale",
            )
            assert stale.result.delivery == "discarded_stale"
            assert stale.result.workspace_lock_version == 2
            async with engine.connect() as connection:
                assert (
                    await connection.scalar(
                        select(agent_tasks.c.delivery_state).where(
                            agent_tasks.c.id == tasks[1][0]
                        )
                    )
                    == "discarded_stale"
                )
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_local_repair_changes_only_authorized_component_and_consumes_one_budget(
    tmp_path: Path,
) -> None:
    database = tmp_path / "chapter-repair.sqlite3"
    command.upgrade(alembic_config(database), "head")

    async def exercise() -> None:
        engine = create_sqlite_async_engine(database)
        try:
            ready = await _prepare_reviewed_chapter(
                engine,
                project_id="project-repair",
                target_chapter_count=2,
                canon_change=False,
                evaluation=LayerEvaluationResult(
                    decision="local_repair",
                    summary="One paragraph overstates what Mara can know.",
                    repair_scope=["prose"],
                ),
            )
            task_id, attempt_id = await insert_successful_task(
                engine,
                project_id=ready.foundation.project_id,
                run_id=ready.foundation.run_id,
                task_id=f"{ready.chapter_id}:repair-prose",
                attempt_id=f"{ready.chapter_id}:repair-prose:attempt",
                role="chapter_writer",
                task_kind="chapter.repair.prose",
                scope_layer="chapter",
                book_id=ready.foundation.book_id,
                book_baseline_id=ready.foundation.book_baseline_id,
                arc_id=ready.foundation.arc_id,
                arc_baseline_id=ready.foundation.arc_baseline_id,
                chapter_id=ready.chapter_id,
                canon_baseline_id=ready.foundation.canon_baseline_id,
                workspace_lock_version=ready.workspace_lock_version,
                output_mode="text_streaming",
                result=ChapterDraftResult(
                    prose="Mara compared the statements and documented only what she directly observed."
                ),
            )
            applied = await ChapterCommandService(CommandBus(engine)).apply_repair_result(
                ApplyChapterTaskRequest(
                    project_id=ready.foundation.project_id,
                    chapter_id=ready.chapter_id,
                    task_id=task_id,
                    attempt_id=attempt_id,
                    expected_workspace_lock_version=ready.workspace_lock_version,
                ),
                idempotency_key=f"{ready.chapter_id}:apply-repair",
            )
            assert applied.result.component == "repair_prose"
            assert applied.result.delivery == "applied"
            async with engine.connect() as connection:
                workspace = (
                    await connection.execute(
                        select(
                            chapter_workspaces.c.semantic_repair_count,
                            chapter_workspaces.c.draft_ref_id,
                            chapter_workspaces.c.observations_ref_id,
                            chapter_workspaces.c.candidate_canon_patch_ref_id,
                        ).where(chapter_workspaces.c.chapter_id == ready.chapter_id)
                    )
                ).one()
            assert workspace.semantic_repair_count == 1
            assert workspace.draft_ref_id is not None
            assert workspace.observations_ref_id is None
            assert workspace.candidate_canon_patch_ref_id is None
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_sixth_semantic_repair_is_not_started_and_run_failure_pauses(
    tmp_path: Path,
) -> None:
    database = tmp_path / "chapter-repair-cap.sqlite3"
    command.upgrade(alembic_config(database), "head")

    async def exercise() -> None:
        engine = create_sqlite_async_engine(database)
        try:
            ready = await _prepare_reviewed_chapter(
                engine,
                project_id="project-repair-cap",
                target_chapter_count=2,
                canon_change=False,
                evaluation=LayerEvaluationResult(
                    decision="local_repair",
                    summary="Another local prose repair would be required.",
                    repair_scope=["prose"],
                ),
                repair_count_before_review=5,
            )
            async with engine.connect() as connection:
                run = (
                    await connection.execute(
                        select(
                            generation_runs.c.status,
                            generation_runs.c.failure_code,
                        ).where(generation_runs.c.id == ready.foundation.run_id)
                    )
                ).one()
                repair_tasks = await connection.scalar(
                    select(func.count())
                    .select_from(agent_tasks)
                    .where(agent_tasks.c.task_kind.like("chapter.repair.%"))
                )
            assert tuple(run) == ("failure_paused", "semantic_repair_exhausted")
            assert repair_tasks == 0
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_chapter_escalation_opens_explicit_arc_request_and_blocks_workspace(
    tmp_path: Path,
) -> None:
    database = tmp_path / "chapter-escalation.sqlite3"
    command.upgrade(alembic_config(database), "head")

    async def exercise() -> None:
        engine = create_sqlite_async_engine(database)
        try:
            ready = await _prepare_reviewed_chapter(
                engine,
                project_id="project-escalation",
                target_chapter_count=2,
                canon_change=False,
                evaluation=LayerEvaluationResult(
                    decision="cross_loop_escalation",
                    summary="The approved Arc requires a contradiction this Chapter cannot resolve.",
                    escalation_target="arc",
                ),
            )
            async with engine.connect() as connection:
                request = (
                    await connection.execute(
                        select(
                            chapter_arc_change_requests.c.status,
                            chapter_arc_change_requests.c.target_arc_baseline_id,
                        ).where(
                            chapter_arc_change_requests.c.chapter_id == ready.chapter_id
                        )
                    )
                ).one()
                workspace_state = await connection.scalar(
                    select(chapter_workspaces.c.state).where(
                        chapter_workspaces.c.chapter_id == ready.chapter_id
                    )
                )
            assert tuple(request) == ("open", ready.foundation.arc_baseline_id)
            assert workspace_state == "blocked_by_upstream"
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_canon_insert_failure_rolls_back_chapter_baseline_and_pointers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "chapter-rollback.sqlite3"
    command.upgrade(alembic_config(database), "head")

    async def fail_insert(*args: object, **kwargs: object) -> None:
        raise RuntimeError("injected Canon baseline failure")

    monkeypatch.setattr(CanonRepository, "insert_baseline", fail_insert)

    async def exercise() -> None:
        engine = create_sqlite_async_engine(database)
        try:
            ready = await _prepare_reviewed_chapter(
                engine,
                project_id="project-rollback",
                target_chapter_count=1,
                canon_change=True,
            )
            with pytest.raises(RuntimeError, match="injected Canon"):
                await ChapterCommandService(CommandBus(engine)).commit_chapter_and_canon(
                    CommitChapterRequest(
                        project_id=ready.foundation.project_id,
                        chapter_id=ready.chapter_id,
                        submission_id=ready.submission_id,
                        review_id=ready.review_id,
                        expected_canon_baseline_id=ready.foundation.canon_baseline_id,
                    ),
                    idempotency_key=f"{ready.chapter_id}:commit",
                )
            async with engine.connect() as connection:
                chapter = (
                    await connection.execute(
                        select(
                            chapters.c.lifecycle_status,
                            chapters.c.current_baseline_id,
                        ).where(chapters.c.id == ready.chapter_id)
                    )
                ).one()
                current_canon = await connection.scalar(
                    select(projects.c.current_canon_baseline_id).where(
                        projects.c.id == ready.foundation.project_id
                    )
                )
                assert (
                    await connection.scalar(
                        select(func.count()).select_from(chapter_baselines)
                    )
                    == 0
                )
                # Prepared category refs were inserted in the failed transaction too.
                changed_refs = await connection.scalar(
                    select(func.count())
                    .select_from(content_refs)
                    .where(content_refs.c.semantic_kind == "canon.characters")
                )
            assert tuple(chapter) == ("drafting", None)
            assert current_canon == ready.foundation.canon_baseline_id
            assert changed_refs == 1  # only the Project seed characters ref remains
        finally:
            await engine.dispose()

    asyncio.run(exercise())
