import json
import random
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Literal
from uuid import uuid4

from app.schemas.runs import (
    HarnessCheckpoint,
    ProviderWaitState,
    RunControlState,
    RunDispatchState,
)
from app.storage.events import read_events
from app.storage.file_lock import exclusive_file_lock
from app.storage.json_files import read_json, write_json
from app.storage.projects import read_project_metadata


RUN_STATE_RELATIVE = Path("book") / "harness" / "run-state.json"
CHECKPOINT_ROOT = Path("book") / "harness" / "checkpoints"
RUN_STATE_LOCK_RELATIVE = Path("book") / "harness" / ".state.lock"
RUN_DISPATCH_STALE_AFTER_SECONDS = 30


def read_run_control_state(project_path: Path) -> RunControlState:
    return _read_run_control_state_unlocked(project_path)


def _read_run_control_state_unlocked(project_path: Path) -> RunControlState:
    payload = read_json(project_path / RUN_STATE_RELATIVE, default=None)
    if payload is not None:
        return RunControlState.model_validate(payload)
    metadata = read_project_metadata(project_path)
    desired: Literal["stopped", "running"] = (
        "running"
        if metadata.run_status in {"running", "pause_requested", "waiting_for_provider"}
        else "stopped"
    )
    return RunControlState(desired_state=desired)


def write_run_control_state(project_path: Path, state: RunControlState) -> None:
    with exclusive_file_lock(project_path / RUN_STATE_LOCK_RELATIVE):
        _write_run_control_state_unlocked(project_path, state)


def _write_run_control_state_unlocked(
    project_path: Path,
    state: RunControlState,
) -> None:
    state.updated_at = datetime.now(UTC)
    write_json(project_path / RUN_STATE_RELATIVE, state.model_dump(mode="json"))


def set_run_intent(
    project_path: Path,
    *,
    desired_state: Literal["stopped", "running"],
    run_id: str | None = None,
    clear_provider_wait: bool = False,
) -> RunControlState:
    with exclusive_file_lock(project_path / RUN_STATE_LOCK_RELATIVE):
        state = _read_run_control_state_unlocked(project_path)
        state.desired_state = desired_state
        if run_id is not None:
            state.run_id = run_id
        if clear_provider_wait:
            state.provider_wait = None
        if desired_state == "stopped":
            state.dispatch = None
        _write_run_control_state_unlocked(project_path, state)
        return state


def accept_run_dispatch(
    project_path: Path,
    *,
    run_id: str,
    action_key: str,
    dispatch_id: str | None = None,
    now: datetime | None = None,
) -> RunDispatchState:
    """Persist Host ownership intent before an API acknowledges start/resume."""
    accepted_at = now or datetime.now(UTC)
    dispatch = RunDispatchState(
        dispatch_id=dispatch_id or str(uuid4()),
        run_id=run_id,
        action_key=action_key,
        status="accepted",
        accepted_at=accepted_at,
    )
    with exclusive_file_lock(project_path / RUN_STATE_LOCK_RELATIVE):
        state = _read_run_control_state_unlocked(project_path)
        state.schema_version = max(state.schema_version, 2)
        state.desired_state = "running"
        state.run_id = run_id
        state.provider_wait = None
        state.dispatch = dispatch
        _write_run_control_state_unlocked(project_path, state)
    return dispatch


def claim_run_dispatch(
    project_path: Path,
    *,
    run_id: str,
    action_key: str,
    now: datetime | None = None,
) -> tuple[RunDispatchState | None, bool]:
    """Mark the accepted command as claimed by RunHost under its runner lease."""
    claimed_at = now or datetime.now(UTC)
    with exclusive_file_lock(project_path / RUN_STATE_LOCK_RELATIVE):
        state = _read_run_control_state_unlocked(project_path)
        if state.desired_state != "running" or state.run_id not in {None, run_id}:
            return state.dispatch, False
        if state.run_id is None:
            state.run_id = run_id
        previous = state.dispatch
        if (
            previous is not None
            and previous.run_id == run_id
            and previous.status == "claimed"
            and previous.action_key == action_key
        ):
            return previous, False
        dispatch = RunDispatchState(
            dispatch_id=(
                previous.dispatch_id
                if previous is not None and previous.run_id == run_id
                else str(uuid4())
            ),
            run_id=run_id,
            action_key=action_key,
            status="claimed",
            accepted_at=(
                previous.accepted_at
                if previous is not None and previous.run_id == run_id
                else claimed_at
            ),
            claimed_at=claimed_at,
        )
        state.schema_version = max(state.schema_version, 2)
        state.dispatch = dispatch
        _write_run_control_state_unlocked(project_path, state)
        return dispatch, True


def run_dispatch_is_pending(
    state: RunControlState,
    *,
    now: datetime | None = None,
) -> bool:
    dispatch = state.dispatch
    if dispatch is None or dispatch.status != "accepted":
        return False
    accepted_at = dispatch.accepted_at
    if accepted_at.tzinfo is None:
        accepted_at = accepted_at.replace(tzinfo=UTC)
    age = ((now or datetime.now(UTC)) - accepted_at).total_seconds()
    return age < RUN_DISPATCH_STALE_AFTER_SECONDS


def clear_provider_wait(
    project_path: Path,
    *,
    expected_action_key: str | None = None,
) -> RunControlState:
    with exclusive_file_lock(project_path / RUN_STATE_LOCK_RELATIVE):
        state = _read_run_control_state_unlocked(project_path)
        if (
            state.provider_wait is not None
            and (
                expected_action_key is None
                or state.provider_wait.action_key == expected_action_key
            )
        ):
            state.provider_wait = None
            _write_run_control_state_unlocked(project_path, state)
        return state


def begin_harness_checkpoint(
    project_path: Path,
    *,
    run_id: str,
    action_key: str,
) -> tuple[HarnessCheckpoint, Path]:
    with exclusive_file_lock(project_path / RUN_STATE_LOCK_RELATIVE):
        state = _read_run_control_state_unlocked(project_path)
        state.checkpoint_sequence += 1
        _write_run_control_state_unlocked(project_path, state)
    metadata = read_project_metadata(project_path)
    events = read_events(project_path)
    event_sequence = events[-1].seq or 0 if events else 0
    fingerprint = _checkpoint_input_fingerprint(
        project_path,
        run_id=run_id,
        action_key=action_key,
        event_sequence=event_sequence,
    )
    candidate_run_id, candidate_revision = checkpoint_candidate_identity(project_path)
    checkpoint = HarnessCheckpoint(
        sequence=state.checkpoint_sequence,
        run_id=run_id,
        action_key=action_key,
        input_fingerprint=fingerprint,
        candidate_run_id=candidate_run_id,
        candidate_revision=candidate_revision,
        provider_wait_attempt=(
            state.provider_wait.attempt if state.provider_wait is not None else None
        ),
        next_wake_at=(
            state.provider_wait.next_wake_at if state.provider_wait is not None else None
        ),
        status="in_progress",
        project_status_before=metadata.run_status,
        event_sequence_before=event_sequence,
    )
    path = project_path / CHECKPOINT_ROOT / f"{checkpoint.sequence:08d}.json"
    write_json(path, checkpoint.model_dump(mode="json"))
    return checkpoint, path


def finish_harness_checkpoint(
    path: Path,
    checkpoint: HarnessCheckpoint,
    *,
    project_status: str,
    event_sequence: int,
    status: str,
    result_artifacts: list[str] | None = None,
    failure: str | None = None,
    candidate_run_id: str | None = None,
    candidate_revision: int | None = None,
    provider_wait_attempt: int | None = None,
    next_wake_at: datetime | None = None,
) -> None:
    completed = checkpoint.model_copy(
        update={
            "status": status,
            "project_status_after": project_status,
            "event_sequence_after": event_sequence,
            "completed_at": datetime.now(UTC),
            "result_artifacts": result_artifacts or [],
            "failure": failure,
            "candidate_run_id": candidate_run_id or checkpoint.candidate_run_id,
            "candidate_revision": (
                candidate_revision
                if candidate_revision is not None
                else checkpoint.candidate_revision
            ),
            "provider_wait_attempt": provider_wait_attempt,
            "next_wake_at": next_wake_at,
        }
    )
    write_json(path, completed.model_dump(mode="json"))


def action_key_for_project(project_path: Path) -> str:
    metadata = read_project_metadata(project_path)
    phase = "plan-story-arc"
    if (
        project_path / "book" / "harness" / "pending-cross-loop-route.json"
    ).exists():
        phase = "process-cross-loop-route"
    elif metadata.active_chapter_id is not None:
        chapter_path = project_path / "chapters" / metadata.active_chapter_id
        if not (chapter_path / "context_snapshot.json").exists():
            phase = "assemble-chapter-context"
        elif not (chapter_path / "verification.json").exists():
            phase = "run-chapter-agent"
        elif not (chapter_path / "final.md").exists():
            phase = "promote-chapter-final"
        elif (chapter_path / "state_patch_rejection.json").exists():
            phase = "repair-state-patch"
        elif not (chapter_path / "committed_state_patch.json").exists():
            phase = "commit-state-patch"
        else:
            phase = "complete-chapter"
    elif metadata.active_arc_id is not None:
        phase = "advance-story-arc"
    parts = [
        metadata.project_id,
        metadata.active_arc_id or "no-arc",
        metadata.active_chapter_id or "no-chapter",
        phase,
    ]
    return ":".join(parts)


def checkpoint_candidate_identity(project_path: Path) -> tuple[str | None, int | None]:
    """Read the active logical candidate identity without mutating Agent state."""
    metadata = read_project_metadata(project_path)
    state_paths: list[Path] = []
    if metadata.active_chapter_id is not None:
        state_paths.append(
            Path("chapters") / metadata.active_chapter_id / "agent" / "state.json"
        )
    if metadata.active_arc_id is not None:
        state_paths.append(
            Path("arcs") / metadata.active_arc_id / "agent" / "state.json"
        )
    state_paths.append(Path("book") / "agent" / "state.json")
    for relative in state_paths:
        payload = read_json(project_path / relative, default=None)
        if not isinstance(payload, dict):
            continue
        candidate_run_id = payload.get("candidate_run_id")
        if not isinstance(candidate_run_id, str) or not candidate_run_id:
            continue
        activation_id = payload.get("activation_id")
        if isinstance(activation_id, str) and activation_id:
            candidate_root = (
                project_path
                / relative.parent
                / "a"
                / activation_id
                / "c"
            )
            for candidate_path in sorted(candidate_root.glob("*.json")):
                candidate_payload = read_json(candidate_path, default=None)
                if not isinstance(candidate_payload, dict):
                    continue
                candidate_revision = candidate_payload.get("candidate_revision")
                if isinstance(candidate_revision, int) and candidate_revision >= 0:
                    return candidate_run_id, candidate_revision
        expected_revision = payload.get("expected_revision")
        candidate_revision = (
            expected_revision + 1
            if isinstance(expected_revision, int) and expected_revision >= 0
            else None
        )
        return candidate_run_id, candidate_revision
    return None, None


def schedule_provider_wait(
    project_path: Path,
    *,
    action_key: str,
    message: str,
    now: datetime | None = None,
    random_value: float | None = None,
) -> ProviderWaitState:
    with exclusive_file_lock(project_path / RUN_STATE_LOCK_RELATIVE):
        state = _read_run_control_state_unlocked(project_path)
        previous = state.provider_wait
        attempt = (
            previous.attempt + 1
            if previous is not None and previous.action_key == action_key
            else 1
        )
        delays = (10, 20, 40, 80, 160, 300)
        base_delay = delays[min(attempt - 1, len(delays) - 1)]
        sample = random.random() if random_value is None else random_value
        jitter_factor = 0.8 + (max(0.0, min(sample, 1.0)) * 0.4)
        from datetime import timedelta

        wait = ProviderWaitState(
            action_key=action_key,
            failure_category="transport_provider",
            message=message[:4_000] or "Temporary provider failure.",
            attempt=attempt,
            next_wake_at=(now or datetime.now(UTC))
            + timedelta(seconds=base_delay * jitter_factor),
        )
        state.provider_wait = wait
        _write_run_control_state_unlocked(project_path, state)
        return wait


def _checkpoint_input_fingerprint(
    project_path: Path,
    *,
    run_id: str,
    action_key: str,
    event_sequence: int,
) -> str:
    metadata = read_project_metadata(project_path)
    canonical = json.dumps(
        {
            "run_id": run_id,
            "action_key": action_key,
            "event_sequence": event_sequence,
            "project": metadata.model_dump(mode="json"),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(canonical.encode("utf-8")).hexdigest()
