from __future__ import annotations

import asyncio
from pathlib import Path

from sqlalchemy import text

from app.db.engine import create_sqlite_async_engine, sqlite_async_url
from app.main import create_app
from app.runtime.resources import ApplicationResources


def test_sqlite_url_keeps_windows_path_as_database_component(tmp_path: Path) -> None:
    database_path = tmp_path / "nested" / "novelpilot.sqlite3"

    url = sqlite_async_url(database_path)

    assert url.drivername == "sqlite+aiosqlite"
    assert Path(url.database or "") == database_path.resolve()


def test_engine_enforces_sqlite_connection_policy_and_disposes(tmp_path: Path) -> None:
    database_path = tmp_path / "novelpilot.sqlite3"

    async def exercise() -> None:
        engine = create_sqlite_async_engine(database_path, busy_timeout_ms=1_234)
        try:
            async with engine.connect() as connection:
                foreign_keys = await connection.scalar(text("PRAGMA foreign_keys"))
                busy_timeout = await connection.scalar(text("PRAGMA busy_timeout"))
                journal_mode = await connection.scalar(text("PRAGMA journal_mode"))
                assert connection.in_transaction()

            assert foreign_keys == 1
            assert busy_timeout == 1_234
            assert journal_mode == "wal"
        finally:
            await engine.dispose()

    asyncio.run(exercise())
    database_path.unlink()


def test_migration_engine_can_disable_foreign_keys_without_changing_default(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        migration_engine = create_sqlite_async_engine(
            tmp_path / "migration.sqlite3",
            enforce_foreign_keys=False,
        )
        runtime_engine = create_sqlite_async_engine(tmp_path / "runtime.sqlite3")
        try:
            async with migration_engine.connect() as connection:
                assert await connection.scalar(text("PRAGMA foreign_keys")) == 0
            async with runtime_engine.connect() as connection:
                assert await connection.scalar(text("PRAGMA foreign_keys")) == 1
        finally:
            await migration_engine.dispose()
            await runtime_engine.dispose()

    asyncio.run(exercise())


def test_application_resources_close_is_idempotent(tmp_path: Path) -> None:
    async def exercise() -> None:
        resources = await ApplicationResources.open(tmp_path / "resources.sqlite3")
        assert not resources.closed

        await resources.close()
        await resources.close()

        assert resources.closed

    asyncio.run(exercise())


def test_app_lifespan_owns_clean_runtime_resources(tmp_path: Path) -> None:
    async def exercise() -> None:
        app = create_app(
            database_path=tmp_path / "lifespan.sqlite3",
        )

        async with app.router.lifespan_context(app):
            resources = app.state.resources
            assert isinstance(resources, ApplicationResources)
            assert not resources.closed

        assert resources.closed
        assert app.state.resources is None

    asyncio.run(exercise())
