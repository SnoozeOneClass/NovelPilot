"""make Arc chapter checkpoints explicitly cumulative

Revision ID: a91f3c7d2e60
Revises: 6d4d321a8c7e
Create Date: 2026-07-27
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "a91f3c7d2e60"
down_revision: str | None = "6d4d321a8c7e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_ARC_RANGE_RENAMES = (
    ("minimum_chapter_count", "minimum_cumulative_chapter_count"),
    (
        "recommended_closure_chapter_count",
        "recommended_closure_cumulative_chapter_count",
    ),
    ("maximum_chapter_count", "maximum_cumulative_chapter_count"),
    ("closure_chapter_count", "closure_cumulative_chapter_count"),
)


def _rename(
    table_name: str,
    old_name: str,
    new_name: str,
    *,
    nullable: bool,
) -> None:
    op.alter_column(
        table_name,
        old_name,
        new_column_name=new_name,
        existing_type=sa.Integer(),
        existing_nullable=nullable,
    )


def upgrade() -> None:
    for old_name, new_name in _ARC_RANGE_RENAMES:
        _rename("arc_workspaces", old_name, new_name, nullable=True)
        _rename("arc_review_submissions", old_name, new_name, nullable=False)
        _rename("arc_baselines", old_name, new_name, nullable=False)
    _rename(
        "arc_approvals",
        "closure_chapter_count",
        "closure_cumulative_chapter_count",
        nullable=True,
    )
    _rename(
        "arc_closure_reviews",
        "committed_chapter_count",
        "cumulative_committed_chapter_count",
        nullable=False,
    )
    _rename(
        "arc_closure_reviews",
        "closure_chapter_count",
        "closure_cumulative_chapter_count",
        nullable=False,
    )
    _rename(
        "arc_closures",
        "committed_chapter_count",
        "cumulative_committed_chapter_count",
        nullable=False,
    )
    with op.batch_alter_table("arc_closures") as batch_op:
        batch_op.drop_constraint(
            op.f("ck_arc_closures_committed_chapter_count_positive"),
            type_="check",
        )
        batch_op.create_check_constraint(
            op.f(
                "ck_arc_closures_cumulative_committed_chapter_count_positive"
            ),
            "cumulative_committed_chapter_count >= 1",
        )


def downgrade() -> None:
    _rename(
        "arc_closures",
        "cumulative_committed_chapter_count",
        "committed_chapter_count",
        nullable=False,
    )
    with op.batch_alter_table("arc_closures") as batch_op:
        batch_op.drop_constraint(
            op.f(
                "ck_arc_closures_cumulative_committed_chapter_count_positive"
            ),
            type_="check",
        )
        batch_op.create_check_constraint(
            op.f("ck_arc_closures_committed_chapter_count_positive"),
            "committed_chapter_count >= 1",
        )
    _rename(
        "arc_closure_reviews",
        "closure_cumulative_chapter_count",
        "closure_chapter_count",
        nullable=False,
    )
    _rename(
        "arc_closure_reviews",
        "cumulative_committed_chapter_count",
        "committed_chapter_count",
        nullable=False,
    )
    _rename(
        "arc_approvals",
        "closure_cumulative_chapter_count",
        "closure_chapter_count",
        nullable=True,
    )
    for old_name, new_name in reversed(_ARC_RANGE_RENAMES):
        _rename("arc_baselines", new_name, old_name, nullable=False)
        _rename("arc_review_submissions", new_name, old_name, nullable=False)
        _rename("arc_workspaces", new_name, old_name, nullable=True)
