"""add Arc outline effective points and Chapter provenance

Revision ID: c4a7d91e2b65
Revises: a91f3c7d2e60
Create Date: 2026-07-28
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "c4a7d91e2b65"
down_revision: str | None = "a91f3c7d2e60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_WORKSPACE_PLAN_COUNT_SHAPE_V1 = (
    "((plan_ref_id IS NULL "
    "AND minimum_cumulative_chapter_count IS NULL "
    "AND recommended_closure_cumulative_chapter_count IS NULL "
    "AND maximum_cumulative_chapter_count IS NULL "
    "AND closure_cumulative_chapter_count IS NULL) "
    "OR (plan_ref_id IS NOT NULL "
    "AND minimum_cumulative_chapter_count >= 1 "
    "AND recommended_closure_cumulative_chapter_count "
    ">= minimum_cumulative_chapter_count "
    "AND maximum_cumulative_chapter_count "
    ">= recommended_closure_cumulative_chapter_count "
    "AND closure_cumulative_chapter_count "
    "BETWEEN minimum_cumulative_chapter_count "
    "AND maximum_cumulative_chapter_count))"
)

_WORKSPACE_PLAN_COUNT_SHAPE_V2 = (
    "((plan_ref_id IS NULL "
    "AND planned_after_cumulative_chapter_count IS NULL "
    "AND planned_after_arc_chapter_count IS NULL "
    "AND minimum_cumulative_chapter_count IS NULL "
    "AND recommended_closure_cumulative_chapter_count IS NULL "
    "AND maximum_cumulative_chapter_count IS NULL "
    "AND closure_cumulative_chapter_count IS NULL) "
    "OR (plan_ref_id IS NOT NULL "
    "AND planned_after_cumulative_chapter_count >= 0 "
    "AND planned_after_arc_chapter_count >= 0 "
    "AND planned_after_arc_chapter_count "
    "<= planned_after_cumulative_chapter_count "
    "AND minimum_cumulative_chapter_count >= 1 "
    "AND recommended_closure_cumulative_chapter_count "
    ">= minimum_cumulative_chapter_count "
    "AND maximum_cumulative_chapter_count "
    ">= recommended_closure_cumulative_chapter_count "
    "AND closure_cumulative_chapter_count "
    "BETWEEN minimum_cumulative_chapter_count "
    "AND maximum_cumulative_chapter_count "
    "AND closure_cumulative_chapter_count "
    ">= planned_after_cumulative_chapter_count))"
)

_CHAPTER_COUNT_RANGES_V1 = (
    "minimum_cumulative_chapter_count >= 1 "
    "AND recommended_closure_cumulative_chapter_count "
    ">= minimum_cumulative_chapter_count "
    "AND maximum_cumulative_chapter_count "
    ">= recommended_closure_cumulative_chapter_count "
    "AND closure_cumulative_chapter_count "
    "BETWEEN minimum_cumulative_chapter_count "
    "AND maximum_cumulative_chapter_count"
)

_CHAPTER_COUNT_RANGES_V2 = (
    "planned_after_cumulative_chapter_count >= 0 "
    "AND planned_after_arc_chapter_count >= 0 "
    "AND planned_after_arc_chapter_count "
    "<= planned_after_cumulative_chapter_count "
    "AND minimum_cumulative_chapter_count >= 1 "
    "AND recommended_closure_cumulative_chapter_count "
    ">= minimum_cumulative_chapter_count "
    "AND maximum_cumulative_chapter_count "
    ">= recommended_closure_cumulative_chapter_count "
    "AND closure_cumulative_chapter_count "
    "BETWEEN minimum_cumulative_chapter_count "
    "AND maximum_cumulative_chapter_count "
    "AND closure_cumulative_chapter_count "
    ">= planned_after_cumulative_chapter_count"
)


def _require_empty_pre_release_history() -> None:
    connection = op.get_bind()
    counts = {
        table_name: connection.execute(
            sa.text(f"SELECT count(*) FROM {table_name}")
        ).scalar_one()
        for table_name in (
            "story_arcs",
            "chapters",
            "arc_workspaces",
            "arc_review_submissions",
            "arc_baselines",
        )
    }
    if any(counts.values()):
        populated = ", ".join(
            f"{table_name}={count}"
            for table_name, count in counts.items()
            if count
        )
        raise RuntimeError(
            "The Arc Chapter-outline refactor intentionally does not synthesize "
            "provenance for pre-release story history. Reset the development "
            f"database and rerun the migration. Populated tables: {populated}."
        )


def upgrade() -> None:
    _require_empty_pre_release_history()

    with op.batch_alter_table("arc_workspaces", recreate="always") as batch_op:
        batch_op.drop_constraint(
            batch_op.f("ck_arc_workspaces_plan_count_shape"),
            type_="check",
        )
        batch_op.add_column(
            sa.Column("planned_after_cumulative_chapter_count", sa.Integer())
        )
        batch_op.add_column(
            sa.Column("planned_after_arc_chapter_count", sa.Integer())
        )
        batch_op.create_check_constraint(
            batch_op.f("ck_arc_workspaces_plan_count_shape"),
            _WORKSPACE_PLAN_COUNT_SHAPE_V2,
        )

    for table_name in ("arc_review_submissions", "arc_baselines"):
        with op.batch_alter_table(table_name, recreate="always") as batch_op:
            batch_op.drop_constraint(
                batch_op.f(f"ck_{table_name}_chapter_count_ranges"),
                type_="check",
            )
            batch_op.add_column(
                sa.Column(
                    "planned_after_cumulative_chapter_count",
                    sa.Integer(),
                    nullable=False,
                )
            )
            batch_op.add_column(
                sa.Column(
                    "planned_after_arc_chapter_count",
                    sa.Integer(),
                    nullable=False,
                )
            )
            batch_op.create_check_constraint(
                batch_op.f(f"ck_{table_name}_chapter_count_ranges"),
                _CHAPTER_COUNT_RANGES_V2,
            )

    with op.batch_alter_table("chapters", recreate="always") as batch_op:
        batch_op.add_column(
            sa.Column("outline_arc_baseline_id", sa.String(), nullable=False)
        )
        batch_op.create_foreign_key(
            batch_op.f(
                "fk_chapters_project_id_book_id_arc_id_"
                "outline_arc_baseline_id_arc_baselines"
            ),
            "arc_baselines",
            [
                "project_id",
                "book_id",
                "arc_id",
                "outline_arc_baseline_id",
            ],
            ["project_id", "book_id", "arc_id", "id"],
        )
        batch_op.create_index(
            "ix_chapters_project_id_book_id_arc_id_outline_arc_baseline_id",
            [
                "project_id",
                "book_id",
                "arc_id",
                "outline_arc_baseline_id",
            ],
        )


def downgrade() -> None:
    with op.batch_alter_table("chapters", recreate="always") as batch_op:
        batch_op.drop_index(
            "ix_chapters_project_id_book_id_arc_id_outline_arc_baseline_id"
        )
        batch_op.drop_constraint(
            batch_op.f(
                "fk_chapters_project_id_book_id_arc_id_"
                "outline_arc_baseline_id_arc_baselines"
            ),
            type_="foreignkey",
        )
        batch_op.drop_column("outline_arc_baseline_id")

    for table_name in ("arc_baselines", "arc_review_submissions"):
        with op.batch_alter_table(table_name, recreate="always") as batch_op:
            batch_op.drop_constraint(
                batch_op.f(f"ck_{table_name}_chapter_count_ranges"),
                type_="check",
            )
            batch_op.drop_column("planned_after_arc_chapter_count")
            batch_op.drop_column("planned_after_cumulative_chapter_count")
            batch_op.create_check_constraint(
                batch_op.f(f"ck_{table_name}_chapter_count_ranges"),
                _CHAPTER_COUNT_RANGES_V1,
            )

    with op.batch_alter_table("arc_workspaces", recreate="always") as batch_op:
        batch_op.drop_constraint(
            batch_op.f("ck_arc_workspaces_plan_count_shape"),
            type_="check",
        )
        batch_op.drop_column("planned_after_arc_chapter_count")
        batch_op.drop_column("planned_after_cumulative_chapter_count")
        batch_op.create_check_constraint(
            batch_op.f("ck_arc_workspaces_plan_count_shape"),
            _WORKSPACE_PLAN_COUNT_SHAPE_V1,
        )
