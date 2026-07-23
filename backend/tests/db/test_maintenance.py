from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db.engine import create_sqlite_async_engine
from app.db.maintenance import (
    BackupManifestError,
    DatabaseHealthError,
    DatabaseNotQuiescentError,
    alembic_config,
    create_consistent_backup,
    manifest_path_for,
    restore_database,
    validate_backup,
    validate_database,
)
from app.db.schema import books, canon_baselines, generation_runs, projects
from app.store.content import ContentRepository, prepare_canonical_json, prepare_exact_text


async def _seed_project(engine: AsyncEngine, project_id: str) -> None:
    seed = prepare_canonical_json({})
    canon_id = f"canon-{project_id}"
    async with engine.begin() as connection:
        await connection.execute(
            projects.insert().values(
                id=project_id,
                operation_mode="full_auto",
                lifecycle_status="active",
                settings_lock_version=1,
                current_canon_baseline_id=canon_id,
                created_at_ms=1,
                updated_at_ms=1,
            )
        )
        seed_ref = await ContentRepository(connection).put(
            project_id=project_id,
            prepared=seed,
            semantic_kind="canon.seed",
            media_type="application/json",
            schema_id="canon.seed",
            schema_version=1,
            created_at_ms=1,
        )
        await connection.execute(
            canon_baselines.insert().values(
                id=canon_id,
                project_id=project_id,
                baseline_version=1,
                characters_ref_id=seed_ref.id,
                relationships_ref_id=seed_ref.id,
                world_facts_ref_id=seed_ref.id,
                foreshadowing_ref_id=seed_ref.id,
                manifest_fingerprint=seed.sha256,
                created_at_ms=1,
            )
        )
        await connection.execute(
            books.insert().values(
                id=f"book-{project_id}",
                project_id=project_id,
                lifecycle_status="developing",
                created_at_ms=1,
                updated_at_ms=1,
            )
        )


def test_backup_is_consistent_validated_and_restorable(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite3"
    backup = tmp_path / "backups" / "snapshot.sqlite3"
    restored = tmp_path / "restored.sqlite3"
    command.upgrade(alembic_config(source), "head")

    async def seed() -> None:
        engine = create_sqlite_async_engine(source)
        try:
            await _seed_project(engine, "project-before-cut")
            prepared = prepare_exact_text("正式正文" * 100)
            async with engine.begin() as connection:
                await ContentRepository(connection).put(
                    project_id="project-before-cut",
                    prepared=prepared,
                    semantic_kind="chapter.prose",
                    media_type="text/plain; charset=utf-8",
                )
        finally:
            await engine.dispose()

    asyncio.run(seed())
    manifest = create_consistent_backup(source, backup)
    assert manifest_path_for(backup).is_file()
    assert manifest.file_size == backup.stat().st_size
    verified_manifest, health = validate_backup(backup)
    assert verified_manifest == manifest
    assert health.blob_count == 2

    async def mutate_after_cut() -> None:
        engine = create_sqlite_async_engine(source)
        try:
            await _seed_project(engine, "project-after-cut")
        finally:
            await engine.dispose()

    asyncio.run(mutate_after_cut())
    restored_health = restore_database(backup, restored)
    assert restored_health == health

    with sqlite3.connect(restored) as connection:
        project_ids = {
            row[0] for row in connection.execute("SELECT id FROM projects").fetchall()
        }
    assert project_ids == {"project-before-cut"}


def test_backup_refuses_active_execution_state(tmp_path: Path) -> None:
    database = tmp_path / "source.sqlite3"
    command.upgrade(alembic_config(database), "head")

    async def seed_active_run() -> None:
        engine = create_sqlite_async_engine(database)
        try:
            await _seed_project(engine, "project-a")
            async with engine.begin() as connection:
                await connection.execute(
                    generation_runs.insert().values(
                        id="run-a",
                        project_id="project-a",
                        run_number=1,
                        status="running",
                        desired_state="running",
                        lock_version=1,
                        created_at_ms=1,
                        updated_at_ms=1,
                    )
                )
        finally:
            await engine.dispose()

    asyncio.run(seed_active_run())
    with pytest.raises(DatabaseNotQuiescentError, match="Backup requires"):
        create_consistent_backup(database, tmp_path / "blocked.sqlite3")


def test_backup_manifest_detects_database_tampering(tmp_path: Path) -> None:
    database = tmp_path / "source.sqlite3"
    backup = tmp_path / "snapshot.sqlite3"
    command.upgrade(alembic_config(database), "head")

    async def seed() -> None:
        engine = create_sqlite_async_engine(database)
        try:
            await _seed_project(engine, "project-a")
        finally:
            await engine.dispose()

    asyncio.run(seed())
    create_consistent_backup(database, backup)
    with backup.open("ab") as handle:
        handle.write(b"tamper")

    with pytest.raises(BackupManifestError, match="size"):
        validate_backup(backup)


def test_database_health_rejects_corrupt_blob(tmp_path: Path) -> None:
    database = tmp_path / "source.sqlite3"
    command.upgrade(alembic_config(database), "head")

    async def seed() -> None:
        engine = create_sqlite_async_engine(database)
        try:
            await _seed_project(engine, "project-a")
        finally:
            await engine.dispose()

    asyncio.run(seed())
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE content_blobs SET payload = ?, stored_size = ?",
            (b"broken", 6),
        )
        connection.commit()

    with pytest.raises(DatabaseHealthError, match="Blob .* mismatch"):
        validate_database(database)
