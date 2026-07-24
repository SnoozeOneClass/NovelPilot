from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
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
from app.db.schema import (
    agent_evidence_items,
    agent_task_attempts,
    agent_tasks,
    projects,
)
from app.domain.book.contracts import BookEvaluation
from app.store.content import ContentRepository, prepare_canonical_json


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

    command.upgrade(config, "7c0d2a9f4b31")


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
        engine = create_sqlite_async_engine(
            database_path,
            enforce_foreign_keys=False,
        )
        try:
            project_id = "project-before-delivery-states"
            task_id = "task-before-delivery-states"
            attempt_id = "attempt-before-delivery-states"
            prepared = prepare_canonical_json(
                BookEvaluation(
                    decision="pass",
                    summary="The frozen candidate is internally coherent.",
                )
            )
            async with engine.begin() as connection:
                await connection.execute(
                    projects.insert().values(
                        id=project_id,
                        operation_mode="full_auto",
                        lifecycle_status="active",
                        settings_lock_version=1,
                        current_canon_baseline_id="legacy-canon",
                        created_at_ms=1,
                        updated_at_ms=1,
                    )
                )
                result_ref = await ContentRepository(connection).put(
                    project_id=project_id,
                    prepared=prepared,
                    semantic_kind="agent.typed_result",
                    media_type="application/json",
                    schema_id="evaluate.book-result",
                    schema_version=1,
                    ref_id="result-before-delivery-states",
                    created_at_ms=20,
                )
                await connection.execute(
                    agent_tasks.insert().values(
                        id=task_id,
                        project_id=project_id,
                        run_id="legacy-run",
                        task_key="evaluate.book:legacy",
                        action_key="evaluate.book",
                        role="evaluator",
                        task_kind="evaluate.book",
                        scope_layer="book",
                        book_id="legacy-book",
                        workspace_lock_version=1,
                        canon_baseline_id="legacy-canon",
                        task_plan_ref_id=result_ref.id,
                        input_manifest_ref_id=result_ref.id,
                        input_messages_ref_id=result_ref.id,
                        profile_snapshot_ref_id=result_ref.id,
                        input_fingerprint=prepared.sha256,
                        prompt_fingerprint=prepared.sha256,
                        context_policy_id="legacy-book-context",
                        context_policy_version=1,
                        context_policy_fingerprint=prepared.sha256,
                        output_schema_id="evaluate.book-result",
                        output_schema_version=1,
                        output_schema_fingerprint=prepared.sha256,
                        rubric_id="legacy-book-rubric",
                        rubric_version=1,
                        harness_policy_id="novelpilot-domain-harness",
                        harness_policy_version=1,
                        profile_id="legacy-profile",
                        profile_fingerprint=prepared.sha256,
                        api_family="openai_responses",
                        model_id="legacy-model",
                        output_mode="native_json_schema",
                        requires_native_json_schema=1,
                        requires_text_streaming=0,
                        transport_retry_limit=5,
                        model_request_limit=2,
                        connect_timeout_ms=10_000,
                        pool_timeout_ms=10_000,
                        write_timeout_ms=60_000,
                        read_timeout_ms=600_000,
                        activation_timeout_ms=1_800_000,
                        timeout_policy_id="provider-timeout-t1-v1",
                        status="succeeded",
                        successful_attempt_id=attempt_id,
                        delivery_state="pending",
                        created_at_ms=20,
                        updated_at_ms=20,
                    )
                )
                await connection.execute(
                    agent_task_attempts.insert().values(
                        id=attempt_id,
                        project_id=project_id,
                        task_id=task_id,
                        attempt_number=1,
                        retry_kind="initial",
                        status="succeeded",
                        framework_fingerprint=prepared.sha256,
                        provider_request_count=1,
                        transport_retry_count=0,
                        model_request_count=1,
                        input_tokens=10,
                        output_tokens=5,
                        total_tokens=15,
                        result_ref_id=result_ref.id,
                        created_at_ms=20,
                        started_at_ms=20,
                        finished_at_ms=21,
                    )
                )
                await connection.execute(
                    agent_evidence_items.insert().values(
                        id="evidence-before-delivery-states",
                        project_id=project_id,
                        task_id=task_id,
                        attempt_id=attempt_id,
                        sequence_number=1,
                        item_kind="completion_message",
                        content_ref_id=result_ref.id,
                        metadata_json='{"source":"previous-revision"}',
                        created_at_ms=22,
                    )
                )
            return result_ref.id
        finally:
            await engine.dispose()

    result_ref_id = asyncio.run(seed_previous_revision())
    command.upgrade(config, "7c0d2a9f4b31")

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


def test_loop_boundary_revision_rejects_pre_refactor_project_graph(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "incompatible-project.sqlite3"
    config = _alembic_config(database_path)
    command.upgrade(config, "7c0d2a9f4b31")
    seed_engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        with seed_engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO projects "
                "(id, operation_mode, lifecycle_status, settings_lock_version, "
                "current_canon_baseline_id, created_at_ms, updated_at_ms) "
                "VALUES "
                "('pre-refactor-project', 'full_auto', 'active', 1, "
                "'pre-refactor-canon', 1, 1)"
            )
    finally:
        seed_engine.dispose()

    with pytest.raises(
        RuntimeError,
        match="intentionally does not migrate pre-release project graphs",
    ):
        command.upgrade(config, "head")

    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        with engine.connect() as connection:
            assert (
                connection.exec_driver_sql(
                    "SELECT version_num FROM alembic_version"
                ).scalar_one()
                == "7c0d2a9f4b31"
            )
        assert "arc_closures" not in inspect(engine).get_table_names()
    finally:
        engine.dispose()
