from dataclasses import dataclass
from typing import Literal


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
    project_status: str
    provider_retry_due: bool = False


def route_run(facts: RunFacts) -> FlowDecision:
    """Pure routing policy; durable I/O remains owned by RunHost and Harness."""
    if facts.desired_state == "stopped":
        return "stop"
    if facts.project_status == "waiting_for_provider":
        return "advance" if facts.provider_retry_due else "wait_provider"
    if facts.project_status == "waiting_for_user":
        return "wait_user"
    if facts.project_status == "failed":
        return "fail"
    if facts.project_status in {"pause_requested", "paused"}:
        return "stop"
    return "advance"
