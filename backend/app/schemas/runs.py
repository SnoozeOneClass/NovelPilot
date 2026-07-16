from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class RunAdvanceRequest(BaseModel):
    stop_after_chapter: bool = False
    max_steps: int = Field(default=36, ge=1, le=120)


class ProviderWaitState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_key: str = Field(min_length=1, max_length=500)
    failure_category: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=4_000)
    attempt: int = Field(ge=1)
    next_wake_at: datetime


class RunControlState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    desired_state: Literal["stopped", "running"] = "stopped"
    run_id: str | None = None
    checkpoint_sequence: int = Field(default=0, ge=0)
    provider_wait: ProviderWaitState | None = None
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class HarnessCheckpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 2
    sequence: int = Field(ge=1)
    run_id: str
    action_key: str = Field(min_length=1, max_length=500)
    input_fingerprint: str = Field(min_length=64, max_length=64)
    candidate_run_id: str | None = Field(default=None, min_length=1, max_length=200)
    candidate_revision: int | None = Field(default=None, ge=0)
    provider_wait_attempt: int | None = Field(default=None, ge=1)
    next_wake_at: datetime | None = None
    status: Literal["in_progress", "completed", "waiting", "failed"]
    project_status_before: str
    project_status_after: str | None = None
    event_sequence_before: int = Field(ge=0)
    event_sequence_after: int | None = Field(default=None, ge=0)
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    completed_at: datetime | None = None
    result_artifacts: list[str] = Field(default_factory=list)
    failure: str | None = Field(default=None, max_length=4_000)
