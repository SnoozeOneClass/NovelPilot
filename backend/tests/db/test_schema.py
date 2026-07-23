from __future__ import annotations

import asyncio
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import (
    CheckConstraint,
    ForeignKeyConstraint,
    UniqueConstraint,
    create_engine,
    inspect,
    select,
)

from app.db.engine import create_sqlite_async_engine
from app.db.schema import EXPECTED_TABLE_NAMES, metadata
from app.db.schema import agent_evidence_items, agent_task_attempts, agent_tasks
from app.domain.book.contracts import BookEvaluation
from app.domain.projects import CreateProjectRequest, ProjectCommandService
from app.runtime.control import RunControlRequest, RunControlService
from app.store.command_bus import CommandBus
from app.store.content import ContentRepository
from tests.helpers.lifecycle_seed import insert_successful_task


def _alembic_config(database_path: Path) -> Config:
    repository_root = Path(__file__).resolve().parents[3]
    config = Config(repository_root / "alembic.ini")
    config.set_main_option("script_location", str(repository_root / "backend" / "alembic"))
    config.attributes["database_path"] = database_path
    return config


def _constraint_names(table_name: str, constraint_type: type[object]) -> set[str]:
    table = metadata.tables[table_name]
    return {
        str(constraint.name)
        for constraint in table.constraints
        if isinstance(constraint, constraint_type) and constraint.name is not None
    }


def test_initial_revision_supports_empty_database_lifecycle(tmp_path: Path) -> None:
    database_path = tmp_path / "schema.sqlite3"
    config = _alembic_config(database_path)

    command.upgrade(config, "head")
    command.upgrade(config, "head")

    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        assert set(inspect(engine).get_table_names()) == EXPECTED_TABLE_NAMES | {
            "alembic_version"
        }
    finally:
        engine.dispose()

    command.check(config)
    command.downgrade(config, "base")

    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        assert not (set(inspect(engine).get_table_names()) & EXPECTED_TABLE_NAMES)
    finally:
        engine.dispose()

    command.upgrade(config, "head")
    command.check(config)


def test_reflected_constraint_and_index_names_match_metadata(tmp_path: Path) -> None:
    database_path = tmp_path / "schema.sqlite3"
    command.upgrade(_alembic_config(database_path), "head")
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")

    try:
        inspector = inspect(engine)
        for table_name in sorted(EXPECTED_TABLE_NAMES):
            expected_indexes = {index.name for index in metadata.tables[table_name].indexes}
            actual_indexes = {
                str(index["name"])
                for index in inspector.get_indexes(table_name)
                if index["name"] is not None
            }
            assert actual_indexes == expected_indexes, table_name

            expected_foreign_keys = _constraint_names(table_name, ForeignKeyConstraint)
            actual_foreign_keys = {
                str(constraint["name"])
                for constraint in inspector.get_foreign_keys(table_name)
                if constraint["name"] is not None
            }
            assert actual_foreign_keys == expected_foreign_keys, table_name

            expected_checks = _constraint_names(table_name, CheckConstraint)
            actual_checks = {
                str(constraint["name"])
                for constraint in inspector.get_check_constraints(table_name)
                if constraint["name"] is not None
            }
            assert actual_checks == expected_checks, table_name

            expected_uniques = _constraint_names(table_name, UniqueConstraint)
            actual_uniques = {
                str(constraint["name"])
                for constraint in inspector.get_unique_constraints(table_name)
                if constraint["name"] is not None
            }
            assert actual_uniques == expected_uniques, table_name
    finally:
        engine.dispose()


def test_delivery_failure_revision_preserves_existing_success_evidence(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "populated-upgrade.sqlite3"
    config = _alembic_config(database_path)
    command.upgrade(config, "ef42ab7a9212")

    async def seed_previous_revision() -> str:
        engine = create_sqlite_async_engine(database_path)
        try:
            bus = CommandBus(engine)
            created = await ProjectCommandService(bus).create_project(
                CreateProjectRequest(
                    project_id="project-before-delivery-states",
                    creator_brief="A witness remembers a crime that has not happened yet.",
                    operation_mode="full_auto",
                ),
                idempotency_key="create-before-delivery-states",
            )
            await RunControlService(bus, now_ms=lambda: 11).start(
                RunControlRequest(
                    project_id=created.result.project_id,
                    run_id=created.result.generation_run_id,
                    expected_lock_version=1,
                ),
                idempotency_key="start-before-delivery-states",
            )
            _, attempt_id = await insert_successful_task(
                engine,
                project_id=created.result.project_id,
                run_id=created.result.generation_run_id,
                task_id="task-before-delivery-states",
                attempt_id="attempt-before-delivery-states",
                role="evaluator",
                task_kind="evaluate.book",
                scope_layer="book",
                book_id=created.result.book_id,
                canon_baseline_id=created.result.canon_baseline_id,
                workspace_lock_version=1,
                result=BookEvaluation(
                    decision="pass",
                    summary="The frozen candidate is internally coherent.",
                ),
            )
            async with engine.connect() as connection:
                result_ref_id = await connection.scalar(
                    select(agent_task_attempts.c.result_ref_id).where(
                        agent_task_attempts.c.id == attempt_id
                    )
                )
            assert result_ref_id is not None
            async with engine.begin() as connection:
                await connection.execute(
                    agent_evidence_items.insert().values(
                        id="evidence-before-delivery-states",
                        project_id=created.result.project_id,
                        task_id="task-before-delivery-states",
                        attempt_id=attempt_id,
                        sequence_number=1,
                        item_kind="completion_message",
                        content_ref_id=result_ref_id,
                        metadata_json='{"source":"previous-revision"}',
                        created_at_ms=22,
                    )
                )
            return result_ref_id
        finally:
            await engine.dispose()

    result_ref_id = asyncio.run(seed_previous_revision())
    command.upgrade(config, "head")
    command.check(config)

    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        with engine.connect() as connection:
            task = connection.execute(
                select(
                    agent_tasks.c.status,
                    agent_tasks.c.delivery_state,
                    agent_tasks.c.successful_attempt_id,
                ).where(agent_tasks.c.id == "task-before-delivery-states")
            ).one()
            attempt = connection.execute(
                select(
                    agent_task_attempts.c.status,
                    agent_task_attempts.c.result_ref_id,
                    agent_task_attempts.c.input_tokens,
                    agent_task_attempts.c.output_tokens,
                    agent_task_attempts.c.total_tokens,
                ).where(agent_task_attempts.c.id == "attempt-before-delivery-states")
            ).one()
            evidence = connection.execute(
                select(
                    agent_evidence_items.c.attempt_id,
                    agent_evidence_items.c.item_kind,
                    agent_evidence_items.c.content_ref_id,
                    agent_evidence_items.c.metadata_json,
                ).where(
                    agent_evidence_items.c.id == "evidence-before-delivery-states"
                )
            ).one()
        assert tuple(task) == (
            "succeeded",
            "pending",
            "attempt-before-delivery-states",
        )
        assert tuple(attempt) == ("succeeded", result_ref_id, 10, 5, 15)
        assert tuple(evidence) == (
            "attempt-before-delivery-states",
            "completion_message",
            result_ref_id,
            '{"source":"previous-revision"}',
        )
    finally:
        engine.dispose()

    async def verify_result_content() -> None:
        async_engine = create_sqlite_async_engine(database_path)
        try:
            async with async_engine.connect() as connection:
                packed = await ContentRepository(connection).get_packed(
                    project_id="project-before-delivery-states",
                    ref_id=result_ref_id,
                )
            assert BookEvaluation.model_validate_json(
                packed.unpack_and_verify()
            ).summary == "The frozen candidate is internally coherent."
        finally:
            await async_engine.dispose()

    asyncio.run(verify_result_content())
