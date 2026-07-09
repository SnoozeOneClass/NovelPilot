from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field


LoopLayer = Literal["book", "story_arc", "chapter", "system"]
EventStatus = Literal["started", "delta", "completed", "failed", "requested"]


class HarnessEvent(BaseModel):
    seq: int | None = Field(default=None, ge=1)
    event_id: str = Field(default_factory=lambda: str(uuid4()))
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    project_id: str
    run_id: str | None = None
    kind: str
    loop_layer: LoopLayer = "system"
    atomic_action: str | None = None
    status: EventStatus = "completed"
    artifact_path: str | None = None
    routing_decision: str | None = None
    message: str
    payload: dict[str, Any] = Field(default_factory=dict)


class UserFeedbackRequest(BaseModel):
    message: str = Field(min_length=1)
