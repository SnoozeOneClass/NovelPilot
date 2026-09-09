from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Literal

from app.authoring.domain.models import RunStatus
from app.authoring.errors import LeaseUnavailableError
from app.authoring.service import AuthoringService


@dataclass(slots=True)
class ActiveAuthoringRun:
    task: asyncio.Task[Any]
    interrupt_event: asyncio.Event


class AuthoringRunRegistry:
    """Process-local signal delivery; persisted Store facts remain authoritative."""

    def __init__(self, service: AuthoringService) -> None:
        self.service = service
        self._runs: dict[str, ActiveAuthoringRun] = {}
        self._lock = asyncio.Lock()

    async def start_or_resume(
        self, project_id: str
    ) -> Literal["started_here", "already_running_here", "owned_elsewhere"]:
        async with self._lock:
            active = self._runs.get(project_id)
            if active is not None and not active.task.done():
                return "already_running_here"
            view = await self.service.status(project_id)
            if view.status in {RunStatus.COMPLETED, RunStatus.CANCELLED}:
                raise ValueError(f"project is already {view.status.value}")
            if view.status in {
                RunStatus.READY,
                RunStatus.PAUSED,
                RunStatus.FAILURE_PAUSED,
            }:
                await self.service.resume(project_id, run=False)
            interrupt = asyncio.Event()
            lease_acquired = asyncio.Event()
            task = asyncio.create_task(
                self.service.run(
                    project_id,
                    interrupt_event=interrupt,
                    lease_acquired_event=lease_acquired,
                ),
                name=f"authoring:{project_id}",
            )
            self._runs[project_id] = ActiveAuthoringRun(task, interrupt)
            task.add_done_callback(self._schedule_cleanup)
            acquired_wait = asyncio.create_task(lease_acquired.wait())
            done, _ = await asyncio.wait({task, acquired_wait}, return_when=asyncio.FIRST_COMPLETED)
            if lease_acquired.is_set():
                acquired_wait.cancel()
                await asyncio.gather(acquired_wait, return_exceptions=True)
                return "started_here"
            acquired_wait.cancel()
            await asyncio.gather(acquired_wait, return_exceptions=True)
            if task in done:
                error = task.exception()
                if isinstance(error, LeaseUnavailableError):
                    return "owned_elsewhere"
                if error is not None:
                    raise error
            return "owned_elsewhere"

    async def pause(self, project_id: str) -> None:
        await self.service.pause(project_id)
        await self._signal(project_id)

    async def cancel(self, project_id: str) -> None:
        await self.service.cancel(project_id)
        await self._signal(project_id)

    async def wait(self, project_id: str) -> None:
        async with self._lock:
            active = self._runs.get(project_id)
        if active is not None:
            await asyncio.gather(active.task, return_exceptions=True)

    async def is_active(self, project_id: str) -> bool:
        async with self._lock:
            active = self._runs.get(project_id)
            return active is not None and not active.task.done()

    async def close(self) -> None:
        async with self._lock:
            active = list(self._runs.items())
        for project_id, handle in active:
            view = await self.service.status(project_id)
            if view.status in {RunStatus.READY, RunStatus.RUNNING}:
                try:
                    await self.service.pause(project_id)
                except ValueError:
                    current = await self.service.status(project_id)
                    if current.status not in {RunStatus.CANCELLED, RunStatus.COMPLETED}:
                        raise
            handle.interrupt_event.set()
        await asyncio.gather(*(handle.task for _, handle in active), return_exceptions=True)

    async def _signal(self, project_id: str) -> None:
        async with self._lock:
            active = self._runs.get(project_id)
            if active is not None and not active.task.done():
                active.interrupt_event.set()

    def _schedule_cleanup(self, task: asyncio.Task[Any]) -> None:
        if not task.cancelled():
            task.exception()
        asyncio.create_task(self._cleanup(task))

    async def _cleanup(self, task: asyncio.Task[Any]) -> None:
        async with self._lock:
            for project_id, active in tuple(self._runs.items()):
                if active.task is task:
                    self._runs.pop(project_id, None)
                    return
