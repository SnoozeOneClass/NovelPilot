from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from collections.abc import Callable
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncEngine

from app.db.uow import UnitOfWork
from app.runtime.reconcile import ReconcileService
from app.store.runs import GenerationRunRecord

LOGGER = logging.getLogger(__name__)

ENGINE_SLOT_LEASE_MS = 60_000
ENGINE_SLOT_HEARTBEAT_SECONDS = 20.0
ENGINE_IDLE_POLL_SECONDS = 1.0


class RunActionDriver(Protocol):
    """Drive at most one durable Route action for one authoritative Run snapshot."""

    async def drive_one(self, run: GenerationRunRecord) -> None: ...


class RunEngine:
    """The one process-owned scheduler; browsers and SSE are never execution owners."""

    def __init__(
        self,
        database_engine: AsyncEngine,
        *,
        driver: RunActionDriver,
        reconciler: ReconcileService,
        instance_id: str | None = None,
        now_ms: Callable[[], int] | None = None,
        idle_poll_seconds: float = ENGINE_IDLE_POLL_SECONDS,
        slot_lease_ms: int = ENGINE_SLOT_LEASE_MS,
        heartbeat_seconds: float = ENGINE_SLOT_HEARTBEAT_SECONDS,
    ) -> None:
        if idle_poll_seconds <= 0 or slot_lease_ms <= 0 or heartbeat_seconds <= 0:
            raise ValueError("Run Engine timing values must be positive.")
        self._database_engine = database_engine
        self._driver = driver
        self._reconciler = reconciler
        self._instance_id = instance_id or f"run-engine-{uuid.uuid4().hex}"
        self._now_ms = now_ms or (lambda: time.time_ns() // 1_000_000)
        self._idle_poll_seconds = idle_poll_seconds
        self._slot_lease_ms = slot_lease_ms
        self._heartbeat_seconds = heartbeat_seconds
        self._wake_event = asyncio.Event()
        self._stop_event = asyncio.Event()
        self._worker: asyncio.Task[None] | None = None

    @property
    def instance_id(self) -> str:
        return self._instance_id

    @property
    def running(self) -> bool:
        return self._worker is not None and not self._worker.done()

    async def start(self) -> None:
        if self.running:
            raise RuntimeError("This Run Engine instance is already running.")
        self._stop_event.clear()
        self._worker = asyncio.create_task(
            self._run_loop(),
            name=f"novelpilot-run-engine:{self._instance_id}",
        )
        await asyncio.sleep(0)

    async def stop(self) -> None:
        worker = self._worker
        if worker is None:
            return
        self._stop_event.set()
        self.wake()
        try:
            await worker
        finally:
            self._worker = None

    def wake(self) -> None:
        """Lossy nudge only; the durable source of truth remains SQLite."""
        self._wake_event.set()

    async def run_once(self) -> bool:
        """Reconcile, then execute at most one claimed action. Return whether one ran."""
        await self._reconciler.reconcile()
        claimed = await self._claim_next_run()
        if claimed is None:
            return False
        run, lease_token = claimed
        heartbeat = asyncio.create_task(
            self._heartbeat_slot(run.id, lease_token),
            name=f"novelpilot-slot-heartbeat:{run.id}",
        )
        action_completed = True
        try:
            await self._driver.drive_one(run)
        except asyncio.CancelledError:
            raise
        except Exception:
            # A driver must persist expected task failures. An unexpected implementation
            # error is logged, while the scheduler remains available for recovery/control.
            LOGGER.exception("Run action driver failed unexpectedly for run %s", run.id)
            action_completed = False
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
            await self._release_and_settle_pause(run.id, lease_token)
        return action_completed

    async def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                did_work = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("Run Engine iteration failed; the next poll will retry.")
                did_work = False
            if did_work:
                continue
            self._wake_event.clear()
            if self._stop_event.is_set():
                break
            try:
                await asyncio.wait_for(
                    self._wake_event.wait(),
                    timeout=self._idle_poll_seconds,
                )
            except TimeoutError:
                pass

    async def _claim_next_run(self) -> tuple[GenerationRunRecord, str] | None:
        timestamp = self._now_ms()
        lease_token = uuid.uuid4().hex
        async with UnitOfWork(self._database_engine, begin_mode="IMMEDIATE") as session:
            run = await session.runs.find_next_runnable()
            if run is None:
                return None
            claimed = await session.runs.claim_engine_slot(
                run_id=run.id,
                owner_instance_id=self._instance_id,
                lease_token=lease_token,
                lease_expires_at_ms=timestamp + self._slot_lease_ms,
                now_ms=timestamp,
            )
            if not claimed:
                return None
            return run, lease_token

    async def _heartbeat_slot(self, run_id: str, lease_token: str) -> None:
        while True:
            await asyncio.sleep(self._heartbeat_seconds)
            timestamp = self._now_ms()
            async with UnitOfWork(self._database_engine, begin_mode="IMMEDIATE") as session:
                retained = await session.runs.heartbeat_engine_slot(
                    run_id=run_id,
                    owner_instance_id=self._instance_id,
                    lease_token=lease_token,
                    lease_expires_at_ms=timestamp + self._slot_lease_ms,
                    now_ms=timestamp,
                )
            if not retained:
                LOGGER.error("Run Engine lost its global slot lease for run %s", run_id)
                return

    async def _release_and_settle_pause(self, run_id: str, lease_token: str) -> None:
        timestamp = self._now_ms()
        async with UnitOfWork(self._database_engine, begin_mode="IMMEDIATE") as session:
            await session.runs.release_engine_slot(
                run_id=run_id,
                owner_instance_id=self._instance_id,
                lease_token=lease_token,
            )
            await session.runs.settle_requested_pause(run_id=run_id, now_ms=timestamp)
