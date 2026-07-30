from __future__ import annotations

import asyncio
from pathlib import Path
from typing import cast

import pytest
from alembic import command

from app.db.engine import create_sqlite_async_engine
from app.db.maintenance import alembic_config
from app.domain.chapter.commands import ChapterCommandService
from app.domain.chapter.contracts import CreateChapterRequest
from app.domain.project_state import ProjectStateQuery
from app.runtime.context import ContextFactError, HarnessContextBuilder
from app.store.command_bus import CommandBus
from tests.helpers.lifecycle_seed import seed_approved_book_and_arc


def test_chapter_context_projects_current_and_next_without_full_arc_outline(
    tmp_path: Path,
) -> None:
    database = tmp_path / "chapter-outline-context.sqlite3"
    command.upgrade(alembic_config(database), "head")

    async def exercise() -> None:
        engine = create_sqlite_async_engine(database)
        try:
            foundation = await seed_approved_book_and_arc(
                engine,
                project_id="project-context-window",
                target_chapter_count=3,
                arc_contract_count=2,
            )
            created = await ChapterCommandService(
                CommandBus(engine)
            ).create_chapter(
                CreateChapterRequest(
                    project_id=foundation.project_id,
                    book_id=foundation.book_id,
                    arc_id=foundation.arc_id,
                    expected_book_baseline_id=foundation.book_baseline_id,
                    expected_arc_baseline_id=foundation.arc_baseline_id,
                    expected_canon_baseline_id=foundation.canon_baseline_id,
                ),
                idempotency_key="context:create-chapter-1",
            )
            builder = HarnessContextBuilder(engine)
            plan_context = await builder.build(
                task_kind="chapter.plan",
                project_id=foundation.project_id,
                book_id=foundation.book_id,
                arc_id=foundation.arc_id,
                chapter_id=created.result.chapter_id,
                semantic_goal="Plan the assigned Chapter.",
            )
            assert "Witnesses disagree" in plan_context.prompt
            assert (
                "physical evidence at assignment 2"
                in plan_context.prompt
            )
            assert (
                "physical evidence at assignment 3"
                not in plan_context.prompt
            )
            assert "approved_story_arc_plan" not in plan_context.prompt
            assert (
                plan_context.manifest["schema_id"]
                == "novelpilot-task-context-manifest-v4"
            )
            assert (
                '<NOVELPILOT_CONTEXT role="current_assignment" '
                'scope="chapter" time="current" use="constraint" '
                'access="read_only" target="false">'
            ) in plan_context.prompt
            projection = cast(
                dict[str, object],
                plan_context.manifest["arc_chapter_window"],
            )
            assert projection["current_arc_ordinal"] == 1
            assert projection["next_arc_ordinal"] == 2
            assert projection["includes_next"] is True
            assert len(cast(list[object], projection["sources"])) == 1

            with pytest.raises(
                ContextFactError,
                match="chapter_plan, chapter_prose",
            ) as captured:
                await builder.build(
                    task_kind="chapter.observe",
                    project_id=foundation.project_id,
                    book_id=foundation.book_id,
                    arc_id=foundation.arc_id,
                    chapter_id=created.result.chapter_id,
                    semantic_goal="Observe the assigned Chapter.",
                )
            assert captured.value.invariant == "context_required_group_present"

            arc_context = await builder.build(
                task_kind="evaluate.arc",
                project_id=foundation.project_id,
                book_id=foundation.book_id,
                arc_id=foundation.arc_id,
                chapter_id=None,
                semantic_goal="Evaluate the complete Arc outline.",
            )
            assert (
                "physical evidence at assignment 3"
                in arc_context.prompt
            )

            state = await ProjectStateQuery(engine).get_project(
                foundation.project_id
            )
            assert state is not None
            assert state.book.arc_contract_count == 2
            assert state.book.final_arc_ordinal == 2
            assert [
                item.lifecycle_status for item in state.book.arc_topology
            ] == ["active", "planned"]
            assert state.current_arc is not None
            assert state.current_arc.is_final is False
            assert state.current_arc.outline is not None
            assert [
                item.status for item in state.current_arc.outline.entries
            ] == ["drafting", "planned", "planned"]
            assert {
                item.source_arc_baseline_version
                for item in state.current_arc.outline.entries
            } == {1}
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_final_planned_chapter_context_has_no_fabricated_next_assignment(
    tmp_path: Path,
) -> None:
    database = tmp_path / "chapter-outline-final-context.sqlite3"
    command.upgrade(alembic_config(database), "head")

    async def exercise() -> None:
        engine = create_sqlite_async_engine(database)
        try:
            foundation = await seed_approved_book_and_arc(
                engine,
                project_id="project-final-context",
                target_chapter_count=1,
            )
            created = await ChapterCommandService(CommandBus(engine)).create_chapter(
                CreateChapterRequest(
                    project_id=foundation.project_id,
                    book_id=foundation.book_id,
                    arc_id=foundation.arc_id,
                    expected_book_baseline_id=foundation.book_baseline_id,
                    expected_arc_baseline_id=foundation.arc_baseline_id,
                    expected_canon_baseline_id=foundation.canon_baseline_id,
                ),
                idempotency_key="context:create-final-chapter",
            )
            context = await HarnessContextBuilder(engine).build(
                task_kind="chapter.plan",
                project_id=foundation.project_id,
                book_id=foundation.book_id,
                arc_id=foundation.arc_id,
                chapter_id=created.result.chapter_id,
                semantic_goal="Plan the final assigned Chapter.",
            )
            projection = cast(
                dict[str, object],
                context.manifest["arc_chapter_window"],
            )
            assert projection["current_arc_ordinal"] == 1
            assert projection["next_arc_ordinal"] is None
            assert projection["next_book_ordinal"] is None
            assert '"next":null' in context.prompt
        finally:
            await engine.dispose()

    asyncio.run(exercise())
