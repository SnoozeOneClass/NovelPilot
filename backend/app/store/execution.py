from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, cast

from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from app.agents.contracts import AgentTaskPlan
from app.db.schema import (
    agent_evidence_items,
    agent_task_attempts,
    agent_tasks,
    generation_runs,
)


@dataclass(frozen=True, slots=True)
class SuccessfulTaskRecord:
    task_id: str
    attempt_id: str
    project_id: str
    run_id: str
    role: str
    task_kind: str
    scope_layer: str
    book_id: str
    arc_id: str | None
    chapter_id: str | None
    workspace_lock_version: int | None
    book_baseline_id: str | None
    arc_baseline_id: str | None
    chapter_baseline_id: str | None
    canon_baseline_id: str
    result_ref_id: str
    delivery_state: str


@dataclass(frozen=True, slots=True)
class FrozenTaskContentRefs:
    task_plan_ref_id: str
    input_manifest_ref_id: str
    input_messages_ref_id: str
    profile_snapshot_ref_id: str


@dataclass(frozen=True, slots=True)
class AgentAttemptRecord:
    attempt_id: str
    project_id: str
    task_id: str
    attempt_number: int
    retry_kind: Literal["initial", "crash_replay", "user_retry"]
    status: Literal["queued", "running", "succeeded", "failed", "interrupted"]
    framework_fingerprint: str


@dataclass(frozen=True, slots=True)
class EvidenceItemDraft:
    item_kind: Literal[
        "message",
        "tool_call",
        "tool_result",
        "validation",
        "transport_retry",
        "model_retry",
        "completion_message",
        "diagnostic_attachment",
    ]
    content_ref_id: str | None = None
    metadata_json: str | None = None


@dataclass(frozen=True, slots=True)
class AbandonedAttemptRecord:
    project_id: str
    run_id: str
    task_id: str
    attempt_id: str
    attempt_number: int
    retry_kind: str
    framework_fingerprint: str


@dataclass(frozen=True, slots=True)
class FailedTaskRecord:
    project_id: str
    run_id: str
    task_id: str
    attempt_id: str
    attempt_number: int
    error_code: str
    error_ref_id: str


@dataclass(frozen=True, slots=True)
class ActionableTaskRecord:
    project_id: str
    run_id: str
    task_id: str
    attempt_id: str
    task_key: str
    role: str
    task_kind: str
    scope_layer: str
    book_id: str
    arc_id: str | None
    chapter_id: str | None
    workspace_lock_version: int | None
    book_baseline_id: str | None
    arc_baseline_id: str | None
    chapter_baseline_id: str | None
    canon_baseline_id: str
    profile_id: str
    task_status: Literal["queued", "succeeded"]
    attempt_status: Literal["queued", "succeeded"]
    delivery_state: str


@dataclass(frozen=True, slots=True)
class TaskSummaryRecord:
    task_id: str
    run_id: str
    role: str
    task_kind: str
    scope_layer: str
    arc_id: str | None
    chapter_id: str | None
    status: str
    delivery_state: str
    profile_id: str
    model_id: str
    attempt_id: str | None
    attempt_number: int | None
    attempt_status: str | None
    retry_kind: str | None
    provider_request_count: int | None
    transport_retry_count: int | None
    model_request_count: int | None
    input_tokens: int | None
    output_tokens: int | None
    error_code: str | None
    error_ref_id: str | None
    diagnostic_ref_id: str | None
    created_at_ms: int
    updated_at_ms: int


@dataclass(frozen=True, slots=True)
class AttemptSummaryRecord:
    task_id: str
    run_id: str
    role: str
    task_kind: str
    scope_layer: str
    arc_id: str | None
    chapter_id: str | None
    task_status: str
    delivery_state: str
    profile_id: str
    model_id: str
    profile_fingerprint: str
    output_schema_id: str
    output_schema_version: int
    harness_policy_id: str
    harness_policy_version: int
    attempt_id: str
    attempt_number: int
    retry_kind: str
    attempt_status: str
    framework_fingerprint: str
    provider_request_count: int
    transport_retry_count: int
    model_request_count: int
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    error_code: str | None
    error_category: str | None
    http_status: int | None
    error_ref_id: str | None
    diagnostic_ref_id: str | None
    created_at_ms: int
    started_at_ms: int | None
    finished_at_ms: int | None


class ExecutionRepository:
    def __init__(self, connection: AsyncConnection) -> None:
        self._connection = connection

    async def find_actionable_for_run(self, *, run_id: str) -> ActionableTaskRecord | None:
        """Return the one task the driver must deliver or execute before routing again."""
        row = (
            await self._connection.execute(
                select(
                    agent_tasks.c.project_id,
                    agent_tasks.c.run_id,
                    agent_tasks.c.id.label("task_id"),
                    agent_task_attempts.c.id.label("attempt_id"),
                    agent_tasks.c.task_key,
                    agent_tasks.c.role,
                    agent_tasks.c.task_kind,
                    agent_tasks.c.scope_layer,
                    agent_tasks.c.book_id,
                    agent_tasks.c.arc_id,
                    agent_tasks.c.chapter_id,
                    agent_tasks.c.workspace_lock_version,
                    agent_tasks.c.book_baseline_id,
                    agent_tasks.c.arc_baseline_id,
                    agent_tasks.c.chapter_baseline_id,
                    agent_tasks.c.canon_baseline_id,
                    agent_tasks.c.profile_id,
                    agent_tasks.c.status.label("task_status"),
                    agent_task_attempts.c.status.label("attempt_status"),
                    agent_tasks.c.delivery_state,
                )
                .join(
                    agent_task_attempts,
                    (agent_task_attempts.c.project_id == agent_tasks.c.project_id)
                    & (agent_task_attempts.c.task_id == agent_tasks.c.id)
                    & (
                        (
                            (agent_tasks.c.status == "succeeded")
                            & (agent_task_attempts.c.id == agent_tasks.c.successful_attempt_id)
                        )
                        | (
                            (agent_tasks.c.status == "queued")
                            & (agent_task_attempts.c.status == "queued")
                        )
                    ),
                )
                .where(
                    agent_tasks.c.run_id == run_id,
                    (
                        (
                            (agent_tasks.c.status == "succeeded")
                            & (agent_tasks.c.delivery_state == "pending")
                        )
                        | (agent_tasks.c.status == "queued")
                    ),
                )
                .order_by(
                    # A completed Provider call must be delivered before another call is planned.
                    (agent_tasks.c.status == "succeeded").desc(),
                    agent_tasks.c.created_at_ms,
                    agent_tasks.c.id,
                    agent_task_attempts.c.attempt_number.desc(),
                )
                .limit(1)
            )
        ).mappings().one_or_none()
        if row is None:
            return None
        return ActionableTaskRecord(
            project_id=cast(str, row["project_id"]),
            run_id=cast(str, row["run_id"]),
            task_id=cast(str, row["task_id"]),
            attempt_id=cast(str, row["attempt_id"]),
            task_key=cast(str, row["task_key"]),
            role=cast(str, row["role"]),
            task_kind=cast(str, row["task_kind"]),
            scope_layer=cast(str, row["scope_layer"]),
            book_id=cast(str, row["book_id"]),
            arc_id=cast(str | None, row["arc_id"]),
            chapter_id=cast(str | None, row["chapter_id"]),
            workspace_lock_version=cast(int | None, row["workspace_lock_version"]),
            book_baseline_id=cast(str | None, row["book_baseline_id"]),
            arc_baseline_id=cast(str | None, row["arc_baseline_id"]),
            chapter_baseline_id=cast(str | None, row["chapter_baseline_id"]),
            canon_baseline_id=cast(str, row["canon_baseline_id"]),
            profile_id=cast(str, row["profile_id"]),
            task_status=cast(Literal["queued", "succeeded"], row["task_status"]),
            attempt_status=cast(Literal["queued", "succeeded"], row["attempt_status"]),
            delivery_state=cast(str, row["delivery_state"]),
        )

    async def has_applied_task(
        self,
        *,
        project_id: str,
        run_id: str,
        task_kind: str,
        book_id: str,
        arc_id: str | None = None,
        chapter_id: str | None = None,
        book_baseline_id: str | None = None,
        arc_baseline_id: str | None = None,
        chapter_baseline_id: str | None = None,
        created_after_ms: int | None = None,
    ) -> bool:
        conditions = [
            agent_tasks.c.project_id == project_id,
            agent_tasks.c.run_id == run_id,
            agent_tasks.c.task_kind == task_kind,
            agent_tasks.c.book_id == book_id,
            agent_tasks.c.status == "succeeded",
            agent_tasks.c.delivery_state == "applied",
        ]
        for column, value in (
            (agent_tasks.c.arc_id, arc_id),
            (agent_tasks.c.chapter_id, chapter_id),
            (agent_tasks.c.book_baseline_id, book_baseline_id),
            (agent_tasks.c.arc_baseline_id, arc_baseline_id),
            (agent_tasks.c.chapter_baseline_id, chapter_baseline_id),
        ):
            conditions.append(column.is_(None) if value is None else column == value)
        if created_after_ms is not None:
            conditions.append(agent_tasks.c.created_at_ms >= created_after_ms)
        return (
            await self._connection.scalar(select(agent_tasks.c.id).where(*conditions).limit(1))
            is not None
        )

    async def list_task_summaries(
        self, *, project_id: str, limit: int = 100
    ) -> list[TaskSummaryRecord]:
        if limit < 1:
            raise ValueError("Task summary limit must be positive.")
        latest_attempts = (
            select(
                agent_task_attempts.c.project_id,
                agent_task_attempts.c.task_id,
                func.max(agent_task_attempts.c.attempt_number).label("attempt_number"),
            )
            .where(agent_task_attempts.c.project_id == project_id)
            .group_by(agent_task_attempts.c.project_id, agent_task_attempts.c.task_id)
            .subquery()
        )
        rows = (
            await self._connection.execute(
                select(
                    agent_tasks.c.id.label("task_id"),
                    agent_tasks.c.run_id,
                    agent_tasks.c.role,
                    agent_tasks.c.task_kind,
                    agent_tasks.c.scope_layer,
                    agent_tasks.c.arc_id,
                    agent_tasks.c.chapter_id,
                    agent_tasks.c.status,
                    agent_tasks.c.delivery_state,
                    agent_tasks.c.profile_id,
                    agent_tasks.c.model_id,
                    agent_task_attempts.c.id.label("attempt_id"),
                    agent_task_attempts.c.attempt_number,
                    agent_task_attempts.c.status.label("attempt_status"),
                    agent_task_attempts.c.retry_kind,
                    agent_task_attempts.c.provider_request_count,
                    agent_task_attempts.c.transport_retry_count,
                    agent_task_attempts.c.model_request_count,
                    agent_task_attempts.c.input_tokens,
                    agent_task_attempts.c.output_tokens,
                    agent_task_attempts.c.error_code,
                    agent_task_attempts.c.error_ref_id,
                    agent_task_attempts.c.diagnostic_ref_id,
                    agent_tasks.c.created_at_ms,
                    agent_tasks.c.updated_at_ms,
                )
                .outerjoin(
                    latest_attempts,
                    (latest_attempts.c.project_id == agent_tasks.c.project_id)
                    & (latest_attempts.c.task_id == agent_tasks.c.id),
                )
                .outerjoin(
                    agent_task_attempts,
                    (agent_task_attempts.c.project_id == latest_attempts.c.project_id)
                    & (agent_task_attempts.c.task_id == latest_attempts.c.task_id)
                    & (
                        agent_task_attempts.c.attempt_number
                        == latest_attempts.c.attempt_number
                    ),
                )
                .where(agent_tasks.c.project_id == project_id)
                .order_by(agent_tasks.c.created_at_ms.desc(), agent_tasks.c.id.desc())
                .limit(limit)
            )
        ).mappings()
        return [
            TaskSummaryRecord(
                task_id=cast(str, row["task_id"]),
                run_id=cast(str, row["run_id"]),
                role=cast(str, row["role"]),
                task_kind=cast(str, row["task_kind"]),
                scope_layer=cast(str, row["scope_layer"]),
                arc_id=cast(str | None, row["arc_id"]),
                chapter_id=cast(str | None, row["chapter_id"]),
                status=cast(str, row["status"]),
                delivery_state=cast(str, row["delivery_state"]),
                profile_id=cast(str, row["profile_id"]),
                model_id=cast(str, row["model_id"]),
                attempt_id=cast(str | None, row["attempt_id"]),
                attempt_number=cast(int | None, row["attempt_number"]),
                attempt_status=cast(str | None, row["attempt_status"]),
                retry_kind=cast(str | None, row["retry_kind"]),
                provider_request_count=cast(int | None, row["provider_request_count"]),
                transport_retry_count=cast(int | None, row["transport_retry_count"]),
                model_request_count=cast(int | None, row["model_request_count"]),
                input_tokens=cast(int | None, row["input_tokens"]),
                output_tokens=cast(int | None, row["output_tokens"]),
                error_code=cast(str | None, row["error_code"]),
                error_ref_id=cast(str | None, row["error_ref_id"]),
                diagnostic_ref_id=cast(str | None, row["diagnostic_ref_id"]),
                created_at_ms=cast(int, row["created_at_ms"]),
                updated_at_ms=cast(int, row["updated_at_ms"]),
            )
            for row in rows
        ]

    async def list_attempt_summaries(
        self,
        *,
        project_id: str,
    ) -> list[AttemptSummaryRecord]:
        rows = (
            await self._connection.execute(
                select(
                    agent_tasks.c.id.label("task_id"),
                    agent_tasks.c.run_id,
                    agent_tasks.c.role,
                    agent_tasks.c.task_kind,
                    agent_tasks.c.scope_layer,
                    agent_tasks.c.arc_id,
                    agent_tasks.c.chapter_id,
                    agent_tasks.c.status.label("task_status"),
                    agent_tasks.c.delivery_state,
                    agent_tasks.c.profile_id,
                    agent_tasks.c.model_id,
                    agent_tasks.c.profile_fingerprint,
                    agent_tasks.c.output_schema_id,
                    agent_tasks.c.output_schema_version,
                    agent_tasks.c.harness_policy_id,
                    agent_tasks.c.harness_policy_version,
                    agent_task_attempts.c.id.label("attempt_id"),
                    agent_task_attempts.c.attempt_number,
                    agent_task_attempts.c.retry_kind,
                    agent_task_attempts.c.status.label("attempt_status"),
                    agent_task_attempts.c.framework_fingerprint,
                    agent_task_attempts.c.provider_request_count,
                    agent_task_attempts.c.transport_retry_count,
                    agent_task_attempts.c.model_request_count,
                    agent_task_attempts.c.input_tokens,
                    agent_task_attempts.c.output_tokens,
                    agent_task_attempts.c.total_tokens,
                    agent_task_attempts.c.error_code,
                    agent_task_attempts.c.error_category,
                    agent_task_attempts.c.http_status,
                    agent_task_attempts.c.error_ref_id,
                    agent_task_attempts.c.diagnostic_ref_id,
                    agent_task_attempts.c.created_at_ms,
                    agent_task_attempts.c.started_at_ms,
                    agent_task_attempts.c.finished_at_ms,
                )
                .join(
                    agent_tasks,
                    (agent_tasks.c.project_id == agent_task_attempts.c.project_id)
                    & (agent_tasks.c.id == agent_task_attempts.c.task_id),
                )
                .where(agent_task_attempts.c.project_id == project_id)
                .order_by(
                    agent_task_attempts.c.created_at_ms,
                    agent_task_attempts.c.task_id,
                    agent_task_attempts.c.attempt_number,
                )
            )
        ).mappings()
        return [
            AttemptSummaryRecord(
                task_id=cast(str, row["task_id"]),
                run_id=cast(str, row["run_id"]),
                role=cast(str, row["role"]),
                task_kind=cast(str, row["task_kind"]),
                scope_layer=cast(str, row["scope_layer"]),
                arc_id=cast(str | None, row["arc_id"]),
                chapter_id=cast(str | None, row["chapter_id"]),
                task_status=cast(str, row["task_status"]),
                delivery_state=cast(str, row["delivery_state"]),
                profile_id=cast(str, row["profile_id"]),
                model_id=cast(str, row["model_id"]),
                profile_fingerprint=cast(str, row["profile_fingerprint"]),
                output_schema_id=cast(str, row["output_schema_id"]),
                output_schema_version=cast(int, row["output_schema_version"]),
                harness_policy_id=cast(str, row["harness_policy_id"]),
                harness_policy_version=cast(int, row["harness_policy_version"]),
                attempt_id=cast(str, row["attempt_id"]),
                attempt_number=cast(int, row["attempt_number"]),
                retry_kind=cast(str, row["retry_kind"]),
                attempt_status=cast(str, row["attempt_status"]),
                framework_fingerprint=cast(str, row["framework_fingerprint"]),
                provider_request_count=cast(int, row["provider_request_count"]),
                transport_retry_count=cast(int, row["transport_retry_count"]),
                model_request_count=cast(int, row["model_request_count"]),
                input_tokens=cast(int | None, row["input_tokens"]),
                output_tokens=cast(int | None, row["output_tokens"]),
                total_tokens=cast(int | None, row["total_tokens"]),
                error_code=cast(str | None, row["error_code"]),
                error_category=cast(str | None, row["error_category"]),
                http_status=cast(int | None, row["http_status"]),
                error_ref_id=cast(str | None, row["error_ref_id"]),
                diagnostic_ref_id=cast(str | None, row["diagnostic_ref_id"]),
                created_at_ms=cast(int, row["created_at_ms"]),
                started_at_ms=cast(int | None, row["started_at_ms"]),
                finished_at_ms=cast(int | None, row["finished_at_ms"]),
            )
            for row in rows
        ]

    async def get_task_plan_ref(self, *, project_id: str, task_id: str) -> str | None:
        return cast(
            str | None,
            await self._connection.scalar(
                select(agent_tasks.c.task_plan_ref_id).where(
                    agent_tasks.c.project_id == project_id,
                    agent_tasks.c.id == task_id,
                )
            ),
        )

    async def insert_task(
        self,
        *,
        plan: AgentTaskPlan,
        refs: FrozenTaskContentRefs,
        created_at_ms: int,
    ) -> None:
        await self._connection.execute(
            agent_tasks.insert().values(
                id=plan.task_id,
                project_id=plan.project_id,
                run_id=plan.run_id,
                task_key=plan.task_key,
                action_key=plan.action_key,
                predecessor_task_id=plan.predecessor_task_id,
                role=plan.role,
                task_kind=plan.task_kind,
                scope_layer=plan.scope_layer,
                book_id=plan.book_id,
                arc_id=plan.arc_id,
                chapter_id=plan.chapter_id,
                workspace_lock_version=plan.workspace_lock_version,
                book_baseline_id=plan.book_baseline_id,
                arc_baseline_id=plan.arc_baseline_id,
                chapter_baseline_id=plan.chapter_baseline_id,
                canon_baseline_id=plan.canon_baseline_id,
                task_plan_ref_id=refs.task_plan_ref_id,
                input_manifest_ref_id=refs.input_manifest_ref_id,
                input_messages_ref_id=refs.input_messages_ref_id,
                profile_snapshot_ref_id=refs.profile_snapshot_ref_id,
                input_fingerprint=plan.input_fingerprint,
                prompt_fingerprint=plan.prompt_fingerprint,
                context_policy_id=plan.context_policy_id,
                context_policy_version=plan.context_policy_version,
                context_policy_fingerprint=plan.context_policy_fingerprint,
                output_schema_id=plan.output_schema_id,
                output_schema_version=plan.output_schema_version,
                output_schema_fingerprint=plan.output_schema_fingerprint,
                rubric_id=plan.rubric_id,
                rubric_version=plan.rubric_version,
                harness_policy_id=plan.harness_policy_id,
                harness_policy_version=plan.harness_policy_version,
                profile_id=plan.profile_snapshot.profile_id,
                profile_fingerprint=plan.profile_fingerprint,
                api_family=plan.profile_snapshot.api_family,
                model_id=plan.profile_snapshot.model_id,
                output_mode=plan.output_mode,
                requires_native_json_schema=int("native_json_schema" in plan.required_capabilities),
                requires_text_streaming=int("text_streaming" in plan.required_capabilities),
                transport_retry_limit=plan.transport_retry_limit,
                model_request_limit=plan.model_request_limit,
                connect_timeout_ms=plan.connect_timeout_ms,
                pool_timeout_ms=plan.pool_timeout_ms,
                write_timeout_ms=plan.write_timeout_ms,
                read_timeout_ms=plan.read_timeout_ms,
                activation_timeout_ms=plan.activation_timeout_ms,
                timeout_policy_id=plan.timeout_policy_id,
                status="queued",
                delivery_state="not_ready",
                created_at_ms=created_at_ms,
                updated_at_ms=created_at_ms,
            )
        )

    async def insert_attempt(
        self,
        *,
        attempt_id: str,
        project_id: str,
        task_id: str,
        attempt_number: int,
        retry_kind: Literal["initial", "crash_replay", "user_retry"],
        predecessor_attempt_id: str | None,
        framework_fingerprint: str,
        created_at_ms: int,
    ) -> None:
        await self._connection.execute(
            agent_task_attempts.insert().values(
                id=attempt_id,
                project_id=project_id,
                task_id=task_id,
                attempt_number=attempt_number,
                retry_kind=retry_kind,
                predecessor_attempt_id=predecessor_attempt_id,
                status="queued",
                framework_fingerprint=framework_fingerprint,
                provider_request_count=0,
                transport_retry_count=0,
                model_request_count=0,
                created_at_ms=created_at_ms,
            )
        )

    async def get_attempt(
        self,
        *,
        project_id: str,
        task_id: str,
        attempt_id: str,
    ) -> AgentAttemptRecord | None:
        row = (
            await self._connection.execute(
                select(agent_task_attempts).where(
                    agent_task_attempts.c.project_id == project_id,
                    agent_task_attempts.c.task_id == task_id,
                    agent_task_attempts.c.id == attempt_id,
                )
            )
        ).mappings().one_or_none()
        if row is None:
            return None
        return AgentAttemptRecord(
            attempt_id=cast(str, row["id"]),
            project_id=cast(str, row["project_id"]),
            task_id=cast(str, row["task_id"]),
            attempt_number=cast(int, row["attempt_number"]),
            retry_kind=cast(
                Literal["initial", "crash_replay", "user_retry"], row["retry_kind"]
            ),
            status=cast(
                Literal["queued", "running", "succeeded", "failed", "interrupted"],
                row["status"],
            ),
            framework_fingerprint=cast(str, row["framework_fingerprint"]),
        )

    async def get_latest_attempt(
        self, *, project_id: str, task_id: str
    ) -> AgentAttemptRecord | None:
        row = (
            await self._connection.execute(
                select(agent_task_attempts)
                .where(
                    agent_task_attempts.c.project_id == project_id,
                    agent_task_attempts.c.task_id == task_id,
                )
                .order_by(agent_task_attempts.c.attempt_number.desc())
                .limit(1)
            )
        ).mappings().one_or_none()
        if row is None:
            return None
        return AgentAttemptRecord(
            attempt_id=cast(str, row["id"]),
            project_id=cast(str, row["project_id"]),
            task_id=cast(str, row["task_id"]),
            attempt_number=cast(int, row["attempt_number"]),
            retry_kind=cast(
                Literal["initial", "crash_replay", "user_retry"], row["retry_kind"]
            ),
            status=cast(
                Literal["queued", "running", "succeeded", "failed", "interrupted"],
                row["status"],
            ),
            framework_fingerprint=cast(str, row["framework_fingerprint"]),
        )

    async def mark_attempt_running(
        self,
        *,
        project_id: str,
        task_id: str,
        attempt_id: str,
        owner_instance_id: str,
        lease_token: str,
        lease_expires_at_ms: int,
        activation_deadline_at_ms: int,
        started_at_ms: int,
    ) -> bool:
        result = await self._connection.execute(
            update(agent_task_attempts)
            .where(
                agent_task_attempts.c.project_id == project_id,
                agent_task_attempts.c.task_id == task_id,
                agent_task_attempts.c.id == attempt_id,
                agent_task_attempts.c.status == "queued",
            )
            .values(
                status="running",
                owner_instance_id=owner_instance_id,
                lease_token=lease_token,
                lease_expires_at_ms=lease_expires_at_ms,
                heartbeat_at_ms=started_at_ms,
                activation_deadline_at_ms=activation_deadline_at_ms,
                started_at_ms=started_at_ms,
            )
        )
        if result.rowcount != 1:
            return False
        task_result = await self._connection.execute(
            update(agent_tasks)
            .where(
                agent_tasks.c.project_id == project_id,
                agent_tasks.c.id == task_id,
                agent_tasks.c.status == "queued",
            )
            .values(status="running", updated_at_ms=started_at_ms)
        )
        return task_result.rowcount == 1

    async def heartbeat_attempt(
        self,
        *,
        project_id: str,
        task_id: str,
        attempt_id: str,
        owner_instance_id: str,
        lease_token: str,
        lease_expires_at_ms: int,
        now_ms: int,
    ) -> bool:
        result = await self._connection.execute(
            update(agent_task_attempts)
            .where(
                agent_task_attempts.c.project_id == project_id,
                agent_task_attempts.c.task_id == task_id,
                agent_task_attempts.c.id == attempt_id,
                agent_task_attempts.c.status == "running",
                agent_task_attempts.c.owner_instance_id == owner_instance_id,
                agent_task_attempts.c.lease_token == lease_token,
            )
            .values(
                heartbeat_at_ms=now_ms,
                lease_expires_at_ms=lease_expires_at_ms,
            )
        )
        return result.rowcount == 1

    async def list_expired_running_attempts(
        self, *, now_ms: int
    ) -> list[AbandonedAttemptRecord]:
        rows = (
            await self._connection.execute(
                select(
                    agent_task_attempts.c.project_id,
                    agent_tasks.c.run_id,
                    agent_task_attempts.c.task_id,
                    agent_task_attempts.c.id.label("attempt_id"),
                    agent_task_attempts.c.attempt_number,
                    agent_task_attempts.c.retry_kind,
                    agent_task_attempts.c.framework_fingerprint,
                )
                .join(
                    agent_tasks,
                    (agent_tasks.c.project_id == agent_task_attempts.c.project_id)
                    & (agent_tasks.c.id == agent_task_attempts.c.task_id),
                )
                .where(
                    agent_task_attempts.c.status == "running",
                    agent_task_attempts.c.lease_expires_at_ms <= now_ms,
                )
                .order_by(agent_task_attempts.c.started_at_ms, agent_task_attempts.c.id)
            )
        ).mappings()
        return [
            AbandonedAttemptRecord(
                project_id=cast(str, row["project_id"]),
                run_id=cast(str, row["run_id"]),
                task_id=cast(str, row["task_id"]),
                attempt_id=cast(str, row["attempt_id"]),
                attempt_number=cast(int, row["attempt_number"]),
                retry_kind=cast(str, row["retry_kind"]),
                framework_fingerprint=cast(str, row["framework_fingerprint"]),
            )
            for row in rows
        ]

    async def interrupt_expired_attempt(
        self, *, record: AbandonedAttemptRecord, now_ms: int
    ) -> bool:
        result = await self._connection.execute(
            update(agent_task_attempts)
            .where(
                agent_task_attempts.c.project_id == record.project_id,
                agent_task_attempts.c.task_id == record.task_id,
                agent_task_attempts.c.id == record.attempt_id,
                agent_task_attempts.c.status == "running",
                agent_task_attempts.c.lease_expires_at_ms <= now_ms,
            )
            .values(
                status="interrupted",
                owner_instance_id=None,
                lease_token=None,
                lease_expires_at_ms=None,
                heartbeat_at_ms=None,
                finished_at_ms=now_ms,
            )
        )
        return result.rowcount == 1

    async def has_crash_replay(self, *, task_id: str) -> bool:
        return (
            await self._connection.scalar(
                select(agent_task_attempts.c.id)
                .where(
                    agent_task_attempts.c.task_id == task_id,
                    agent_task_attempts.c.retry_kind == "crash_replay",
                )
                .limit(1)
            )
            is not None
        )

    async def next_attempt_number(self, *, task_id: str) -> int:
        value = await self._connection.scalar(
            select(func.coalesce(func.max(agent_task_attempts.c.attempt_number), 0)).where(
                agent_task_attempts.c.task_id == task_id
            )
        )
        return cast(int, value) + 1

    async def reset_running_task_to_queued(
        self, *, project_id: str, task_id: str, updated_at_ms: int
    ) -> bool:
        result = await self._connection.execute(
            update(agent_tasks)
            .where(
                agent_tasks.c.project_id == project_id,
                agent_tasks.c.id == task_id,
                agent_tasks.c.status == "running",
            )
            .values(status="queued", updated_at_ms=updated_at_ms)
        )
        return result.rowcount == 1

    async def mark_running_task_failed(
        self, *, project_id: str, task_id: str, updated_at_ms: int
    ) -> bool:
        result = await self._connection.execute(
            update(agent_tasks)
            .where(
                agent_tasks.c.project_id == project_id,
                agent_tasks.c.id == task_id,
                agent_tasks.c.status == "running",
            )
            .values(status="failed", updated_at_ms=updated_at_ms)
        )
        return result.rowcount == 1

    async def reset_failed_task_to_queued(
        self, *, project_id: str, task_id: str, updated_at_ms: int
    ) -> bool:
        result = await self._connection.execute(
            update(agent_tasks)
            .where(
                agent_tasks.c.project_id == project_id,
                agent_tasks.c.id == task_id,
                agent_tasks.c.status == "failed",
                agent_tasks.c.successful_attempt_id.is_(None),
                agent_tasks.c.delivery_state == "not_ready",
            )
            .values(status="queued", updated_at_ms=updated_at_ms)
        )
        return result.rowcount == 1

    async def list_failed_tasks_on_active_runs(self) -> list[FailedTaskRecord]:
        rows = (
            await self._connection.execute(
                select(
                    agent_tasks.c.project_id,
                    agent_tasks.c.run_id,
                    agent_tasks.c.id.label("task_id"),
                    agent_task_attempts.c.id.label("attempt_id"),
                    agent_task_attempts.c.attempt_number,
                    agent_task_attempts.c.error_code,
                    agent_task_attempts.c.error_ref_id,
                )
                .join(
                    generation_runs,
                    (generation_runs.c.project_id == agent_tasks.c.project_id)
                    & (generation_runs.c.id == agent_tasks.c.run_id),
                )
                .join(
                    agent_task_attempts,
                    (agent_task_attempts.c.project_id == agent_tasks.c.project_id)
                    & (agent_task_attempts.c.task_id == agent_tasks.c.id),
                )
                .where(
                    agent_tasks.c.status == "failed",
                    agent_task_attempts.c.status == "failed",
                    generation_runs.c.status.in_(("running", "pause_requested", "paused")),
                )
                .order_by(
                    agent_tasks.c.id,
                    agent_task_attempts.c.attempt_number.desc(),
                )
            )
        ).mappings()
        latest: dict[str, FailedTaskRecord] = {}
        for row in rows:
            task_id = cast(str, row["task_id"])
            if task_id in latest:
                continue
            latest[task_id] = FailedTaskRecord(
                project_id=cast(str, row["project_id"]),
                run_id=cast(str, row["run_id"]),
                task_id=task_id,
                attempt_id=cast(str, row["attempt_id"]),
                attempt_number=cast(int, row["attempt_number"]),
                error_code=cast(str, row["error_code"]),
                error_ref_id=cast(str, row["error_ref_id"]),
            )
        return list(latest.values())

    async def complete_attempt_success(
        self,
        *,
        project_id: str,
        task_id: str,
        attempt_id: str,
        provider_request_count: int,
        transport_retry_count: int,
        model_request_count: int,
        input_tokens: int,
        output_tokens: int,
        usage_ref_id: str,
        result_ref_id: str,
        finished_at_ms: int,
    ) -> bool:
        result = await self._connection.execute(
            update(agent_task_attempts)
            .where(
                agent_task_attempts.c.project_id == project_id,
                agent_task_attempts.c.task_id == task_id,
                agent_task_attempts.c.id == attempt_id,
                agent_task_attempts.c.status == "running",
            )
            .values(
                status="succeeded",
                owner_instance_id=None,
                lease_token=None,
                lease_expires_at_ms=None,
                heartbeat_at_ms=None,
                provider_request_count=provider_request_count,
                transport_retry_count=transport_retry_count,
                model_request_count=model_request_count,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=input_tokens + output_tokens,
                usage_ref_id=usage_ref_id,
                result_ref_id=result_ref_id,
                finished_at_ms=finished_at_ms,
            )
        )
        if result.rowcount != 1:
            return False
        task_result = await self._connection.execute(
            update(agent_tasks)
            .where(
                agent_tasks.c.project_id == project_id,
                agent_tasks.c.id == task_id,
                agent_tasks.c.status == "running",
            )
            .values(
                status="succeeded",
                successful_attempt_id=attempt_id,
                delivery_state="pending",
                updated_at_ms=finished_at_ms,
            )
        )
        return task_result.rowcount == 1

    async def complete_attempt_failure(
        self,
        *,
        project_id: str,
        task_id: str,
        attempt_id: str,
        provider_request_count: int,
        transport_retry_count: int,
        model_request_count: int,
        error_code: str,
        error_category: str,
        http_status: int | None,
        error_ref_id: str,
        diagnostic_ref_id: str | None,
        finished_at_ms: int,
    ) -> bool:
        result = await self._connection.execute(
            update(agent_task_attempts)
            .where(
                agent_task_attempts.c.project_id == project_id,
                agent_task_attempts.c.task_id == task_id,
                agent_task_attempts.c.id == attempt_id,
                agent_task_attempts.c.status == "running",
            )
            .values(
                status="failed",
                owner_instance_id=None,
                lease_token=None,
                lease_expires_at_ms=None,
                heartbeat_at_ms=None,
                provider_request_count=provider_request_count,
                transport_retry_count=transport_retry_count,
                model_request_count=model_request_count,
                error_code=error_code,
                error_category=error_category,
                http_status=http_status,
                error_ref_id=error_ref_id,
                diagnostic_ref_id=diagnostic_ref_id,
                finished_at_ms=finished_at_ms,
            )
        )
        if result.rowcount != 1:
            return False
        task_result = await self._connection.execute(
            update(agent_tasks)
            .where(
                agent_tasks.c.project_id == project_id,
                agent_tasks.c.id == task_id,
                agent_tasks.c.status == "running",
            )
            .values(status="failed", updated_at_ms=finished_at_ms)
        )
        return task_result.rowcount == 1

    async def insert_evidence_items(
        self,
        *,
        project_id: str,
        task_id: str,
        attempt_id: str,
        items: Sequence[EvidenceItemDraft],
        created_at_ms: int,
    ) -> None:
        if not items:
            return
        await self._connection.execute(
            insert(agent_evidence_items),
            [
                {
                    "id": f"{attempt_id}:evidence:{sequence}",
                    "project_id": project_id,
                    "task_id": task_id,
                    "attempt_id": attempt_id,
                    "sequence_number": sequence,
                    "item_kind": item.item_kind,
                    "content_ref_id": item.content_ref_id,
                    "metadata_json": item.metadata_json,
                    "created_at_ms": created_at_ms,
                }
                for sequence, item in enumerate(items, start=1)
            ],
        )

    async def get_successful_task(
        self,
        *,
        project_id: str,
        task_id: str,
        attempt_id: str,
    ) -> SuccessfulTaskRecord | None:
        row = (
            await self._connection.execute(
                select(
                    agent_tasks.c.id.label("task_id"),
                    agent_task_attempts.c.id.label("attempt_id"),
                    agent_tasks.c.project_id,
                    agent_tasks.c.run_id,
                    agent_tasks.c.role,
                    agent_tasks.c.task_kind,
                    agent_tasks.c.scope_layer,
                    agent_tasks.c.book_id,
                    agent_tasks.c.arc_id,
                    agent_tasks.c.chapter_id,
                    agent_tasks.c.workspace_lock_version,
                    agent_tasks.c.book_baseline_id,
                    agent_tasks.c.arc_baseline_id,
                    agent_tasks.c.chapter_baseline_id,
                    agent_tasks.c.canon_baseline_id,
                    agent_task_attempts.c.result_ref_id,
                    agent_tasks.c.delivery_state,
                ).join(
                    agent_task_attempts,
                    (agent_task_attempts.c.project_id == agent_tasks.c.project_id)
                    & (agent_task_attempts.c.task_id == agent_tasks.c.id)
                    & (agent_task_attempts.c.id == agent_tasks.c.successful_attempt_id),
                ).where(
                    agent_tasks.c.project_id == project_id,
                    agent_tasks.c.id == task_id,
                    agent_task_attempts.c.id == attempt_id,
                    agent_tasks.c.status == "succeeded",
                    agent_task_attempts.c.status == "succeeded",
                )
            )
        ).mappings().one_or_none()
        if row is None:
            return None
        return SuccessfulTaskRecord(
            task_id=cast(str, row["task_id"]),
            attempt_id=cast(str, row["attempt_id"]),
            project_id=cast(str, row["project_id"]),
            run_id=cast(str, row["run_id"]),
            role=cast(str, row["role"]),
            task_kind=cast(str, row["task_kind"]),
            scope_layer=cast(str, row["scope_layer"]),
            book_id=cast(str, row["book_id"]),
            arc_id=cast(str | None, row["arc_id"]),
            chapter_id=cast(str | None, row["chapter_id"]),
            workspace_lock_version=cast(int | None, row["workspace_lock_version"]),
            book_baseline_id=cast(str | None, row["book_baseline_id"]),
            arc_baseline_id=cast(str | None, row["arc_baseline_id"]),
            chapter_baseline_id=cast(str | None, row["chapter_baseline_id"]),
            canon_baseline_id=cast(str, row["canon_baseline_id"]),
            result_ref_id=cast(str, row["result_ref_id"]),
            delivery_state=cast(str, row["delivery_state"]),
        )

    async def mark_delivery_applied(
        self,
        *,
        project_id: str,
        task_id: str,
        attempt_id: str,
        command_id: str,
        updated_at_ms: int,
    ) -> bool:
        result = await self._connection.execute(
            update(agent_tasks)
            .where(
                agent_tasks.c.project_id == project_id,
                agent_tasks.c.id == task_id,
                agent_tasks.c.status == "succeeded",
                agent_tasks.c.successful_attempt_id == attempt_id,
                agent_tasks.c.delivery_state == "pending",
            )
            .values(
                delivery_state="applied",
                applied_command_id=command_id,
                updated_at_ms=updated_at_ms,
            )
        )
        return result.rowcount == 1

    async def mark_delivery_discarded_stale(
        self,
        *,
        project_id: str,
        task_id: str,
        attempt_id: str,
        updated_at_ms: int,
    ) -> bool:
        result = await self._connection.execute(
            update(agent_tasks)
            .where(
                agent_tasks.c.project_id == project_id,
                agent_tasks.c.id == task_id,
                agent_tasks.c.status == "succeeded",
                agent_tasks.c.successful_attempt_id == attempt_id,
                agent_tasks.c.delivery_state == "pending",
            )
            .values(
                delivery_state="discarded_stale",
                updated_at_ms=updated_at_ms,
            )
        )
        return result.rowcount == 1
