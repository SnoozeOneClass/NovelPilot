from __future__ import annotations

import re
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
from app.store.runs import GenerationRunRecord


@dataclass(frozen=True, slots=True)
class NormalizedDeliveryFailure:
    code: str
    message: str
    exception_type: str
    details: dict[str, object] | None = None


_SAFE_INVARIANT = re.compile(r"^[a-z][a-z0-9_.:-]{0,199}$")
_SENSITIVE_HEADER = re.compile(
    r"(?im)\b(?:authorization|proxy-authorization|cookie|set-cookie|"
    r"x-api-key|anthropic-api-key)\s*:\s*[^\r\n]+"
)
_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|secret|password)"
    r"\s*[:=]\s*(?:\"(?:\\.|[^\"])*\"|'(?:\\.|[^'])*'|[^\s,;]+)"
)
_BEARER_TOKEN = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_SENSITIVE_QUERY_VALUE = re.compile(
    r"(?i)([?&](?:api[_-]?key|access[_-]?token|token|signature|sig|"
    r"x-amz-signature)=)[^&#\s]+"
)
_MODEL_PAYLOAD = re.compile(
    r"(?i)(\b(?:prompt|context(?:_manifest)?|prose|hidden[_-]?thinking)"
    r"\b\s*[:=]\s*)"
    r"(?:\"(?:\\.|[^\"])*\"|'(?:\\.|[^'])*'|[^\r\n;]+)"
)


def _sanitize_harness_message(message: str) -> str:
    """Redact protocol secrets and model payloads from durable Harness evidence."""
    sanitized = _SENSITIVE_HEADER.sub("[REDACTED_HEADER]", message)
    sanitized = _MODEL_PAYLOAD.sub(r"\1[REDACTED_PAYLOAD]", sanitized)
    sanitized = _SENSITIVE_ASSIGNMENT.sub("[REDACTED_SECRET]", sanitized)
    sanitized = _BEARER_TOKEN.sub("Bearer [REDACTED]", sanitized)
    sanitized = _SENSITIVE_QUERY_VALUE.sub(r"\1[REDACTED]", sanitized)
    return sanitized


def normalize_harness_action_details(
    error: Exception,
    *,
    phase: str,
    task_kind: str | None = None,
) -> dict[str, object]:
    """Build bounded, secret-safe evidence without storing traceback or model content."""
    details: dict[str, object] = {"phase": phase}
    if task_kind is not None:
        details["task_kind"] = task_kind

    cause_chain: list[dict[str, str]] = []
    failed_invariant: str | None = None
    current: BaseException | None = error
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        entry = {
            "exception_type": type(current).__name__,
            "message": _sanitize_harness_message(str(current)),
        }
        invariant = getattr(current, "invariant", None)
        if isinstance(invariant, str) and _SAFE_INVARIANT.fullmatch(invariant):
            entry["invariant"] = invariant
            failed_invariant = invariant
        cause_chain.append(entry)
        current = (
            current.__cause__
            if current.__cause__ is not None
            else (
                None
                if current.__suppress_context__
                else current.__context__
            )
        )

    details["cause_chain"] = cause_chain
    if failed_invariant is not None:
        details["failed_invariant"] = failed_invariant
    return details


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


class RecordHarnessActionFailureRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    run_id: str
    expected_run_lock_version: int
    action_key: str
    failure_code: str
    message: str
    exception_type: str
    details: dict[str, object] | None = None


class RecordHarnessActionFailureResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    run_id: str
    action_key: str
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
            if not await session.runs.failure_pause_for_task(
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


class HarnessActionFailureService:
    """Persist a deterministic failure that occurs before any Agent task exists."""

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
        run: GenerationRunRecord,
        action_key: str,
        failure_code: str,
        message: str,
        exception_type: str,
        details: dict[str, object] | None = None,
    ) -> CommandExecution[RecordHarnessActionFailureResult]:
        timestamp = self._now_ms()
        request = RecordHarnessActionFailureRequest(
            project_id=run.project_id,
            run_id=run.id,
            expected_run_lock_version=run.lock_version,
            action_key=action_key,
            failure_code=failure_code,
            message=message,
            exception_type=exception_type,
            details=details,
        )
        prepared_failure = prepare_canonical_json(
            {
                "schema_id": "harness-action-failure-v2",
                "code": failure_code,
                "message": message,
                "exception_type": exception_type,
                "details": details,
                "action_key": action_key,
            }
        )
        envelope = CommandEnvelope.for_request(
            project_id=run.project_id,
            idempotency_key=(
                f"failure-pause-action:{run.id}:{run.lock_version}:{action_key}"
            ),
            command_kind="failure_pause_for_harness_action",
            request_schema="failure_pause_for_harness_action.request.v2",
            request_payload=request,
            actor="system",
            command_id=self._id_factory(),
            run_id=run.id,
            created_at_ms=timestamp,
        )

        async def handler(
            session: StoreSession,
        ) -> CommandEffect[RecordHarnessActionFailureResult]:
            failure_ref = await session.content.put(
                project_id=run.project_id,
                prepared=prepared_failure,
                semantic_kind="harness_action_error_summary",
                media_type="application/json",
                schema_id="harness-action-failure",
                schema_version=2,
                created_at_ms=timestamp,
            )
            if not await session.runs.failure_pause_for_action(
                run_id=run.id,
                expected_lock_version=run.lock_version,
                action_key=action_key,
                failure_code=failure_code,
                failure_ref_id=failure_ref.id,
                now_ms=timestamp,
            ):
                raise RuntimeError(
                    "Run changed before Harness-action failure could be persisted."
                )
            return CommandEffect(
                result=RecordHarnessActionFailureResult(
                    project_id=run.project_id,
                    run_id=run.id,
                    action_key=action_key,
                    failure_code=failure_code,
                    failure_ref_id=failure_ref.id,
                    run_status="failure_paused",
                ),
                events=(
                    EventDraft(
                        event_type="run.failure_paused",
                        aggregate_type="generation_run",
                        aggregate_id=run.id,
                        payload={
                            "action_key": action_key,
                            "failure_code": failure_code,
                            "failure_kind": "harness_action",
                        },
                    ),
                ),
            )

        return await self._command_bus.execute(
            envelope=envelope,
            result_type=RecordHarnessActionFailureResult,
            handler=handler,
        )
