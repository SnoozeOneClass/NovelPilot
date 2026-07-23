from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db.engine import create_sqlite_async_engine
from app.db.schema import books, canon_baselines, content_blobs, content_refs, projects
from app.db.uow import UnitOfWork
from app.store.content import (
    ContentReferenceNotFoundError,
    ContentRepository,
    StorageIntegrityError,
    prepare_canonical_json,
    prepare_exact_text,
    prepare_redacted_bytes,
)


def _upgrade(database_path: Path) -> None:
    repository_root = Path(__file__).resolve().parents[3]
    config = Config(repository_root / "alembic.ini")
    config.set_main_option("script_location", str(repository_root / "backend" / "alembic"))
    config.attributes["database_path"] = database_path
    command.upgrade(config, "head")


async def _seed_project(engine: AsyncEngine, project_id: str) -> None:
    canon_id = f"canon-{project_id}"
    book_id = f"book-{project_id}"
    seed = prepare_canonical_json({})
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
            ref_id=f"seed-ref-{project_id}",
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
                id=book_id,
                project_id=project_id,
                lifecycle_status="developing",
                created_at_ms=1,
                updated_at_ms=1,
            )
        )


def test_canonicalizers_have_versioned_golden_bytes_and_hashes() -> None:
    exact = prepare_exact_text("雪\r\n ")
    canonical_json = prepare_canonical_json({"b": [2, 1], "a": "雪"})
    redacted = prepare_redacted_bytes(b"\x00redacted\n")

    assert exact.canonical_bytes == "雪\r\n ".encode()
    assert exact.sha256 == "80621384063ef2446293f5ff3abad36facb5b1d9dc5e0ff5c019f3458b8e13d3"
    assert canonical_json.canonical_bytes == b'{"a":"\xe9\x9b\xaa","b":[2,1]}'
    assert canonical_json.sha256 == (
        "56c8d3ed8d1f69db1afda2902382ae3be008673af830d53070da4837d8f0a226"
    )
    assert redacted.canonical_bytes == b"\x00redacted\n"
    assert redacted.sha256 == (
        "c340b88d5f5ae718eed5340b4121f0ec6bddce5c7037f2aaac64b797ff773182"
    )
    assert prepare_canonical_json({"a": "雪", "b": [2, 1]}).sha256 == canonical_json.sha256


def test_canonical_json_rejects_non_json_and_non_finite_values() -> None:
    with pytest.raises(ValueError, match="NaN and Infinity"):
        prepare_canonical_json({"score": float("nan")})
    with pytest.raises(TypeError, match="string object keys"):
        prepare_canonical_json({1: "not allowed"})
    with pytest.raises(TypeError, match="Unsupported"):
        prepare_canonical_json({"value": object()})


def test_project_scoped_dedup_read_integrity_and_root_delete(tmp_path: Path) -> None:
    database_path = tmp_path / "content.sqlite3"
    _upgrade(database_path)

    async def exercise() -> None:
        engine = create_sqlite_async_engine(database_path)
        try:
            await _seed_project(engine, "project-a")
            await _seed_project(engine, "project-b")
            prepared = prepare_exact_text("同一项目内复用；项目之间不共享。" * 20)

            async with UnitOfWork(engine, begin_mode="IMMEDIATE") as session:
                first = await session.content.put(
                    project_id="project-a",
                    prepared=prepared,
                    semantic_kind="chapter.prose",
                    media_type="text/plain; charset=utf-8",
                    ref_id="project-a-ref-1",
                )
                second = await session.content.put(
                    project_id="project-a",
                    prepared=prepared,
                    semantic_kind="agent.final_result",
                    media_type="text/plain; charset=utf-8",
                    ref_id="project-a-ref-2",
                )
            async with UnitOfWork(engine, begin_mode="IMMEDIATE") as session:
                other = await session.content.put(
                    project_id="project-b",
                    prepared=prepared,
                    semantic_kind="chapter.prose",
                    media_type="text/plain; charset=utf-8",
                    ref_id="project-b-ref-1",
                )

            assert first.blob_sha256 == second.blob_sha256 == other.blob_sha256
            async with UnitOfWork(engine) as session:
                packed = await session.content.get_packed(
                    project_id="project-a", ref_id=first.id
                )
                with pytest.raises(ContentReferenceNotFoundError):
                    await session.content.get_packed(
                        project_id="project-b", ref_id=first.id
                    )
            assert packed.unpack_and_verify() == prepared.canonical_bytes

            async with engine.connect() as connection:
                blob_counts = dict(
                    (
                        await connection.execute(
                            select(content_blobs.c.project_id, func.count())
                            .where(content_blobs.c.sha256 == prepared.sha256)
                            .group_by(content_blobs.c.project_id)
                        )
                    ).all()
                )
                assert blob_counts == {"project-a": 1, "project-b": 1}

            async with engine.begin() as connection:
                await connection.execute(delete(projects).where(projects.c.id == "project-a"))

            async with engine.connect() as connection:
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
                        select(func.count()).select_from(content_refs).where(
                            content_refs.c.project_id == "project-a"
                        )
                    )
                    == 0
                )
                assert (
                    await connection.scalar(
                        select(func.count()).select_from(content_blobs).where(
                            content_blobs.c.project_id == "project-b",
                            content_blobs.c.sha256 == prepared.sha256,
                        )
                    )
                    == 1
                )
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_content_ref_failure_rolls_back_new_blob(tmp_path: Path) -> None:
    database_path = tmp_path / "rollback.sqlite3"
    _upgrade(database_path)

    async def exercise() -> None:
        engine = create_sqlite_async_engine(database_path)
        try:
            await _seed_project(engine, "project-a")
            prepared = prepare_exact_text("must roll back with the reference")
            with pytest.raises(IntegrityError):
                async with UnitOfWork(engine, begin_mode="IMMEDIATE") as session:
                    await session.content.put(
                        project_id="project-a",
                        prepared=prepared,
                        semantic_kind="test",
                        media_type="text/plain",
                        ref_id="seed-ref-project-a",
                    )

            async with engine.connect() as connection:
                assert (
                    await connection.scalar(
                        select(func.count()).select_from(content_blobs).where(
                            content_blobs.c.project_id == "project-a",
                            content_blobs.c.sha256 == prepared.sha256,
                        )
                    )
                    == 0
                )
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_corrupt_payload_is_detected_on_unpack(tmp_path: Path) -> None:
    database_path = tmp_path / "corrupt.sqlite3"
    _upgrade(database_path)

    async def exercise() -> None:
        engine = create_sqlite_async_engine(database_path)
        try:
            await _seed_project(engine, "project-a")
            prepared = prepare_exact_text("integrity")
            async with UnitOfWork(engine, begin_mode="IMMEDIATE") as session:
                reference = await session.content.put(
                    project_id="project-a",
                    prepared=prepared,
                    semantic_kind="test",
                    media_type="text/plain",
                )
            async with engine.begin() as connection:
                await connection.execute(
                    update(content_blobs)
                    .where(
                        content_blobs.c.project_id == "project-a",
                        content_blobs.c.sha256 == reference.blob_sha256,
                    )
                    .values(payload=b"corrupt", stored_size=7)
                )
            async with UnitOfWork(engine) as session:
                packed = await session.content.get_packed(
                    project_id="project-a", ref_id=reference.id
                )
            with pytest.raises(StorageIntegrityError, match="mismatch"):
                packed.unpack_and_verify()
        finally:
            await engine.dispose()

    asyncio.run(exercise())
