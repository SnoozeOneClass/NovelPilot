from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import func, select

from app.db.engine import create_sqlite_async_engine
from app.db.maintenance import alembic_config
from app.db.schema import (
    book_workspaces,
    books,
    canon_baselines,
    command_receipts,
    content_blobs,
    domain_events,
    generation_runs,
    projects,
)
from app.domain.commands import CommandPreconditionError, IdempotencyConflictError
from app.domain.projects import (
    CreateProjectRequest,
    DeleteProjectRequest,
    ProjectCommandService,
    ProjectNotFoundError,
    UpdateProjectSettingsRequest,
)
from app.store.command_bus import CommandBus
from app.store.commands import CommandRepository


def test_create_project_is_atomic_idempotent_and_emits_one_event(tmp_path: Path) -> None:
    database = tmp_path / "projects.sqlite3"
    command.upgrade(alembic_config(database), "head")

    async def exercise() -> None:
        engine = create_sqlite_async_engine(database)
        try:
            service = ProjectCommandService(CommandBus(engine))
            request = CreateProjectRequest(
                project_id="project-a",
                creator_brief="写一部长篇悬疑小说。\n保留原始换行。",
                operation_mode="full_auto",
                default_profile_id="grok-4.5",
            )
            created = await service.create_project(request, idempotency_key="create-a")
            replayed = await service.create_project(request, idempotency_key="create-a")

            assert not created.replayed
            assert replayed.replayed
            assert replayed.result == created.result
            assert replayed.receipt_id == created.receipt_id
            assert created.first_event_sequence == created.last_event_sequence == 1

            async with engine.connect() as connection:
                assert await connection.scalar(select(func.count()).select_from(projects)) == 1
                assert await connection.scalar(select(func.count()).select_from(books)) == 1
                assert (
                    await connection.scalar(select(func.count()).select_from(book_workspaces))
                    == 1
                )
                assert (
                    await connection.scalar(select(func.count()).select_from(canon_baselines))
                    == 1
                )
                assert (
                    await connection.scalar(select(func.count()).select_from(generation_runs))
                    == 1
                )
                assert (
                    await connection.scalar(select(func.count()).select_from(command_receipts))
                    == 1
                )
                assert (
                    await connection.scalar(select(func.count()).select_from(domain_events)) == 1
                )
                # Four empty Canon documents share one physical [] Blob; Book state adds three.
                assert (
                    await connection.scalar(select(func.count()).select_from(content_blobs)) == 4
                )

            conflicting = request.model_copy(update={"creator_brief": "different bytes"})
            with pytest.raises(IdempotencyConflictError):
                await service.create_project(conflicting, idempotency_key="create-a")
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_event_failure_rolls_back_entire_project_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "rollback.sqlite3"
    command.upgrade(alembic_config(database), "head")

    async def fail_event(*args: object, **kwargs: object) -> int:
        raise RuntimeError("injected event persistence failure")

    monkeypatch.setattr(CommandRepository, "append_event", fail_event)

    async def exercise() -> None:
        engine = create_sqlite_async_engine(database)
        try:
            service = ProjectCommandService(CommandBus(engine))
            with pytest.raises(RuntimeError, match="injected event"):
                await service.create_project(
                    CreateProjectRequest(
                        project_id="project-rollback",
                        creator_brief="must not become visible",
                        operation_mode="participatory",
                    ),
                    idempotency_key="create-rollback",
                )
            async with engine.connect() as connection:
                for table in (
                    projects,
                    books,
                    book_workspaces,
                    canon_baselines,
                    generation_runs,
                    content_blobs,
                    command_receipts,
                    domain_events,
                ):
                    assert await connection.scalar(select(func.count()).select_from(table)) == 0
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_settings_use_cas_and_command_replay(tmp_path: Path) -> None:
    database = tmp_path / "settings.sqlite3"
    command.upgrade(alembic_config(database), "head")

    async def exercise() -> None:
        engine = create_sqlite_async_engine(database)
        try:
            service = ProjectCommandService(CommandBus(engine))
            await service.create_project(
                CreateProjectRequest(
                    project_id="project-a",
                    creator_brief="brief",
                    operation_mode="full_auto",
                ),
                idempotency_key="create",
            )
            request = UpdateProjectSettingsRequest(
                project_id="project-a",
                expected_lock_version=1,
                operation_mode="participatory",
                default_profile_id="profile-2",
            )
            changed = await service.update_settings(request, idempotency_key="settings-1")
            replayed = await service.update_settings(request, idempotency_key="settings-1")
            assert changed.result.settings_lock_version == 2
            assert replayed.replayed

            with pytest.raises(CommandPreconditionError, match="stale"):
                await service.update_settings(request, idempotency_key="settings-stale")

            async with engine.connect() as connection:
                row = (
                    await connection.execute(
                        select(
                            projects.c.operation_mode,
                            projects.c.settings_lock_version,
                            projects.c.default_profile_id,
                        ).where(projects.c.id == "project-a")
                    )
                ).one()
                assert tuple(row) == ("participatory", 2, "profile-2")
                assert (
                    await connection.scalar(select(func.count()).select_from(domain_events)) == 2
                )
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_project_delete_cascades_its_receipt_event_and_blobs_only(tmp_path: Path) -> None:
    database = tmp_path / "delete.sqlite3"
    command.upgrade(alembic_config(database), "head")

    async def exercise() -> None:
        engine = create_sqlite_async_engine(database)
        try:
            service = ProjectCommandService(CommandBus(engine))
            for project_id in ("project-a", "project-b"):
                await service.create_project(
                    CreateProjectRequest(
                        project_id=project_id,
                        creator_brief="identical project bytes",
                        operation_mode="full_auto",
                    ),
                    idempotency_key=f"create-{project_id}",
                )

            result = await service.delete_project(
                DeleteProjectRequest(project_id="project-a"),
                idempotency_key="delete-a",
            )
            assert result.deleted
            with pytest.raises(ProjectNotFoundError):
                await service.delete_project(
                    DeleteProjectRequest(project_id="project-a"),
                    idempotency_key="delete-a",
                )

            async with engine.connect() as connection:
                assert (
                    await connection.scalar(
                        select(func.count()).select_from(projects).where(
                            projects.c.id == "project-a"
                        )
                    )
                    == 0
                )
                assert (
                    await connection.scalar(
                        select(func.count()).select_from(command_receipts).where(
                            command_receipts.c.project_id == "project-a"
                        )
                    )
                    == 0
                )
                assert (
                    await connection.scalar(
                        select(func.count()).select_from(domain_events).where(
                            domain_events.c.project_id == "project-a"
                        )
                    )
                    == 0
                )
                assert (
                    await connection.scalar(
                        select(func.count()).select_from(content_blobs).where(
                            content_blobs.c.project_id == "project-a"
                        )
                    )
                    == 0
                )
                assert (
                    await connection.scalar(
                        select(func.count()).select_from(content_blobs).where(
                            content_blobs.c.project_id == "project-b"
                        )
                    )
                    == 4
                )
        finally:
            await engine.dispose()

    asyncio.run(exercise())
