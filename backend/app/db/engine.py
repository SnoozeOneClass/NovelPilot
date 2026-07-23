from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from sqlalchemy import event
from sqlalchemy.engine import Connection, URL
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import ConnectionPoolEntry

DEFAULT_SQLITE_BUSY_TIMEOUT_MS = 5_000
_BEGIN_MODE_OPTION = "novelpilot_sqlite_begin_mode"


def sqlite_async_url(database_path: Path) -> URL:
    """Build an absolute aiosqlite URL without hand-escaping Windows paths."""
    return URL.create("sqlite+aiosqlite", database=str(database_path.resolve()))


def create_sqlite_async_engine(
    database_path: Path,
    *,
    busy_timeout_ms: int = DEFAULT_SQLITE_BUSY_TIMEOUT_MS,
    enforce_foreign_keys: bool = True,
) -> AsyncEngine:
    """Create an isolated engine; callers own and must dispose it.

    SQLite connection policy is intentionally installed per engine rather than in
    module globals so tests and the FastAPI lifespan cannot leak connection pools.
    """
    if busy_timeout_ms <= 0:
        raise ValueError("busy_timeout_ms must be positive.")

    resolved_path = database_path.resolve()
    resolved_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_async_engine(
        sqlite_async_url(resolved_path),
        pool_pre_ping=True,
    )

    def configure_connection(
        dbapi_connection: DBAPIConnection,
        _connection_record: ConnectionPoolEntry,
    ) -> None:
        connection = cast(Any, dbapi_connection)
        connection.isolation_level = None
        cursor = connection.cursor()
        try:
            foreign_keys = "ON" if enforce_foreign_keys else "OFF"
            cursor.execute(f"PRAGMA foreign_keys={foreign_keys}")
            cursor.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
            cursor.execute("PRAGMA journal_mode=WAL")
        finally:
            cursor.close()

    def emit_explicit_begin(connection: Connection) -> None:
        begin_mode = connection.get_execution_options().get(_BEGIN_MODE_OPTION, "DEFERRED")
        if begin_mode not in {"DEFERRED", "IMMEDIATE"}:
            raise ValueError(f"Unsupported SQLite begin mode: {begin_mode!r}.")
        statement = "BEGIN IMMEDIATE" if begin_mode == "IMMEDIATE" else "BEGIN"
        connection.exec_driver_sql(statement)

    event.listen(engine.sync_engine, "connect", configure_connection)
    event.listen(engine.sync_engine, "begin", emit_explicit_begin)
    return engine
