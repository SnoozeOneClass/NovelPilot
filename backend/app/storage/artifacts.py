from pathlib import Path
from typing import Any

from app.schemas.artifacts import ArtifactSummary
from app.storage.events import read_events
from app.storage.json_files import read_json
from app.storage.text_files import read_text_file

EVENT_TRACKED_ARTIFACT_KINDS = {
    "arc_plan",
    "arc_revision",
    "book_feedback",
    "book_direction",
    "book_direction_candidate",
    "book_direction_draft",
    "book_direction_constraints",
    "book_rolling_contract",
    "book_rolling_contract_candidate",
    "candidate_observations",
    "candidate_state_patch",
    "committed_state_patch",
    "context_snapshot",
    "draft",
    "export",
    "final",
    "retry_manifest",
    "review",
    "state_patch_rejection",
    "verification",
}

INTERNAL_ARTIFACT_SUFFIXES = (".tmp", ".lock")
INTERNAL_ARTIFACT_DIRECTORIES = {".event-outbox", ".transactions"}


def list_project_artifacts(project_path: Path) -> list[str]:
    artifacts: list[str] = []
    for path in project_path.rglob("*"):
        if path.is_file() and not is_internal_artifact_path(path):
            artifacts.append(path.relative_to(project_path).as_posix())
    return sorted(artifacts)


def is_internal_artifact_path(path: Path) -> bool:
    return path.name.endswith(INTERNAL_ARTIFACT_SUFFIXES) or any(
        part in INTERNAL_ARTIFACT_DIRECTORIES for part in path.parts
    )


def summarize_project_artifacts(project_path: Path) -> list[ArtifactSummary]:
    events = read_events(project_path)
    recorded_artifact_paths = {event.artifact_path for event in events if event.artifact_path}
    provenance_by_path = {
        event.artifact_path: provenance
        for event in events
        if event.artifact_path
        for provenance in [_artifact_event_provenance(event.payload)]
        if provenance
    }
    return [
        _annotate_event_status(
            summarize_artifact(project_path, relative_path),
            recorded_artifact_paths,
            provenance_by_path,
        )
        for relative_path in list_project_artifacts(project_path)
    ]


def summarize_artifact(project_path: Path, relative_path: str) -> ArtifactSummary:
    path = project_path / relative_path
    name = path.name
    if name == "context_snapshot.json":
        return _summarize_context_snapshot(path, relative_path)
    if name == "observations.json":
        return _summarize_observations(path, relative_path)
    if name == "verification.json":
        return _summarize_verification(path, relative_path)
    if name == "candidate_state_patch.json":
        return _summarize_candidate_patch(path, relative_path)
    if name == "committed_state_patch.json":
        return _summarize_committed_patch(path, relative_path)
    if name == "state_patch_rejection.json":
        return _summarize_patch_rejection(path, relative_path)
    if name == "retry_manifest.json":
        return _summarize_retry_manifest(path, relative_path)
    if name == "review.md":
        return _summarize_markdown(path, relative_path, "review", "Review", "reviewed")
    if name == "draft.md":
        return _summarize_markdown(path, relative_path, "draft", "Draft", "candidate", candidate=True)
    if name == "final.md":
        return _summarize_markdown(path, relative_path, "final", "Final", "committed", committed=True)
    if name == "plan.md" and relative_path.startswith("arcs/"):
        return _summarize_markdown(path, relative_path, "arc_plan", "Arc Plan", "planned")
    if name == "revision.md" and relative_path.startswith("arcs/"):
        return _summarize_markdown(
            path,
            relative_path,
            "arc_revision",
            "Arc Revision",
            "revised",
        )
    if name == "feedback.md" and relative_path == "book/feedback.md":
        return _summarize_markdown(
            path,
            relative_path,
            "book_feedback",
            "Book Feedback",
            "recorded",
        )
    if name == "direction_draft.md" and relative_path == "book/direction_draft.md":
        return _summarize_markdown(
            path,
            relative_path,
            "book_direction_draft",
            "Book Direction Draft",
            "candidate",
            candidate=True,
        )
    if name == "candidate_direction.md" and relative_path.startswith("book/reviews/"):
        return _summarize_markdown(
            path,
            relative_path,
            "book_direction_candidate",
            "Candidate Book Direction",
            "candidate",
            candidate=True,
        )
    if name == "candidate_constraints.json" and relative_path.startswith("book/reviews/"):
        return ArtifactSummary(
            path=relative_path,
            kind="book_direction_constraints",
            title="Candidate Book Constraints",
            status="candidate",
            detail=_constraint_detail(path),
            candidate=True,
        )
    if name == "rolling_plan.md" and relative_path.startswith("book/reviews/"):
        return _summarize_markdown(
            path,
            relative_path,
            "book_rolling_contract_candidate",
            "Candidate Rolling Story Arc Contract",
            "candidate",
            candidate=True,
        )
    if name == "direction.md" and relative_path == "book/direction.md":
        return _summarize_markdown(
            path,
            relative_path,
            "book_direction",
            "Approved Book Direction",
            "committed",
            committed=True,
        )
    if name == "constraints.json" and relative_path == "book/constraints.json":
        return ArtifactSummary(
            path=relative_path,
            kind="book_direction_constraints",
            title="Approved Book Constraints",
            status="committed",
            detail=_constraint_detail(path),
            committed=True,
        )
    if name == "outline.md" and relative_path == "book/outline.md":
        return _summarize_markdown(
            path,
            relative_path,
            "book_rolling_contract",
            "Rolling Story Arc Contract",
            "committed",
            committed=True,
        )
    if name == "transcript.jsonl" and relative_path == "book/discussion/transcript.jsonl":
        line_count = len([line for line in read_text_file(path).splitlines() if line.strip()])
        return ArtifactSummary(
            path=relative_path,
            kind="book_discussion_transcript",
            title="Book Discussion Transcript",
            status="audited",
            detail=f"{line_count} messages",
        )
    if name == "manuscript.md":
        return _summarize_markdown(path, relative_path, "export", "Export", "generated", committed=True)
    return ArtifactSummary(
        path=relative_path,
        kind="other",
        title=name,
        status="available",
        detail=_file_size_detail(path),
    )


def _annotate_event_status(
    summary: ArtifactSummary,
    recorded_artifact_paths: set[str],
    provenance_by_path: dict[str, dict[str, str]],
) -> ArtifactSummary:
    if summary.kind not in EVENT_TRACKED_ARTIFACT_KINDS:
        return summary
    if summary.path in recorded_artifact_paths:
        return summary.model_copy(
            update={
                "event_status": "recorded",
                "event_note": "events.jsonl records this artifact write.",
                **provenance_by_path.get(summary.path, {}),
            }
        )
    return summary.model_copy(
        update={
            "event_status": "missing",
            "event_note": (
                "Artifact exists without a matching durable event; inspect before using it "
                "as recovered harness evidence."
            ),
        }
    )


def _artifact_event_provenance(payload: dict[str, Any]) -> dict[str, str]:
    provenance: dict[str, str] = {}
    profile_id = payload.get("profile_id")
    model_snapshot = payload.get("model_snapshot")
    if isinstance(profile_id, str) and profile_id:
        provenance["profile_id"] = profile_id
    if isinstance(model_snapshot, str) and model_snapshot:
        provenance["model_snapshot"] = model_snapshot
    return provenance


def _summarize_context_snapshot(path: Path, relative_path: str) -> ArtifactSummary:
    payload = _read_dict(path)
    sources = _as_list(payload.get("sources"))
    excluded = _as_list(payload.get("excluded"))
    return ArtifactSummary(
        path=relative_path,
        kind="context_snapshot",
        title="Context Snapshot",
        status="audited",
        detail=f"{len(sources)} sources, {len(excluded)} exclusions",
        signals=[
            f"{_string_value(source.get('id'), 'source')}:{_string_value(source.get('usage'), 'usage')}"
            for source in sources
            if isinstance(source, dict)
        ],
    )


def _summarize_observations(path: Path, relative_path: str) -> ArtifactSummary:
    payload = _read_dict(path)
    signals = [
        f"events:{len(_as_list(payload.get('events')))}",
        f"characters:{len(_as_list(payload.get('character_changes')))}",
        f"relationships:{len(_as_list(payload.get('relationship_changes')))}",
        f"world:{len(_as_list(payload.get('world_fact_candidates')))}",
        f"foreshadowing:{len(_as_list(payload.get('foreshadowing_candidates')))}",
    ]
    return ArtifactSummary(
        path=relative_path,
        kind="candidate_observations",
        title="Candidate Observations",
        status=_string_value(payload.get("status"), "candidate"),
        detail="Candidate-only observations; not canon.",
        candidate=True,
        signals=signals,
    )


def _summarize_verification(path: Path, relative_path: str) -> ArtifactSummary:
    payload = _read_dict(path)
    commit_allowed = bool(payload.get("commit_allowed"))
    reasons = _as_list(payload.get("reasons"))
    signals = [
        f"{_string_value(signal.get('name'), 'signal')}:{_string_value(signal.get('status'), 'unknown')}"
        for signal in _as_list(payload.get("signals"))
        if isinstance(signal, dict)
    ]
    return ArtifactSummary(
        path=relative_path,
        kind="verification",
        title="Verification",
        status="passed" if commit_allowed else "failed",
        detail="Commit allowed." if commit_allowed else _reason_detail(reasons),
        routing_decision=_optional_string(payload.get("routing_decision")),
        signals=signals,
    )


def _summarize_candidate_patch(path: Path, relative_path: str) -> ArtifactSummary:
    payload = _read_dict(path)
    operations = _as_list(payload.get("operations"))
    targets = [
        _string_value(operation.get("target_file"), "target")
        for operation in operations
        if isinstance(operation, dict)
    ]
    return ArtifactSummary(
        path=relative_path,
        kind="candidate_state_patch",
        title="Candidate State Patch",
        status="candidate",
        detail=f"{len(operations)} proposed operations",
        candidate=True,
        signals=targets,
    )


def _summarize_committed_patch(path: Path, relative_path: str) -> ArtifactSummary:
    payload = _read_dict(path)
    operations = _as_list(payload.get("operations"))
    raw_validation = payload.get("validation")
    validation = raw_validation if isinstance(raw_validation, dict) else {}
    failed_checks = [
        key
        for key in ["schema", "versions", "evidence", "conflicts"]
        if validation.get(key) != "passed"
    ]
    return ArtifactSummary(
        path=relative_path,
        kind="committed_state_patch",
        title="Committed State Patch",
        status="passed" if not failed_checks else "failed",
        detail=f"{len(operations)} committed operations",
        committed=True,
        signals=[f"{key}:{validation.get(key, 'missing')}" for key in validation],
    )


def _summarize_patch_rejection(path: Path, relative_path: str) -> ArtifactSummary:
    payload = _read_dict(path)
    reasons = _as_list(payload.get("reasons"))
    return ArtifactSummary(
        path=relative_path,
        kind="state_patch_rejection",
        title="Patch Rejection",
        status="failed",
        detail=_reason_detail(reasons),
        signals=[str(reason) for reason in reasons[:5]],
    )


def _summarize_retry_manifest(path: Path, relative_path: str) -> ArtifactSummary:
    payload = _read_dict(path)
    archived = _as_list(payload.get("archived_artifacts"))
    retry_scope = _string_value(payload.get("retry_scope"), "retry")
    return ArtifactSummary(
        path=relative_path,
        kind="retry_manifest",
        title="Retry Manifest",
        status="prepared",
        detail=f"{retry_scope}: {len(archived)} archived artifacts",
        routing_decision="retry",
        signals=[str(item) for item in archived[:5]],
    )


def _summarize_markdown(
    path: Path,
    relative_path: str,
    kind: str,
    title: str,
    status: str,
    *,
    candidate: bool = False,
    committed: bool = False,
) -> ArtifactSummary:
    text = read_text_file(path) if path.exists() else ""
    first_line = next((line.strip("# ").strip() for line in text.splitlines() if line.strip()), "")
    return ArtifactSummary(
        path=relative_path,
        kind=kind,
        title=title,
        status=status,
        detail=first_line or _file_size_detail(path),
        candidate=candidate,
        committed=committed,
    )


def _read_dict(path: Path) -> dict[str, Any]:
    payload = read_json(path, default={})
    return payload if isinstance(payload, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _string_value(value: Any, fallback: str) -> str:
    return value if isinstance(value, str) and value else fallback


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _reason_detail(reasons: list[Any]) -> str:
    if not reasons:
        return "No reason recorded."
    return str(reasons[0])


def _constraint_detail(path: Path) -> str:
    payload = _read_dict(path)
    count = sum(
        len(_as_list(payload.get(key)))
        for key in [
            "confirmed",
            "must_preserve",
            "must_avoid",
            "creative_freedoms",
            "open_decisions",
        ]
    )
    return f"{count} structured constraints"


def _file_size_detail(path: Path) -> str:
    if not path.exists():
        return "Missing file."
    return f"{path.stat().st_size} bytes"
