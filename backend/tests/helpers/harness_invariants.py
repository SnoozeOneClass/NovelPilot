from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from app.storage.events import read_events
from app.storage.json_files import read_json
from app.storage.projects import read_project_metadata
from app.storage.readiness import build_project_readiness
from app.storage.run_state import read_run_control_state


_COMMITTED_PATTERNS = (
    "book/direction.md",
    "book/constraints.json",
    "book/settings.md",
    "book/outline.md",
    "book/state.json",
    "arcs/*/plan.md",
    "chapters/*/final.md",
    "chapters/*/committed_state_patch.json",
    "canon/*.json",
)
_HUMAN_GATE_KINDS = {
    "book_question_requested",
    "book_direction_review_required",
    "story_arc_review_required",
    "book_revision_approval_required",
}


@dataclass(frozen=True)
class HarnessInvariantSnapshot:
    committed_hashes: tuple[tuple[str, str], ...]
    project_status: str
    desired_state: str
    dispatch_status: str | None
    dispatch_id: str | None
    action_key: str | None
    candidate_run_id: str | None
    activation_id: str | None
    candidate_revision: int | None
    evaluation_id: str | None
    evaluation_fingerprint: str | None
    checkpoint_sequence: int
    checkpoint_action_key: str | None
    checkpoint_input_fingerprint: str | None
    readiness_action: str | None
    action_local_budget_scope: str | None
    action_local_budget_usage: tuple[tuple[str, int], ...]
    creation_stage: str
    creation_primary_action: str | None
    creation_is_running: bool
    human_gate_count: int


def capture_harness_invariants(
    project_path: Path,
    *,
    active_runner: bool | None = False,
    include_readiness: bool = True,
) -> HarnessInvariantSnapshot:
    metadata = read_project_metadata(project_path)
    run_state = read_run_control_state(project_path)
    dispatch = run_state.dispatch
    agent_state = _active_agent_state(project_path, metadata.active_chapter_id)
    evaluation = _latest_evaluation(project_path, metadata.active_chapter_id)
    checkpoint = _latest_checkpoint(project_path)
    readiness_action = (
        build_project_readiness(project_path, active_runner=active_runner).next_action.id
        if include_readiness
        else None
    )
    creation_stage, creation_primary_action, creation_is_running = (
        _creation_projection_contract(
            project_status=metadata.run_status,
            readiness_action=readiness_action,
            active_chapter_id=metadata.active_chapter_id,
        )
    )
    budgets = agent_state.get("budgets")
    budget_payload = budgets if isinstance(budgets, dict) else {}
    events = read_events(project_path)
    return HarnessInvariantSnapshot(
        committed_hashes=_committed_hashes(project_path),
        project_status=metadata.run_status,
        desired_state=run_state.desired_state,
        dispatch_status=dispatch.status if dispatch is not None else None,
        dispatch_id=dispatch.dispatch_id if dispatch is not None else None,
        action_key=(
            dispatch.action_key
            if dispatch is not None
            else _string_value(checkpoint, "action_key")
        ),
        candidate_run_id=_string_value(agent_state, "candidate_run_id"),
        activation_id=_string_value(agent_state, "activation_id"),
        candidate_revision=_integer_value(evaluation, "candidate_revision"),
        evaluation_id=_string_value(evaluation, "evaluation_id"),
        evaluation_fingerprint=_string_value(evaluation, "input_fingerprint"),
        checkpoint_sequence=run_state.checkpoint_sequence,
        checkpoint_action_key=_string_value(checkpoint, "action_key"),
        checkpoint_input_fingerprint=_string_value(checkpoint, "input_fingerprint"),
        readiness_action=readiness_action,
        action_local_budget_scope=_string_value(
            budget_payload,
            "budget_scope_version",
        ),
        action_local_budget_usage=tuple(
            (key, value)
            for key in (
                "used_turns",
                "used_tool_schema_repairs",
                "used_transport_retries",
            )
            if isinstance((value := budget_payload.get(key)), int)
            and not isinstance(value, bool)
        ),
        creation_stage=creation_stage,
        creation_primary_action=creation_primary_action,
        creation_is_running=creation_is_running,
        human_gate_count=sum(event.kind in _HUMAN_GATE_KINDS for event in events),
    )


def assert_committed_state_unchanged(
    before: HarnessInvariantSnapshot,
    after: HarnessInvariantSnapshot,
) -> None:
    assert after.committed_hashes == before.committed_hashes


def assert_control_plane_state(
    snapshot: HarnessInvariantSnapshot,
    *,
    project_status: str,
    desired_state: str,
    dispatch_status: str | None,
    readiness_action: str | None,
) -> None:
    assert snapshot.project_status == project_status
    assert snapshot.desired_state == desired_state
    assert snapshot.dispatch_status == dispatch_status
    assert snapshot.readiness_action == readiness_action


def _committed_hashes(project_path: Path) -> tuple[tuple[str, str], ...]:
    paths: set[Path] = set()
    for pattern in _COMMITTED_PATTERNS:
        paths.update(path for path in project_path.glob(pattern) if path.is_file())
    return tuple(
        sorted(
            (
                path.relative_to(project_path).as_posix(),
                sha256(path.read_bytes()).hexdigest(),
            )
            for path in paths
        )
    )


def _active_agent_state(project_path: Path, chapter_id: str | None) -> dict[str, object]:
    if chapter_id is None:
        return {}
    payload = read_json(
        project_path / "chapters" / chapter_id / "agent" / "state.json",
        default={},
    )
    return payload if isinstance(payload, dict) else {}


def _latest_evaluation(
    project_path: Path,
    chapter_id: str | None,
) -> dict[str, object]:
    if chapter_id is None:
        return {}
    root = project_path / "chapters" / chapter_id
    paths = sorted(root.rglob("evaluation.json")) if root.is_dir() else []
    if not paths:
        return {}
    payload = read_json(paths[-1], default={})
    return payload if isinstance(payload, dict) else {}


def _latest_checkpoint(project_path: Path) -> dict[str, object]:
    root = project_path / "book" / "harness" / "checkpoints"
    paths = sorted(root.glob("*.json")) if root.is_dir() else []
    if not paths:
        return {}
    payload = read_json(paths[-1], default={})
    return payload if isinstance(payload, dict) else {}


def _string_value(payload: dict[str, object], key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) else None


def _integer_value(payload: dict[str, object], key: str) -> int | None:
    value = payload.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _creation_projection_contract(
    *,
    project_status: str,
    readiness_action: str | None,
    active_chapter_id: str | None,
) -> tuple[str, str | None, bool]:
    """Cross-layer oracle for control states asserted by Phase 16 recovery tests."""
    if project_status == "paused" and readiness_action == "resume_run":
        return "paused", "resume", False
    if readiness_action == "recover_stale_run":
        return "failed", "recover_stale", project_status == "running"
    if project_status in {"running", "pause_requested"}:
        return (
            "writing_chapter" if active_chapter_id is not None else "continuing",
            None,
            True,
        )
    if project_status == "failed":
        return "failed", None, False
    return "completed", None, False
