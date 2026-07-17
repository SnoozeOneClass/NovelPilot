import json
from datetime import UTC, datetime
from pathlib import Path

from app.schemas.arcs import (
    MAX_ARC_CHAPTER_COUNT,
    MIN_ARC_CHAPTER_COUNT,
    CurrentArcState,
)
from app.storage.json_files import read_json, write_json
from app.storage.projects import (
    project_metadata_lock,
    read_project_metadata,
)
from app.storage.transactions import commit_file_transaction


def current_arc_state_path(project_path: Path) -> Path | None:
    metadata = read_project_metadata(project_path)
    if metadata.active_arc_id is None:
        return None
    return project_path / "arcs" / metadata.active_arc_id / "state.json"


def read_current_arc_state(project_path: Path) -> CurrentArcState | None:
    state_path = current_arc_state_path(project_path)
    if state_path is None or not state_path.exists():
        return None
    return _read_arc_state_path(state_path)


def read_arc_state(project_path: Path, arc_id: str) -> CurrentArcState | None:
    state_path = project_path / "arcs" / arc_id / "state.json"
    if not state_path.exists():
        return None
    return _read_arc_state_path(state_path)


def _read_arc_state_path(state_path: Path) -> CurrentArcState | None:
    payload = read_json(state_path)
    if payload is None:
        return None
    return CurrentArcState.model_validate(_normalize_arc_payload(payload))


def approve_current_arc(
    project_path: Path,
    target_chapter_count: int | None = None,
) -> CurrentArcState:
    with project_metadata_lock(project_path):
        state_path = current_arc_state_path(project_path)
        if state_path is None or not state_path.exists():
            raise FileNotFoundError("No current story arc plan exists.")

        payload = read_json(state_path)
        if payload is None:
            raise FileNotFoundError("No current story arc plan exists.")

        payload = _normalize_arc_payload(payload)
        if target_chapter_count is not None:
            if not _is_valid_chapter_count(target_chapter_count):
                raise ValueError(
                    "Story arc target chapter count must be between "
                    f"{MIN_ARC_CHAPTER_COUNT} and {MAX_ARC_CHAPTER_COUNT}."
                )
            payload["target_chapter_count"] = target_chapter_count
        approved_at = datetime.now(UTC).isoformat()
        payload["human_review"] = "approved"
        payload["status"] = "approved"
        payload["approved_at"] = approved_at

        metadata = read_project_metadata(project_path)
        if metadata.run_status == "waiting_for_user":
            metadata.run_status = "idle"
        metadata.updated_at = datetime.now(UTC)
        commit_file_transaction(
            project_path,
            kind=f"approve-story-arc-{payload['arc_id']}",
            files={
                state_path.relative_to(project_path).as_posix(): _json_document(payload),
                "project.json": _json_document(metadata.model_dump(mode="json")),
            },
        )

        return CurrentArcState.model_validate(payload)


def record_chapter_committed(project_path: Path, chapter_id: str) -> CurrentArcState | None:
    state_path = current_arc_state_path(project_path)
    if state_path is None or not state_path.exists():
        return None

    payload = read_json(state_path)
    if payload is None:
        return None

    payload = _normalize_arc_payload(payload)
    raw_completed = payload.get("completed_chapter_ids")
    completed_chapter_ids = [
        item for item in raw_completed if isinstance(item, str)
    ] if isinstance(raw_completed, list) else []
    if chapter_id not in completed_chapter_ids:
        completed_chapter_ids.append(chapter_id)
    payload["completed_chapter_ids"] = completed_chapter_ids

    raw_target_chapter_count = payload.get("target_chapter_count")
    target_chapter_count = (
        raw_target_chapter_count if isinstance(raw_target_chapter_count, int) else 3
    )
    if len(completed_chapter_ids) >= target_chapter_count:
        payload["status"] = "completed"
        payload["completed_at"] = datetime.now(UTC).isoformat()
    else:
        payload["status"] = "in_progress"

    write_json(state_path, payload)
    return CurrentArcState.model_validate(payload)


def _normalize_arc_payload(payload: dict[str, object]) -> dict[str, object]:
    normalized = dict(payload)
    arc_id = normalized.get("arc_id")
    if not isinstance(arc_id, str) or not arc_id:
        raise ValueError("Arc state is missing arc_id.")
    normalized.setdefault("status", "planned")
    normalized.setdefault("plan_path", f"arcs/{arc_id}/plan.md")
    normalized.setdefault("human_review", "not_required")
    normalized.setdefault("approved_at", None)
    target_chapter_count = normalized.get("target_chapter_count")
    if not _is_valid_chapter_count(target_chapter_count):
        normalized["target_chapter_count"] = 3
    recommended_target_chapter_count = normalized.get("recommended_target_chapter_count")
    if not _is_valid_chapter_count(recommended_target_chapter_count):
        normalized["recommended_target_chapter_count"] = normalized[
            "target_chapter_count"
        ]
    completed_chapter_ids = normalized.get("completed_chapter_ids")
    if not isinstance(completed_chapter_ids, list):
        normalized["completed_chapter_ids"] = []
    else:
        normalized["completed_chapter_ids"] = [
            item for item in completed_chapter_ids if isinstance(item, str)
        ]
    normalized.setdefault("completed_at", None)
    return normalized


def _is_valid_chapter_count(value: object) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and MIN_ARC_CHAPTER_COUNT <= value <= MAX_ARC_CHAPTER_COUNT
    )


def _json_document(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"
