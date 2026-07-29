"""replace dynamic Book boundaries with approved Arc topology

Revision ID: d82f1c4a7b90
Revises: c4a7d91e2b65
Create Date: 2026-07-28

This is an intentionally incompatible development-stage migration.  There is
no honest semantic conversion from the old model-authored Arc purpose/range
contract into the new Book-owned Arc topology.  Empty databases are rebuilt
from the declared LT1 metadata; databases containing novel lifecycle data
must be explicitly reset by the developer.
"""

from __future__ import annotations

import importlib.util
from collections.abc import Callable, Sequence
from pathlib import Path
from types import ModuleType
from typing import cast

import sqlalchemy as sa
from alembic import op

from app.db.schema import metadata


revision: str = "d82f1c4a7b90"
down_revision: str | None = "c4a7d91e2b65"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_LEGACY_REVISIONS = (
    "ef42ab7a9212_initial_lt1_schema.py",
    "7c0d2a9f4b31_delivery_failure_states.py",
    "1be6decc58a4_add_explicit_loop_boundary_authority.py",
    "6d4d321a8c7e_remove_chapter_needs_user.py",
    "a91f3c7d2e60_explicit_cumulative_arc_counts.py",
    "c4a7d91e2b65_arc_chapter_outline_provenance.py",
)


def _load_revision(filename: str) -> ModuleType:
    path = Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location(
        f"novelpilot_legacy_migration_{path.stem}",
        path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load legacy migration {filename!r}.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _migration_function(filename: str, name: str) -> Callable[[], None]:
    function = getattr(_load_revision(filename), name, None)
    if not callable(function):
        raise RuntimeError(
            f"Legacy migration {filename!r} has no callable {name}()."
        )
    return cast(Callable[[], None], function)


def _require_empty_incompatible_history() -> None:
    connection = op.get_bind()
    counts = {
        table_name: connection.execute(
            sa.text(f"SELECT count(*) FROM {table_name}")
        ).scalar_one()
        for table_name in (
            "projects",
            "books",
            "story_arcs",
            "chapters",
            "book_workspaces",
            "arc_workspaces",
            "chapter_workspaces",
        )
    }
    if any(counts.values()):
        populated = ", ".join(
            f"{table_name}={count}"
            for table_name, count in counts.items()
            if count
        )
        raise RuntimeError(
            "The Book-owned Arc-topology refactor intentionally does not infer "
            "new authority contracts for pre-release novel history. Reset the "
            "development database and rerun the migration. Populated tables: "
            f"{populated}."
        )


def _remove_application_schema() -> None:
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    for table_name in inspector.get_table_names():
        if table_name == "alembic_version":
            continue
        quoted_name = table_name.replace('"', '""')
        connection.exec_driver_sql(f'DROP TABLE "{quoted_name}"')


def _restore_legacy_schema() -> None:
    for filename in _LEGACY_REVISIONS:
        _migration_function(filename, "upgrade")()


def upgrade() -> None:
    _require_empty_incompatible_history()
    _remove_application_schema()
    metadata.create_all(bind=op.get_bind(), checkfirst=False)


def downgrade() -> None:
    _require_empty_incompatible_history()
    _remove_application_schema()
    _restore_legacy_schema()
