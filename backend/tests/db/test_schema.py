from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, UniqueConstraint, create_engine, inspect

from app.db.schema import EXPECTED_TABLE_NAMES, metadata


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
