from __future__ import annotations

import asyncio
from pathlib import Path

from alembic import command
from sqlalchemy import select, update

from app.db.engine import create_sqlite_async_engine
from app.db.maintenance import alembic_config
from app.db.schema import arc_workspaces, chapter_workspaces
from app.domain.arc.commands import ArcCommandService
from app.domain.arc.contracts import RebaseStaleArcRequest
from app.domain.chapter.commands import ChapterCommandService
from app.domain.chapter.contracts import (
    CreateChapterRequest,
    RebaseStaleChapterRequest,
)
from app.store.command_bus import CommandBus
from tests.helpers.lifecycle_seed import seed_approved_book_and_arc


def test_stale_arc_workspace_rebases_to_current_upstream_facts(tmp_path: Path) -> None:
    database = tmp_path / "stale-arc.sqlite3"
    command.upgrade(alembic_config(database), "head")

    async def exercise() -> None:
        engine = create_sqlite_async_engine(database)
        try:
            foundation = await seed_approved_book_and_arc(
                engine,
                project_id="stale-arc-project",
            )
            async with engine.begin() as connection:
                await connection.execute(
                    update(arc_workspaces)
                    .where(arc_workspaces.c.arc_id == foundation.arc_id)
                    .values(
                        state="stale",
                        stale_reason_code="upstream_book_revised",
                        stale_at_ms=10,
                    )
                )
                workspace = (
                    await connection.execute(
                        select(arc_workspaces).where(
                            arc_workspaces.c.arc_id == foundation.arc_id
                        )
                    )
                ).mappings().one()
            result = await ArcCommandService(CommandBus(engine)).rebase_stale_workspace(
                RebaseStaleArcRequest(
                    project_id=foundation.project_id,
                    book_id=foundation.book_id,
                    arc_id=foundation.arc_id,
                    expected_workspace_lock_version=workspace["lock_version"],
                    expected_book_baseline_id=foundation.book_baseline_id,
                    expected_arc_baseline_id=foundation.arc_baseline_id,
                    expected_canon_baseline_id=foundation.canon_baseline_id,
                ),
                idempotency_key="rebase-stale-arc",
            )
            assert result.result.workspace_lock_version == workspace["lock_version"] + 1
            async with engine.connect() as connection:
                row = (
                    await connection.execute(
                        select(
                            arc_workspaces.c.state,
                            arc_workspaces.c.base_arc_baseline_id,
                            arc_workspaces.c.plan_ref_id,
                            arc_workspaces.c.recommended_target_chapter_count,
                            arc_workspaces.c.stale_reason_code,
                        ).where(arc_workspaces.c.arc_id == foundation.arc_id)
                    )
                ).one()
            assert tuple(row) == (
                "active",
                foundation.arc_baseline_id,
                None,
                None,
                None,
            )
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_stale_chapter_workspace_discards_invalid_downstream_draft(tmp_path: Path) -> None:
    database = tmp_path / "stale-chapter.sqlite3"
    command.upgrade(alembic_config(database), "head")

    async def exercise() -> None:
        engine = create_sqlite_async_engine(database)
        try:
            foundation = await seed_approved_book_and_arc(
                engine,
                project_id="stale-chapter-project",
            )
            service = ChapterCommandService(CommandBus(engine))
            created = await service.create_chapter(
                CreateChapterRequest(
                    project_id=foundation.project_id,
                    book_id=foundation.book_id,
                    arc_id=foundation.arc_id,
                    expected_book_baseline_id=foundation.book_baseline_id,
                    expected_arc_baseline_id=foundation.arc_baseline_id,
                    expected_canon_baseline_id=foundation.canon_baseline_id,
                ),
                idempotency_key="create-stale-chapter",
            )
            async with engine.begin() as connection:
                await connection.execute(
                    update(chapter_workspaces)
                    .where(
                        chapter_workspaces.c.chapter_id == created.result.chapter_id
                    )
                    .values(
                        state="stale",
                        stale_reason_code="upstream_arc_revised",
                        stale_at_ms=10,
                    )
                )
            rebased = await service.rebase_stale_workspace(
                RebaseStaleChapterRequest(
                    project_id=foundation.project_id,
                    book_id=foundation.book_id,
                    arc_id=foundation.arc_id,
                    chapter_id=created.result.chapter_id,
                    expected_workspace_lock_version=1,
                    expected_book_baseline_id=foundation.book_baseline_id,
                    expected_arc_baseline_id=foundation.arc_baseline_id,
                    expected_chapter_baseline_id=None,
                    expected_canon_baseline_id=foundation.canon_baseline_id,
                ),
                idempotency_key="rebase-stale-chapter",
            )
            assert rebased.result.workspace_lock_version == 2
            async with engine.connect() as connection:
                row = (
                    await connection.execute(
                        select(
                            chapter_workspaces.c.state,
                            chapter_workspaces.c.plan_ref_id,
                            chapter_workspaces.c.draft_ref_id,
                            chapter_workspaces.c.observations_ref_id,
                            chapter_workspaces.c.candidate_canon_patch_ref_id,
                            chapter_workspaces.c.stale_reason_code,
                        ).where(
                            chapter_workspaces.c.chapter_id
                            == created.result.chapter_id
                        )
                    )
                ).one()
            assert tuple(row) == ("active", None, None, None, None, None)
        finally:
            await engine.dispose()

    asyncio.run(exercise())
