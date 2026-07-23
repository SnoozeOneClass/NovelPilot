from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TypeVar

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db.uow import StoreSession, UnitOfWork
from app.domain.commands import (
    CommandEffect,
    CommandEnvelope,
    CommandExecution,
    IdempotencyConflictError,
)

ResultT = TypeVar("ResultT", bound=BaseModel)
CommandHandler = Callable[[StoreSession], Awaitable[CommandEffect[ResultT]]]


class CommandBus:
    """Execute one deterministic Domain Command and its outbox transaction."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    def unit_of_work(self) -> UnitOfWork:
        """Create the same IMMEDIATE boundary for exceptional root commands."""
        return UnitOfWork(self._engine, begin_mode="IMMEDIATE")

    def read_unit_of_work(self) -> UnitOfWork:
        """Create a read snapshot used only to prepare content before a command."""
        return UnitOfWork(self._engine, begin_mode="DEFERRED")

    async def execute(
        self,
        *,
        envelope: CommandEnvelope,
        result_type: type[ResultT],
        handler: CommandHandler[ResultT],
    ) -> CommandExecution[ResultT]:
        async with UnitOfWork(self._engine, begin_mode="IMMEDIATE") as session:
            existing = await session.commands.find_receipt(
                project_id=envelope.project_id,
                idempotency_key=envelope.idempotency_key,
            )
            if existing is not None:
                if (
                    existing.command_kind != envelope.command_kind
                    or existing.request_fingerprint != envelope.request_fingerprint
                ):
                    raise IdempotencyConflictError(
                        "The idempotency key is already bound to a different command request."
                    )
                return CommandExecution(
                    result=result_type.model_validate_json(existing.result_json),
                    receipt_id=existing.id,
                    first_event_sequence=existing.first_event_sequence,
                    last_event_sequence=existing.last_event_sequence,
                    replayed=True,
                )

            effect = await handler(session)
            receipt = await session.commands.insert_receipt(
                envelope=envelope,
                result=effect.result.model_dump(mode="json"),
            )
            sequences = [
                await session.commands.append_event(
                    envelope=envelope,
                    receipt_id=receipt.id,
                    draft=draft,
                )
                for draft in effect.events
            ]
            if sequences:
                await session.commands.set_event_range(
                    receipt_id=receipt.id,
                    first_sequence=sequences[0],
                    last_sequence=sequences[-1],
                )
            return CommandExecution(
                result=effect.result,
                receipt_id=receipt.id,
                first_event_sequence=sequences[0] if sequences else None,
                last_event_sequence=sequences[-1] if sequences else None,
                replayed=False,
            )
