from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.types import Lifespan

from app.authoring.api import router as authoring_router
from app.authoring.api.errors import install_error_handlers
from app.authoring.config import (
    DEFAULT_AUTHORING_DATABASE_PATH,
    DEFAULT_AUTHORING_MODEL_METADATA_PATH,
)
from app.authoring.resources import AuthoringResources
from app.core.config import LLM_PROFILES_PATH, ensure_runtime_dirs


def _build_lifespan(
    database_path: Path,
    profile_path: Path,
    metadata_path: Path,
    fake: bool,
) -> Lifespan[FastAPI]:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        resources = await AuthoringResources.open(
            database_path, profile_path, metadata_path, fake=fake
        )
        app.state.authoring_resources = resources
        try:
            yield
        finally:
            await resources.close()
            app.state.authoring_resources = None

    return lifespan


def create_app(
    database_path: Path | None = None,
    profile_path: Path | None = None,
    metadata_path: Path | None = None,
    fake: bool = False,
) -> FastAPI:
    """Build the single Authoring API and runtime product."""

    ensure_runtime_dirs()
    lifespan = _build_lifespan(
        (database_path or DEFAULT_AUTHORING_DATABASE_PATH).resolve(),
        (profile_path or LLM_PROFILES_PATH).resolve(),
        (metadata_path or DEFAULT_AUTHORING_MODEL_METADATA_PATH).resolve(),
        fake,
    )
    app = FastAPI(title="NovelPilot Authoring", version="1.0.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    install_error_handlers(app)
    app.include_router(authoring_router, prefix="/api")
    return app


app = create_app()
