from dataclasses import dataclass
from typing import Any

from app.schemas.events import EventStatus


_KNOWN_AGENT_EVENTS = {
    "agent_activation_started",
    "agent_tool_result",
    "agent_transport_retry",
    "agent_semantic_revision_scheduled",
    "agent_evaluation_started",
    "agent_evaluation_completed",
    "agent_evaluation_failed",
    "agent_activation_completed",
    "agent_activation_failed",
}
_SAFE_SCALAR_KEYS = {
    "activation_id",
    "candidate_run_id",
    "role",
    "phase",
    "tool_name",
    "tool_call_id",
    "status",
    "error_code",
    "checkpoint_id",
    "terminal",
    "outcome",
    "turns_used",
    "retry",
    "revision",
    "limit",
    "evaluation_id",
    "candidate_artifact_id",
    "failure_id",
    "category",
    "code",
}
_SAFE_PATH_KEYS = {"artifact_paths", "evidence_paths"}


@dataclass(frozen=True)
class AgentEventProjection:
    kind: str
    status: EventStatus
    message: str
    artifact_path: str | None
    routing_decision: str | None
    payload: dict[str, object]


def project_agent_event(value: dict[str, Any]) -> AgentEventProjection | None:
    kind = value.get("kind")
    if not isinstance(kind, str) or kind not in _KNOWN_AGENT_EVENTS:
        return None
    payload = _safe_payload(value)
    status = _event_status(kind, payload)
    paths = _paths(payload)
    return AgentEventProjection(
        kind=kind,
        status=status,
        message=_event_message(kind, status),
        artifact_path=_primary_artifact(kind, paths),
        routing_decision=_routing_decision(kind, payload),
        payload=payload,
    )


def _safe_payload(value: dict[str, Any]) -> dict[str, object]:
    payload: dict[str, object] = {}
    for key in _SAFE_SCALAR_KEYS:
        item = value.get(key)
        if isinstance(item, (str, int, float, bool)) or item is None:
            payload[key] = item
    for key in _SAFE_PATH_KEYS:
        item = value.get(key)
        if isinstance(item, list):
            payload[key] = [path for path in item if isinstance(path, str)][:100]
    allowed_tools = value.get("allowed_tools")
    if isinstance(allowed_tools, list):
        payload["allowed_tools"] = [
            name for name in allowed_tools if isinstance(name, str)
        ][:100]
    return payload


def _event_status(kind: str, payload: dict[str, object]) -> EventStatus:
    if kind == "agent_activation_started":
        return "started"
    if kind == "agent_evaluation_started":
        return "started"
    if kind in {"agent_transport_retry", "agent_semantic_revision_scheduled"}:
        return "requested"
    if kind in {"agent_activation_failed", "agent_evaluation_failed"}:
        return "failed"
    if kind == "agent_tool_result" and payload.get("status") == "error":
        return "failed"
    return "completed"


def _event_message(kind: str, status: EventStatus) -> str:
    messages = {
        "agent_activation_started": "Bounded Loop Agent activation started.",
        "agent_tool_result": (
            "Agent Tool call was rejected by Harness validation."
            if status == "failed"
            else "Agent Tool call completed through the Harness boundary."
        ),
        "agent_transport_retry": "Agent provider call scheduled a bounded transport retry.",
        "agent_semantic_revision_scheduled": (
            "Evaluator feedback scheduled a bounded same-Agent candidate revision."
        ),
        "agent_evaluation_started": "Stateless semantic evaluation started.",
        "agent_evaluation_completed": "Stateless semantic evaluation completed.",
        "agent_evaluation_failed": "Stateless semantic evaluation failed closed.",
        "agent_activation_completed": "Bounded Loop Agent reached a durable checkpoint.",
        "agent_activation_failed": "Bounded Loop Agent activation failed closed.",
    }
    return messages[kind]


def _paths(payload: dict[str, object]) -> list[str]:
    paths: list[str] = []
    for key in ("artifact_paths", "evidence_paths"):
        value = payload.get(key)
        if isinstance(value, list):
            paths.extend(path for path in value if isinstance(path, str))
    return list(dict.fromkeys(paths))


def _primary_artifact(kind: str, paths: list[str]) -> str | None:
    if not paths:
        return None
    if kind == "agent_activation_completed":
        telemetry = next((path for path in paths if path.endswith("telemetry.json")), None)
        return telemetry or paths[-1]
    if kind == "agent_activation_failed":
        failure = next((path for path in paths if path.endswith("failure.json")), None)
        return failure or paths[0]
    return paths[0]


def _routing_decision(kind: str, payload: dict[str, object]) -> str | None:
    if kind == "agent_semantic_revision_scheduled":
        return "revise_current_candidate"
    if kind == "agent_transport_retry":
        return "retry_provider_call"
    if kind == "agent_activation_completed":
        outcome = payload.get("outcome")
        return outcome if isinstance(outcome, str) else None
    if kind == "agent_activation_failed":
        code = payload.get("code")
        return code if isinstance(code, str) else None
    return None
