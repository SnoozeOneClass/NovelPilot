from __future__ import annotations

import asyncio
from collections.abc import Mapping

from app.authoring.domain.models import (
    AuthoringProfileSnapshot,
    ProjectView,
    RunStatus,
    TargetLength,
    WorkerRole,
)
from app.authoring.runtime.engine import Engine, EngineResult, ProfileResolver
from app.authoring.store.store import AuthoringStore
from app.authoring.workers.runtime import WorkerRuntime


def fake_profile(_project_id: str, role: WorkerRole) -> AuthoringProfileSnapshot:
    return AuthoringProfileSnapshot(
        profile_id=f"fake-{role.value}",
        provider_protocol="deterministic_function_model",
        model_id="authoring-fake-v1",
        context_window=16_384,
        max_output_tokens=2_048,
    )


class AuthoringService:
    """Application facade shared by Headless, Eval, and the later Web adapter."""

    def __init__(
        self,
        store: AuthoringStore,
        worker_runtime: WorkerRuntime | None = None,
        profile_resolver: ProfileResolver = fake_profile,
    ) -> None:
        self.store = store
        self.worker_runtime = worker_runtime
        self.profile_resolver = profile_resolver

    async def initialize(self) -> None:
        await self.store.migrate()

    async def create(
        self,
        brief: str,
        target_chapters: int | None = None,
        target_words: int | None = None,
        project_id: str | None = None,
        profile_bindings: Mapping[str, str] | None = None,
    ) -> str:
        target = TargetLength.resolve(
            target_chapters=target_chapters,
            target_words=target_words,
        )
        return await self.store.create_project(
            brief=brief,
            target=target,
            project_id=project_id,
            profile_bindings=profile_bindings,
        )

    async def run(
        self,
        project_id: str,
        *,
        max_instructions: int | None = None,
        interrupt_event: asyncio.Event | None = None,
        lease_acquired_event: asyncio.Event | None = None,
    ) -> EngineResult:
        if self.worker_runtime is None:
            raise RuntimeError("no Worker runtime is configured")
        engine = Engine(self.store, self.worker_runtime, self.profile_resolver)
        return await engine.run(
            project_id,
            max_instructions=max_instructions,
            cancel_event=interrupt_event,
            lease_acquired_event=lease_acquired_event,
        )

    async def status(self, project_id: str) -> ProjectView:
        return await self.store.project(project_id)

    async def projects(self) -> list[ProjectView]:
        return await self.store.list_projects()

    async def pause(self, project_id: str) -> ProjectView:
        await self.store.pause(project_id)
        return await self.status(project_id)

    async def resume(self, project_id: str, *, run: bool = True) -> ProjectView:
        await self.store.resume(project_id)
        if run:
            await self.run(project_id)
        return await self.status(project_id)

    async def cancel(self, project_id: str) -> ProjectView:
        await self.store.cancel(project_id)
        return await self.status(project_id)

    async def export(self, project_id: str, *, format: str = "markdown") -> tuple[str, str]:
        return await self.store.export_snapshot(project_id, format)

    async def events(self, project_id: str, *, after_seq: int = 0) -> list[dict[str, object]]:
        return await self.store.events(project_id, after_seq=after_seq)

    async def ensure_runnable(self, project_id: str) -> None:
        view = await self.status(project_id)
        if view.status in {RunStatus.CANCELLED, RunStatus.COMPLETED}:
            raise ValueError(f"project is already {view.status.value}")
