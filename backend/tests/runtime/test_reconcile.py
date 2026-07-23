from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.agents.contracts import ProfileCapabilities, ProfileSnapshot
from app.agents.registry import DEFAULT_TASK_REGISTRY
from app.db.engine import create_sqlite_async_engine
from app.db.maintenance import alembic_config
from app.db.schema import agent_task_attempts, agent_tasks, generation_runs
from app.db.uow import UnitOfWork
from app.domain.commands import CommandPreconditionError
from app.domain.projects import CreateProjectRequest, ProjectCommandService
from app.runtime.control import (
    RetryFailedTaskRequest,
    RunControlRequest,
    RunControlService,
)
from app.runtime.reconcile import ReconcileService
from app.store.agent_tasks import AgentTaskStore
from app.store.command_bus import CommandBus
from app.store.content import prepare_canonical_json


@dataclass(frozen=True, slots=True)
class SeededTask:
    project_id: str
    run_id: str
    task_id: str
    attempt_id: str


async def _seed_task(engine: AsyncEngine, *, suffix: str = "a") -> SeededTask:
    project_id = f"project-{suffix}"
    created = await ProjectCommandService(CommandBus(engine)).create_project(
        CreateProjectRequest(
            project_id=project_id,
            creator_brief="A mystery whose witnesses remember incompatible histories.",
            operation_mode="full_auto",
        ),
        idempotency_key=f"create-{suffix}",
    )
    await RunControlService(CommandBus(engine), now_ms=lambda: 10).start(
        RunControlRequest(
            project_id=project_id,
            run_id=created.result.generation_run_id,
            expected_lock_version=1,
        ),
        idempotency_key=f"start-{suffix}",
    )
    profile = ProfileSnapshot.create(
        profile_id="test-profile",
        display_name="Test profile",
        api_family="openai_responses",
        base_url="https://provider.example/v1",
        model_id="opaque-test-model",
        capabilities=ProfileCapabilities(
            text_streaming=True,
            native_json_schema=True,
        ),
    )
    task_id = f"task-{suffix}"
    attempt_id = f"attempt-{suffix}"
    plan = DEFAULT_TASK_REGISTRY.freeze_plan(
        task_id=task_id,
        project_id=project_id,
        run_id=created.result.generation_run_id,
        task_key=f"evaluate.book:{suffix}",
        action_key="evaluate.book",
        role="evaluator",
        task_kind="evaluate.book",
        contract_version=1,
        book_id=created.result.book_id,
        canon_baseline_id=created.result.canon_baseline_id,
        semantic_goal="Evaluate a frozen Book candidate.",
        prompt="Evaluate this frozen Book candidate.",
        context_manifest={"candidate": {"direction": "Contradictory memory."}},
        profile_snapshot=profile,
        workspace_lock_version=1,
    )
    await AgentTaskStore(engine).create_initial(
        plan=plan,
        attempt_id=attempt_id,
        created_at_ms=20,
    )
    return SeededTask(project_id, created.result.generation_run_id, task_id, attempt_id)


async def _mark_running(
    engine: AsyncEngine,
    seed: SeededTask,
    *,
    attempt_id: str,
    lease_expires_at_ms: int,
) -> None:
    async with UnitOfWork(engine, begin_mode="IMMEDIATE") as session:
        claimed = await session.execution.mark_attempt_running(
            project_id=seed.project_id,
            task_id=seed.task_id,
            attempt_id=attempt_id,
            owner_instance_id="dead-engine",
            lease_token=f"lease:{attempt_id}",
            lease_expires_at_ms=lease_expires_at_ms,
            activation_deadline_at_ms=1_800_000,
            started_at_ms=30,
        )
        assert claimed


def test_c1_creates_one_crash_replay_then_failure_pauses(tmp_path: Path) -> None:
    database = tmp_path / "c1.sqlite3"
    command.upgrade(alembic_config(database), "head")

    async def exercise() -> None:
        engine = create_sqlite_async_engine(database)
        try:
            seed = await _seed_task(engine)
            await _mark_running(
                engine,
                seed,
                attempt_id=seed.attempt_id,
                lease_expires_at_ms=50,
            )
            first = await ReconcileService(
                engine,
                CommandBus(engine),
                id_factory=iter(("replay-a", "command-a")).__next__,
                now_ms=lambda: 100,
            ).reconcile()
            assert first.crash_replays_created == 1
            assert first.tasks_failure_paused == 0

            async with engine.connect() as connection:
                attempts = list(
                    (
                        await connection.execute(
                            select(
                                agent_task_attempts.c.id,
                                agent_task_attempts.c.retry_kind,
                                agent_task_attempts.c.status,
                            )
                            .where(agent_task_attempts.c.task_id == seed.task_id)
                            .order_by(agent_task_attempts.c.attempt_number)
                        )
                    ).all()
                )
                task_state = await connection.scalar(
                    select(agent_tasks.c.status).where(agent_tasks.c.id == seed.task_id)
                )
            assert attempts == [
                (seed.attempt_id, "initial", "interrupted"),
                ("replay-a", "crash_replay", "queued"),
            ]
            assert task_state == "queued"

            # Reconciliation is monotonic and cannot create another replay for the same fact.
            repeated = await ReconcileService(
                engine,
                CommandBus(engine),
                now_ms=lambda: 100,
            ).reconcile()
            assert repeated.crash_replays_created == 0

            await _mark_running(
                engine,
                seed,
                attempt_id="replay-a",
                lease_expires_at_ms=150,
            )
            second = await ReconcileService(
                engine,
                CommandBus(engine),
                now_ms=lambda: 200,
            ).reconcile()
            assert second.tasks_failure_paused == 1

            async with engine.connect() as connection:
                run = (
                    await connection.execute(
                        select(
                            generation_runs.c.status,
                            generation_runs.c.blocking_task_id,
                            generation_runs.c.failure_code,
                        ).where(generation_runs.c.id == seed.run_id)
                    )
                ).one()
                task_state = await connection.scalar(
                    select(agent_tasks.c.status).where(agent_tasks.c.id == seed.task_id)
                )
                attempt_count = await connection.scalar(
                    select(func.count())
                    .select_from(agent_task_attempts)
                    .where(agent_task_attempts.c.task_id == seed.task_id)
                )
            assert tuple(run) == (
                "failure_paused",
                seed.task_id,
                "crash_replay_exhausted",
            )
            assert task_state == "failed"
            assert attempt_count == 2
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_pause_during_abandoned_attempt_settles_after_replay_is_queued(
    tmp_path: Path,
) -> None:
    database = tmp_path / "pause-reconcile.sqlite3"
    command.upgrade(alembic_config(database), "head")

    async def exercise() -> None:
        engine = create_sqlite_async_engine(database)
        try:
            seed = await _seed_task(engine, suffix="pause")
            await _mark_running(
                engine,
                seed,
                attempt_id=seed.attempt_id,
                lease_expires_at_ms=50,
            )
            paused = await RunControlService(CommandBus(engine), now_ms=lambda: 60).pause(
                RunControlRequest(
                    project_id=seed.project_id,
                    run_id=seed.run_id,
                    expected_lock_version=2,
                ),
                idempotency_key="pause-active",
            )
            assert paused.result.status == "pause_requested"

            report = await ReconcileService(
                engine,
                CommandBus(engine),
                now_ms=lambda: 100,
            ).reconcile()
            assert report.crash_replays_created == 1
            async with engine.connect() as connection:
                run_state = await connection.scalar(
                    select(generation_runs.c.status).where(generation_runs.c.id == seed.run_id)
                )
                queued = await connection.scalar(
                    select(func.count())
                    .select_from(agent_task_attempts)
                    .where(
                        agent_task_attempts.c.task_id == seed.task_id,
                        agent_task_attempts.c.retry_kind == "crash_replay",
                        agent_task_attempts.c.status == "queued",
                    )
                )
            assert run_state == "paused"
            assert queued == 1
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_complete_result_is_not_replayed_and_failed_task_requires_retry_command(
    tmp_path: Path,
) -> None:
    database = tmp_path / "terminal-reconcile.sqlite3"
    command.upgrade(alembic_config(database), "head")

    async def exercise() -> None:
        engine = create_sqlite_async_engine(database)
        try:
            succeeded = await _seed_task(engine, suffix="success")
            await _mark_running(
                engine,
                succeeded,
                attempt_id=succeeded.attempt_id,
                lease_expires_at_ms=50,
            )
            async with UnitOfWork(engine, begin_mode="IMMEDIATE") as session:
                usage = await session.content.put(
                    project_id=succeeded.project_id,
                    prepared=prepare_canonical_json({"requests": 1}),
                    semantic_kind="agent_usage",
                    media_type="application/json",
                )
                result = await session.content.put(
                    project_id=succeeded.project_id,
                    prepared=prepare_canonical_json({"decision": "pass"}),
                    semantic_kind="agent_typed_result",
                    media_type="application/json",
                )
                assert await session.execution.complete_attempt_success(
                    project_id=succeeded.project_id,
                    task_id=succeeded.task_id,
                    attempt_id=succeeded.attempt_id,
                    provider_request_count=1,
                    transport_retry_count=0,
                    model_request_count=1,
                    input_tokens=1,
                    output_tokens=1,
                    usage_ref_id=usage.id,
                    result_ref_id=result.id,
                    finished_at_ms=70,
                )
            report = await ReconcileService(
                engine,
                CommandBus(engine),
                now_ms=lambda: 100,
            ).reconcile()
            assert report.crash_replays_created == 0
            async with engine.connect() as connection:
                success_row = (
                    await connection.execute(
                        select(agent_tasks.c.status, agent_tasks.c.delivery_state).where(
                            agent_tasks.c.id == succeeded.task_id
                        )
                    )
                ).one()
            assert tuple(success_row) == ("succeeded", "pending")

            failed = await _seed_task(engine, suffix="failed")
            await _mark_running(
                engine,
                failed,
                attempt_id=failed.attempt_id,
                lease_expires_at_ms=150,
            )
            async with UnitOfWork(engine, begin_mode="IMMEDIATE") as session:
                error = await session.content.put(
                    project_id=failed.project_id,
                    prepared=prepare_canonical_json(
                        {"code": "provider_quota", "message": "quota exhausted"}
                    ),
                    semantic_kind="agent_error_summary",
                    media_type="application/json",
                )
                assert await session.execution.complete_attempt_failure(
                    project_id=failed.project_id,
                    task_id=failed.task_id,
                    attempt_id=failed.attempt_id,
                    provider_request_count=1,
                    transport_retry_count=0,
                    model_request_count=1,
                    error_code="provider_quota",
                    error_category="quota",
                    http_status=429,
                    error_ref_id=error.id,
                    diagnostic_ref_id=None,
                    input_tokens=None,
                    output_tokens=None,
                    usage_ref_id=None,
                    finished_at_ms=170,
                )
            report = await ReconcileService(
                engine,
                CommandBus(engine),
                now_ms=lambda: 200,
            ).reconcile()
            assert report.tasks_failure_paused == 1

            async with engine.connect() as connection:
                run = (
                    await connection.execute(
                        select(
                            generation_runs.c.status,
                            generation_runs.c.lock_version,
                        ).where(generation_runs.c.id == failed.run_id)
                    )
                ).one()
            assert run[0] == "failure_paused"
            with pytest.raises(CommandPreconditionError, match="dedicated explicit Retry"):
                await RunControlService(CommandBus(engine)).resume(
                    RunControlRequest(
                        project_id=failed.project_id,
                        run_id=failed.run_id,
                        expected_lock_version=run[1],
                    ),
                    idempotency_key="invalid-resume",
                )
            retried = await RunControlService(
                CommandBus(engine),
                id_factory=iter(("user-retry-attempt", "retry-command")).__next__,
                now_ms=lambda: 210,
            ).retry_failed_task(
                RetryFailedTaskRequest(
                    project_id=failed.project_id,
                    run_id=failed.run_id,
                    task_id=failed.task_id,
                    expected_lock_version=run[1],
                ),
                idempotency_key="explicit-retry",
            )
            assert retried.result.status == "running"
            assert retried.result.attempt_id == "user-retry-attempt"
            async with engine.connect() as connection:
                task_state = await connection.scalar(
                    select(agent_tasks.c.status).where(agent_tasks.c.id == failed.task_id)
                )
                retry_kind = await connection.scalar(
                    select(agent_task_attempts.c.retry_kind).where(
                        agent_task_attempts.c.id == "user-retry-attempt"
                    )
                )
            assert task_state == "queued"
            assert retry_kind == "user_retry"
        finally:
            await engine.dispose()

    asyncio.run(exercise())
