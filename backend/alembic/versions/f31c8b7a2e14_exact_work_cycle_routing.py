"""bind lifecycle work to exact semantic cycles and source reviews

Revision ID: f31c8b7a2e14
Revises: d82f1c4a7b90
Create Date: 2026-07-30

This is an intentionally incompatible development-stage migration.  Existing
pre-release runs do not contain the exact work-cycle and source-review
identities needed to migrate them honestly.  Empty databases are rebuilt from
the declared LT1 metadata; populated development databases must be reset.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.db.schema import metadata


revision: str = "f31c8b7a2e14"
down_revision: str | None = "d82f1c4a7b90"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


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
            "agent_tasks",
        )
    }
    if any(counts.values()):
        populated = ", ".join(
            f"{table_name}={count}"
            for table_name, count in counts.items()
            if count
        )
        raise RuntimeError(
            "The exact semantic work-cycle refactor intentionally does not "
            "guess source-review identities for pre-release novel history. "
            "Reset the development database and rerun the migration. "
            f"Populated tables: {populated}."
        )


def _remove_application_schema() -> None:
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    for table_name in inspector.get_table_names():
        if table_name == "alembic_version":
            continue
        quoted_name = table_name.replace('"', '""')
        connection.exec_driver_sql(f'DROP TABLE "{quoted_name}"')


def _rebuild_declared_schema() -> None:
    _remove_application_schema()
    metadata.create_all(bind=op.get_bind(), checkfirst=False)


def upgrade() -> None:
    _require_empty_incompatible_history()
    _rebuild_declared_schema()


def downgrade() -> None:
    _require_empty_incompatible_history()
    # The immediately preceding incompatible revision also builds from the
    # declared metadata. Rebuilding here preserves its empty-development-DB
    # downgrade semantics before Alembic stamps d82f1c4a7b90.
    _rebuild_declared_schema()
