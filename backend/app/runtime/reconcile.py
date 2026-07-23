from __future__ import annotations

import time
import uuid
from collections.abc import Callable

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db.uow import StoreSession, UnitOfWork
from app.domain.commands import (
    CommandEffect,
    CommandEnvelope,
    CommandExecution,
    CommandPreconditionError,
    EventDraft,
)
from app.store.command_bus import CommandBus
from app.store.content import prepare_canonical_json
from app.store.execution import AbandonedAttemptRecord, FailedTaskRecord


class ReconcileAttemptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    run_id: str
    task_id: str
    attempt_id: str
    attempt_number: int = Field(ge=1)
    retry_kind: str
    framework_fingerprint: str


class FailurePauseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    run_id: str
    task_id: str
    attempt_id: str
    attempt_number: int = Field(ge=1)
    error_code: str
    error_ref_id: str


class ReconcileResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    run_id: str
    task_id: str
    attempt_id: str
    outcome: str
    replay_attempt_id: str | None = None


class ReconcileReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    released_run_id: str | None
    crash_replays_created: int = Field(ge=0)
    tasks_failure_paused: int = Field(ge=0)


class ReconcileService:
    """Repair only expired leases and terminal execution facts from SQLite state."""

    def __init__(
        self,
        engine: AsyncEngine,
        command_bus: CommandBus,
        *,
        id_factory: Callable[[], str] | None = None,
        now_ms: Callable[[], int] | None = None,
    ) -> None:
        # Keep one engine reference for short read/infrastructure transactions. Command
        # mutations still pass through CommandBus and its IMMEDIATE transaction.
        self._engine = engine
        self._command_bus = command_bus
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex)
        self._now_ms = now_ms or (lambda: time.time_ns() // 1_000_000)

    async def reconcile(self) -> ReconcileReport:
        timestamp = self._now_ms()
        async with UnitOfWork(self._engine, begin_mode="IMMEDIATE") as session:
            released_run_id = await session.runs.release_expired_engine_slot(now_ms=timestamp)

        async with UnitOfWork(self._engine, begin_mode="DEFERRED") as session:
            abandoned = await session.execution.list_expired_running_attempts(now_ms=timestamp)

        crash_replays = 0
        failure_pauses = 0
        for record in abandoned:
            try:
                execution = await self._reconcile_attempt(record, timestamp=timestamp)
            except CommandPreconditionError:
                continue
            if not execution.replayed:
                if execution.result.outcome == "crash_replay_queued":
                    crash_replays += 1
                elif execution.result.outcome == "failure_paused":
                    failure_pauses += 1

        async with UnitOfWork(self._engine, begin_mode="DEFERRED") as session:
            failed = await session.execution.list_failed_tasks_on_active_runs()
        for failed_record in failed:
            try:
                execution = await self._pause_for_failed_task(
                    failed_record,
                    timestamp=timestamp,
                )
            except CommandPreconditionError:
                continue
            if not execution.replayed:
                failure_pauses += 1

        return ReconcileReport(
            released_run_id=released_run_id,
            crash_replays_created=crash_replays,
            tasks_failure_paused=failure_pauses,
        )

    async def _reconcile_attempt(
        self,
        record: AbandonedAttemptRecord,
        *,
        timestamp: int,
    ) -> CommandExecution[ReconcileResult]:
        request = ReconcileAttemptRequest(
            project_id=record.project_id,
            run_id=record.run_id,
            task_id=record.task_id,
            attempt_id=record.attempt_id,
            attempt_number=record.attempt_number,
            retry_kind=record.retry_kind,
            framework_fingerprint=record.framework_fingerprint,
        )
        replay_attempt_id = self._id_factory()
        envelope = CommandEnvelope.for_request(
            project_id=record.project_id,
            idempotency_key=f"reconcile-expired-attempt:{record.attempt_id}",
            command_kind="reconcile_expired_agent_attempt",
            request_schema="reconcile_expired_agent_attempt.request.v1",
            request_payload=request,
            actor="system",
            command_id=self._id_factory(),
            run_id=record.run_id,
            source_task_id=record.task_id,
            created_at_ms=timestamp,
        )

        async def handler(session: StoreSession) -> CommandEffect[ReconcileResult]:
            if not await session.execution.interrupt_expired_attempt(
                record=record,
                now_ms=timestamp,
            ):
                raise CommandPreconditionError("Attempt lease is no longer expired and running.")

            already_replayed = await session.execution.has_crash_replay(task_id=record.task_id)
            if not already_replayed:
                await session.execution.insert_attempt(
                    attempt_id=replay_attempt_id,
                    project_id=record.project_id,
                    task_id=record.task_id,
                    attempt_number=await session.execution.next_attempt_number(
                        task_id=record.task_id
                    ),
                    retry_kind="crash_replay",
                    predecessor_attempt_id=record.attempt_id,
                    framework_fingerprint=record.framework_fingerprint,
                    created_at_ms=timestamp,
                )
                if not await session.execution.reset_running_task_to_queued(
                    project_id=record.project_id,
                    task_id=record.task_id,
                    updated_at_ms=timestamp,
                ):
                    raise CommandPreconditionError("Abandoned task is no longer running.")
                run = await session.runs.get(
                    project_id=record.project_id,
                    run_id=record.run_id,
                )
                if run is not None and run.status == "pause_requested":
                    await session.runs.settle_requested_pause(
                        run_id=record.run_id,
                        now_ms=timestamp,
                    )
                result = ReconcileResult(
                    project_id=record.project_id,
                    run_id=record.run_id,
                    task_id=record.task_id,
                    attempt_id=record.attempt_id,
                    outcome="crash_replay_queued",
                    replay_attempt_id=replay_attempt_id,
                )
                event_type = "agent_attempt.crash_replay_queued"
            else:
                failure_ref = await session.content.put(
                    project_id=record.project_id,
                    prepared=prepare_canonical_json(
                        {
                            "code": "crash_replay_exhausted",
                            "message": "The single automatic crash replay was exhausted.",
                            "task_id": record.task_id,
                            "attempt_id": record.attempt_id,
                        }
                    ),
                    semantic_kind="agent_error_summary",
                    media_type="application/json",
                    schema_id="agent-recovery-failure",
                    schema_version=1,
                    created_at_ms=timestamp,
                )
                if not await session.execution.mark_running_task_failed(
                    project_id=record.project_id,
                    task_id=record.task_id,
                    updated_at_ms=timestamp,
                ):
                    raise CommandPreconditionError("Abandoned task is no longer running.")
                if not await session.runs.failure_pause(
                    run_id=record.run_id,
                    task_id=record.task_id,
                    failure_code="crash_replay_exhausted",
                    failure_ref_id=failure_ref.id,
                    now_ms=timestamp,
                ):
                    raise CommandPreconditionError("Run cannot enter its failure pause boundary.")
                result = ReconcileResult(
                    project_id=record.project_id,
                    run_id=record.run_id,
                    task_id=record.task_id,
                    attempt_id=record.attempt_id,
                    outcome="failure_paused",
                )
                event_type = "run.failure_paused"

            return CommandEffect(
                result=result,
                events=(
                    EventDraft(
                        event_type=event_type,
                        aggregate_type="generation_run",
                        aggregate_id=record.run_id,
                        payload={
                            "task_id": record.task_id,
                            "attempt_id": record.attempt_id,
                            "outcome": result.outcome,
                            "replay_attempt_id": result.replay_attempt_id,
                        },
                    ),
                ),
            )

        return await self._command_bus.execute(
            envelope=envelope,
            result_type=ReconcileResult,
            handler=handler,
        )

    async def _pause_for_failed_task(
        self,
        record: FailedTaskRecord,
        *,
        timestamp: int,
    ) -> CommandExecution[ReconcileResult]:
        request = FailurePauseRequest(
            project_id=record.project_id,
            run_id=record.run_id,
            task_id=record.task_id,
            attempt_id=record.attempt_id,
            attempt_number=record.attempt_number,
            error_code=record.error_code,
            error_ref_id=record.error_ref_id,
        )
        envelope = CommandEnvelope.for_request(
            project_id=record.project_id,
            idempotency_key=f"failure-pause-attempt:{record.attempt_id}",
            command_kind="failure_pause_for_agent_task",
            request_schema="failure_pause_for_agent_task.request.v1",
            request_payload=request,
            actor="system",
            command_id=self._id_factory(),
            run_id=record.run_id,
            source_task_id=record.task_id,
            created_at_ms=timestamp,
        )

        async def handler(session: StoreSession) -> CommandEffect[ReconcileResult]:
            latest = await session.execution.get_latest_attempt(
                project_id=record.project_id,
                task_id=record.task_id,
            )
            if (
                latest is None
                or latest.attempt_id != record.attempt_id
                or latest.status not in {"failed", "delivery_failed"}
            ):
                raise CommandPreconditionError("Task no longer has the observed failed attempt.")
            if not await session.runs.failure_pause(
                run_id=record.run_id,
                task_id=record.task_id,
                failure_code=record.error_code,
                failure_ref_id=record.error_ref_id,
                now_ms=timestamp,
            ):
                raise CommandPreconditionError("Run cannot enter its failure pause boundary.")
            result = ReconcileResult(
                project_id=record.project_id,
                run_id=record.run_id,
                task_id=record.task_id,
                attempt_id=record.attempt_id,
                outcome="failure_paused",
            )
            return CommandEffect(
                result=result,
                events=(
                    EventDraft(
                        event_type="run.failure_paused",
                        aggregate_type="generation_run",
                        aggregate_id=record.run_id,
                        payload={
                            "task_id": record.task_id,
                            "attempt_id": record.attempt_id,
                            "failure_code": record.error_code,
                        },
                    ),
                ),
            )

        return await self._command_bus.execute(
            envelope=envelope,
            result_type=ReconcileResult,
            handler=handler,
        )
