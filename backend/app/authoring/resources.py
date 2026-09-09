from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.authoring.models import AuthoringProfileResolver
from app.authoring.models.catalog import ProfileCatalog
from app.authoring.runtime.engine import ProfileResolver
from app.authoring.runtime.registry import AuthoringRunRegistry
from app.authoring.service import AuthoringService, fake_profile
from app.authoring.store import AuthoringStore
from app.authoring.tools import ToolGateway
from app.authoring.workers import (
    PydanticWorkerRuntime,
    ScriptedAutoWorkerRuntime,
    WorkerRuntime,
)


@dataclass(slots=True)
class AuthoringResources:
    store: AuthoringStore
    service: AuthoringService
    runs: AuthoringRunRegistry
    profile_catalog: ProfileCatalog
    metadata_path: Path
    fake: bool
    profile_resolver: AuthoringProfileResolver | None
    _closed: bool = False

    @classmethod
    async def open(
        cls,
        database_path: Path,
        profile_path: Path,
        metadata_path: Path,
        *,
        fake: bool = False,
    ) -> AuthoringResources:
        store = AuthoringStore(database_path)
        await store.migrate()
        catalog = ProfileCatalog(profile_path)
        worker: WorkerRuntime
        resolver: ProfileResolver
        if fake:
            worker = ScriptedAutoWorkerRuntime(ToolGateway(store))
            resolver = fake_profile
            authoring_resolver = None
        else:
            worker = PydanticWorkerRuntime()
            authoring_resolver = AuthoringProfileResolver(catalog, metadata_path)
            resolver = authoring_resolver
        service = AuthoringService(store, worker_runtime=worker, profile_resolver=resolver)
        return cls(
            store=store,
            service=service,
            runs=AuthoringRunRegistry(service),
            profile_catalog=catalog,
            metadata_path=metadata_path,
            fake=fake,
            profile_resolver=authoring_resolver,
        )

    def validate_profile_bindings(self, bindings: dict[str, str]) -> None:
        if self.fake:
            return
        if self.profile_resolver is None:
            raise RuntimeError("real Authoring resources lack a Profile resolver")
        self.profile_resolver.validate_bindings(bindings)

    async def close(self) -> None:
        if self._closed:
            return
        await self.runs.close()
        self._closed = True
