from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest
from app.authoring.domain.models import TargetLength
from app.authoring.errors import StateCorruptionError
from app.authoring.store import AuthoringStore


def test_authoring_database_is_repeatable_and_frozen_target_round_trips(tmp_path: Path) -> None:
    async def exercise() -> None:
        store = AuthoringStore(tmp_path / "authoring.sqlite3")
        await store.migrate()
        await store.migrate()
        target = TargetLength.resolve(target_words=6_001)
        project_id = await store.create_project(
            brief="A novice supplies one idea.", target=target, project_id="isolated-p1"
        )

        view = await store.project(project_id)
        with sqlite3.connect(store.database_path) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            }

        assert view.target == target
        assert view.target.target_chapters == 3
        assert tables == {
            "authoring_schema_migrations",
            "projects",
            "run_state",
            "planning_revisions",
            "content_blobs",
            "chapters",
            "chapter_versions",
            "chapter_facts",
            "canon_snapshots",
            "summaries",
            "reviews",
            "rewrite_queue",
            "checkpoints",
            "worker_episodes",
            "tool_invocations",
            "decisions",
            "model_usage",
            "model_requests",
            "export_manifests",
            "domain_events",
        }
        assert "alembic_version" not in tables
        assert await store.scalar("SELECT count(*) FROM authoring_schema_migrations") == 3
        assert (
            await store.scalar(
                "SELECT count(*) FROM sqlite_master WHERE type='table' "
                "AND name IN ('model_requests','export_manifests')"
            )
            == 2
        )

    asyncio.run(exercise())


def test_authoring_migration_refuses_a_legacy_database_without_modifying_schema(
    tmp_path: Path,
) -> None:
    database = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE alembic_version(version_num TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO alembic_version VALUES ('legacy-head')")

    async def exercise() -> None:
        with pytest.raises(StateCorruptionError, match="non-authoring tables"):
            await AuthoringStore(database).migrate()

    asyncio.run(exercise())
    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
    assert tables == {"alembic_version"}


def test_existing_initial_schema_is_upgraded_by_incremental_migration(tmp_path: Path) -> None:
    database = tmp_path / "authoring-v1.sqlite3"
    initial = (
        Path(__file__).parents[2]
        / "app"
        / "authoring"
        / "store"
        / "migrations"
        / "0001_initial.sql"
    )
    with sqlite3.connect(database) as connection:
        connection.executescript(initial.read_text(encoding="utf-8"))
        before = {row[1] for row in connection.execute("PRAGMA table_info(worker_episodes)")}
    assert "instruction_kind" not in before

    async def exercise() -> None:
        store = AuthoringStore(database)
        await store.migrate()
        assert await store.scalar("SELECT count(*) FROM authoring_schema_migrations") == 3
        assert (
            await store.scalar(
                "SELECT count(*) FROM sqlite_master WHERE type='table' "
                "AND name IN ('model_requests','export_manifests')"
            )
            == 2
        )

    asyncio.run(exercise())
    with sqlite3.connect(database) as connection:
        after = {row[1] for row in connection.execute("PRAGMA table_info(worker_episodes)")}
    assert {
        "instruction_kind",
        "logical_target",
        "fallback_profile_snapshot_json",
        "compaction_failure_count",
    }.issubset(after)


def test_existing_second_schema_is_upgraded_to_requests_and_exports(tmp_path: Path) -> None:
    database = tmp_path / "authoring-v2.sqlite3"
    migrations = Path(__file__).parents[2] / "app" / "authoring" / "store" / "migrations"
    with sqlite3.connect(database) as connection:
        connection.executescript((migrations / "0001_initial.sql").read_text(encoding="utf-8"))
        connection.executescript(
            (migrations / "0002_episode_runtime_evidence.sql").read_text(encoding="utf-8")
        )
        versions = connection.execute(
            "SELECT version FROM authoring_schema_migrations ORDER BY version"
        ).fetchall()
        assert versions == [("0001_initial",), ("0002_episode_runtime_evidence",)]
        assert connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table' "
            "AND name IN ('model_requests','export_manifests')"
        ).fetchone() == (0,)

    async def exercise() -> None:
        store = AuthoringStore(database)
        await store.migrate()
        assert await store.scalar("SELECT count(*) FROM authoring_schema_migrations") == 3
        assert (
            await store.scalar(
                "SELECT count(*) FROM sqlite_master WHERE type='table' "
                "AND name IN ('model_requests','export_manifests')"
            )
            == 2
        )

    asyncio.run(exercise())


def test_target_length_rejects_two_product_modes() -> None:
    try:
        TargetLength.resolve(target_chapters=3, target_words=9_000)
    except ValueError as error:
        assert "mutually exclusive" in str(error)
    else:
        raise AssertionError("two target modes must be rejected")
