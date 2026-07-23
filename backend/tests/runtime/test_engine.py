from __future__ import annotations

import asyncio
from pathlib import Path

from alembic import command
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db.engine import create_sqlite_async_engine
from app.db.maintenance import alembic_config
from app.db.schema import generation_runs, projects
from app.db.uow import UnitOfWork
from app.domain.projects import CreateProjectRequest, ProjectCommandService
from app.runtime.control import RunControlRequest, RunControlService
from app.runtime.engine import RunEngine
from app.runtime.reconcile import ReconcileService
from app.store.command_bus import CommandBus
from app.store.runs import GenerationRunRecord


async def _seed_running_project(
    engine: AsyncEngine,
    *,
    suffix: str,
) -> tuple[str, str]:
    project_id = f"project-{suffix}"
    created = await ProjectCommandService(CommandBus(engine)).create_project(
        CreateProjectRequest(
            project_id=project_id,
            creator_brief="A deterministic Run Engine test novel.",
            operation_mode="full_auto",
        ),
        idempotency_key=f"create-{suffix}",
    )
    run_id = created.result.generation_run_id
    await RunControlService(CommandBus(engine), now_ms=lambda: 10).start(
        RunControlRequest(
            project_id=project_id,
            run_id=run_id,
            expected_lock_version=1,
        ),
        idempotency_key=f"start-{suffix}",
    )
    return project_id, run_id


class CompleteDriver:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self.calls = 0

    async def drive_one(self, run: GenerationRunRecord) -> None:
        self.calls += 1
        async with UnitOfWork(self._engine, begin_mode="IMMEDIATE") as session:
            assert await session.runs.complete(run_id=run.id, now_ms=20)


class BlockingCompleteDriver:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def drive_one(self, run: GenerationRunRecord) -> None:
        self.calls += 1
        self.entered.set()
        await self.release.wait()
        async with UnitOfWork(self._engine, begin_mode="IMMEDIATE") as session:
            assert await session.runs.complete(run_id=run.id, now_ms=30)


class PauseBoundaryDriver:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.provider_calls = 0
        self.delivery_calls = 0
        self.cancelled = False
        self.result_persisted = False

    async def drive_one(self, run: GenerationRunRecord) -> None:
        if not self.result_persisted:
            self.provider_calls += 1
            self.entered.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
            # This flag stands for the already committed terminal task result. The next
            # Route iteration is delivery only and must not invoke the Provider again.
            self.result_persisted = True
            return
        self.delivery_calls += 1
        async with UnitOfWork(self._engine, begin_mode="IMMEDIATE") as session:
            assert await session.runs.complete(run_id=run.id, now_ms=100)


class UnexpectedFailureDriver:
    def __init__(self) -> None:
        self.calls = 0

    async def drive_one(self, run: GenerationRunRecord) -> None:
        del run
        self.calls += 1
        raise RuntimeError("unexpected driver failure")


def test_unexpected_driver_failure_uses_idle_poll_instead_of_hot_loop(
    tmp_path: Path,
) -> None:
    database = tmp_path / "driver-failure-backoff.sqlite3"
    command.upgrade(alembic_config(database), "head")

    async def exercise() -> None:
        engine = create_sqlite_async_engine(database)
        try:
            await _seed_running_project(engine, suffix="driver-failure-backoff")
            driver = UnexpectedFailureDriver()
            worker = RunEngine(
                engine,
                driver=driver,
                reconciler=ReconcileService(engine, CommandBus(engine)),
                idle_poll_seconds=0.2,
            )
            await worker.start()
            await asyncio.sleep(0.05)
            await worker.stop()
            assert driver.calls == 1
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_background_engine_progresses_without_browser_or_sse(tmp_path: Path) -> None:
    database = tmp_path / "background.sqlite3"
    command.upgrade(alembic_config(database), "head")

    async def exercise() -> None:
        engine = create_sqlite_async_engine(database)
        try:
            _, run_id = await _seed_running_project(engine, suffix="background")
            driver = CompleteDriver(engine)
            worker = RunEngine(
                engine,
                driver=driver,
                reconciler=ReconcileService(engine, CommandBus(engine)),
                idle_poll_seconds=0.01,
            )
            await worker.start()
            for _ in range(100):
                async with engine.connect() as connection:
                    status = await connection.scalar(
                        select(generation_runs.c.status).where(generation_runs.c.id == run_id)
                    )
                if status == "completed":
                    break
                await asyncio.sleep(0.01)
            await worker.stop()
            assert status == "completed"
            assert driver.calls == 1
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_global_slot_prevents_two_engines_from_driving_same_run(tmp_path: Path) -> None:
    database = tmp_path / "global-slot.sqlite3"
    command.upgrade(alembic_config(database), "head")

    async def exercise() -> None:
        engine = create_sqlite_async_engine(database)
        try:
            _, run_id = await _seed_running_project(engine, suffix="slot")
            first_driver = BlockingCompleteDriver(engine)
            second_driver = CompleteDriver(engine)
            first = RunEngine(
                engine,
                driver=first_driver,
                reconciler=ReconcileService(engine, CommandBus(engine), now_ms=lambda: 100),
                instance_id="engine-one",
                now_ms=lambda: 100,
            )
            second = RunEngine(
                engine,
                driver=second_driver,
                reconciler=ReconcileService(engine, CommandBus(engine), now_ms=lambda: 100),
                instance_id="engine-two",
                now_ms=lambda: 100,
            )
            first_turn = asyncio.create_task(first.run_once())
            await asyncio.wait_for(first_driver.entered.wait(), timeout=1)
            assert not await second.run_once()
            assert second_driver.calls == 0
            first_driver.release.set()
            assert await first_turn
            async with engine.connect() as connection:
                status = await connection.scalar(
                    select(generation_runs.c.status).where(generation_runs.c.id == run_id)
                )
            assert status == "completed"
            assert first_driver.calls == 1
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_u1_pause_does_not_cancel_and_resume_delivers_saved_result(
    tmp_path: Path,
) -> None:
    database = tmp_path / "pause.sqlite3"
    command.upgrade(alembic_config(database), "head")

    async def exercise() -> None:
        engine = create_sqlite_async_engine(database)
        try:
            project_id, run_id = await _seed_running_project(engine, suffix="pause")
            driver = PauseBoundaryDriver(engine)
            worker = RunEngine(
                engine,
                driver=driver,
                reconciler=ReconcileService(engine, CommandBus(engine), now_ms=lambda: 50),
                instance_id="pause-engine",
                now_ms=lambda: 50,
            )
            first_turn = asyncio.create_task(worker.run_once())
            await asyncio.wait_for(driver.entered.wait(), timeout=1)

            # A separate write command completes while the action waits, proving the Run
            # Engine does not hold a database transaction across Provider latency.
            parallel = await asyncio.wait_for(
                ProjectCommandService(CommandBus(engine)).create_project(
                    CreateProjectRequest(
                        project_id="project-written-during-provider",
                        creator_brief="This write must not wait for Provider completion.",
                        operation_mode="full_auto",
                    ),
                    idempotency_key="create-during-provider",
                ),
                timeout=1,
            )
            assert parallel.result.project_id == "project-written-during-provider"
            pause = await RunControlService(CommandBus(engine), now_ms=lambda: 60).pause(
                RunControlRequest(
                    project_id=project_id,
                    run_id=run_id,
                    expected_lock_version=2,
                ),
                idempotency_key="pause-during-provider",
            )
            assert pause.result.status == "pause_requested"
            assert not first_turn.done()
            assert not driver.cancelled

            driver.release.set()
            assert await first_turn
            async with engine.connect() as connection:
                paused = (
                    await connection.execute(
                        select(
                            generation_runs.c.status,
                            generation_runs.c.lock_version,
                        ).where(generation_runs.c.id == run_id)
                    )
                ).one()
            assert tuple(paused) == ("paused", 4)
            assert not await worker.run_once()

            resumed = await RunControlService(CommandBus(engine), now_ms=lambda: 90).resume(
                RunControlRequest(
                    project_id=project_id,
                    run_id=run_id,
                    expected_lock_version=paused.lock_version,
                ),
                idempotency_key="resume-after-result",
            )
            assert resumed.result.status == "running"
            assert await worker.run_once()
            assert driver.provider_calls == 1
            assert driver.delivery_calls == 1
            assert not driver.cancelled
            async with engine.connect() as connection:
                final_status = await connection.scalar(
                    select(generation_runs.c.status).where(generation_runs.c.id == run_id)
                )
                parallel_project = await connection.scalar(
                    select(projects.c.id).where(
                        projects.c.id == "project-written-during-provider"
                    )
                )
            assert final_status == "completed"
            assert parallel_project == "project-written-during-provider"
        finally:
            await engine.dispose()

    asyncio.run(exercise())
