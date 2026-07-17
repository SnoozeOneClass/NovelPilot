from dataclasses import dataclass
from typing import Literal

from app.schemas.projects import RunStatus


FlowDecision = Literal[
    "advance",
    "wait_provider",
    "wait_user",
    "stop",
    "fail",
]


@dataclass(frozen=True)
class RunFacts:
    desired_state: Literal["stopped", "running"]
    project_status: RunStatus
    provider_retry_due: bool = False


RUNNING_FLOW_DECISIONS: dict[RunStatus, FlowDecision] = {
    "idle": "advance",
    "running": "advance",
    "pause_requested": "stop",
    "paused": "stop",
    "waiting_for_user": "wait_user",
    "waiting_for_provider": "wait_provider",
    "failed": "fail",
}


def route_run(facts: RunFacts) -> FlowDecision:
    """Pure routing policy; durable I/O remains owned by RunHost and Harness."""
    if facts.desired_state == "stopped":
        return "stop"
    if facts.project_status == "waiting_for_provider":
        return "advance" if facts.provider_retry_due else "wait_provider"
    return RUNNING_FLOW_DECISIONS[facts.project_status]
