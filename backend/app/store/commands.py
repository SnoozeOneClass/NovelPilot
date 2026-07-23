from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import cast

from sqlalchemy import RowMapping, func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from app.db.schema import command_receipts, domain_events
from app.domain.commands import CommandEnvelope, EventDraft, canonical_json_bytes


@dataclass(frozen=True, slots=True)
class CommandReceiptRecord:
    id: str
    project_id: str
    run_id: str | None
    idempotency_key: str
    command_kind: str
    actor: str
    request_fingerprint: str
    source_task_id: str | None
    result_json: str
    first_event_sequence: int | None
    last_event_sequence: int | None
    created_at_ms: int


@dataclass(frozen=True, slots=True)
class DomainEventRecord:
    sequence: int
    event_id: str
    project_id: str
    run_id: str | None
    event_type: str
    aggregate_type: str
    aggregate_id: str
    payload: object
    occurred_at_ms: int


def _receipt_record(row: RowMapping) -> CommandReceiptRecord:
    return CommandReceiptRecord(
        id=cast(str, row["id"]),
        project_id=cast(str, row["project_id"]),
        run_id=cast(str | None, row["run_id"]),
        idempotency_key=cast(str, row["idempotency_key"]),
        command_kind=cast(str, row["command_kind"]),
        actor=cast(str, row["actor"]),
        request_fingerprint=cast(str, row["request_fingerprint"]),
        source_task_id=cast(str | None, row["source_task_id"]),
        result_json=cast(str, row["result_json"]),
        first_event_sequence=cast(int | None, row["first_event_sequence"]),
        last_event_sequence=cast(int | None, row["last_event_sequence"]),
        created_at_ms=cast(int, row["created_at_ms"]),
    )


class CommandRepository:
    """Mechanical receipt/event persistence inside a Domain Command transaction."""

    def __init__(self, connection: AsyncConnection) -> None:
        self._connection = connection

    async def list_events_after(
        self,
        *,
        project_id: str,
        after_sequence: int,
        limit: int = 200,
    ) -> list[DomainEventRecord]:
        if after_sequence < 0 or limit < 1:
            raise ValueError("Event cursor must be non-negative and limit must be positive.")
        rows = (
            await self._connection.execute(
                select(domain_events)
                .where(
                    domain_events.c.project_id == project_id,
                    domain_events.c.sequence > after_sequence,
                )
                .order_by(domain_events.c.sequence)
                .limit(limit)
            )
        ).mappings()
        return [
            DomainEventRecord(
                sequence=cast(int, row["sequence"]),
                event_id=cast(str, row["event_id"]),
                project_id=cast(str, row["project_id"]),
                run_id=cast(str | None, row["run_id"]),
                event_type=cast(str, row["event_type"]),
                aggregate_type=cast(str, row["aggregate_type"]),
                aggregate_id=cast(str, row["aggregate_id"]),
                payload=json.loads(cast(str, row["payload_json"])),
                occurred_at_ms=cast(int, row["occurred_at_ms"]),
            )
            for row in rows
        ]

    async def latest_event_sequence(self, *, project_id: str) -> int:
        value = await self._connection.scalar(
            select(func.coalesce(func.max(domain_events.c.sequence), 0)).where(
                domain_events.c.project_id == project_id
            )
        )
        return cast(int, value)

    async def find_receipt(
        self, *, project_id: str, idempotency_key: str
    ) -> CommandReceiptRecord | None:
        row = (
            await self._connection.execute(
                select(command_receipts).where(
                    command_receipts.c.project_id == project_id,
                    command_receipts.c.idempotency_key == idempotency_key,
                )
            )
        ).mappings().one_or_none()
        return None if row is None else _receipt_record(row)

    async def insert_receipt(
        self,
        *,
        envelope: CommandEnvelope,
        result: object,
    ) -> CommandReceiptRecord:
        result_json = canonical_json_bytes(result).decode("utf-8")
        await self._connection.execute(
            command_receipts.insert().values(
                id=envelope.command_id,
                project_id=envelope.project_id,
                run_id=envelope.run_id,
                idempotency_key=envelope.idempotency_key,
                command_kind=envelope.command_kind,
                actor=envelope.actor,
                request_fingerprint=envelope.request_fingerprint,
                source_task_id=envelope.source_task_id,
                result_json=result_json,
                created_at_ms=envelope.created_at_ms,
            )
        )
        return CommandReceiptRecord(
            id=envelope.command_id,
            project_id=envelope.project_id,
            run_id=envelope.run_id,
            idempotency_key=envelope.idempotency_key,
            command_kind=envelope.command_kind,
            actor=envelope.actor,
            request_fingerprint=envelope.request_fingerprint,
            source_task_id=envelope.source_task_id,
            result_json=result_json,
            first_event_sequence=None,
            last_event_sequence=None,
            created_at_ms=envelope.created_at_ms,
        )

    async def append_event(
        self,
        *,
        envelope: CommandEnvelope,
        receipt_id: str,
        draft: EventDraft,
    ) -> int:
        event_id = uuid.uuid4().hex
        sequence = await self._connection.scalar(
            domain_events.insert()
            .values(
                event_id=event_id,
                project_id=envelope.project_id,
                run_id=envelope.run_id,
                command_receipt_id=receipt_id,
                event_type=draft.event_type,
                schema_version=draft.schema_version,
                aggregate_type=draft.aggregate_type,
                aggregate_id=draft.aggregate_id,
                causation_id=envelope.causation_id,
                correlation_id=envelope.correlation_id,
                payload_json=canonical_json_bytes(draft.payload).decode("utf-8"),
                occurred_at_ms=envelope.created_at_ms,
            )
            .returning(domain_events.c.sequence)
        )
        if sequence is None:  # pragma: no cover - SQLite RETURNING is required by Python 3.13.
            raise RuntimeError("SQLite did not return a domain event sequence.")
        return cast(int, sequence)

    async def set_event_range(
        self,
        *,
        receipt_id: str,
        first_sequence: int,
        last_sequence: int,
    ) -> None:
        result = await self._connection.execute(
            update(command_receipts)
            .where(
                command_receipts.c.id == receipt_id,
                command_receipts.c.first_event_sequence.is_(None),
                command_receipts.c.last_event_sequence.is_(None),
            )
            .values(
                first_event_sequence=first_sequence,
                last_event_sequence=last_sequence,
            )
        )
        if result.rowcount != 1:
            raise RuntimeError("Command receipt event range was not initialized exactly once.")

    @staticmethod
    def decode_result(receipt: CommandReceiptRecord) -> object:
        return json.loads(receipt.result_json)
