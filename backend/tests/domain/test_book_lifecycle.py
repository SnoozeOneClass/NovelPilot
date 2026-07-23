from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from app.agents.contracts import (
    BookDiscussionContinue,
    BookDiscussionReady,
    BookDiscussionResult,
    BookDiscussionSuggestion,
)
from app.db.engine import create_sqlite_async_engine
from app.db.maintenance import alembic_config
from app.db.schema import (
    agent_task_attempts,
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
    BookCandidatePack,
    BookDiscussionState,
    BookEvaluation,
    BookRepairContract,
    CompletionContract,
    RecordBookReviewRequest,
    RecordBookUserInputRequest,
    SubmitBookRequest,
)
from app.domain.commands import CommandPreconditionError
from app.domain.projects import CreateProjectRequest, ProjectCommandService
from app.runtime.control import RunControlRequest, RunControlService
from app.store.command_bus import CommandBus
from app.store.content import ContentRepository, prepare_canonical_json
from tests.helpers.lifecycle_seed import insert_successful_task


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
    prepared = prepare_canonical_json(evaluation)
    async with engine.begin() as connection:
        result_ref = await ContentRepository(connection).put(
            project_id=project_id,
            prepared=prepared,
            semantic_kind="agent.typed_result",
            media_type="application/json",
            schema_id="book-evaluation",
            schema_version=1,
            created_at_ms=20,
        )
        await connection.execute(
            agent_tasks.insert().values(
                id=task_id,
                project_id=project_id,
                run_id=run_id,
                task_key="evaluate.book:first-submission",
                action_key="evaluate.book",
                role="evaluator",
                task_kind="evaluate.book",
                scope_layer="book",
                book_id=book_id,
                workspace_lock_version=workspace_lock_version,
                canon_baseline_id=canon_id,
                task_plan_ref_id=result_ref.id,
                input_manifest_ref_id=result_ref.id,
                input_messages_ref_id=result_ref.id,
                profile_snapshot_ref_id=result_ref.id,
                input_fingerprint=prepared.sha256,
                prompt_fingerprint=prepared.sha256,
                context_policy_id="book-evaluator-context-v1",
                context_policy_version=1,
                context_policy_fingerprint=prepared.sha256,
                output_schema_id="book-evaluation",
                output_schema_version=1,
                output_schema_fingerprint=prepared.sha256,
                rubric_id="book-rubric",
                rubric_version=1,
                harness_policy_id="novelpilot-domain-harness",
                harness_policy_version=1,
                profile_id="fixture-profile",
                profile_fingerprint=prepared.sha256,
                api_family="openai_responses",
                model_id="fixture-model",
                output_mode="native_json_schema",
                requires_native_json_schema=1,
                requires_text_streaming=0,
                transport_retry_limit=5,
                model_request_limit=2,
                connect_timeout_ms=10_000,
                pool_timeout_ms=10_000,
                write_timeout_ms=60_000,
                read_timeout_ms=600_000,
                activation_timeout_ms=1_800_000,
                timeout_policy_id="provider-timeout-t1-v1",
                status="succeeded",
                successful_attempt_id=attempt_id,
                delivery_state="pending",
                created_at_ms=20,
                updated_at_ms=20,
            )
        )
        await connection.execute(
            agent_task_attempts.insert().values(
                id=attempt_id,
                project_id=project_id,
                task_id=task_id,
                attempt_number=1,
                retry_kind="initial",
                status="succeeded",
                framework_fingerprint=prepared.sha256,
                provider_request_count=1,
                transport_retry_count=0,
                model_request_count=1,
                input_tokens=10,
                output_tokens=5,
                total_tokens=15,
                result_ref_id=result_ref.id,
                created_at_ms=20,
                started_at_ms=20,
                finished_at_ms=21,
            )
        )
    return task_id, attempt_id


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
            candidate = await book_service.apply_candidate(
                ApplyBookCandidateRequest(
                    project_id="project-a",
                    book_id=project.result.book_id,
                    expected_workspace_lock_version=1,
                    candidate=BookCandidatePack(
                        direction="围绕一份会改变叙述者记忆的证词展开。",
                        constraints={"pov": "limited-third", "tone": "suspense"},
                        selected_title="《证词回声》",
                        rolling_plan={"strategy": "plan-one-arc-at-a-time"},
                        completion_contract=CompletionContract(
                            minimum_chapter_count=18,
                            maximum_chapter_count=22,
                            completion_requirements=["主谜题闭合", "人物选择产生后果"],
                        ),
                    ),
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
                ),
            )
            reviewed = await book_service.record_review(
                RecordBookReviewRequest(
                    project_id="project-a",
                    book_id=project.result.book_id,
                    submission_id=submitted.result.submission_id,
                    evaluator_task_id=task_id,
                    evaluator_attempt_id=attempt_id,
                    rubric_id="book-rubric",
                    rubric_version=1,
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
                    constraints={},
                    selected_title="A",
                    rolling_plan={},
                    completion_contract=CompletionContract(
                        minimum_chapter_count=18,
                        maximum_chapter_count=22,
                    ),
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
                constraints={"pov": "limited-third", "tone": "suspense"},
                selected_title="Echo Testimony",
                rolling_plan={"strategy": "one-arc-at-a-time"},
                completion_contract=CompletionContract(
                    minimum_chapter_count=18,
                    maximum_chapter_count=22,
                    completion_requirements=["Resolve the central memory conflict"],
                ),
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
                ),
            )
            reviewed = await service.record_review(
                RecordBookReviewRequest(
                    project_id="project-loop",
                    book_id=project.result.book_id,
                    submission_id=submitted.result.submission_id,
                    evaluator_task_id="evaluate-loop-book",
                    evaluator_attempt_id="evaluate-loop-book-attempt",
                    rubric_id="book-rubric-v1",
                    rubric_version=1,
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


def test_book_local_repair_is_scope_bounded_and_sixth_cycle_failure_pauses_run(
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
                constraints={"pov": "limited-third"},
                selected_title="Echo Testimony",
                rolling_plan={"strategy": "one-arc-at-a-time"},
                completion_contract=CompletionContract(
                    minimum_chapter_count=18,
                    maximum_chapter_count=22,
                ),
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
                repair_contract=BookRepairContract(
                    authorized_components=["direction"],
                    issue_summary="Clarify why the witness can detect the memory edit.",
                ),
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
                    rubric_id="book-rubric-v1",
                    rubric_version=1,
                    deterministic_precheck={"passed": True},
                ),
                idempotency_key="record-book-local-repair",
            )
            assert reviewed.result.decision == "local_repair"

            unauthorized = original.model_copy(
                update={"constraints": {"pov": "first-person"}}
            )
            await insert_successful_task(
                engine,
                project_id="project-repair",
                run_id=project.result.generation_run_id,
                task_id="unauthorized-book-repair",
                attempt_id="unauthorized-book-repair-attempt",
                role="book_strategist",
                task_kind="book.repair",
                scope_layer="book",
                book_id=project.result.book_id,
                canon_baseline_id=project.result.canon_baseline_id,
                workspace_lock_version=4,
                result=unauthorized,
            )
            with pytest.raises(CommandPreconditionError, match="unauthorized"):
                await service.apply_candidate_result(
                    ApplyBookCandidateTaskRequest(
                        project_id="project-repair",
                        book_id=project.result.book_id,
                        task_id="unauthorized-book-repair",
                        attempt_id="unauthorized-book-repair-attempt",
                        expected_workspace_lock_version=4,
                    ),
                    idempotency_key="reject-unauthorized-book-repair",
                )

            repaired_candidate = original.model_copy(
                update={
                    "direction": (
                        "A witness detects a memory edit through an impossible timestamp, "
                        "then investigates who altered her testimony."
                    )
                }
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
                assert (
                    await connection.scalar(
                        select(book_workspaces.c.semantic_repair_count).where(
                            book_workspaces.c.book_id == project.result.book_id
                        )
                    )
                    == 1
                )

            exhausted_submission = await service.submit_for_review(
                SubmitBookRequest(
                    project_id="project-repair",
                    book_id=project.result.book_id,
                    expected_workspace_lock_version=5,
                ),
                idempotency_key="submit-exhausted-book",
            )
            async with engine.begin() as connection:
                await connection.execute(
                    update(book_workspaces)
                    .where(book_workspaces.c.book_id == project.result.book_id)
                    .values(semantic_repair_count=5)
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
                    rubric_id="book-rubric-v1",
                    rubric_version=1,
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
