from __future__ import annotations

import asyncio
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy.engine import Connection

from app.core.config import DATABASE_PATH
from app.db.engine import create_sqlite_async_engine, sqlite_async_url
from app.db.schema import metadata

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = metadata


def _database_path() -> Path:
    attributed_path = config.attributes.get("database_path")
    if attributed_path is not None:
        return Path(attributed_path).resolve()
    arguments = context.get_x_argument(as_dictionary=True)
    configured_path = arguments.get("database_path")
    return Path(configured_path).resolve() if configured_path else DATABASE_PATH


def run_migrations_offline() -> None:
    context.configure(
        url=sqlite_async_url(_database_path()).render_as_string(hide_password=False),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        render_as_batch=False,
    )

    with context.begin_transaction():
        context.run_migrations()


def _run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        render_as_batch=False,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    # SQLite cannot drop tables participating in FK cycles while enforcement is
    # enabled.  The migration connection is isolated from the application pool;
    # every runtime connection still enables FK enforcement.  Migrations must
    # therefore validate the resulting schema before handing the database back
    # to the application.
    engine = create_sqlite_async_engine(
        _database_path(),
        enforce_foreign_keys=False,
    )
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_run_migrations)
    finally:
        await engine.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
