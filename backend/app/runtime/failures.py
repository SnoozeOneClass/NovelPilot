from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict

from app.db.uow import StoreSession
from app.domain.commands import CommandEffect, CommandEnvelope, CommandExecution, EventDraft
from app.store.command_bus import CommandBus
from app.store.content import prepare_canonical_json
from app.store.execution import ActionableTaskRecord


@dataclass(frozen=True, slots=True)
class NormalizedDeliveryFailure:
    code: str
    message: str
    exception_type: str
    details: dict[str, object] | None = None


class RecordDeliveryFailureRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    run_id: str
    task_id: str
    attempt_id: str
    task_kind: str
    failure_code: str
    message: str
    exception_type: str
    details: dict[str, object] | None


class RecordDeliveryFailureResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    run_id: str
    task_id: str
    attempt_id: str
    failure_code: str
    failure_ref_id: str
    run_status: str


class DeliveryFailureService:
    """Persist one failed Domain delivery without rewriting Provider execution evidence."""

    def __init__(
        self,
        command_bus: CommandBus,
        *,
        id_factory: Callable[[], str] | None = None,
        now_ms: Callable[[], int] | None = None,
    ) -> None:
        self._command_bus = command_bus
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex)
        self._now_ms = now_ms or (lambda: time.time_ns() // 1_000_000)

    async def failure_pause(
        self,
        *,
        task: ActionableTaskRecord,
        failure: NormalizedDeliveryFailure,
    ) -> CommandExecution[RecordDeliveryFailureResult]:
        timestamp = self._now_ms()
        request = RecordDeliveryFailureRequest(
            project_id=task.project_id,
            run_id=task.run_id,
            task_id=task.task_id,
            attempt_id=task.attempt_id,
            task_kind=task.task_kind,
            failure_code=failure.code,
            message=failure.message,
            exception_type=failure.exception_type,
            details=failure.details,
        )
        prepared_failure = prepare_canonical_json(
            {
                "schema_id": "domain-delivery-failure-v1",
                "code": failure.code,
                "message": failure.message,
                "exception_type": failure.exception_type,
                "details": failure.details,
                "task_id": task.task_id,
                "attempt_id": task.attempt_id,
                "task_kind": task.task_kind,
            }
        )
        envelope = CommandEnvelope.for_request(
            project_id=task.project_id,
            idempotency_key=f"failure-pause-delivery:{task.attempt_id}",
            command_kind="failure_pause_for_domain_delivery",
            request_schema="failure_pause_for_domain_delivery.request.v1",
            request_payload=request,
            actor="system",
            command_id=self._id_factory(),
            run_id=task.run_id,
            source_task_id=task.task_id,
            created_at_ms=timestamp,
        )

        async def handler(
            session: StoreSession,
        ) -> CommandEffect[RecordDeliveryFailureResult]:
            failure_ref = await session.content.put(
                project_id=task.project_id,
                prepared=prepared_failure,
                semantic_kind="agent_error_summary",
                media_type="application/json",
                schema_id="domain-delivery-failure",
                schema_version=1,
                created_at_ms=timestamp,
            )
            if not await session.execution.fail_pending_delivery(
                project_id=task.project_id,
                task_id=task.task_id,
                attempt_id=task.attempt_id,
                error_code=failure.code,
                error_ref_id=failure_ref.id,
                diagnostic_ref_id=None,
                updated_at_ms=timestamp,
            ):
                raise RuntimeError(
                    "Successful Agent task changed before delivery failure could be persisted."
                )
            if not await session.runs.failure_pause(
                run_id=task.run_id,
                task_id=task.task_id,
                failure_code=failure.code,
                failure_ref_id=failure_ref.id,
                now_ms=timestamp,
            ):
                raise RuntimeError("Run changed before delivery failure pause could be persisted.")
            result = RecordDeliveryFailureResult(
                project_id=task.project_id,
                run_id=task.run_id,
                task_id=task.task_id,
                attempt_id=task.attempt_id,
                failure_code=failure.code,
                failure_ref_id=failure_ref.id,
                run_status="failure_paused",
            )
            return CommandEffect(
                result=result,
                events=(
                    EventDraft(
                        event_type="run.failure_paused",
                        aggregate_type="generation_run",
                        aggregate_id=task.run_id,
                        payload={
                            "task_id": task.task_id,
                            "attempt_id": task.attempt_id,
                            "failure_code": failure.code,
                            "failure_kind": "domain_delivery",
                        },
                    ),
                ),
            )

        return await self._command_bus.execute(
            envelope=envelope,
            result_type=RecordDeliveryFailureResult,
            handler=handler,
        )
