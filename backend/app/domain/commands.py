from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass
from typing import Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

Actor = Literal["user", "engine", "system"]
ResultT = TypeVar("ResultT", bound=BaseModel)


def canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def command_request_fingerprint(
    *,
    command_kind: str,
    request_schema: str,
    request_payload: BaseModel,
) -> str:
    envelope = {
        "command_kind": command_kind,
        "request_schema": request_schema,
        "request": request_payload.model_dump(mode="json"),
    }
    return hashlib.sha256(canonical_json_bytes(envelope)).hexdigest()


class CommandEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    command_id: str
    project_id: str
    idempotency_key: str
    command_kind: str
    request_fingerprint: str
    actor: Actor
    run_id: str | None = None
    source_task_id: str | None = None
    causation_id: str | None = None
    correlation_id: str | None = None
    created_at_ms: int = Field(ge=0)

    @field_validator("command_id", "project_id", "idempotency_key", "command_kind")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("command identity fields must be non-blank")
        return value

    @field_validator("request_fingerprint")
    @classmethod
    def _sha256(cls, value: str) -> str:
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise ValueError("request_fingerprint must be a lowercase SHA-256")
        return value

    @classmethod
    def for_request(
        cls,
        *,
        project_id: str,
        idempotency_key: str,
        command_kind: str,
        request_schema: str,
        request_payload: BaseModel,
        actor: Actor,
        command_id: str | None = None,
        run_id: str | None = None,
        source_task_id: str | None = None,
        causation_id: str | None = None,
        correlation_id: str | None = None,
        created_at_ms: int | None = None,
    ) -> CommandEnvelope:
        return cls(
            command_id=command_id or uuid.uuid4().hex,
            project_id=project_id,
            idempotency_key=idempotency_key,
            command_kind=command_kind,
            request_fingerprint=command_request_fingerprint(
                command_kind=command_kind,
                request_schema=request_schema,
                request_payload=request_payload,
            ),
            actor=actor,
            run_id=run_id,
            source_task_id=source_task_id,
            causation_id=causation_id,
            correlation_id=correlation_id,
            created_at_ms=(time.time_ns() // 1_000_000 if created_at_ms is None else created_at_ms),
        )


class EventDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_type: str
    aggregate_type: str
    aggregate_id: str
    payload: dict[str, object]
    schema_version: int = Field(default=1, ge=1)

    @field_validator("event_type", "aggregate_type", "aggregate_id")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("event descriptors must be non-blank")
        return value


@dataclass(frozen=True, slots=True)
class CommandEffect(Generic[ResultT]):
    result: ResultT
    events: tuple[EventDraft, ...]


@dataclass(frozen=True, slots=True)
class CommandExecution(Generic[ResultT]):
    result: ResultT
    receipt_id: str
    first_event_sequence: int | None
    last_event_sequence: int | None
    replayed: bool


class IdempotencyConflictError(RuntimeError):
    """One project/idempotency key was reused for a different request."""


class CommandPreconditionError(RuntimeError):
    """Authoritative current facts no longer satisfy a command precondition."""
