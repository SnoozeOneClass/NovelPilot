"""add explicit Domain delivery failure states

Revision ID: 7c0d2a9f4b31
Revises: ef42ab7a9212
Create Date: 2026-07-23
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op


revision: str = "7c0d2a9f4b31"
down_revision: str | None = "ef42ab7a9212"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("agent_task_attempts", recreate="always") as batch_op:
        batch_op.drop_constraint(op.f("ck_agent_task_attempts_status"), type_="check")
        batch_op.create_check_constraint(
            op.f("ck_agent_task_attempts_status"),
            (
                "status IN ('queued', 'running', 'succeeded', 'failed', 'interrupted', "
                "'delivery_failed')"
            ),
        )
        batch_op.create_check_constraint(
            op.f("ck_agent_task_attempts_delivery_failed_fields"),
            (
                "status <> 'delivery_failed' OR (result_ref_id IS NOT NULL "
                "AND finished_at_ms IS NOT NULL AND error_code IS NOT NULL "
                "AND error_category = 'domain_delivery' AND error_ref_id IS NOT NULL "
                "AND owner_instance_id IS NULL AND lease_token IS NULL "
                "AND lease_expires_at_ms IS NULL AND heartbeat_at_ms IS NULL)"
            ),
        )

    with op.batch_alter_table("agent_tasks", recreate="always") as batch_op:
        batch_op.drop_constraint(op.f("ck_agent_tasks_delivery_state"), type_="check")
        batch_op.drop_constraint(op.f("ck_agent_tasks_success_delivery"), type_="check")
        batch_op.create_check_constraint(
            op.f("ck_agent_tasks_delivery_state"),
            (
                "delivery_state IN "
                "('not_ready', 'pending', 'applied', 'discarded_stale', 'failed')"
            ),
        )
        batch_op.create_check_constraint(
            op.f("ck_agent_tasks_success_delivery"),
            (
                "((status = 'succeeded' AND successful_attempt_id IS NOT NULL "
                "AND delivery_state IN ('pending', 'applied', 'discarded_stale')) "
                "OR (status = 'failed' AND successful_attempt_id IS NULL "
                "AND delivery_state IN ('not_ready', 'failed')) "
                "OR (status NOT IN ('succeeded', 'failed') "
                "AND successful_attempt_id IS NULL AND delivery_state = 'not_ready'))"
            ),
        )


def downgrade() -> None:
    with op.batch_alter_table("agent_tasks", recreate="always") as batch_op:
        batch_op.drop_constraint(op.f("ck_agent_tasks_success_delivery"), type_="check")
        batch_op.drop_constraint(op.f("ck_agent_tasks_delivery_state"), type_="check")
        batch_op.create_check_constraint(
            op.f("ck_agent_tasks_delivery_state"),
            "delivery_state IN ('not_ready', 'pending', 'applied', 'discarded_stale')",
        )
        batch_op.create_check_constraint(
            op.f("ck_agent_tasks_success_delivery"),
            (
                "((status = 'succeeded' AND successful_attempt_id IS NOT NULL "
                "AND delivery_state IN ('pending', 'applied', 'discarded_stale')) "
                "OR (status <> 'succeeded' AND successful_attempt_id IS NULL "
                "AND delivery_state = 'not_ready'))"
            ),
        )

    with op.batch_alter_table("agent_task_attempts", recreate="always") as batch_op:
        batch_op.drop_constraint(
            op.f("ck_agent_task_attempts_delivery_failed_fields"),
            type_="check",
        )
        batch_op.drop_constraint(op.f("ck_agent_task_attempts_status"), type_="check")
        batch_op.create_check_constraint(
            op.f("ck_agent_task_attempts_status"),
            "status IN ('queued', 'running', 'succeeded', 'failed', 'interrupted')",
        )
