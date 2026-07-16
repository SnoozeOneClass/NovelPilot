from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.completion import GateStatus

RunNextActionId = Literal[
    "continue_book_discussion",
    "review_book_direction",
    "approve_book_direction",
    "approve_book_revision",
    "configure_llm_profile",
    "repair_project_state",
    "wait_for_safe_checkpoint",
    "wait_for_provider_retry",
    "recover_stale_run",
    "inspect_failure",
    "retry_provider_connection",
    "retry_failed_run",
    "retry_current_chapter",
    "approve_story_arc",
    "start_run",
    "resume_run",
]


class ReadinessGate(BaseModel):
    id: str
    status: GateStatus
    required: bool = True
    message: str
    evidence: list[str] = Field(default_factory=list)


class RunNextAction(BaseModel):
    id: RunNextActionId
    command: str | None = None
    requires_user: bool = False
    can_auto_continue: bool = False
    message: str
    evidence: list[str] = Field(default_factory=list)


class ProjectReadiness(BaseModel):
    status: GateStatus
    can_start_run: bool
    gates: list[ReadinessGate] = Field(default_factory=list)
    next_action: RunNextAction
