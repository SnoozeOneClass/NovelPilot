from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from alembic import command
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.types import Lifespan

from app.api.errors import install_error_handlers
from app.api.workspace import router as workspace_router
from app.core.config import DATABASE_PATH, LLM_PROFILES_PATH, OUTPUT_DIR, ensure_runtime_dirs
from app.db.maintenance import alembic_config, validate_database
from app.runtime.resources import ApplicationResources


def _build_lifespan(
    *,
    database_path: Path,
    profile_path: Path,
    export_root: Path,
    auto_migrate: bool,
    run_engine_enabled: bool,
) -> Lifespan[FastAPI]:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        database_path.parent.mkdir(parents=True, exist_ok=True)
        export_root.mkdir(parents=True, exist_ok=True)
        if auto_migrate:
            await asyncio.to_thread(
                command.upgrade,
                alembic_config(database_path),
                "head",
            )
        await asyncio.to_thread(validate_database, database_path)
        resources = await ApplicationResources.open(
            database_path,
            profile_path=profile_path,
        )
        app.state.resources = resources
        app.state.export_root = export_root.resolve()
        try:
            if run_engine_enabled:
                await resources.start()
            yield
        finally:
            await resources.close()
            app.state.resources = None

    return lifespan


def create_app(
    *,
    database_path: Path | None = None,
    profile_path: Path | None = None,
    export_root: Path | None = None,
    auto_migrate: bool = True,
    run_engine_enabled: bool = True,
) -> FastAPI:
    """Build the single clean-slate API and runtime path."""
    ensure_runtime_dirs()
    lifespan = _build_lifespan(
        database_path=(database_path or DATABASE_PATH).resolve(),
        profile_path=(profile_path or LLM_PROFILES_PATH).resolve(),
        export_root=(export_root or OUTPUT_DIR).resolve(),
        auto_migrate=auto_migrate,
        run_engine_enabled=run_engine_enabled,
    )
    app = FastAPI(title="NovelPilot", version="0.2.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    install_error_handlers(app)
    app.include_router(workspace_router, prefix="/api")
    return app


app = create_app()
