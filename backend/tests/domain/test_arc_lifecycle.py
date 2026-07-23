from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import pytest
from alembic import command
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from app.agents.contracts import ArcPlanProposal
from app.db.engine import create_sqlite_async_engine
from app.db.maintenance import alembic_config
from app.db.schema import (
    agent_tasks,
    arc_approval_gates,
    arc_approvals,
    arc_baselines,
    arc_workspaces,
    generation_runs,
    story_arcs,
)
from app.domain.arc.commands import ArcCommandService
from app.domain.arc.contracts import (
    ApplyArcTaskRequest,
    ApproveArcRequest,
    ArcBeatsRepair,
    ArcEvaluation,
    ArcRepairPatch,
    ArcTitleRepair,
    CommitArcAutoRequest,
    CreateStoryArcRequest,
    RecordArcReviewRequest,
    RecordArcReviewResult,
    SubmitArcRequest,
)
from app.domain.book.commands import BookCommandService
from app.domain.book.contracts import (
    ApplyBookCandidateRequest,
    ApproveBookRequest,
    BookCandidatePack,
    BookEvaluation,
    CompletionContract,
    RecordBookReviewRequest,
    SubmitBookRequest,
)
from app.domain.commands import CommandPreconditionError
from app.domain.projects import (
    CreateProjectRequest,
    ProjectCommandService,
    UpdateProjectSettingsRequest,
)
from app.runtime.control import RunControlRequest, RunControlService
from app.store.command_bus import CommandBus
from app.store.content import ContentRepository
from tests.helpers.lifecycle_seed import insert_successful_task


@dataclass(frozen=True, slots=True)
class ApprovedBook:
    project_id: str
    run_id: str
    book_id: str
    book_baseline_id: str
    canon_baseline_id: str


@dataclass(frozen=True, slots=True)
class ReviewedArc:
    book: ApprovedBook
    arc_id: str
    submission_id: str
    review: RecordArcReviewResult
    workspace_lock_version: int
    plan: ArcPlanProposal


async def _seed_approved_book(
    engine: AsyncEngine,
    *,
    project_id: str,
    operation_mode: Literal["full_auto", "participatory"],
) -> ApprovedBook:
    bus = CommandBus(engine)
    project = await ProjectCommandService(bus).create_project(
        CreateProjectRequest(
            project_id=project_id,
            creator_brief="A mystery about memories that rewrite their witnesses.",
            operation_mode=operation_mode,
        ),
        idempotency_key=f"{project_id}:create",
    )
    await RunControlService(bus).start(
        RunControlRequest(
            project_id=project_id,
            run_id=project.result.generation_run_id,
            expected_lock_version=1,
        ),
        idempotency_key=f"{project_id}:start",
    )
    book_service = BookCommandService(bus)
    candidate = await book_service.apply_candidate(
        ApplyBookCandidateRequest(
            project_id=project_id,
            book_id=project.result.book_id,
            expected_workspace_lock_version=1,
            candidate=BookCandidatePack(
                direction="Conflicting testimony reveals that memory can be edited.",
                constraints={"pov": "limited-third"},
                selected_title="Echo Testimony",
                rolling_plan={"strategy": "one-arc-at-a-time"},
                completion_contract=CompletionContract(
                    minimum_chapter_count=1,
                    maximum_chapter_count=12,
                    completion_requirements=["Resolve the central memory conflict"],
                ),
            ),
        ),
        idempotency_key=f"{project_id}:book-candidate",
    )
    submitted = await book_service.submit_for_review(
        SubmitBookRequest(
            project_id=project_id,
            book_id=project.result.book_id,
            expected_workspace_lock_version=candidate.result.workspace_lock_version,
        ),
        idempotency_key=f"{project_id}:book-submit",
    )
    task_id, attempt_id = await insert_successful_task(
        engine,
        project_id=project_id,
        run_id=project.result.generation_run_id,
        task_id=f"{project_id}:evaluate-book",
        attempt_id=f"{project_id}:evaluate-book:attempt",
        role="evaluator",
        task_kind="evaluate.book",
        scope_layer="book",
        book_id=project.result.book_id,
        canon_baseline_id=project.result.canon_baseline_id,
        workspace_lock_version=candidate.result.workspace_lock_version,
        result=BookEvaluation(
            decision="pass",
            summary="The direction and completion contract are coherent.",
        ),
    )
    reviewed = await book_service.record_review(
        RecordBookReviewRequest(
            project_id=project_id,
            book_id=project.result.book_id,
            submission_id=submitted.result.submission_id,
            evaluator_task_id=task_id,
            evaluator_attempt_id=attempt_id,
            rubric_id="book-rubric",
            rubric_version=1,
            deterministic_precheck={"passed": True},
        ),
        idempotency_key=f"{project_id}:book-review",
    )
    approved = await book_service.approve_and_commit(
        ApproveBookRequest(
            project_id=project_id,
            book_id=project.result.book_id,
            submission_id=submitted.result.submission_id,
            review_id=reviewed.result.review_id,
        ),
        idempotency_key=f"{project_id}:book-approve",
    )
    return ApprovedBook(
        project_id=project_id,
        run_id=project.result.generation_run_id,
        book_id=project.result.book_id,
        book_baseline_id=approved.result.baseline_id,
        canon_baseline_id=project.result.canon_baseline_id,
    )


async def _prepare_reviewed_arc(
    engine: AsyncEngine,
    *,
    project_id: str,
    operation_mode: Literal["full_auto", "participatory"],
    purpose: Literal["regular", "final"] = "regular",
    target_chapter_count: int = 3,
    evaluation: ArcEvaluation | None = None,
    repair_count_before_review: int | None = None,
) -> ReviewedArc:
    book = await _seed_approved_book(
        engine,
        project_id=project_id,
        operation_mode=operation_mode,
    )
    service = ArcCommandService(CommandBus(engine))
    created = await service.create_story_arc(
        CreateStoryArcRequest(
            project_id=project_id,
            book_id=book.book_id,
            expected_book_baseline_id=book.book_baseline_id,
            expected_canon_baseline_id=book.canon_baseline_id,
            purpose=purpose,
        ),
        idempotency_key=f"{project_id}:arc-create",
    )
    plan = ArcPlanProposal(
        title="The First Contradiction",
        purpose="Expose the memory edit mechanism.",
        beats=["Witnesses disagree", "The discrepancy leaves physical evidence"],
        target_chapter_count=target_chapter_count,
        completion_signals=["The source of the first edit is identified"],
    )
    plan_task, plan_attempt = await insert_successful_task(
        engine,
        project_id=project_id,
        run_id=book.run_id,
        task_id=f"{created.result.arc_id}:plan",
        attempt_id=f"{created.result.arc_id}:plan:attempt",
        role="arc_planner",
        task_kind="arc.plan",
        scope_layer="arc",
        book_id=book.book_id,
        book_baseline_id=book.book_baseline_id,
        arc_id=created.result.arc_id,
        canon_baseline_id=book.canon_baseline_id,
        workspace_lock_version=created.result.workspace_lock_version,
        result=plan,
    )
    applied = await service.apply_task_result(
        ApplyArcTaskRequest(
            project_id=project_id,
            book_id=book.book_id,
            arc_id=created.result.arc_id,
            task_id=plan_task,
            attempt_id=plan_attempt,
            expected_workspace_lock_version=created.result.workspace_lock_version,
        ),
        idempotency_key=f"{project_id}:arc-plan-apply",
    )
    submitted = await service.submit_for_review(
        SubmitArcRequest(
            project_id=project_id,
            book_id=book.book_id,
            arc_id=created.result.arc_id,
            expected_workspace_lock_version=applied.result.workspace_lock_version,
        ),
        idempotency_key=f"{project_id}:arc-submit",
    )
    evaluator_task, evaluator_attempt = await insert_successful_task(
        engine,
        project_id=project_id,
        run_id=book.run_id,
        task_id=f"{created.result.arc_id}:evaluate",
        attempt_id=f"{created.result.arc_id}:evaluate:attempt",
        role="evaluator",
        task_kind=(
            "verify_repair.arc"
            if repair_count_before_review is not None
            and repair_count_before_review > 0
            else "evaluate.arc"
        ),
        scope_layer="arc",
        book_id=book.book_id,
        book_baseline_id=book.book_baseline_id,
        arc_id=created.result.arc_id,
        canon_baseline_id=book.canon_baseline_id,
        workspace_lock_version=applied.result.workspace_lock_version,
        result=(
            evaluation
            or ArcEvaluation(
                decision="pass",
                summary="The rolling Arc plan fits the approved Book contract.",
            )
        ),
    )
    if repair_count_before_review is not None:
        async with engine.begin() as connection:
            await connection.execute(
                update(arc_workspaces)
                .where(arc_workspaces.c.arc_id == created.result.arc_id)
                .values(semantic_repair_count=repair_count_before_review)
            )
    reviewed = await service.record_review(
        RecordArcReviewRequest(
            project_id=project_id,
            book_id=book.book_id,
            arc_id=created.result.arc_id,
            submission_id=submitted.result.submission_id,
            evaluator_task_id=evaluator_task,
            evaluator_attempt_id=evaluator_attempt,
            rubric_id="arc-rubric",
            rubric_version=1,
            deterministic_precheck={"passed": True},
        ),
        idempotency_key=f"{project_id}:arc-review",
    )
    async with engine.connect() as connection:
        workspace_lock_version = await connection.scalar(
            select(arc_workspaces.c.lock_version).where(
                arc_workspaces.c.arc_id == created.result.arc_id
            )
        )
    assert workspace_lock_version is not None
    return ReviewedArc(
        book=book,
        arc_id=created.result.arc_id,
        submission_id=submitted.result.submission_id,
        review=reviewed.result,
        workspace_lock_version=workspace_lock_version,
        plan=plan,
    )


def test_full_auto_pass_commits_without_arc_gate_and_preserves_final_hint(
    tmp_path: Path,
) -> None:
    database = tmp_path / "arc-auto.sqlite3"
    command.upgrade(alembic_config(database), "head")

    async def exercise() -> None:
        engine = create_sqlite_async_engine(database)
        try:
            setup = await _prepare_reviewed_arc(
                engine,
                project_id="project-auto",
                operation_mode="full_auto",
                purpose="final",
            )
            assert setup.review.next_action == "auto_commit"
            assert setup.review.approval_gate_id is None
            async with engine.connect() as connection:
                assert await connection.scalar(select(func.count()).select_from(arc_baselines)) == 0
                assert (
                    await connection.scalar(
                        select(func.count()).select_from(arc_approval_gates)
                    )
                    == 0
                )

            committed = await ArcCommandService(CommandBus(engine)).commit_baseline_auto(
                CommitArcAutoRequest(
                    project_id=setup.book.project_id,
                    book_id=setup.book.book_id,
                    arc_id=setup.arc_id,
                    submission_id=setup.submission_id,
                    review_id=setup.review.review_id,
                ),
                idempotency_key="project-auto:arc-commit",
            )
            assert committed.result.authorization_kind == "policy_auto"
            assert committed.result.target_chapter_count == 3
            assert committed.result.lifecycle_status == "active"
            async with engine.connect() as connection:
                arc = (
                    await connection.execute(
                        select(
                            story_arcs.c.purpose,
                            story_arcs.c.lifecycle_status,
                            story_arcs.c.current_baseline_id,
                        ).where(story_arcs.c.id == setup.arc_id)
                    )
                ).one()
                baseline = (
                    await connection.execute(
                        select(
                            arc_baselines.c.purpose,
                            arc_baselines.c.recommended_target_chapter_count,
                            arc_baselines.c.target_chapter_count,
                            arc_baselines.c.authorization_kind,
                        ).where(arc_baselines.c.id == committed.result.baseline_id)
                    )
                ).one()
                assert tuple(arc) == ("final", "active", committed.result.baseline_id)
                assert tuple(baseline) == ("final", 3, 3, "policy_auto")
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_participatory_pass_waits_for_exactly_one_adjustable_user_approval(
    tmp_path: Path,
) -> None:
    database = tmp_path / "arc-participatory.sqlite3"
    command.upgrade(alembic_config(database), "head")

    async def exercise() -> None:
        engine = create_sqlite_async_engine(database)
        try:
            setup = await _prepare_reviewed_arc(
                engine,
                project_id="project-participatory",
                operation_mode="participatory",
            )
            assert setup.review.next_action == "await_approval"
            assert setup.review.approval_gate_id is not None
            service = ArcCommandService(CommandBus(engine))
            with pytest.raises(CommandPreconditionError, match="cannot bypass"):
                await service.commit_baseline_auto(
                    CommitArcAutoRequest(
                        project_id=setup.book.project_id,
                        book_id=setup.book.book_id,
                        arc_id=setup.arc_id,
                        submission_id=setup.submission_id,
                        review_id=setup.review.review_id,
                    ),
                    idempotency_key="participatory:auto-forbidden",
                )
            async with engine.connect() as connection:
                assert await connection.scalar(select(func.count()).select_from(arc_baselines)) == 0
                assert (
                    await connection.scalar(
                        select(generation_runs.c.status).where(
                            generation_runs.c.id == setup.book.run_id
                        )
                    )
                    == "waiting_for_user"
                )

            committed = await service.approve_and_commit(
                ApproveArcRequest(
                    project_id=setup.book.project_id,
                    book_id=setup.book.book_id,
                    arc_id=setup.arc_id,
                    submission_id=setup.submission_id,
                    review_id=setup.review.review_id,
                    approval_gate_id=setup.review.approval_gate_id,
                    target_chapter_count=4,
                ),
                idempotency_key="participatory:approve",
            )
            assert committed.result.authorization_kind == "human_approval"
            assert committed.result.target_chapter_count == 4
            async with engine.connect() as connection:
                assert await connection.scalar(select(func.count()).select_from(arc_approvals)) == 1
                assert (
                    await connection.scalar(
                        select(func.count()).select_from(arc_approval_gates)
                    )
                    == 1
                )
                assert (
                    await connection.scalar(
                        select(generation_runs.c.status).where(
                            generation_runs.c.id == setup.book.run_id
                        )
                    )
                    == "running"
                )
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_full_auto_review_then_mode_switch_creates_persistent_gate(
    tmp_path: Path,
) -> None:
    database = tmp_path / "arc-mode-race.sqlite3"
    command.upgrade(alembic_config(database), "head")

    async def exercise() -> None:
        engine = create_sqlite_async_engine(database)
        try:
            setup = await _prepare_reviewed_arc(
                engine,
                project_id="project-race",
                operation_mode="full_auto",
            )
            bus = CommandBus(engine)
            projects = ProjectCommandService(bus)
            switched_to_participatory = await projects.update_settings(
                UpdateProjectSettingsRequest(
                    project_id=setup.book.project_id,
                    expected_lock_version=1,
                    operation_mode="participatory",
                ),
                idempotency_key="race:participatory",
            )
            gate_id = switched_to_participatory.result.arc_approval_gate_id
            assert gate_id is not None
            switched_back = await projects.update_settings(
                UpdateProjectSettingsRequest(
                    project_id=setup.book.project_id,
                    expected_lock_version=2,
                    operation_mode="full_auto",
                ),
                idempotency_key="race:auto-again",
            )
            assert switched_back.result.arc_approval_gate_id is None

            service = ArcCommandService(bus)
            with pytest.raises(CommandPreconditionError, match="persistent gate"):
                await service.commit_baseline_auto(
                    CommitArcAutoRequest(
                        project_id=setup.book.project_id,
                        book_id=setup.book.book_id,
                        arc_id=setup.arc_id,
                        submission_id=setup.submission_id,
                        review_id=setup.review.review_id,
                    ),
                    idempotency_key="race:auto-forbidden",
                )
            committed = await service.approve_and_commit(
                ApproveArcRequest(
                    project_id=setup.book.project_id,
                    book_id=setup.book.book_id,
                    arc_id=setup.arc_id,
                    submission_id=setup.submission_id,
                    review_id=setup.review.review_id,
                    approval_gate_id=gate_id,
                    target_chapter_count=3,
                ),
                idempotency_key="race:approve",
            )
            assert committed.result.authorization_kind == "human_approval"
            async with engine.connect() as connection:
                gates = (
                    await connection.execute(
                        select(arc_approval_gates.c.reason, arc_approval_gates.c.state)
                    )
                ).all()
                assert [tuple(row) for row in gates] == [("mode_switch", "decided")]
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_participatory_gate_survives_switch_to_full_auto(tmp_path: Path) -> None:
    database = tmp_path / "arc-existing-gate.sqlite3"
    command.upgrade(alembic_config(database), "head")

    async def exercise() -> None:
        engine = create_sqlite_async_engine(database)
        try:
            setup = await _prepare_reviewed_arc(
                engine,
                project_id="project-existing-gate",
                operation_mode="participatory",
            )
            assert setup.review.approval_gate_id is not None
            bus = CommandBus(engine)
            await ProjectCommandService(bus).update_settings(
                UpdateProjectSettingsRequest(
                    project_id=setup.book.project_id,
                    expected_lock_version=1,
                    operation_mode="full_auto",
                ),
                idempotency_key="existing-gate:switch-auto",
            )
            with pytest.raises(CommandPreconditionError, match="persistent gate"):
                await ArcCommandService(bus).commit_baseline_auto(
                    CommitArcAutoRequest(
                        project_id=setup.book.project_id,
                        book_id=setup.book.book_id,
                        arc_id=setup.arc_id,
                        submission_id=setup.submission_id,
                        review_id=setup.review.review_id,
                    ),
                    idempotency_key="existing-gate:auto-forbidden",
                )
            async with engine.connect() as connection:
                assert (
                    await connection.scalar(
                        select(func.count())
                        .select_from(arc_approval_gates)
                        .where(arc_approval_gates.c.state == "pending")
                    )
                    == 1
                )
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_stale_arc_plan_delivery_is_discarded_without_overwriting_workspace(
    tmp_path: Path,
) -> None:
    database = tmp_path / "arc-stale.sqlite3"
    command.upgrade(alembic_config(database), "head")

    async def exercise() -> None:
        engine = create_sqlite_async_engine(database)
        try:
            book = await _seed_approved_book(
                engine,
                project_id="project-stale",
                operation_mode="full_auto",
            )
            service = ArcCommandService(CommandBus(engine))
            created = await service.create_story_arc(
                CreateStoryArcRequest(
                    project_id=book.project_id,
                    book_id=book.book_id,
                    expected_book_baseline_id=book.book_baseline_id,
                    expected_canon_baseline_id=book.canon_baseline_id,
                ),
                idempotency_key="stale:create",
            )
            first = ArcPlanProposal(
                title="First",
                purpose="First accepted plan",
                beats=["A"],
                target_chapter_count=2,
                completion_signals=["A resolved"],
            )
            stale = first.model_copy(update={"title": "Stale"})
            tasks: list[tuple[str, str]] = []
            for suffix, result in (("first", first), ("stale", stale)):
                tasks.append(
                    await insert_successful_task(
                        engine,
                        project_id=book.project_id,
                        run_id=book.run_id,
                        task_id=f"{created.result.arc_id}:{suffix}",
                        attempt_id=f"{created.result.arc_id}:{suffix}:attempt",
                        role="arc_planner",
                        task_kind="arc.plan",
                        scope_layer="arc",
                        book_id=book.book_id,
                        book_baseline_id=book.book_baseline_id,
                        arc_id=created.result.arc_id,
                        canon_baseline_id=book.canon_baseline_id,
                        workspace_lock_version=1,
                        result=result,
                    )
                )
            applied = await service.apply_task_result(
                ApplyArcTaskRequest(
                    project_id=book.project_id,
                    book_id=book.book_id,
                    arc_id=created.result.arc_id,
                    task_id=tasks[0][0],
                    attempt_id=tasks[0][1],
                    expected_workspace_lock_version=1,
                ),
                idempotency_key="stale:apply-first",
            )
            discarded = await service.apply_task_result(
                ApplyArcTaskRequest(
                    project_id=book.project_id,
                    book_id=book.book_id,
                    arc_id=created.result.arc_id,
                    task_id=tasks[1][0],
                    attempt_id=tasks[1][1],
                    expected_workspace_lock_version=1,
                ),
                idempotency_key="stale:discard-second",
            )
            assert applied.result.delivery == "applied"
            assert discarded.result.delivery == "discarded_stale"
            assert discarded.result.workspace_lock_version == 2
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


def test_arc_local_repair_is_bounded_by_components_and_five_attempts(
    tmp_path: Path,
) -> None:
    database = tmp_path / "arc-repair.sqlite3"
    command.upgrade(alembic_config(database), "head")

    async def exercise() -> None:
        engine = create_sqlite_async_engine(database)
        try:
            setup = await _prepare_reviewed_arc(
                engine,
                project_id="project-repair",
                operation_mode="full_auto",
                evaluation=ArcEvaluation(
                    decision="local_repair",
                    summary="Only the beats need a bounded repair.",
                    repair_scope=["beats"],
                ),
            )
            assert setup.review.next_action == "repair"
            unauthorized = ArcRepairPatch(
                changes=[
                    ArcTitleRepair(component="title", value="Unauthorized title"),
                    ArcBeatsRepair(component="beats", value=["Repaired beat"]),
                ]
            )
            authorized = ArcRepairPatch(
                changes=[
                    ArcBeatsRepair(component="beats", value=["Repaired beat"]),
                ]
            )
            no_op = ArcRepairPatch(
                changes=[
                    ArcBeatsRepair(component="beats", value=setup.plan.beats),
                ]
            )
            task_pairs: list[tuple[str, str]] = []
            for suffix, result in (
                ("unauthorized", unauthorized),
                ("no-op", no_op),
                ("authorized", authorized),
            ):
                task_pairs.append(
                    await insert_successful_task(
                        engine,
                        project_id=setup.book.project_id,
                        run_id=setup.book.run_id,
                        task_id=f"{setup.arc_id}:repair:{suffix}",
                        attempt_id=f"{setup.arc_id}:repair:{suffix}:attempt",
                        role="arc_planner",
                        task_kind="arc.repair",
                        scope_layer="arc",
                        book_id=setup.book.book_id,
                        book_baseline_id=setup.book.book_baseline_id,
                        arc_id=setup.arc_id,
                        canon_baseline_id=setup.book.canon_baseline_id,
                        workspace_lock_version=setup.workspace_lock_version,
                        result=result,
                    )
                )
            service = ArcCommandService(CommandBus(engine))
            with pytest.raises(CommandPreconditionError, match="unauthorized components"):
                await service.apply_task_result(
                    ApplyArcTaskRequest(
                        project_id=setup.book.project_id,
                        book_id=setup.book.book_id,
                        arc_id=setup.arc_id,
                        task_id=task_pairs[0][0],
                        attempt_id=task_pairs[0][1],
                        expected_workspace_lock_version=setup.workspace_lock_version,
                    ),
                    idempotency_key="repair:unauthorized",
                )
            with pytest.raises(CommandPreconditionError, match="no authorized change"):
                await service.apply_task_result(
                    ApplyArcTaskRequest(
                        project_id=setup.book.project_id,
                        book_id=setup.book.book_id,
                        arc_id=setup.arc_id,
                        task_id=task_pairs[1][0],
                        attempt_id=task_pairs[1][1],
                        expected_workspace_lock_version=setup.workspace_lock_version,
                    ),
                    idempotency_key="repair:no-op",
                )
            repaired = await service.apply_task_result(
                ApplyArcTaskRequest(
                    project_id=setup.book.project_id,
                    book_id=setup.book.book_id,
                    arc_id=setup.arc_id,
                    task_id=task_pairs[2][0],
                    attempt_id=task_pairs[2][1],
                    expected_workspace_lock_version=setup.workspace_lock_version,
                ),
                idempotency_key="repair:authorized",
            )
            assert repaired.result.delivery == "applied"
            async with engine.connect() as connection:
                workspace = (
                    await connection.execute(
                        select(
                            arc_workspaces.c.semantic_repair_count,
                            arc_workspaces.c.plan_ref_id,
                        ).where(arc_workspaces.c.arc_id == setup.arc_id)
                    )
                ).one()
                unauthorized_state = await connection.scalar(
                    select(agent_tasks.c.delivery_state).where(
                        agent_tasks.c.id == task_pairs[0][0]
                    )
                )
                assert workspace.plan_ref_id is not None
                packed = await ContentRepository(connection).get_packed(
                    project_id=setup.book.project_id,
                    ref_id=workspace.plan_ref_id,
                )
                merged_plan = ArcPlanProposal.model_validate(
                    json.loads(packed.unpack_and_verify())
                )
                assert workspace.semantic_repair_count == 1
                assert unauthorized_state == "pending"
                assert merged_plan.beats == ["Repaired beat"]
                assert merged_plan.purpose == setup.plan.purpose
                assert merged_plan.title == setup.plan.title
                assert merged_plan.target_chapter_count == setup.plan.target_chapter_count
                assert merged_plan.completion_signals == setup.plan.completion_signals

            exhausted = await _prepare_reviewed_arc(
                engine,
                project_id="project-repair-exhausted",
                operation_mode="full_auto",
                evaluation=ArcEvaluation(
                    decision="local_repair",
                    summary="The sixth repair must not start.",
                    repair_scope=["beats"],
                ),
                repair_count_before_review=5,
            )
            assert exhausted.review.next_action == "failure_paused"
            async with engine.connect() as connection:
                run = (
                    await connection.execute(
                        select(
                            generation_runs.c.status,
                            generation_runs.c.failure_code,
                        ).where(generation_runs.c.id == exhausted.book.run_id)
                    )
                ).one()
                assert tuple(run) == ("failure_paused", "semantic_repair_exhausted")
        finally:
            await engine.dispose()

    asyncio.run(exercise())
