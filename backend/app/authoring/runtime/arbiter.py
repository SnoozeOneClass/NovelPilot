from __future__ import annotations

from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict


class FailureContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    project_id: str
    instruction_key: str
    reason: str
    episode_count: int
    failure_count: int


class FailureDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    action: Literal["retry_once", "pause"]
    reason_code: str


class FailureArbiter(Protocol):
    async def decide(self, context: FailureContext) -> FailureDecision: ...


class DeterministicFailureArbiter:
    async def decide(self, context: FailureContext) -> FailureDecision:
        return FailureDecision(action="pause", reason_code=f"bounded_{context.reason}")
