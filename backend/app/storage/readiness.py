from pathlib import Path

from app.schemas.completion import GateStatus
from app.schemas.events import HarnessEvent
from app.schemas.projects import ProjectMetadata
from app.schemas.readiness import ProjectReadiness, ReadinessGate, RunNextAction
from app.storage import arcs as arc_storage
from app.storage import book_revisions as book_revision_storage
from app.storage.completion import audit_project_completion
from app.storage.events import read_events
from app.storage.json_files import read_json
from app.storage.profiles import list_public_profiles
from app.storage.projects import read_project_metadata
from app.storage.retries import retry_scope_for_chapter
from app.storage.setup import read_setup_state

APPROVED_BOOK_ARTIFACTS = [
    "book/setup.json",
    "book/direction.md",
    "book/constraints.json",
    "book/settings.md",
    "book/outline.md",
    "book/state.json",
]


def build_project_readiness(
    project_path: Path,
    *,
    active_runner: bool | None = None,
) -> ProjectReadiness:
    metadata = read_project_metadata(project_path)
    gates = [
        _book_setup_gate(project_path, metadata),
        _book_revision_gate(project_path),
        _active_profile_gate(),
        _run_control_gate(metadata.run_status),
        _completion_gate(project_path),
    ]
    required_gates = [gate for gate in gates if gate.required]
    can_start_run = all(gate.status == "passed" for gate in required_gates)
    status = _overall_status(required_gates)
    return ProjectReadiness(
        status=status,
        can_start_run=can_start_run,
        gates=gates,
        next_action=_next_action(project_path, metadata, gates, active_runner),
    )


def _book_setup_gate(project_path: Path, metadata: ProjectMetadata) -> ReadinessGate:
    setup_state = read_setup_state(project_path)
    if setup_state.approved:
        missing = [
            relative_path
            for relative_path in APPROVED_BOOK_ARTIFACTS
            if not (project_path / relative_path).exists()
        ]
        book_state = read_json(project_path / "book" / "state.json", default={}) or {}
        if book_state.get("setup_approved") is not True:
            missing.append("book/state.json:setup_approved")
        if not metadata.title:
            missing.append("project.json:title")
        if missing:
            return ReadinessGate(
                id="book_setup",
                status="failed",
                message="Book setup is approved but required book artifacts are incomplete.",
                evidence=missing,
            )
        return ReadinessGate(
            id="book_setup",
            status="passed",
            message="Book setup is approved.",
            evidence=APPROVED_BOOK_ARTIFACTS,
        )
    evidence = [
        f"phase:{setup_state.phase}",
        f"turns:{setup_state.turn_count}",
        f"direction_draft:{'present' if setup_state.direction_draft.strip() else 'missing'}",
    ]
    if setup_state.candidate is not None:
        evidence.extend(
            [
                f"candidate_revision:{setup_state.candidate.revision}",
                f"candidate_review:{setup_state.candidate.review.status}",
            ]
        )
    return ReadinessGate(
        id="book_setup",
        status="pending",
        message="Book direction must be discussed, reviewed, and explicitly approved.",
        evidence=evidence,
    )


def _active_profile_gate() -> ReadinessGate:
    profiles = list_public_profiles()
    if profiles.active_profile_id is None:
        return ReadinessGate(
            id="active_llm_profile",
            status="pending",
            message="Select an enabled LLM profile with a stored API key.",
            evidence=[],
        )

    active = next(
        (profile for profile in profiles.profiles if profile.id == profiles.active_profile_id),
        None,
    )
    if active is None:
        return ReadinessGate(
            id="active_llm_profile",
            status="failed",
            message=f"Active LLM profile is missing: {profiles.active_profile_id}",
            evidence=[profiles.active_profile_id],
        )
    if not active.enabled:
        return ReadinessGate(
            id="active_llm_profile",
            status="pending",
            message=f"Active LLM profile is disabled: {active.id}",
            evidence=[active.id],
        )
    if not active.has_api_key:
        return ReadinessGate(
            id="active_llm_profile",
            status="pending",
            message=f"Active LLM profile has no stored API key: {active.id}",
            evidence=[active.id],
        )
    return ReadinessGate(
        id="active_llm_profile",
        status="passed",
        message=f"Active LLM profile is ready: {active.id}",
        evidence=[active.id, active.model, active.protocol],
    )


def _book_revision_gate(project_path: Path) -> ReadinessGate:
    revision = book_revision_storage.read_pending_book_revision(project_path)
    if revision is None:
        return ReadinessGate(
            id="book_revision",
            status="passed",
            message="No Book revision is awaiting explicit approval.",
            evidence=[],
        )
    return ReadinessGate(
        id="book_revision",
        status="pending",
        message=(
            "An evaluated Book revision requires explicit user approval, including in "
            "full-auto mode."
        ),
        evidence=[
            revision.revision_id,
            f"base_book_version:{revision.base_book_version}",
            revision.candidate.direction_path,
            revision.verification_path,
        ],
    )


def _run_control_gate(run_status: str) -> ReadinessGate:
    if run_status in {"running", "pause_requested"}:
        return ReadinessGate(
            id="run_control",
            status="pending",
            message=f"A harness run is already in progress: {run_status}",
            evidence=[run_status],
        )
    return ReadinessGate(
        id="run_control",
        status="passed",
        message=f"Run control accepts a start or resume command from status: {run_status}",
        evidence=[run_status],
    )


def _completion_gate(project_path: Path) -> ReadinessGate:
    audit = audit_project_completion(project_path)
    return ReadinessGate(
        id="completion_evidence",
        status=audit.status,
        required=False,
        message="Completion audit summarizes output-secret, live-provider, and literary-review evidence.",
        evidence=[f"{gate.id}:{gate.status}" for gate in audit.gates],
    )


def _overall_status(gates: list[ReadinessGate]) -> GateStatus:
    if any(gate.status == "failed" for gate in gates):
        return "failed"
    if any(gate.status == "pending" for gate in gates):
        return "pending"
    return "passed"


def _next_action(
    project_path: Path,
    metadata: ProjectMetadata,
    gates: list[ReadinessGate],
    active_runner: bool | None,
) -> RunNextAction:
    gate_by_id = {gate.id: gate for gate in gates}
    run_control_gate = gate_by_id["run_control"]
    if run_control_gate.status != "passed":
        if active_runner is False and metadata.run_status in {"running", "pause_requested"}:
            return RunNextAction(
                id="recover_stale_run",
                command="POST /api/runs/recover-stale",
                requires_user=True,
                message=(
                    "Recover a stale run lock after a stopped backend process before resuming."
                ),
                evidence=[metadata.run_status, "no_active_runner"],
            )
        return RunNextAction(
            id="wait_for_safe_checkpoint",
            message="A harness action is already running; wait for the next safe checkpoint.",
            evidence=run_control_gate.evidence,
        )

    failed_required = [gate for gate in gates if gate.required and gate.status == "failed"]
    if failed_required:
        return RunNextAction(
            id="repair_project_state",
            requires_user=True,
            message="A required project readiness gate failed and must be repaired before running.",
            evidence=[f"{gate.id}:{item}" for gate in failed_required for item in gate.evidence],
        )

    book_gate = gate_by_id["book_setup"]
    if book_gate.status != "passed":
        setup_state = read_setup_state(project_path)
        candidate = setup_state.candidate
        if candidate is not None and candidate.approval_allowed:
            return RunNextAction(
                id="approve_book_direction",
                command="POST /api/setup/approve",
                requires_user=True,
                message="Explicitly approve the reviewed candidate Book Direction.",
                evidence=[
                    f"candidate_revision:{candidate.revision}",
                    candidate.verification_path,
                ],
            )
        profile_gate = gate_by_id["active_llm_profile"]
        if profile_gate.status != "passed":
            return RunNextAction(
                id="configure_llm_profile",
                command="POST /api/profiles",
                requires_user=True,
                message="Select an enabled LLM profile with a stored API key.",
                evidence=profile_gate.evidence,
            )
        if candidate is not None:
            return RunNextAction(
                id="continue_book_discussion",
                command="POST /api/setup/turn",
                requires_user=True,
                message="Continue the open-ended Book Direction discussion.",
                evidence=[
                    f"candidate_revision:{candidate.revision}",
                    f"candidate_review:{candidate.review.status}",
                    *[
                        issue.message
                        for issue in candidate.review.issues
                        if issue.severity == "blocking"
                    ][:3],
                ],
            )
        if setup_state.direction_draft.strip() and setup_state.readiness.status == "ready":
            return RunNextAction(
                id="review_book_direction",
                command="POST /api/setup/prepare-review",
                requires_user=True,
                message="Prepare the current Book Direction draft for review.",
                evidence=[f"turns:{setup_state.turn_count}", setup_state.readiness.reason],
            )
        return RunNextAction(
            id="continue_book_discussion",
            command="POST /api/setup/turn",
            requires_user=True,
            message="Continue the open-ended Book Direction discussion.",
            evidence=[
                f"phase:{setup_state.phase}",
                f"turns:{setup_state.turn_count}",
                *setup_state.unresolved_questions[:3],
            ],
        )

    book_revision = book_revision_storage.read_pending_book_revision(project_path)
    if book_revision is not None:
        return RunNextAction(
            id="approve_book_revision",
            command="POST /api/book-revisions/approve",
            requires_user=True,
            message=(
                "Explicitly approve the evaluated Book revision before any approved "
                "Book contract changes."
            ),
            evidence=[
                book_revision.revision_id,
                f"base_book_version:{book_revision.base_book_version}",
                book_revision.candidate.direction_path,
                book_revision.review_path,
                book_revision.verification_path,
            ],
        )

    profile_gate = gate_by_id["active_llm_profile"]
    if profile_gate.status != "passed":
        return RunNextAction(
            id="configure_llm_profile",
            command="POST /api/profiles",
            requires_user=True,
            message="Select an enabled LLM profile with a stored API key.",
            evidence=profile_gate.evidence,
        )

    approved_book_revision = (
        book_revision_storage.read_approved_book_revision_with_pending_downstream(
            project_path
        )
    )
    if approved_book_revision is not None:
        return RunNextAction(
            id="resume_run",
            command="POST /api/runs/resume",
            can_auto_continue=True,
            message=(
                "Continue the Harness so the approved Book revision can update only "
                "uncommitted downstream planning."
            ),
            evidence=[
                approved_book_revision.revision_id,
                f"book_version:{approved_book_revision.target_book_version}",
                approved_book_revision.candidate.direction_path,
            ],
        )

    if metadata.run_status == "failed":
        events = read_events(project_path)
        last_event = events[-1] if events else None
        return RunNextAction(
            id="inspect_failure",
            requires_user=True,
            message="Inspect the latest harness failure before resuming.",
            evidence=_event_evidence(last_event),
        )

    retry_action = _retry_next_action(project_path, metadata)
    if retry_action is not None:
        return retry_action

    arc_action = _story_arc_review_next_action(project_path, metadata)
    if arc_action is not None:
        return arc_action

    if metadata.run_status in {"paused", "waiting_for_user"}:
        return RunNextAction(
            id="resume_run",
            command="POST /api/runs/resume",
            can_auto_continue=True,
            message="Resume the harness from the latest committed checkpoint.",
            evidence=[metadata.run_status],
        )

    if _has_started_before(project_path):
        return RunNextAction(
            id="resume_run",
            command="POST /api/runs/resume",
            can_auto_continue=True,
            message="Continue the existing harness run from committed state.",
            evidence=[metadata.run_status],
        )

    return RunNextAction(
        id="start_run",
        command="POST /api/runs/start",
        can_auto_continue=True,
        message="Start the harness run.",
        evidence=[metadata.run_status],
    )


def _retry_next_action(project_path: Path, metadata: ProjectMetadata) -> RunNextAction | None:
    if metadata.active_chapter_id is None:
        return None

    chapter_path = project_path / "chapters" / metadata.active_chapter_id
    if not chapter_path.exists():
        return None

    retry_scope, artifact_names = retry_scope_for_chapter(chapter_path)
    if retry_scope is None:
        return None

    return RunNextAction(
        id="retry_current_chapter",
        command="POST /api/runs/retry-current-chapter",
        requires_user=True,
        message=f"Prepare a retry for the current chapter {metadata.active_chapter_id}.",
        evidence=[retry_scope, *artifact_names],
    )


def _story_arc_review_next_action(
    project_path: Path,
    metadata: ProjectMetadata,
) -> RunNextAction | None:
    if metadata.active_arc_id is None:
        return None

    arc = arc_storage.read_current_arc_state(project_path)
    if arc is None:
        if metadata.operation_mode != "participatory":
            return None
        evidence = [metadata.active_arc_id, "missing_arc_state"]
    elif arc.human_review == "approved":
        return None
    elif (
        arc.human_review != "awaiting_review"
        and metadata.operation_mode != "participatory"
    ):
        return None
    else:
        evidence = [arc.arc_id, arc.plan_path, arc.human_review]

    return RunNextAction(
        id="approve_story_arc",
        command="POST /api/arcs/current/approve",
        requires_user=True,
        message="The current story arc plan has a pending human-review gate.",
        evidence=evidence,
    )


def _has_started_before(project_path: Path) -> bool:
    return any(event.kind in {"run_started", "run_resumed"} for event in read_events(project_path))


def _event_evidence(event: HarnessEvent | None) -> list[str]:
    if event is None:
        return []
    return [
        value
        for value in [
            getattr(event, "kind", None),
            getattr(event, "atomic_action", None),
            getattr(event, "artifact_path", None),
            getattr(event, "message", None),
        ]
        if isinstance(value, str) and value
    ]
