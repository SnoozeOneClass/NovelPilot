from typing import Literal

from pydantic import BaseModel, Field

from app.schemas.completion import GateStatus

RunNextActionId = Literal[
    "answer_book_setup",
    "approve_book_setup",
    "configure_llm_profile",
    "repair_project_state",
    "wait_for_safe_checkpoint",
    "recover_stale_run",
    "inspect_failure",
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
