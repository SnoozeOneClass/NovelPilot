from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

RunStatus = Literal[
    "running",
    "pause_requested",
    "paused",
    "waiting_for_user",
    "failure_paused",
    "completed",
]
RouteAction = Literal[
    "execute_agent_task",
    "await_agent_task",
    "apply_agent_result",
    "execute_domain_command",
    "complete_run",
    "await_user",
    "await_retry",
    "await_resume",
    "idle",
]


class RouteSnapshot(BaseModel):
    """Only compact authoritative relationship/status facts allowed to influence Route."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    run_id: str
    run_status: RunStatus
    desired_state: Literal["running", "paused"]
    wait_reason_code: str | None = None
    blocking_task_id: str | None = None
    active_task_id: str | None = None
    queued_task_id: str | None = None
    pending_delivery_task_id: str | None = None
    next_domain_action: str | None = None
    domain_complete: bool = False

    @model_validator(mode="after")
    def _one_executable_boundary(self) -> RouteSnapshot:
        present = sum(
            value is not None
            for value in (
                self.active_task_id,
                self.queued_task_id,
                self.pending_delivery_task_id,
                self.next_domain_action,
            )
        )
        if present > 1:
            raise ValueError("A Route snapshot may expose only one executable boundary.")
        if self.domain_complete and present:
            raise ValueError("A complete domain cannot expose another executable boundary.")
        return self


class RouteDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    action: RouteAction
    target_id: str | None = None
    reason_code: str


def decide_route(snapshot: RouteSnapshot) -> RouteDecision:
    """Choose one next boundary without reading Blob content, evidence, or live events."""
    if snapshot.run_status == "completed":
        return RouteDecision(action="idle", reason_code="run_completed")
    if snapshot.run_status == "failure_paused":
        return RouteDecision(
            action="await_retry",
            target_id=snapshot.blocking_task_id,
            reason_code="failed_task_requires_explicit_retry",
        )
    if snapshot.run_status in {"paused", "pause_requested"}:
        return RouteDecision(action="await_resume", reason_code="run_paused")
    if snapshot.run_status == "waiting_for_user":
        return RouteDecision(
            action="await_user",
            reason_code=snapshot.wait_reason_code or "user_action_required",
        )
    if snapshot.active_task_id is not None:
        return RouteDecision(
            action="await_agent_task",
            target_id=snapshot.active_task_id,
            reason_code="task_activation_in_progress",
        )
    if snapshot.pending_delivery_task_id is not None:
        return RouteDecision(
            action="apply_agent_result",
            target_id=snapshot.pending_delivery_task_id,
            reason_code="typed_result_requires_domain_command",
        )
    if snapshot.queued_task_id is not None:
        return RouteDecision(
            action="execute_agent_task",
            target_id=snapshot.queued_task_id,
            reason_code="frozen_task_ready",
        )
    if snapshot.next_domain_action is not None:
        return RouteDecision(
            action="execute_domain_command",
            target_id=snapshot.next_domain_action,
            reason_code="authoritative_transition_ready",
        )
    if snapshot.domain_complete:
        return RouteDecision(action="complete_run", reason_code="completion_contract_satisfied")
    raise ValueError("Running Route snapshot has no deterministic next boundary.")
