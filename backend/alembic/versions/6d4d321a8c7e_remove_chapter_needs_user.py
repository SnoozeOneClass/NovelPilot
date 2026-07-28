"""remove unsupported Chapter needs-user decision

Revision ID: 6d4d321a8c7e
Revises: 1be6decc58a4
Create Date: 2026-07-27
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "6d4d321a8c7e"
down_revision: str | None = "1be6decc58a4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    legacy_count = op.get_bind().execute(
        sa.text(
            "SELECT count(*) FROM chapter_reviews WHERE decision = 'needs_user'"
        )
    ).scalar_one()
    if legacy_count:
        raise RuntimeError(
            "The Chapter recovery refactor intentionally does not reinterpret "
            "pre-release needs_user reviews. Delete the incompatible test projects "
            "or restore a clean database, then rerun the migration."
        )
    with op.batch_alter_table("chapter_reviews", recreate="always") as batch_op:
        batch_op.drop_constraint(
            batch_op.f("ck_chapter_reviews_decision"),
            type_="check",
        )
        batch_op.create_check_constraint(
            batch_op.f("ck_chapter_reviews_decision"),
            "decision IN ('pass', 'local_repair', 'escalate_to_arc')",
        )


def downgrade() -> None:
    with op.batch_alter_table("chapter_reviews", recreate="always") as batch_op:
        batch_op.drop_constraint(
            batch_op.f("ck_chapter_reviews_decision"),
            type_="check",
        )
        batch_op.create_check_constraint(
            batch_op.f("ck_chapter_reviews_decision"),
            (
                "decision IN "
                "('pass', 'local_repair', 'escalate_to_arc', 'needs_user')"
            ),
        )
