"""separate Book-parent request origin from the reviewed Arc subject

Revision ID: b6a2e74c9d18
Revises: f31c8b7a2e14
Create Date: 2026-08-03

This is an intentionally incompatible development-stage migration. Existing
pre-release runs did not freeze the Arc baseline actually reviewed by each
Book-parent task, so populated databases cannot be upgraded without inventing
authority history.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.db.schema import metadata


revision: str = "b6a2e74c9d18"
down_revision: str | None = "f31c8b7a2e14"
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
            "book_parent_reviews",
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
            "The Book-parent subject-identity refactor cannot infer which Arc "
            "baseline a historical task actually reviewed. Reset the development "
            f"database and rerun the migration. Populated tables: {populated}."
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
    _rebuild_declared_schema()
