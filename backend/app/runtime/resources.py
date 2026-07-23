from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.agents.executor import AgentExecutor, AgentLiveEvent
from app.agents.registry import DEFAULT_TASK_REGISTRY
from app.core.config import LLM_PROFILES_PATH
from app.db.engine import create_sqlite_async_engine
from app.profiles import ProfileCatalog
from app.runtime.control import RunControlService
from app.runtime.driver import DomainRunDriver
from app.runtime.engine import RunEngine
from app.runtime.live import LossyLiveFanout
from app.runtime.reconcile import ReconcileService
from app.store.command_bus import CommandBus


@dataclass(slots=True)
class ApplicationResources:
    """Resources with exactly the same lifetime as one FastAPI process."""

    database_engine: AsyncEngine
    profile_catalog: ProfileCatalog
    live_events: LossyLiveFanout[AgentLiveEvent]
    run_engine: RunEngine
    run_control: RunControlService
    _started: bool = False
    _closed: bool = False

    @classmethod
    async def open(
        cls,
        database_path: Path,
        *,
        profile_path: Path | None = None,
        idle_poll_seconds: float = 1.0,
    ) -> ApplicationResources:
        engine = create_sqlite_async_engine(database_path)
        try:
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        except BaseException:
            await engine.dispose()
            raise
        catalog = ProfileCatalog(profile_path or LLM_PROFILES_PATH)
        live_events: LossyLiveFanout[AgentLiveEvent] = LossyLiveFanout()
        executor = AgentExecutor(
            engine,
            registry=DEFAULT_TASK_REGISTRY,
            live_publisher=live_events,
        )
        driver = DomainRunDriver(
            engine,
            profile_catalog=catalog,
            registry=DEFAULT_TASK_REGISTRY,
            executor=executor,
        )
        command_bus = CommandBus(engine)
        reconciler = ReconcileService(engine, command_bus)
        run_engine = RunEngine(
            engine,
            driver=driver,
            reconciler=reconciler,
            idle_poll_seconds=idle_poll_seconds,
        )
        run_control = RunControlService(command_bus, wake=run_engine.wake)
        return cls(
            database_engine=engine,
            profile_catalog=catalog,
            live_events=live_events,
            run_engine=run_engine,
            run_control=run_control,
        )

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def started(self) -> bool:
        return self._started

    async def start(self) -> None:
        if self._closed:
            raise RuntimeError("Closed application resources cannot be restarted.")
        if self._started:
            return
        await self.run_engine.start()
        self._started = True

    async def close(self) -> None:
        if self._closed:
            return
        try:
            if self._started:
                await self.run_engine.stop()
        finally:
            await self.database_engine.dispose()
            self._started = False
            self._closed = True
