from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db.engine import create_sqlite_async_engine
from app.db.maintenance import alembic_config
from app.db.schema import (
    agent_task_attempts,
    agent_tasks,
    book_review_submissions,
    book_workspaces,
    books,
    canon_baselines,
    content_refs,
    projects,
    story_arcs,
)
from app.domain.book.contracts import BookEvaluation
from app.domain.projects import CreateProjectRequest, ProjectCommandService
from app.runtime.control import RunControlRequest, RunControlService
from app.store.command_bus import CommandBus
from app.store.content import ContentRepository, prepare_canonical_json, prepare_exact_text
from tests.helpers.lifecycle_seed import insert_successful_task


@dataclass(frozen=True, slots=True)
class SeededProject:
    project_id: str
    book_id: str
    canon_id: str
    seed_ref_id: str
    seed_hash: str


async def _seed_project(engine: AsyncEngine, project_id: str) -> SeededProject:
    book_id = f"book-{project_id}"
    canon_id = f"canon-{project_id}"
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
    return SeededProject(project_id, book_id, canon_id, seed_ref.id, seed.sha256)


def test_composite_foreign_keys_reject_cross_project_content_and_scope(
    tmp_path: Path,
) -> None:
    database = tmp_path / "constraints.sqlite3"
    command.upgrade(alembic_config(database), "head")

    async def exercise() -> None:
        engine = create_sqlite_async_engine(database)
        try:
            first = await _seed_project(engine, "project-a")
            second = await _seed_project(engine, "project-b")
            only_in_second = prepare_exact_text("project B only")
            async with engine.begin() as connection:
                await ContentRepository(connection).put(
                    project_id=second.project_id,
                    prepared=only_in_second,
                    semantic_kind="test",
                    media_type="text/plain",
                )

            with pytest.raises(IntegrityError, match="FOREIGN KEY"):
                async with engine.begin() as connection:
                    await connection.execute(
                        content_refs.insert().values(
                            id="cross-project-ref",
                            project_id=first.project_id,
                            blob_sha256=only_in_second.sha256,
                            semantic_kind="test",
                            media_type="text/plain",
                            canonicalizer_id="exact-utf8-v1",
                            created_at_ms=2,
                        )
                    )

            with pytest.raises(IntegrityError, match="FOREIGN KEY"):
                async with engine.begin() as connection:
                    await connection.execute(
                        story_arcs.insert().values(
                            id="cross-project-arc",
                            project_id=first.project_id,
                            book_id=second.book_id,
                            ordinal=1,
                            purpose="regular",
                            lifecycle_status="planning",
                            created_at_ms=2,
                            updated_at_ms=2,
                        )
                    )
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_deferred_current_pointer_commits_only_with_matching_target(tmp_path: Path) -> None:
    database = tmp_path / "deferred.sqlite3"
    command.upgrade(alembic_config(database), "head")

    async def exercise() -> None:
        engine = create_sqlite_async_engine(database)
        try:
            await _seed_project(engine, "complete-pair")
            with pytest.raises(IntegrityError, match="FOREIGN KEY"):
                async with engine.begin() as connection:
                    await connection.execute(
                        projects.insert().values(
                            id="missing-half",
                            operation_mode="full_auto",
                            lifecycle_status="active",
                            settings_lock_version=1,
                            current_canon_baseline_id="missing-canon",
                            created_at_ms=1,
                            updated_at_ms=1,
                        )
                    )
            async with engine.connect() as connection:
                assert (
                    await connection.scalar(
                        select(func.count()).select_from(projects).where(
                            projects.c.id == "missing-half"
                        )
                    )
                    == 0
                )
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_partial_unique_indexes_reject_duplicate_pending_work(tmp_path: Path) -> None:
    database = tmp_path / "pending.sqlite3"
    command.upgrade(alembic_config(database), "head")

    async def exercise() -> None:
        engine = create_sqlite_async_engine(database)
        try:
            seeded = await _seed_project(engine, "project-a")
            workspace_id = "book-workspace"
            async with engine.begin() as connection:
                await connection.execute(
                    book_workspaces.insert().values(
                        id=workspace_id,
                        project_id=seeded.project_id,
                        book_id=seeded.book_id,
                        state="active",
                        lock_version=1,
                        base_canon_baseline_id=seeded.canon_id,
                        direction_draft_ref_id=seeded.seed_ref_id,
                        discussion_state_ref_id=seeded.seed_ref_id,
                        transcript_ref_id=seeded.seed_ref_id,
                        candidate_constraints_ref_id=seeded.seed_ref_id,
                        candidate_titles_ref_id=seeded.seed_ref_id,
                        candidate_rolling_plan_ref_id=seeded.seed_ref_id,
                        candidate_completion_contract_ref_id=seeded.seed_ref_id,
                        readiness_status="ready",
                        repair_policy_id="semantic-repair-v1",
                        semantic_repair_count=0,
                        semantic_repair_limit=5,
                        created_at_ms=1,
                        updated_at_ms=1,
                    )
                )

            def submission_values(identifier: str) -> dict[str, object]:
                return {
                    "id": identifier,
                    "project_id": seeded.project_id,
                    "book_id": seeded.book_id,
                    "workspace_id": workspace_id,
                    "workspace_lock_version": 1,
                    "canon_baseline_id": seeded.canon_id,
                    "direction_ref_id": seeded.seed_ref_id,
                    "constraints_ref_id": seeded.seed_ref_id,
                    "titles_ref_id": seeded.seed_ref_id,
                    "rolling_plan_ref_id": seeded.seed_ref_id,
                    "completion_contract_ref_id": seeded.seed_ref_id,
                    "content_manifest_ref_id": seeded.seed_ref_id,
                    "content_fingerprint": seeded.seed_hash,
                    "disposition": "pending",
                    "created_at_ms": 2,
                }

            with pytest.raises(IntegrityError, match="UNIQUE"):
                async with engine.begin() as connection:
                    await connection.execute(
                        book_review_submissions.insert().values(
                            **submission_values("submission-1")
                        )
                    )
                    await connection.execute(
                        book_review_submissions.insert().values(
                            **submission_values("submission-2")
                        )
                    )

            with pytest.raises(IntegrityError, match="UNIQUE"):
                async with engine.begin() as connection:
                    for ordinal in (1, 2):
                        await connection.execute(
                            story_arcs.insert().values(
                                id=f"arc-{ordinal}",
                                project_id=seeded.project_id,
                                book_id=seeded.book_id,
                                ordinal=ordinal,
                                purpose="regular",
                                lifecycle_status="planning",
                                created_at_ms=ordinal,
                                updated_at_ms=ordinal,
                            )
                        )
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_delivery_failure_states_require_result_and_error_consistency(
    tmp_path: Path,
) -> None:
    database = tmp_path / "delivery-failure-constraints.sqlite3"
    command.upgrade(alembic_config(database), "head")

    async def exercise() -> None:
        engine = create_sqlite_async_engine(database)
        try:
            bus = CommandBus(engine)
            created = await ProjectCommandService(bus).create_project(
                CreateProjectRequest(
                    project_id="project-delivery-constraints",
                    creator_brief="A schema constraint test.",
                    operation_mode="full_auto",
                ),
                idempotency_key="create-delivery-constraints",
            )
            await RunControlService(bus).start(
                RunControlRequest(
                    project_id=created.result.project_id,
                    run_id=created.result.generation_run_id,
                    expected_lock_version=1,
                ),
                idempotency_key="start-delivery-constraints",
            )
            task_id, attempt_id = await insert_successful_task(
                engine,
                project_id=created.result.project_id,
                run_id=created.result.generation_run_id,
                task_id="task-delivery-constraints",
                attempt_id="attempt-delivery-constraints",
                role="evaluator",
                task_kind="evaluate.book",
                scope_layer="book",
                book_id=created.result.book_id,
                canon_baseline_id=created.result.canon_baseline_id,
                workspace_lock_version=1,
                result=BookEvaluation(
                    decision="pass",
                    summary="The candidate passes for this constraint fixture.",
                ),
            )

            with pytest.raises(IntegrityError, match="CHECK constraint failed"):
                async with engine.begin() as connection:
                    await connection.execute(
                        update(agent_task_attempts)
                        .where(agent_task_attempts.c.id == attempt_id)
                        .values(status="delivery_failed")
                    )

            with pytest.raises(IntegrityError, match="CHECK constraint failed"):
                async with engine.begin() as connection:
                    await connection.execute(
                        update(agent_tasks)
                        .where(agent_tasks.c.id == task_id)
                        .values(delivery_state="failed")
                    )
        finally:
            await engine.dispose()

    asyncio.run(exercise())
