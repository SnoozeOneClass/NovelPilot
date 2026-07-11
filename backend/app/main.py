from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import (
    arcs,
    artifacts,
    completion,
    exports,
    feedback,
    profiles,
    projects,
    readiness,
    runs,
    setup,
)
from app.core.config import ensure_runtime_dirs
from app.storage.projects import recover_all_project_transactions


def create_app() -> FastAPI:
    ensure_runtime_dirs()
    recover_all_project_transactions()

    app = FastAPI(title="Novelpilot", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(projects.router, prefix="/api/projects", tags=["projects"])
    app.include_router(profiles.router, prefix="/api/profiles", tags=["profiles"])
    app.include_router(setup.router, prefix="/api/setup", tags=["setup"])
    app.include_router(arcs.router, prefix="/api/arcs", tags=["arcs"])
    app.include_router(runs.router, prefix="/api/runs", tags=["runs"])
    app.include_router(feedback.router, prefix="/api/feedback", tags=["feedback"])
    app.include_router(artifacts.router, prefix="/api/artifacts", tags=["artifacts"])
    app.include_router(exports.router, prefix="/api/export", tags=["export"])
    app.include_router(completion.router, prefix="/api/completion", tags=["completion"])
    app.include_router(readiness.router, prefix="/api/readiness", tags=["readiness"])

    return app


app = create_app()
