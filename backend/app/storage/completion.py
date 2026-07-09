from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.schemas.completion import (
    CompletionGate,
    GateStatus,
    LiteraryReviewRecord,
    LiteraryReviewRequest,
    ProjectCompletionAudit,
)
from app.storage.json_files import read_json, write_json
from app.storage.secret_audit import audit_path_for_profile_secrets
from app.storage.text_files import write_text_file


REQUIRED_SMOKE_ARTIFACT_KEYS = [
    "final",
    "review",
    "verification",
    "candidate_state_patch",
    "committed_state_patch",
]


def audit_project_completion(project_path: Path) -> ProjectCompletionAudit:
    smoke_report_path = project_path / "exports" / "live_smoke_report.json"
    gates = [
        build_output_secret_audit_gate(project_path),
        _live_smoke_gate(smoke_report_path),
        _literary_review_gate(project_path, smoke_report_path),
    ]
    return ProjectCompletionAudit(status=_overall_status(gates), gates=gates)


def build_output_secret_audit_gate(root_path: Path) -> CompletionGate:
    result = audit_path_for_profile_secrets(root_path)
    evidence = [
        str(root_path),
        f"profiles_checked:{result.profile_count}",
        f"files_scanned:{result.scanned_file_count}",
    ]
    if result.findings:
        return CompletionGate(
            id="output_secret_audit",
            status="failed",
            message="Output contains configured LLM profile API keys or base URLs.",
            evidence=[
                *evidence,
                *[
                    f"{finding.path}:profile={finding.profile_id}:kind={finding.kind}"
                    for finding in result.findings
                ],
            ],
        )
    return CompletionGate(
        id="output_secret_audit",
        status="passed",
        message="Output contains no configured LLM profile API keys or base URLs.",
        evidence=evidence,
    )


def record_literary_review(
    project_path: Path,
    request: LiteraryReviewRequest,
) -> LiteraryReviewRecord:
    smoke_report_path = project_path / "exports" / "live_smoke_report.json"
    if not smoke_report_path.exists():
        raise FileNotFoundError(f"Missing live smoke report: {smoke_report_path}")
    live_smoke_gate = _live_smoke_gate(smoke_report_path)
    if live_smoke_gate.status != "passed":
        raise ValueError(
            "Cannot record literary review until live provider smoke passes: "
            + live_smoke_gate.message
        )
    smoke_report = _read_json_object(smoke_report_path)
    artifacts, external_artifacts = _artifact_paths(smoke_report, project_path)
    if external_artifacts:
        raise ValueError(
            "Smoke report references artifacts outside the project: "
            + ", ".join(external_artifacts)
        )
    missing = [
        f"{key}: {artifacts.get(key)}"
        for key in REQUIRED_SMOKE_ARTIFACT_KEYS
        if key not in artifacts or not artifacts[key].exists()
    ]
    if missing:
        raise FileNotFoundError("Missing smoke artifacts: " + ", ".join(missing))

    exports_path = project_path / "exports"
    exports_path.mkdir(parents=True, exist_ok=True)
    review_json_path = exports_path / "literary_review.json"
    review_markdown_path = exports_path / "literary_review.md"
    record = LiteraryReviewRecord(
        decision=request.decision,
        reviewer=request.reviewer,
        reviewed_at=datetime.now(UTC).isoformat(),
        chapter_assessment=request.chapter_assessment,
        state_patch_assessment=request.state_patch_assessment,
        notes=request.notes,
        smoke_report=str(smoke_report_path),
        reviewed_artifacts={key: str(artifacts[key]) for key in REQUIRED_SMOKE_ARTIFACT_KEYS},
        literary_review_json=str(review_json_path),
        literary_review_markdown=str(review_markdown_path),
    )
    write_json(review_json_path, record.model_dump(mode="json"))
    write_text_file(review_markdown_path, _render_literary_review_markdown(record))
    return record


def _live_smoke_gate(smoke_report_path: Path) -> CompletionGate:
    if not smoke_report_path.exists():
        return CompletionGate(
            id="live_provider_smoke",
            status="pending",
            message=(
                "No live smoke report found. Run "
                "`npm.cmd run smoke:live -- --profile-id <profile-id>`."
            ),
            evidence=[],
        )

    payload = _read_json_object(smoke_report_path)
    if payload.get("status") != "passed":
        failure_message = _failure_message(payload)
        return CompletionGate(
            id="live_provider_smoke",
            status="failed",
            message=(
                f"Live smoke report status is not passed: {payload.get('status')}"
                + (f". {failure_message}" if failure_message else "")
            ),
            evidence=[str(smoke_report_path), *_failure_evidence(payload)],
        )

    project_path = smoke_report_path.parents[1]
    artifacts, external_artifacts = _artifact_paths(payload, project_path)
    if external_artifacts:
        return CompletionGate(
            id="live_provider_smoke",
            status="failed",
            message="Live smoke report references artifacts outside the smoke project.",
            evidence=[str(smoke_report_path), *external_artifacts],
        )
    missing_keys = [key for key in REQUIRED_SMOKE_ARTIFACT_KEYS if key not in artifacts]
    if missing_keys:
        return CompletionGate(
            id="live_provider_smoke",
            status="failed",
            message="Live smoke report is missing required artifact entries: "
            + ", ".join(missing_keys),
            evidence=[str(smoke_report_path)],
        )

    missing_artifacts = [
        str(artifacts[key]) for key in REQUIRED_SMOKE_ARTIFACT_KEYS if not artifacts[key].exists()
    ]
    if missing_artifacts:
        return CompletionGate(
            id="live_provider_smoke",
            status="failed",
            message="Live smoke report references missing required artifacts.",
            evidence=[str(smoke_report_path), *missing_artifacts],
        )

    return CompletionGate(
        id="live_provider_smoke",
        status="passed",
        message="Live provider smoke report and required artifacts are present.",
        evidence=[
            str(smoke_report_path),
            *[str(artifacts[key]) for key in REQUIRED_SMOKE_ARTIFACT_KEYS],
        ],
    )


def _literary_review_gate(project_path: Path, smoke_report_path: Path) -> CompletionGate:
    if not smoke_report_path.exists():
        return CompletionGate(
            id="literary_quality_review",
            status="pending",
            message="Literary review waits for a completed live smoke project.",
            evidence=[],
        )
    review_path = project_path / "exports" / "literary_review.json"
    if not review_path.exists():
        return CompletionGate(
            id="literary_quality_review",
            status="pending",
            message=f"Literary review has not been recorded: {review_path}",
            evidence=[str(review_path)],
        )

    payload = _read_json_object(review_path)
    decision = payload.get("decision")
    if decision != "approved":
        return CompletionGate(
            id="literary_quality_review",
            status="failed",
            message=f"Literary review decision is not approved: {decision}",
            evidence=[str(review_path)],
        )
    missing = _missing_non_empty_strings(
        payload,
        ["reviewer", "chapter_assessment", "state_patch_assessment"],
    )
    if missing:
        return CompletionGate(
            id="literary_quality_review",
            status="failed",
            message="Literary review is missing fields: " + ", ".join(missing),
            evidence=[str(review_path)],
        )

    return CompletionGate(
        id="literary_quality_review",
        status="passed",
        message="Literary/usefulness review approved the generated chapter and state patch.",
        evidence=[str(review_path)],
    )


def _missing_non_empty_strings(payload: dict[str, Any], fields: list[str]) -> list[str]:
    return [
        field
        for field in fields
        if not isinstance(payload.get(field), str) or not str(payload.get(field)).strip()
    ]


def _failure_message(payload: dict[str, Any]) -> str | None:
    failure = payload.get("failure")
    if not isinstance(failure, dict):
        return None
    message = failure.get("message")
    return message if isinstance(message, str) and message.strip() else None


def _failure_evidence(payload: dict[str, Any]) -> list[str]:
    failure = payload.get("failure")
    if not isinstance(failure, dict):
        return []
    evidence: list[str] = []
    project_path = failure.get("project_path")
    if isinstance(project_path, str) and project_path:
        evidence.append(project_path)
    last_event = failure.get("last_event")
    if isinstance(last_event, dict):
        event_parts = [
            str(last_event.get("kind") or "unknown"),
            f"action={last_event.get('atomic_action') or 'none'}",
            f"status={last_event.get('status') or 'unknown'}",
            f"route={last_event.get('routing_decision') or 'none'}",
        ]
        artifact_path = last_event.get("artifact_path")
        if isinstance(artifact_path, str) and artifact_path:
            event_parts.append(f"artifact={artifact_path}")
        evidence.append("last_event:" + ":".join(event_parts))
    reasons = failure.get("artifact_reasons")
    if isinstance(reasons, list):
        evidence.extend(f"reason:{reason}" for reason in reasons[:5] if isinstance(reason, str))
    return evidence


def _read_json_object(path: Path) -> dict[str, Any]:
    value = read_json(path)
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return value


def _artifact_paths(payload: dict[str, Any], project_path: Path) -> tuple[dict[str, Path], list[str]]:
    artifacts_value = payload.get("artifacts")
    if not isinstance(artifacts_value, dict):
        return {}, []
    artifacts: dict[str, Path] = {}
    external_artifacts: list[str] = []
    project_root = project_path.resolve()
    for key, value in artifacts_value.items():
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        artifact_path = Path(value)
        if not artifact_path.is_absolute():
            artifact_path = project_path / artifact_path
        resolved = artifact_path.resolve()
        if not resolved.is_relative_to(project_root):
            external_artifacts.append(f"{key}: {value}")
            continue
        artifacts[key] = resolved
    return artifacts, external_artifacts


def _overall_status(gates: list[CompletionGate]) -> GateStatus:
    if any(gate.status == "failed" for gate in gates):
        return "failed"
    if any(gate.status == "pending" for gate in gates):
        return "pending"
    return "passed"


def _render_literary_review_markdown(record: LiteraryReviewRecord) -> str:
    lines = [
        "# Literary Review",
        "",
        f"Decision: {record.decision}",
        f"Reviewer: {record.reviewer}",
        f"Reviewed at: {record.reviewed_at}",
        "",
        "## Chapter Assessment",
        "",
        record.chapter_assessment,
        "",
        "## State Patch Assessment",
        "",
        record.state_patch_assessment,
        "",
        "## Notes",
        "",
        record.notes,
        "",
        "## Reviewed Artifacts",
        "",
    ]
    lines.extend(f"- {name}: `{path}`" for name, path in record.reviewed_artifacts.items())
    return "\n".join(lines).rstrip() + "\n"
