from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from alembic import command
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.agents.contracts import (
    BookDiscussionContinue,
    BookDiscussionReady,
    BookDiscussionResult,
    BookDiscussionSuggestion,
)
from app.agents.registry import DEFAULT_EVALUATION_STRATEGY_REGISTRY
from app.db.engine import create_sqlite_async_engine
from app.db.maintenance import alembic_config
from app.db.schema import (
    agent_tasks,
    book_approvals,
    book_baselines,
    book_review_submissions,
    book_reviews,
    book_workspaces,
    books,
    generation_runs,
)
from app.domain.book.commands import BookCommandService
from app.domain.book.contracts import (
    ApplyBookCandidateRequest,
    ApplyBookCandidateTaskRequest,
    ApplyBookDiscussionTaskRequest,
    ApproveBookRequest,
    BookArcContract,
    BookArcTopology,
    BookCandidatePack,
    BookCompletionRequirement,
    BookConstraintsRepair,
    BookCreativeConstraints,
    BookDirectionRepair,
    BookDiscussionState,
    BookEvaluation,
    BookEvaluationIssue,
    BookRepairPatch,
    BookRollingPlan,
    CompletionContract,
    RecordBookReviewRequest,
    RecordBookUserInputRequest,
    SubmitBookRequest,
)
from app.domain.commands import CommandPreconditionError
from app.domain.projects import CreateProjectRequest, ProjectCommandService
from app.runtime.control import RunControlRequest, RunControlService
from app.store.command_bus import CommandBus
from app.store.content import ContentRepository
from tests.helpers.lifecycle_seed import insert_successful_task


BOOK_EVALUATION_STRATEGY = DEFAULT_EVALUATION_STRATEGY_REGISTRY.for_task("evaluate.book")
BOOK_REPAIR_EVALUATION_STRATEGY = DEFAULT_EVALUATION_STRATEGY_REGISTRY.for_task(
    "verify_repair.book"
)


def _book_constraints(*, perspective: str = "limited-third") -> BookCreativeConstraints:
    return BookCreativeConstraints(
        genre_reader_promise="A fair-play memory mystery.",
        premise_story_engine=(
            f"Physical evidence challenges rewritten memory through a {perspective} viewpoint."
        ),
        stable_world_invariants=["Physical evidence cannot be memory-edited."],
        stable_character_invariants=["The investigator pursues verifiable truth."],
        core_selling_points=["Each contradiction can be investigated."],
        prohibited_outcomes=["Committed facts cannot be dismissed as a dream."],
    )


def _book_rolling_plan() -> BookRollingPlan:
    return BookRollingPlan(
        long_term_character_directions=["Trust evidence over memory."],
        whole_book_pacing_strategy="Escalate through bounded rolling Arcs.",
        ending_tendency="The investigator chooses truth at personal cost.",
        arc_planning_guidelines=["Each Arc must close observable evidence."],
        whole_book_scale_guidance="About twenty Chapters is advisory.",
    )


def _completion_contract() -> CompletionContract:
    return CompletionContract(
        completion_requirements=[
            BookCompletionRequirement(
                requirement_key="memory_conflict_resolved",
                description="Resolve the central memory conflict.",
                evidence_expectation="Committed Chapter evidence proves the resolution.",
            )
        ],
    )


def _book_topology() -> BookArcTopology:
    return BookArcTopology(
        arcs=[
            BookArcContract(
                whole_book_role="Resolve the memory mystery.",
                core_goal="Identify the edit source with physical evidence.",
                handoff_from_previous="Begin from the approved premise.",
                exit_conditions=["The source and central conflict are resolved."],
                completion_requirement_keys=["memory_conflict_resolved"],
                is_final=True,
            )
        ]
    )


def _aligned_requirement_coverage() -> list[dict[str, str]]:
    return [
        {
            "requirement_key": "memory_conflict_resolved",
            "judgment": "aligned",
            "rationale": "The responsible final Arc resolves the central conflict.",
        }
    ]


def test_book_arc_topology_is_semantic_count_free_and_final_last() -> None:
    final_contract = _book_topology().arcs[0]
    nonfinal_contract = final_contract.model_copy(update={"is_final": False})

    with pytest.raises(ValidationError, match="at least 1 item"):
        BookArcTopology(arcs=[])
    with pytest.raises(ValidationError, match="exactly one final Arc"):
        BookArcTopology(arcs=[nonfinal_contract])
    with pytest.raises(ValidationError, match="exactly one final Arc"):
        BookArcTopology(arcs=[final_contract, nonfinal_contract])

    count_bearing_contract = final_contract.model_dump(mode="python")
    count_bearing_contract["chapter_count"] = 10
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        BookArcContract.model_validate(count_bearing_contract)


async def _insert_successful_book_evaluator_task(
    engine: AsyncEngine,
    *,
    project_id: str,
    run_id: str,
    book_id: str,
    canon_id: str,
    workspace_lock_version: int,
    evaluation: BookEvaluation,
) -> tuple[str, str]:
    task_id = "evaluate-book-task"
    attempt_id = "evaluate-book-attempt"
    return await insert_successful_task(
        engine,
        project_id=project_id,
        run_id=run_id,
        task_id=task_id,
        attempt_id=attempt_id,
        role="evaluator",
        task_kind="evaluate.book",
        scope_layer="book",
        book_id=book_id,
        canon_baseline_id=canon_id,
        workspace_lock_version=workspace_lock_version,
        result=evaluation,
    )


def test_book_requires_review_and_user_approval_before_formal_baseline(
    tmp_path: Path,
) -> None:
    database = tmp_path / "book.sqlite3"
    command.upgrade(alembic_config(database), "head")

    async def exercise() -> None:
        engine = create_sqlite_async_engine(database)
        try:
            bus = CommandBus(engine)
            project_service = ProjectCommandService(bus)
            book_service = BookCommandService(bus)
            project = await project_service.create_project(
                CreateProjectRequest(
                    project_id="project-a",
                    creator_brief="一部关于记忆证词冲突的悬疑长篇。",
                    operation_mode="full_auto",
                ),
                idempotency_key="create-project",
            )
            book_candidate = BookCandidatePack(
                direction="围绕一份会改变叙述者记忆的证词展开。",
                constraints=_book_constraints(),
                selected_title="《证词回声》",
                rolling_plan=_book_rolling_plan(),
                completion_contract=_completion_contract(),
                arc_topology=_book_topology(),
            )
            unowned_topology = BookArcTopology(
                arcs=[
                    book_candidate.arc_topology.arcs[0].model_copy(
                        update={"completion_requirement_keys": []}
                    )
                ]
            )
            with pytest.raises(
                CommandPreconditionError,
                match="lack an owning Story Arc",
            ):
                await book_service.apply_candidate(
                    ApplyBookCandidateRequest(
                        project_id="project-a",
                        book_id=project.result.book_id,
                        expected_workspace_lock_version=1,
                        candidate=book_candidate.model_copy(
                            update={"arc_topology": unowned_topology}
                        ),
                    ),
                    idempotency_key="reject-unowned-completion-requirement",
                )
            candidate = await book_service.apply_candidate(
                ApplyBookCandidateRequest(
                    project_id="project-a",
                    book_id=project.result.book_id,
                    expected_workspace_lock_version=1,
                    candidate=book_candidate,
                ),
                idempotency_key="apply-candidate",
            )
            submitted = await book_service.submit_for_review(
                SubmitBookRequest(
                    project_id="project-a",
                    book_id=project.result.book_id,
                    expected_workspace_lock_version=candidate.result.workspace_lock_version,
                ),
                idempotency_key="submit-book",
            )

            async with engine.connect() as connection:
                book_before_review = (
                    await connection.execute(
                        select(books.c.lifecycle_status, books.c.current_baseline_id).where(
                            books.c.id == project.result.book_id
                        )
                    )
                ).one()
                assert tuple(book_before_review) == ("developing", None)

            wrong_task_id, wrong_attempt_id = await insert_successful_task(
                engine,
                project_id="project-a",
                run_id=project.result.generation_run_id,
                task_id="evaluate-book-wrong-coverage",
                attempt_id="evaluate-book-wrong-coverage-attempt",
                role="evaluator",
                task_kind="evaluate.book",
                scope_layer="book",
                book_id=project.result.book_id,
                canon_baseline_id=project.result.canon_baseline_id,
                workspace_lock_version=candidate.result.workspace_lock_version,
                result=BookEvaluation(
                    decision="pass",
                    summary="This result omitted the actual completion key.",
                    requirement_coverage=[
                        {
                            "requirement_key": "wrong_requirement",
                            "judgment": "aligned",
                            "rationale": "This is intentionally the wrong key.",
                        }
                    ],
                ),
            )
            with pytest.raises(
                CommandPreconditionError,
                match="exactly match the current completion contract",
            ):
                await book_service.record_review(
                    RecordBookReviewRequest(
                        project_id="project-a",
                        book_id=project.result.book_id,
                        submission_id=submitted.result.submission_id,
                        evaluator_task_id=wrong_task_id,
                        evaluator_attempt_id=wrong_attempt_id,
                        rubric_id=BOOK_EVALUATION_STRATEGY.rubric_id,
                        rubric_version=BOOK_EVALUATION_STRATEGY.rubric_version,
                        deterministic_precheck={"passed": True},
                    ),
                    idempotency_key="reject-wrong-requirement-coverage",
                )

            task_id, attempt_id = await _insert_successful_book_evaluator_task(
                engine,
                project_id="project-a",
                run_id=project.result.generation_run_id,
                book_id=project.result.book_id,
                canon_id=project.result.canon_baseline_id,
                workspace_lock_version=candidate.result.workspace_lock_version,
                evaluation=BookEvaluation(
                    decision="pass",
                    summary="方向、约束与完成合同一致，可以提交用户批准。",
                    findings=[],
                    requirement_coverage=_aligned_requirement_coverage(),
                ),
            )
            reviewed = await book_service.record_review(
                RecordBookReviewRequest(
                    project_id="project-a",
                    book_id=project.result.book_id,
                    submission_id=submitted.result.submission_id,
                    evaluator_task_id=task_id,
                    evaluator_attempt_id=attempt_id,
                    rubric_id=BOOK_EVALUATION_STRATEGY.rubric_id,
                    rubric_version=BOOK_EVALUATION_STRATEGY.rubric_version,
                    deterministic_precheck={"passed": True, "checks": ["chapter_range"]},
                ),
                idempotency_key="record-review",
            )
            assert reviewed.result.decision == "pass"

            async with engine.connect() as connection:
                assert (
                    await connection.scalar(select(func.count()).select_from(book_baselines)) == 0
                )
                assert (
                    await connection.scalar(
                        select(agent_tasks.c.delivery_state).where(agent_tasks.c.id == task_id)
                    )
                    == "applied"
                )
                assert (
                    await connection.scalar(
                        select(book_review_submissions.c.disposition).where(
                            book_review_submissions.c.id == submitted.result.submission_id
                        )
                    )
                    == "pending"
                )

            approved = await book_service.approve_and_commit(
                ApproveBookRequest(
                    project_id="project-a",
                    book_id=project.result.book_id,
                    submission_id=submitted.result.submission_id,
                    review_id=reviewed.result.review_id,
                ),
                idempotency_key="approve-book",
            )
            replayed = await book_service.approve_and_commit(
                ApproveBookRequest(
                    project_id="project-a",
                    book_id=project.result.book_id,
                    submission_id=submitted.result.submission_id,
                    review_id=reviewed.result.review_id,
                ),
                idempotency_key="approve-book",
            )
            assert approved.result.baseline_version == 1
            assert replayed.replayed
            assert replayed.result == approved.result

            async with engine.connect() as connection:
                book_after = (
                    await connection.execute(
                        select(books.c.lifecycle_status, books.c.current_baseline_id).where(
                            books.c.id == project.result.book_id
                        )
                    )
                ).one()
                assert tuple(book_after) == ("active", approved.result.baseline_id)
                assert (
                    await connection.scalar(select(func.count()).select_from(book_baselines)) == 1
                )
                assert (
                    await connection.scalar(select(func.count()).select_from(book_approvals)) == 1
                )
                assert await connection.scalar(select(func.count()).select_from(book_reviews)) == 1
                workspace = (
                    await connection.execute(
                        select(
                            book_workspaces.c.state,
                            book_workspaces.c.lock_version,
                            book_workspaces.c.base_book_baseline_id,
                        ).where(book_workspaces.c.book_id == project.result.book_id)
                    )
                ).one()
                assert tuple(workspace) == ("idle", 3, approved.result.baseline_id)
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_stale_book_workspace_cannot_overwrite_newer_candidate(tmp_path: Path) -> None:
    database = tmp_path / "stale.sqlite3"
    command.upgrade(alembic_config(database), "head")

    async def exercise() -> None:
        engine = create_sqlite_async_engine(database)
        try:
            bus = CommandBus(engine)
            project = await ProjectCommandService(bus).create_project(
                CreateProjectRequest(
                    project_id="project-a",
                    creator_brief="brief",
                    operation_mode="participatory",
                ),
                idempotency_key="create",
            )
            service = BookCommandService(bus)
            request = ApplyBookCandidateRequest(
                project_id="project-a",
                book_id=project.result.book_id,
                expected_workspace_lock_version=1,
                candidate=BookCandidatePack(
                    direction="first",
                    constraints=_book_constraints(),
                    selected_title="A",
                    rolling_plan=_book_rolling_plan(),
                    completion_contract=_completion_contract(),
                    arc_topology=_book_topology(),
                ),
            )
            await service.apply_candidate(request, idempotency_key="candidate-1")
            with pytest.raises(CommandPreconditionError, match="stale"):
                await service.apply_candidate(
                    request.model_copy(
                        update={"candidate": request.candidate.model_copy(update={"direction": "old"})}
                    ),
                    idempotency_key="candidate-stale",
                )
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_task_driven_book_loop_reaches_baseline_only_after_explicit_approval(
    tmp_path: Path,
) -> None:
    database = tmp_path / "book-loop.sqlite3"
    command.upgrade(alembic_config(database), "head")

    async def exercise() -> None:
        engine = create_sqlite_async_engine(database)
        try:
            bus = CommandBus(engine)
            service = BookCommandService(bus)
            project = await ProjectCommandService(bus).create_project(
                CreateProjectRequest(
                    project_id="project-loop",
                    creator_brief="Write a memory mystery led by one unreliable witness.",
                    operation_mode="participatory",
                ),
                idempotency_key="create-loop",
            )
            title_turn = BookDiscussionResult(
                reply="The direction is sufficiently constrained; the formal title remains.",
                direction_draft="An unreliable witness investigates who edited her memory.",
                discussion_summary="The witness and central memory conflict are fixed.",
                readiness=BookDiscussionContinue(
                    status="continue",
                    reason="The formal title must be confirmed.",
                    question="Which formal title should this novel use?",
                    suggestions=[
                        BookDiscussionSuggestion(
                            label="Echo Testimony",
                            message="Use Echo Testimony as the formal title.",
                            formal_title="Echo Testimony",
                            recommended=True,
                        ),
                        BookDiscussionSuggestion(
                            label="The Second Memory",
                            message="Use The Second Memory as the formal title.",
                            formal_title="The Second Memory",
                        ),
                    ],
                ),
            )
            await insert_successful_task(
                engine,
                project_id="project-loop",
                run_id=project.result.generation_run_id,
                task_id="book-discuss-title",
                attempt_id="book-discuss-title-attempt",
                role="book_strategist",
                task_kind="book.discuss",
                scope_layer="book",
                book_id=project.result.book_id,
                canon_baseline_id=project.result.canon_baseline_id,
                workspace_lock_version=1,
                result=title_turn,
            )
            discussed = await service.apply_discussion_result(
                ApplyBookDiscussionTaskRequest(
                    project_id="project-loop",
                    book_id=project.result.book_id,
                    task_id="book-discuss-title",
                    attempt_id="book-discuss-title-attempt",
                    expected_workspace_lock_version=1,
                ),
                idempotency_key="apply-title-turn",
            )
            assert discussed.result.workspace_lock_version == 2

            async with bus.read_unit_of_work() as session:
                workspace = await session.books.get_workspace(
                    project_id="project-loop",
                    book_id=project.result.book_id,
                )
                assert workspace is not None
                state = BookDiscussionState.model_validate_json(
                    (
                        await session.content.get_packed(
                            project_id="project-loop",
                            ref_id=workspace.discussion_state_ref_id,
                        )
                    ).unpack_and_verify()
                )
            selected = state.suggestions[0]
            answered = await service.record_user_input(
                RecordBookUserInputRequest(
                    project_id="project-loop",
                    book_id=project.result.book_id,
                    expected_workspace_lock_version=2,
                    message=selected.message,
                    suggestion_id=selected.id,
                ),
                idempotency_key="select-title",
            )
            assert answered.result.selected_title == "Echo Testimony"
            assert answered.result.workspace_lock_version == 3

            ready_turn = BookDiscussionResult(
                reply="The direction and formal title are ready for synthesis.",
                direction_draft="An unreliable witness investigates who edited her memory.",
                discussion_summary="The Book direction and title are confirmed.",
                readiness=BookDiscussionReady(
                    status="ready",
                    reason="All Book-level decisions converged.",
                ),
            )
            await insert_successful_task(
                engine,
                project_id="project-loop",
                run_id=project.result.generation_run_id,
                task_id="book-discuss-ready",
                attempt_id="book-discuss-ready-attempt",
                role="book_strategist",
                task_kind="book.discuss",
                scope_layer="book",
                book_id=project.result.book_id,
                canon_baseline_id=project.result.canon_baseline_id,
                workspace_lock_version=3,
                result=ready_turn,
            )
            ready = await service.apply_discussion_result(
                ApplyBookDiscussionTaskRequest(
                    project_id="project-loop",
                    book_id=project.result.book_id,
                    task_id="book-discuss-ready",
                    attempt_id="book-discuss-ready-attempt",
                    expected_workspace_lock_version=3,
                ),
                idempotency_key="apply-ready-turn",
            )
            assert ready.result.readiness_status == "ready"
            assert ready.result.workspace_lock_version == 4

            candidate = BookCandidatePack(
                direction="An unreliable witness investigates who edited her memory.",
                constraints=_book_constraints(),
                selected_title="Echo Testimony",
                rolling_plan=_book_rolling_plan(),
                completion_contract=_completion_contract(),
                arc_topology=_book_topology(),
            )
            await insert_successful_task(
                engine,
                project_id="project-loop",
                run_id=project.result.generation_run_id,
                task_id="book-synthesize",
                attempt_id="book-synthesize-attempt",
                role="book_strategist",
                task_kind="book.synthesize",
                scope_layer="book",
                book_id=project.result.book_id,
                canon_baseline_id=project.result.canon_baseline_id,
                workspace_lock_version=4,
                result=candidate,
            )
            applied = await service.apply_candidate_result(
                ApplyBookCandidateTaskRequest(
                    project_id="project-loop",
                    book_id=project.result.book_id,
                    task_id="book-synthesize",
                    attempt_id="book-synthesize-attempt",
                    expected_workspace_lock_version=4,
                ),
                idempotency_key="apply-synthesis",
            )
            assert applied.result.workspace_lock_version == 5
            submitted = await service.submit_for_review(
                SubmitBookRequest(
                    project_id="project-loop",
                    book_id=project.result.book_id,
                    expected_workspace_lock_version=5,
                ),
                idempotency_key="submit-loop-book",
            )
            await insert_successful_task(
                engine,
                project_id="project-loop",
                run_id=project.result.generation_run_id,
                task_id="evaluate-loop-book",
                attempt_id="evaluate-loop-book-attempt",
                role="evaluator",
                task_kind="evaluate.book",
                scope_layer="book",
                book_id=project.result.book_id,
                canon_baseline_id=project.result.canon_baseline_id,
                workspace_lock_version=5,
                result=BookEvaluation(
                    decision="pass",
                    summary="The candidate is coherent and satisfies its contract.",
                    requirement_coverage=_aligned_requirement_coverage(),
                ),
            )
            reviewed = await service.record_review(
                RecordBookReviewRequest(
                    project_id="project-loop",
                    book_id=project.result.book_id,
                    submission_id=submitted.result.submission_id,
                    evaluator_task_id="evaluate-loop-book",
                    evaluator_attempt_id="evaluate-loop-book-attempt",
                    rubric_id=BOOK_EVALUATION_STRATEGY.rubric_id,
                    rubric_version=BOOK_EVALUATION_STRATEGY.rubric_version,
                    deterministic_precheck={"passed": True},
                ),
                idempotency_key="review-loop-book",
            )
            async with engine.connect() as connection:
                assert await connection.scalar(select(func.count()).select_from(book_baselines)) == 0

            approved = await service.approve_and_commit(
                ApproveBookRequest(
                    project_id="project-loop",
                    book_id=project.result.book_id,
                    submission_id=submitted.result.submission_id,
                    review_id=reviewed.result.review_id,
                ),
                idempotency_key="approve-loop-book",
            )
            assert approved.result.approved_title == "Echo Testimony"
            assert approved.result.baseline_version == 1
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_late_book_discussion_result_is_discarded_without_overwriting_user_input(
    tmp_path: Path,
) -> None:
    database = tmp_path / "stale-book-task.sqlite3"
    command.upgrade(alembic_config(database), "head")

    async def exercise() -> None:
        engine = create_sqlite_async_engine(database)
        try:
            bus = CommandBus(engine)
            service = BookCommandService(bus)
            project = await ProjectCommandService(bus).create_project(
                CreateProjectRequest(
                    project_id="project-stale-task",
                    creator_brief="Write a mystery.",
                    operation_mode="full_auto",
                ),
                idempotency_key="create-stale-task",
            )
            result = BookDiscussionResult(
                reply="The protagonist boundary controls the investigation structure.",
                direction_draft="A detective investigates a memory conspiracy.",
                discussion_summary="The mystery needs one protagonist boundary.",
                readiness=BookDiscussionContinue(
                    status="continue",
                    reason="Identity is open.",
                    question="Is the detective also the altered-memory witness?",
                    suggestions=[
                        BookDiscussionSuggestion(
                            label="Same person",
                            message="The detective is the altered-memory witness.",
                        ),
                        BookDiscussionSuggestion(
                            label="Separate witness",
                            message="The detective protects a separate altered-memory witness.",
                        ),
                    ],
                ),
            )
            await insert_successful_task(
                engine,
                project_id="project-stale-task",
                run_id=project.result.generation_run_id,
                task_id="late-discussion",
                attempt_id="late-discussion-attempt",
                role="book_strategist",
                task_kind="book.discuss",
                scope_layer="book",
                book_id=project.result.book_id,
                canon_baseline_id=project.result.canon_baseline_id,
                workspace_lock_version=1,
                result=result,
            )
            user_input = await service.record_user_input(
                RecordBookUserInputRequest(
                    project_id="project-stale-task",
                    book_id=project.result.book_id,
                    expected_workspace_lock_version=1,
                    message="Make the detective and witness separate people.",
                ),
                idempotency_key="newer-user-input",
            )
            assert user_input.result.workspace_lock_version == 2

            delivery = await service.apply_discussion_result(
                ApplyBookDiscussionTaskRequest(
                    project_id="project-stale-task",
                    book_id=project.result.book_id,
                    task_id="late-discussion",
                    attempt_id="late-discussion-attempt",
                    expected_workspace_lock_version=1,
                ),
                idempotency_key="discard-late-discussion",
            )
            assert delivery.result.delivery == "discarded_stale"
            assert delivery.result.workspace_lock_version == 2
            async with engine.connect() as connection:
                assert (
                    await connection.scalar(
                        select(agent_tasks.c.delivery_state).where(
                            agent_tasks.c.id == "late-discussion"
                        )
                    )
                    == "discarded_stale"
                )
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_book_local_repair_is_scope_bounded_and_second_review_failure_pauses_run(
    tmp_path: Path,
) -> None:
    database = tmp_path / "book-repair.sqlite3"
    command.upgrade(alembic_config(database), "head")

    async def exercise() -> None:
        engine = create_sqlite_async_engine(database)
        try:
            bus = CommandBus(engine)
            service = BookCommandService(bus)
            project = await ProjectCommandService(bus).create_project(
                CreateProjectRequest(
                    project_id="project-repair",
                    creator_brief=(
                        "Write a memory mystery and use Echo Testimony as its formal title."
                    ),
                    operation_mode="full_auto",
                ),
                idempotency_key="create-repair-project",
            )
            await RunControlService(bus).start(
                RunControlRequest(
                    project_id="project-repair",
                    run_id=project.result.generation_run_id,
                    expected_lock_version=1,
                ),
                idempotency_key="start-repair-run",
            )
            ready_turn = BookDiscussionResult(
                reply="The delegated direction and explicit formal title are ready.",
                direction_draft="A witness investigates the deliberate editing of her memory.",
                discussion_summary="The Book direction and formal title are explicit.",
                newly_selected_title="Echo Testimony",
                readiness=BookDiscussionReady(
                    status="ready",
                    reason="The creator brief directly resolves the Book contract.",
                ),
            )
            await insert_successful_task(
                engine,
                project_id="project-repair",
                run_id=project.result.generation_run_id,
                task_id="repair-book-discussion",
                attempt_id="repair-book-discussion-attempt",
                role="book_strategist",
                task_kind="book.discuss",
                scope_layer="book",
                book_id=project.result.book_id,
                canon_baseline_id=project.result.canon_baseline_id,
                workspace_lock_version=1,
                result=ready_turn,
            )
            await service.apply_discussion_result(
                ApplyBookDiscussionTaskRequest(
                    project_id="project-repair",
                    book_id=project.result.book_id,
                    task_id="repair-book-discussion",
                    attempt_id="repair-book-discussion-attempt",
                    expected_workspace_lock_version=1,
                ),
                idempotency_key="apply-repair-book-discussion",
            )
            original = BookCandidatePack(
                direction="A witness investigates the deliberate editing of her memory.",
                constraints=_book_constraints(),
                selected_title="Echo Testimony",
                rolling_plan=_book_rolling_plan(),
                completion_contract=_completion_contract(),
                arc_topology=_book_topology(),
            )
            await insert_successful_task(
                engine,
                project_id="project-repair",
                run_id=project.result.generation_run_id,
                task_id="repair-book-synthesis",
                attempt_id="repair-book-synthesis-attempt",
                role="book_strategist",
                task_kind="book.synthesize",
                scope_layer="book",
                book_id=project.result.book_id,
                canon_baseline_id=project.result.canon_baseline_id,
                workspace_lock_version=2,
                result=original,
            )
            synthesized = await service.apply_candidate_result(
                ApplyBookCandidateTaskRequest(
                    project_id="project-repair",
                    book_id=project.result.book_id,
                    task_id="repair-book-synthesis",
                    attempt_id="repair-book-synthesis-attempt",
                    expected_workspace_lock_version=2,
                ),
                idempotency_key="apply-repair-book-synthesis",
            )
            assert synthesized.result.workspace_lock_version == 3
            first_submission = await service.submit_for_review(
                SubmitBookRequest(
                    project_id="project-repair",
                    book_id=project.result.book_id,
                    expected_workspace_lock_version=3,
                ),
                idempotency_key="submit-book-before-repair",
            )
            local_repair = BookEvaluation(
                decision="local_repair",
                summary="The causal direction needs one precise repair.",
                findings=[
                    BookEvaluationIssue(
                        kind="contract_unfulfilled",
                        code="causal_direction_incomplete",
                        subject="Book causal direction",
                        summary=(
                            "The candidate does not explain how the witness can "
                            "detect the memory edit."
                        ),
                        evidence=[
                            "The candidate direction names detection without a causal mechanism."
                        ],
                        contract_item=(
                            "The Book direction must provide a usable causal story engine."
                        ),
                        observed_components=["direction"],
                    )
                ],
                requirement_coverage=_aligned_requirement_coverage(),
            )
            await insert_successful_task(
                engine,
                project_id="project-repair",
                run_id=project.result.generation_run_id,
                task_id="evaluate-book-for-repair",
                attempt_id="evaluate-book-for-repair-attempt",
                role="evaluator",
                task_kind="evaluate.book",
                scope_layer="book",
                book_id=project.result.book_id,
                canon_baseline_id=project.result.canon_baseline_id,
                workspace_lock_version=3,
                result=local_repair,
            )
            reviewed = await service.record_review(
                RecordBookReviewRequest(
                    project_id="project-repair",
                    book_id=project.result.book_id,
                    submission_id=first_submission.result.submission_id,
                    evaluator_task_id="evaluate-book-for-repair",
                    evaluator_attempt_id="evaluate-book-for-repair-attempt",
                    rubric_id=BOOK_EVALUATION_STRATEGY.rubric_id,
                    rubric_version=BOOK_EVALUATION_STRATEGY.rubric_version,
                    deterministic_precheck={"passed": True},
                ),
                idempotency_key="record-book-local-repair",
            )
            assert reviewed.result.decision == "local_repair"

            repaired_candidate = BookRepairPatch(
                changes=[
                    BookDirectionRepair(
                        component="direction",
                        value=(
                        "A witness detects a memory edit through an impossible timestamp, "
                            "then investigates who altered her testimony."
                        ),
                    ),
                    BookConstraintsRepair(
                        component="constraints",
                        value=BookCreativeConstraints(
                            genre_reader_promise=original.constraints.genre_reader_promise,
                            premise_story_engine=(
                                "An impossible physical timestamp exposes rewritten memory "
                                "and drives the investigation."
                            ),
                            stable_world_invariants=(
                                original.constraints.stable_world_invariants
                            ),
                            stable_character_invariants=(
                                original.constraints.stable_character_invariants
                            ),
                            core_selling_points=original.constraints.core_selling_points,
                            prohibited_outcomes=original.constraints.prohibited_outcomes,
                        ),
                    ),
                ]
            )
            await insert_successful_task(
                engine,
                project_id="project-repair",
                run_id=project.result.generation_run_id,
                task_id="authorized-book-repair",
                attempt_id="authorized-book-repair-attempt",
                role="book_strategist",
                task_kind="book.repair",
                scope_layer="book",
                book_id=project.result.book_id,
                canon_baseline_id=project.result.canon_baseline_id,
                workspace_lock_version=4,
                result=repaired_candidate,
            )
            repaired = await service.apply_candidate_result(
                ApplyBookCandidateTaskRequest(
                    project_id="project-repair",
                    book_id=project.result.book_id,
                    task_id="authorized-book-repair",
                    attempt_id="authorized-book-repair-attempt",
                    expected_workspace_lock_version=4,
                ),
                idempotency_key="apply-authorized-book-repair",
            )
            assert repaired.result.workspace_lock_version == 5
            async with engine.connect() as connection:
                workspace = (
                    await connection.execute(
                        select(
                            book_workspaces.c.semantic_repair_count,
                            book_workspaces.c.candidate_constraints_ref_id,
                            book_workspaces.c.candidate_titles_ref_id,
                            book_workspaces.c.candidate_rolling_plan_ref_id,
                            book_workspaces.c.candidate_completion_contract_ref_id,
                        ).where(book_workspaces.c.book_id == project.result.book_id)
                    )
                ).one()
                assert workspace.semantic_repair_count == 1
                repository = ContentRepository(connection)
                assert workspace.candidate_titles_ref_id is not None
                title_payload = json.loads(
                    (
                        await repository.get_packed(
                            project_id="project-repair",
                            ref_id=workspace.candidate_titles_ref_id,
                        )
                    ).unpack_and_verify()
                )
                preserved = []
                for ref_id in (
                    workspace.candidate_constraints_ref_id,
                    workspace.candidate_rolling_plan_ref_id,
                    workspace.candidate_completion_contract_ref_id,
                ):
                    assert ref_id is not None
                    packed = await repository.get_packed(
                        project_id="project-repair",
                        ref_id=ref_id,
                    )
                    preserved.append(json.loads(packed.unpack_and_verify()))
            assert preserved[0] == repaired_candidate.changes[1].value.model_dump(
                mode="json"
            )
            assert preserved[1:] == [
                original.rolling_plan.model_dump(mode="json"),
                original.completion_contract.model_dump(mode="json"),
            ]
            assert title_payload["selected_title"] == original.selected_title

            exhausted_submission = await service.submit_for_review(
                SubmitBookRequest(
                    project_id="project-repair",
                    book_id=project.result.book_id,
                    expected_workspace_lock_version=5,
                ),
                idempotency_key="submit-exhausted-book",
            )
            await insert_successful_task(
                engine,
                project_id="project-repair",
                run_id=project.result.generation_run_id,
                task_id="evaluate-exhausted-book",
                attempt_id="evaluate-exhausted-book-attempt",
                role="evaluator",
                task_kind="verify_repair.book",
                scope_layer="book",
                book_id=project.result.book_id,
                canon_baseline_id=project.result.canon_baseline_id,
                workspace_lock_version=5,
                result=local_repair,
            )
            await service.record_review(
                RecordBookReviewRequest(
                    project_id="project-repair",
                    book_id=project.result.book_id,
                    submission_id=exhausted_submission.result.submission_id,
                    evaluator_task_id="evaluate-exhausted-book",
                    evaluator_attempt_id="evaluate-exhausted-book-attempt",
                    rubric_id=BOOK_REPAIR_EVALUATION_STRATEGY.rubric_id,
                    rubric_version=BOOK_REPAIR_EVALUATION_STRATEGY.rubric_version,
                    deterministic_precheck={"passed": True},
                ),
                idempotency_key="pause-exhausted-book",
            )
            async with engine.connect() as connection:
                run = (
                    await connection.execute(
                        select(
                            generation_runs.c.status,
                            generation_runs.c.desired_state,
                            generation_runs.c.failure_code,
                            generation_runs.c.blocking_task_id,
                        ).where(generation_runs.c.id == project.result.generation_run_id)
                    )
                ).one()
                assert tuple(run) == (
                    "failure_paused",
                    "paused",
                    "semantic_repair_exhausted",
                    "evaluate-exhausted-book",
                )
        finally:
            await engine.dispose()

    asyncio.run(exercise())
