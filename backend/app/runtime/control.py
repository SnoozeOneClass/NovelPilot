from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.db.uow import StoreSession
from app.domain.commands import (
    CommandEffect,
    CommandEnvelope,
    CommandExecution,
    CommandPreconditionError,
    EventDraft,
)
from app.store.agent_tasks import framework_fingerprint
from app.store.command_bus import CommandBus
from app.store.runs import GenerationRunRecord

RunMutation = Callable[[StoreSession, int], Awaitable["RunControlResult"]]


class RunControlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    run_id: str
    expected_lock_version: int = Field(ge=1)

    @field_validator("project_id", "run_id")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Run control identity must be non-blank.")
        return value


class RetryFailedTaskRequest(RunControlRequest):
    task_id: str

    @field_validator("task_id")
    @classmethod
    def _task_non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("task_id must be non-blank.")
        return value


class RunControlResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    run_id: str
    status: str
    desired_state: str
    lock_version: int
    attempt_id: str | None = None


class RunControlService:
    def __init__(
        self,
        command_bus: CommandBus,
        *,
        wake: Callable[[], None] | None = None,
        id_factory: Callable[[], str] | None = None,
        now_ms: Callable[[], int] | None = None,
    ) -> None:
        self._command_bus = command_bus
        self._wake = wake or (lambda: None)
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex)
        self._now_ms = now_ms or (lambda: time.time_ns() // 1_000_000)

    async def start(
        self, request: RunControlRequest, *, idempotency_key: str
    ) -> CommandExecution[RunControlResult]:
        async def mutate(session: StoreSession, timestamp: int) -> RunControlResult:
            run = await session.runs.get(project_id=request.project_id, run_id=request.run_id)
            if (
                run is None
                or run.lock_version != request.expected_lock_version
                or not await session.runs.start_waiting_run(
                    project_id=request.project_id,
                    run_id=request.run_id,
                    expected_lock_version=request.expected_lock_version,
                    now_ms=timestamp,
                )
            ):
                raise CommandPreconditionError("Run is not at the expected start boundary.")
            updated = await session.runs.get(
                project_id=request.project_id,
                run_id=request.run_id,
            )
            if updated is None:  # pragma: no cover - the update retained the row.
                raise CommandPreconditionError("Run disappeared after start.")
            return _result(updated)

        return await self._execute(
            request=request,
            idempotency_key=idempotency_key,
            command_kind="start_generation_run",
            event_type="run.started",
            mutate=mutate,
        )

    async def pause(
        self, request: RunControlRequest, *, idempotency_key: str
    ) -> CommandExecution[RunControlResult]:
        async def mutate(session: StoreSession, timestamp: int) -> RunControlResult:
            run = await session.runs.get(project_id=request.project_id, run_id=request.run_id)
            if run is None or run.lock_version != request.expected_lock_version:
                raise CommandPreconditionError("Run state changed before Pause.")
            updated = await session.runs.request_pause(run=run, now_ms=timestamp)
            if updated is None:
                raise CommandPreconditionError("This Run cannot be paused from its current state.")
            return _result(updated)

        return await self._execute(
            request=request,
            idempotency_key=idempotency_key,
            command_kind="pause_generation_run",
            event_type="run.pause_requested",
            mutate=mutate,
        )

    async def resume(
        self, request: RunControlRequest, *, idempotency_key: str
    ) -> CommandExecution[RunControlResult]:
        async def mutate(session: StoreSession, timestamp: int) -> RunControlResult:
            run = await session.runs.get(project_id=request.project_id, run_id=request.run_id)
            if run is None or run.lock_version != request.expected_lock_version:
                raise CommandPreconditionError("Run state changed before Resume.")
            if run.status == "failure_paused":
                raise CommandPreconditionError(
                    "A failed task requires the dedicated explicit Retry command."
                )
            updated = await session.runs.resume_paused_run(run=run, now_ms=timestamp)
            if updated is None:
                raise CommandPreconditionError("This Run cannot be resumed from its current state.")
            return _result(updated)

        return await self._execute(
            request=request,
            idempotency_key=idempotency_key,
            command_kind="resume_generation_run",
            event_type="run.resumed",
            mutate=mutate,
        )

    async def retry_failed_task(
        self, request: RetryFailedTaskRequest, *, idempotency_key: str
    ) -> CommandExecution[RunControlResult]:
        attempt_id = self._id_factory()
        frozen_framework = framework_fingerprint()

        async def mutate(session: StoreSession, timestamp: int) -> RunControlResult:
            run = await session.runs.get(project_id=request.project_id, run_id=request.run_id)
            if (
                run is None
                or run.lock_version != request.expected_lock_version
                or run.status != "failure_paused"
                or run.blocking_task_id != request.task_id
            ):
                raise CommandPreconditionError("Run is not blocked by the requested failed task.")
            predecessor = await session.execution.get_latest_attempt(
                project_id=request.project_id,
                task_id=request.task_id,
            )
            if predecessor is None or predecessor.status not in {
                "failed",
                "interrupted",
                "delivery_failed",
            }:
                raise CommandPreconditionError("Failed task has no retryable terminal attempt.")
            next_number = await session.execution.next_attempt_number(task_id=request.task_id)
            await session.execution.insert_attempt(
                attempt_id=attempt_id,
                project_id=request.project_id,
                task_id=request.task_id,
                attempt_number=next_number,
                retry_kind="user_retry",
                predecessor_attempt_id=predecessor.attempt_id,
                framework_fingerprint=frozen_framework,
                created_at_ms=timestamp,
            )
            if not await session.execution.reset_failed_task_to_queued(
                project_id=request.project_id,
                task_id=request.task_id,
                updated_at_ms=timestamp,
            ):
                raise CommandPreconditionError("Failed task could not enter its retry boundary.")
            if not await session.runs.retry_failure(run=run, now_ms=timestamp):
                raise CommandPreconditionError("Failure-paused Run changed before Retry.")
            updated = await session.runs.get(
                project_id=request.project_id,
                run_id=request.run_id,
            )
            if updated is None:  # pragma: no cover
                raise CommandPreconditionError("Run disappeared after Retry.")
            return _result(updated, attempt_id=attempt_id)

        return await self._execute(
            request=request,
            idempotency_key=idempotency_key,
            command_kind="retry_failed_agent_task",
            event_type="run.failed_task_retry_requested",
            mutate=mutate,
        )

    async def _execute(
        self,
        *,
        request: RunControlRequest,
        idempotency_key: str,
        command_kind: str,
        event_type: str,
        mutate: RunMutation,
    ) -> CommandExecution[RunControlResult]:
        timestamp = self._now_ms()
        envelope = CommandEnvelope.for_request(
            project_id=request.project_id,
            idempotency_key=idempotency_key,
            command_kind=command_kind,
            request_schema=f"{command_kind}.request.v1",
            request_payload=request,
            actor="user",
            command_id=self._id_factory(),
            run_id=request.run_id,
            created_at_ms=timestamp,
        )

        async def handler(session: StoreSession) -> CommandEffect[RunControlResult]:
            result = await mutate(session, timestamp)
            return CommandEffect(
                result=result,
                events=(
                    EventDraft(
                        event_type=event_type,
                        aggregate_type="generation_run",
                        aggregate_id=request.run_id,
                        payload={
                            "status": result.status,
                            "desired_state": result.desired_state,
                            "lock_version": result.lock_version,
                            "attempt_id": result.attempt_id,
                        },
                    ),
                ),
            )

        execution = await self._command_bus.execute(
            envelope=envelope,
            result_type=RunControlResult,
            handler=handler,
        )
        self._wake()
        return execution


def _result(
    run: GenerationRunRecord, *, attempt_id: str | None = None
) -> RunControlResult:
    return RunControlResult(
        project_id=run.project_id,
        run_id=run.id,
        status=run.status,
        desired_state=run.desired_state,
        lock_version=run.lock_version,
        attempt_id=attempt_id,
    )
