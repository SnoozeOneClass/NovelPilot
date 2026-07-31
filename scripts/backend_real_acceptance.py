from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import os
import sqlite3
import subprocess
import sys
import time
import uuid
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
CASE_ROOT = REPO_ROOT / "scripts" / "backend_acceptance_cases"
DEFAULT_REPORT_ROOT = REPO_ROOT / "data" / "backend-real-acceptance"
DEFAULT_PROFILE_ID = "jemmy-gpt-5.4-mini"
DEFAULT_CASE_IDS = (
    "base-vertical-v1",
    "open-world-evidence-v1",
    "derived-evidence-v1",
    "hierarchical-pressure-v1",
)
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.agents.probe import (  # noqa: E402
    ProfileCapabilityProbeError,
    probe_stored_profile,
)
from app.core.config import LLM_PROFILES_PATH  # noqa: E402
from app.main import create_app  # noqa: E402
from app.profiles import ProfileCatalog, ProfileConfigurationError  # noqa: E402


JsonObject = dict[str, Any]
Terminal = Literal[
    "first_chapter_committed",
    "continued_after_restart",
    "hierarchical_resolution",
]


class AcceptanceConfigurationError(RuntimeError):
    """The local Profile or a versioned real-scenario contract is invalid."""


class AcceptanceApiError(RuntimeError):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.status_code = status_code
        self.code = code
        self.message = message


class AcceptanceInvariantError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class FeedbackStep(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    route_layer: Literal["book", "arc", "chapter"]
    content: str = Field(min_length=1)

    @field_validator("content")
    @classmethod
    def _trim_content(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Scenario feedback must be non-blank.")
        return stripped


class AcceptanceCase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    case_id: str = Field(min_length=1)
    scenario_ids: list[Literal["S1", "S2", "S3", "S4", "S5"]] = Field(
        min_length=1
    )
    operation_mode: Literal["full_auto", "participatory"]
    creator_brief: str = Field(min_length=1)
    terminal: Terminal
    restart_after_first_chapter: bool
    maximum_minutes: int = Field(ge=1, le=180)
    maximum_agent_tasks: int = Field(ge=1, le=200)
    required_task_kinds: list[str] = Field(default_factory=list)
    required_event_types: list[str] = Field(default_factory=list)
    forbidden_event_types: list[str] = Field(default_factory=list)
    feedback_after_first_chapter: FeedbackStep | None = None
    accepted_hierarchical_terminals: list[
        Literal[
            "book_successor_approval_required",
            "creator_decision_required",
            "book_baseline_kept",
        ]
    ] = Field(default_factory=list)
    preserve_prose_when_evidence_only_repair_occurs: bool = False

    @field_validator("case_id", "creator_brief")
    @classmethod
    def _trim_required_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Scenario identity and creator brief must be non-blank.")
        return stripped

    @field_validator(
        "required_task_kinds",
        "required_event_types",
        "forbidden_event_types",
    )
    @classmethod
    def _unique_non_blank_list(cls, value: list[str]) -> list[str]:
        if any(not item.strip() for item in value):
            raise ValueError("Scenario oracle names must be non-blank.")
        if len(value) != len(set(value)):
            raise ValueError("Scenario oracle names must be unique.")
        return value

    @model_validator(mode="after")
    def _scenario_shape(self) -> AcceptanceCase:
        if self.restart_after_first_chapter != (
            self.terminal == "continued_after_restart"
        ):
            raise ValueError(
                "Only the durable-restart scenario may request an application restart."
            )
        if self.terminal == "hierarchical_resolution":
            if (
                self.feedback_after_first_chapter is None
                or not self.accepted_hierarchical_terminals
            ):
                raise ValueError(
                    "Hierarchical pressure requires public feedback and explicit terminals."
                )
        elif (
            self.feedback_after_first_chapter is not None
            or self.accepted_hierarchical_terminals
        ):
            raise ValueError(
                "Only hierarchical pressure may include post-Chapter feedback."
            )
        return self


@dataclass(frozen=True, slots=True)
class Checkpoint:
    state: JsonObject
    evidence: JsonObject
    event_types: tuple[str, ...]


class ProductApi:
    """Synchronous public-API actor backed by a real FastAPI TestClient."""

    def __init__(self, client: TestClient) -> None:
        self._client = client

    def _request(
        self,
        method: str,
        path: str,
        *,
        key: str | None = None,
        body: JsonObject | None = None,
    ) -> JsonObject:
        headers = {} if key is None else {"Idempotency-Key": key}
        response = self._client.request(method, path, headers=headers, json=body)
        if response.is_success:
            try:
                value = response.json()
            except ValueError as exc:
                raise AcceptanceApiError(
                    response.status_code,
                    "api_non_json_success",
                    "The product API returned a non-JSON success response.",
                ) from exc
            if not isinstance(value, dict):
                raise AcceptanceApiError(
                    response.status_code,
                    "api_non_object_success",
                    "The product API returned a non-object success response.",
                )
            return cast(JsonObject, value)
        try:
            envelope = response.json()
            error = envelope.get("error", {}) if isinstance(envelope, dict) else {}
            code = str(error.get("code", "api_request_failed"))
            message = str(error.get("message", f"HTTP {response.status_code}"))
        except (TypeError, ValueError):
            code = "api_request_failed"
            message = f"HTTP {response.status_code}"
        raise AcceptanceApiError(response.status_code, code, message)

    def profiles(self) -> JsonObject:
        return self._request("GET", "/api/profiles")

    def create_project(
        self,
        *,
        project_id: str,
        creator_brief: str,
        operation_mode: str,
        profile_id: str,
        key: str,
    ) -> JsonObject:
        response = self._request(
            "POST",
            "/api/projects",
            key=key,
            body={
                "project_id": project_id,
                "creator_brief": creator_brief,
                "operation_mode": operation_mode,
                "default_profile_id": profile_id,
                "book_profile_id": profile_id,
                "arc_profile_id": profile_id,
                "chapter_profile_id": profile_id,
                "evaluator_profile_id": profile_id,
            },
        )
        return cast(JsonObject, response["state"])

    def state(self, project_id: str) -> JsonObject:
        return self._request("GET", f"/api/projects/{project_id}")

    def diagnostics(self, project_id: str) -> JsonObject:
        return self._request("GET", f"/api/projects/{project_id}/diagnostics")

    def events(self, project_id: str) -> list[JsonObject]:
        cursor = 0
        result: list[JsonObject] = []
        while True:
            page = self._request(
                "GET",
                f"/api/projects/{project_id}/events?after={cursor}&limit=500",
            )
            batch = cast(list[JsonObject], page.get("events", []))
            result.extend(batch)
            next_cursor = int(page.get("next_cursor", cursor))
            if not batch or next_cursor <= cursor:
                return result
            cursor = next_cursor

    def snapshot(self, project_id: str) -> JsonObject:
        return self._request("GET", f"/api/projects/{project_id}/snapshot")

    def start(self, project_id: str, state: JsonObject, *, key: str) -> JsonObject:
        return self._run_control("start", project_id, state, key=key)

    def pause(self, project_id: str, state: JsonObject, *, key: str) -> JsonObject:
        return self._run_control("pause", project_id, state, key=key)

    def resume(self, project_id: str, state: JsonObject, *, key: str) -> JsonObject:
        return self._run_control("resume", project_id, state, key=key)

    def _run_control(
        self,
        action: Literal["start", "pause", "resume"],
        project_id: str,
        state: JsonObject,
        *,
        key: str,
    ) -> JsonObject:
        run = cast(JsonObject, state["run"])
        response = self._request(
            "POST",
            f"/api/projects/{project_id}/run/{action}",
            key=key,
            body={"expected_lock_version": int(run["lock_version"])},
        )
        return cast(JsonObject, response["state"])

    def send_recommended_book_input(
        self,
        project_id: str,
        state: JsonObject,
        *,
        key: str,
    ) -> JsonObject:
        book = cast(JsonObject, state["book"])
        discussion = cast(JsonObject, book.get("discussion", {}))
        suggestions = cast(list[JsonObject], discussion.get("suggestions", []))
        suggestion = next(
            (item for item in suggestions if item.get("recommended") is True),
            suggestions[0] if suggestions else None,
        )
        if suggestion is None:
            raise AcceptanceInvariantError(
                "book_recommendation_missing",
                "Book input became executable without a public suggested answer.",
            )
        response = self._request(
            "POST",
            f"/api/projects/{project_id}/book/input",
            key=key,
            body={
                "expected_workspace_lock_version": int(
                    book["workspace_lock_version"]
                ),
                "message": str(suggestion["message"]),
                "suggestion_id": str(suggestion["id"]),
            },
        )
        return cast(JsonObject, response["state"])

    def approve_book(
        self,
        project_id: str,
        *,
        key: str,
    ) -> JsonObject:
        response = self._request(
            "POST",
            f"/api/projects/{project_id}/book/approve",
            key=key,
        )
        return cast(JsonObject, response["state"])

    def approve_arc(
        self,
        project_id: str,
        *,
        key: str,
    ) -> JsonObject:
        response = self._request(
            "POST",
            f"/api/projects/{project_id}/arc/approve",
            key=key,
            body={},
        )
        return cast(JsonObject, response["state"])

    def submit_feedback(
        self,
        project_id: str,
        step: FeedbackStep,
        *,
        key: str,
    ) -> JsonObject:
        response = self._request(
            "POST",
            f"/api/projects/{project_id}/feedback",
            key=key,
            body=step.model_dump(mode="json"),
        )
        return cast(JsonObject, response["state"])


def load_case(case_id: str) -> tuple[AcceptanceCase, str]:
    path = CASE_ROOT / f"{case_id.replace('-', '_')}.json"
    if not path.is_file():
        raise AcceptanceConfigurationError(f"Unknown backend scenario: {case_id}")
    try:
        raw = path.read_bytes()
        value = AcceptanceCase.model_validate_json(raw)
    except (OSError, ValueError) as exc:
        raise AcceptanceConfigurationError(
            f"Backend scenario {case_id!r} is invalid."
        ) from exc
    if value.case_id != case_id:
        raise AcceptanceConfigurationError(
            f"Backend scenario file identity does not match {case_id!r}."
        )
    return value, hashlib.sha256(raw).hexdigest()


def _command_enabled(state: JsonObject, command_id: str) -> bool:
    return any(
        item.get("command_id") == command_id and item.get("enabled") is True
        for item in cast(list[JsonObject], state.get("commands", []))
    )


def _compact_state(state: JsonObject) -> JsonObject:
    project = cast(JsonObject, state.get("project", {}))
    run = cast(JsonObject, state.get("run", {}))
    book = cast(JsonObject, state.get("book", {}))
    arc = cast(JsonObject, state.get("current_arc") or {})
    chapter = cast(JsonObject, state.get("current_chapter") or {})
    recent_tasks = cast(list[JsonObject], state.get("recent_tasks", []))
    latest = recent_tasks[0] if recent_tasks else {}
    return {
        "project_lifecycle_status": project.get("lifecycle_status"),
        "run_status": run.get("status"),
        "wait_reason_code": run.get("wait_reason_code"),
        "failure_source_kind": run.get("failure_source_kind"),
        "failure_code": run.get("failure_code"),
        "book_baseline_id": book.get("current_baseline_id"),
        "book_baseline_version": book.get("baseline_version"),
        "current_arc_id": arc.get("arc_id"),
        "current_arc_ordinal": arc.get("ordinal"),
        "current_arc_status": arc.get("lifecycle_status"),
        "current_arc_baseline_id": arc.get("current_baseline_id"),
        "current_arc_baseline_version": arc.get("baseline_version"),
        "current_chapter_id": chapter.get("chapter_id"),
        "current_chapter_ordinal": chapter.get("book_ordinal"),
        "current_chapter_status": chapter.get("lifecycle_status"),
        "current_chapter_baseline_id": chapter.get("current_baseline_id"),
        "committed_chapter_count": project.get("committed_chapter_count", 0),
        "creator_input_review_kind": cast(
            JsonObject, state.get("creator_input_request") or {}
        ).get("review_kind"),
        "latest_task_kind": latest.get("task_kind"),
        "latest_task_status": latest.get("task_status"),
        "latest_task_delivery_state": latest.get("delivery_state"),
        "latest_event_sequence": state.get("latest_event_sequence", 0),
    }


def _state_marker(state: JsonObject) -> tuple[object, ...]:
    return tuple(_compact_state(state).values())


def _describe_state(state: JsonObject) -> str:
    compact = _compact_state(state)
    scope = "Book"
    if compact["current_chapter_ordinal"] is not None:
        scope = f"Chapter {compact['current_chapter_ordinal']}"
    elif compact["current_arc_ordinal"] is not None:
        scope = f"Arc {compact['current_arc_ordinal']}"
    task = compact["latest_task_kind"] or "none"
    return (
        f"{scope} | task={task}/{compact['latest_task_status']} | "
        f"delivery={compact['latest_task_delivery_state']} | "
        f"committed={compact['committed_chapter_count']} | "
        f"run={compact['run_status']}"
    )


def _atomic_json(path: Path, value: JsonObject) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _git_commit() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip() if completed.returncode == 0 else "unavailable"


def _working_tree_dirty() -> bool:
    completed = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    return completed.returncode != 0 or bool(completed.stdout.strip())


def _source_tree_sha256() -> str:
    roots = (
        REPO_ROOT / "backend" / "app",
        REPO_ROOT / "backend" / "alembic",
        REPO_ROOT / "scripts",
    )
    files = [
        path
        for root in roots
        for path in root.rglob("*")
        if path.is_file()
        and "__pycache__" not in path.parts
        and not path.name.endswith(".pyc")
    ]
    files.extend(
        path
        for path in (
            REPO_ROOT / "alembic.ini",
            REPO_ROOT / "pyproject.toml",
            REPO_ROOT / "package.json",
        )
        if path.is_file()
    )
    digest = hashlib.sha256()
    for path in sorted(set(files), key=lambda item: item.as_posix()):
        relative = path.relative_to(REPO_ROOT).as_posix().encode("utf-8")
        payload = path.read_bytes()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


def _framework_versions() -> JsonObject:
    versions: JsonObject = {"python": sys.version.split()[0]}
    for name in ("pydantic-ai-slim", "pydantic", "sqlalchemy", "fastapi"):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = "unavailable"
    return versions


def _read_rows(
    connection: sqlite3.Connection,
    query: str,
    parameters: tuple[object, ...],
) -> list[JsonObject]:
    cursor = connection.execute(query, parameters)
    return [dict(row) for row in cursor.fetchall()]


def read_authority_evidence(database_path: Path, project_id: str) -> JsonObject:
    """Read identity/authority metadata only; never read content blobs."""

    connection = sqlite3.connect(
        f"file:{database_path.resolve().as_posix()}?mode=ro",
        uri=True,
        timeout=30,
    )
    connection.row_factory = sqlite3.Row
    try:
        tasks = _read_rows(
            connection,
            """
            SELECT id, task_kind, role, scope_layer, book_id, arc_id, chapter_id,
                   workspace_lock_version, workspace_work_cycle_id,
                   book_baseline_id, arc_baseline_id, chapter_baseline_id,
                   canon_baseline_id, correction_lineage_id,
                   correction_lineage_origin, automatic_correction_round,
                   source_arc_parent_review_id, source_book_parent_review_id,
                   source_arc_closure_review_id, source_book_completion_review_id,
                   source_chapter_candidate_review_id, source_chapter_arc_request_id,
                   source_arc_book_request_id, source_feedback_id,
                   profile_id, profile_fingerprint, status, successful_attempt_id,
                   delivery_state, created_at_ms, updated_at_ms
            FROM agent_tasks
            WHERE project_id = ?
            ORDER BY created_at_ms, id
            """,
            (project_id,),
        )
        attempts = _read_rows(
            connection,
            """
            SELECT a.id, a.task_id, t.task_kind, a.attempt_number, a.retry_kind,
                   a.status, a.provider_request_count, a.transport_retry_count,
                   a.model_request_count, a.input_tokens, a.output_tokens,
                   a.total_tokens, a.error_code, a.error_category, a.http_status,
                   a.created_at_ms, a.started_at_ms, a.finished_at_ms
            FROM agent_task_attempts AS a
            JOIN agent_tasks AS t
              ON t.project_id = a.project_id AND t.id = a.task_id
            WHERE a.project_id = ?
            ORDER BY a.created_at_ms, a.task_id, a.attempt_number
            """,
            (project_id,),
        )
        book_baselines = _read_rows(
            connection,
            """
            SELECT id, book_id, baseline_version, parent_baseline_id,
                   submission_id, review_id, approval_id, created_at_ms
            FROM book_baselines
            WHERE project_id = ?
            ORDER BY book_id, baseline_version
            """,
            (project_id,),
        )
        arc_baselines = _read_rows(
            connection,
            """
            SELECT id, book_id, arc_id, baseline_version, parent_baseline_id,
                   submission_id, review_id, book_baseline_id, canon_baseline_id,
                   revision_origin, authorization_kind, approval_gate_id,
                   approval_id, created_at_ms
            FROM arc_baselines
            WHERE project_id = ?
            ORDER BY arc_id, baseline_version
            """,
            (project_id,),
        )
        chapter_baselines = _read_rows(
            connection,
            """
            SELECT id, book_id, arc_id, chapter_id, baseline_version,
                   parent_baseline_id, submission_id, review_id,
                   book_baseline_id, arc_baseline_id, canon_before_id,
                   canon_after_id, revision_origin,
                   source_arc_parent_review_id, source_arc_closure_review_id,
                   plan_ref_id, prose_ref_id, observations_ref_id,
                   accepted_canon_patch_ref_id, created_at_ms
            FROM chapter_baselines
            WHERE project_id = ?
            ORDER BY chapter_id, baseline_version
            """,
            (project_id,),
        )
        chapter_submissions = _read_rows(
            connection,
            """
            SELECT id, chapter_id, workspace_lock_version, work_cycle_id,
                   base_chapter_baseline_id, book_baseline_id, arc_baseline_id,
                   canon_before_id, plan_ref_id, draft_ref_id,
                   observations_ref_id, candidate_canon_patch_ref_id,
                   disposition, close_reason_code, created_at_ms, closed_at_ms
            FROM chapter_review_submissions
            WHERE project_id = ?
            ORDER BY chapter_id, created_at_ms, id
            """,
            (project_id,),
        )
        chapter_reviews = _read_rows(
            connection,
            """
            SELECT id, chapter_id, submission_id, evaluator_task_id,
                   evaluator_attempt_id, decision, repair_contract_ref_id,
                   created_at_ms
            FROM chapter_reviews
            WHERE project_id = ?
            ORDER BY chapter_id, created_at_ms, id
            """,
            (project_id,),
        )
        feedback = _read_rows(
            connection,
            """
            SELECT id, feedback_kind, status, route_layer, book_id, arc_id,
                   chapter_id, captured_run_id, captured_book_baseline_id,
                   captured_arc_baseline_id, captured_chapter_baseline_id,
                   arc_parent_review_id, book_parent_review_id,
                   arc_closure_review_id, book_completion_review_id,
                   resulting_correction_lineage_id, dismiss_reason_code,
                   applied_command_id, created_at_ms, routed_at_ms, applied_at_ms
            FROM user_feedback
            WHERE project_id = ?
            ORDER BY created_at_ms, id
            """,
            (project_id,),
        )
        chapter_arc_requests = _read_rows(
            connection,
            """
            SELECT id, chapter_id, source_submission_id, source_review_id,
                   target_arc_baseline_id, status, latest_parent_review_id,
                   resolved_by_arc_baseline_id, resolution_code,
                   created_at_ms, closed_at_ms
            FROM chapter_arc_change_requests
            WHERE project_id = ?
            ORDER BY created_at_ms, id
            """,
            (project_id,),
        )
        arc_book_requests = _read_rows(
            connection,
            """
            SELECT id, arc_id, source_candidate_submission_id,
                   source_candidate_review_id, source_arc_parent_review_id,
                   source_arc_closure_review_id, target_book_baseline_id,
                   status, latest_parent_review_id, resolved_by_book_baseline_id,
                   resolution_code, created_at_ms, closed_at_ms
            FROM arc_book_change_requests
            WHERE project_id = ?
            ORDER BY created_at_ms, id
            """,
            (project_id,),
        )
        arc_parent_reviews = _read_rows(
            connection,
            """
            SELECT id, arc_id, request_id, target_arc_baseline_id,
                   source_task_id, source_attempt_id, arc_contract_judgment,
                   parent_review_judgment, disposition, resolution_owner,
                   correction_lineage_id, correction_lineage_origin,
                   automatic_correction_round, review_ordinal,
                   predecessor_review_id, source_feedback_id,
                   source_exhausted_review_id, opened_arc_workspace_id,
                   created_at_ms
            FROM arc_parent_reviews
            WHERE project_id = ?
            ORDER BY created_at_ms, id
            """,
            (project_id,),
        )
        book_parent_reviews = _read_rows(
            connection,
            """
            SELECT id, arc_id, request_id, target_book_baseline_id,
                   source_task_id, source_attempt_id, book_contract_judgment,
                   disposition, resolution_owner, correction_lineage_id,
                   correction_lineage_origin, automatic_correction_round,
                   review_ordinal, predecessor_review_id, source_feedback_id,
                   source_exhausted_review_id, opened_book_workspace_id,
                   created_at_ms
            FROM book_parent_reviews
            WHERE project_id = ?
            ORDER BY created_at_ms, id
            """,
            (project_id,),
        )
        runs = _read_rows(
            connection,
            """
            SELECT id, status, desired_state, lock_version, wait_reason_code,
                   failure_source_kind, blocking_task_id, blocking_action_key,
                   failure_code, started_at_ms, finished_at_ms
            FROM generation_runs
            WHERE project_id = ?
            ORDER BY run_number
            """,
            (project_id,),
        )
    finally:
        connection.close()
    return {
        "tasks": tasks,
        "attempts": attempts,
        "book_baselines": book_baselines,
        "arc_baselines": arc_baselines,
        "chapter_baselines": chapter_baselines,
        "chapter_submissions": chapter_submissions,
        "chapter_reviews": chapter_reviews,
        "feedback": feedback,
        "chapter_arc_change_requests": chapter_arc_requests,
        "arc_book_change_requests": arc_book_requests,
        "arc_parent_reviews": arc_parent_reviews,
        "book_parent_reviews": book_parent_reviews,
        "runs": runs,
    }


def _event_index(events: list[JsonObject]) -> tuple[str, ...]:
    return tuple(str(event.get("event_type")) for event in events)


def _pause_at_checkpoint(
    api: ProductApi,
    *,
    project_id: str,
    state: JsonObject,
    key_prefix: str,
    deadline: float,
    poll_seconds: float,
) -> JsonObject:
    run = cast(JsonObject, state.get("run", {}))
    status = str(run.get("status"))
    if status in {"paused", "completed", "failure_paused"}:
        return state
    if not _command_enabled(state, "pause_run"):
        raise AcceptanceInvariantError(
            "pause_checkpoint_unavailable",
            f"The public pause command was unavailable at checkpoint status {status!r}.",
        )
    state = api.pause(project_id, state, key=f"{key_prefix}:pause")
    while True:
        status = str(cast(JsonObject, state.get("run", {})).get("status"))
        if status in {"paused", "completed", "failure_paused"}:
            return state
        if time.monotonic() >= deadline:
            raise AcceptanceInvariantError(
                "pause_checkpoint_timeout",
                "The Run did not settle its requested safe-boundary pause.",
            )
        time.sleep(poll_seconds)
        state = api.state(project_id)


def _perform_normal_actor_action(
    api: ProductApi,
    *,
    project_id: str,
    state: JsonObject,
    key_prefix: str,
    action_counts: Counter[str],
    stop_before_book_successor_approval: bool,
) -> tuple[JsonObject, str | None]:
    if _command_enabled(state, "send_book_input"):
        book = cast(JsonObject, state.get("book", {}))
        discussion = cast(JsonObject, book.get("discussion", {}))
        turn = int(discussion.get("turn_count", 0))
        action_counts["book_input"] += 1
        return (
            api.send_recommended_book_input(
                project_id,
                state,
                key=f"{key_prefix}:book-input:{turn}",
            ),
            None,
        )
    if _command_enabled(state, "approve_book"):
        book = cast(JsonObject, state.get("book", {}))
        if stop_before_book_successor_approval and book.get("current_baseline_id"):
            return state, "book_successor_approval_required"
        action_counts["book_approval"] += 1
        return (
            api.approve_book(
                project_id,
                key=f"{key_prefix}:book-approve:{action_counts['book_approval']}",
            ),
            None,
        )
    if _command_enabled(state, "approve_arc"):
        arc = cast(JsonObject, state.get("current_arc") or {})
        action_counts["arc_approval"] += 1
        return (
            api.approve_arc(
                project_id,
                key=(
                    f"{key_prefix}:arc-approve:"
                    f"{arc.get('arc_id')}:{action_counts['arc_approval']}"
                ),
            ),
            None,
        )
    return state, None


def _first_chapter_committed(state: JsonObject) -> bool:
    project = cast(JsonObject, state.get("project", {}))
    return int(project.get("committed_chapter_count") or 0) >= 1


def _drive_until(
    api: ProductApi,
    *,
    project_id: str,
    case: AcceptanceCase,
    key_prefix: str,
    goal: Callable[[JsonObject], bool],
    poll_seconds: float,
    announce: Callable[[str], None],
    stop_before_book_successor_approval: bool = False,
    allow_creator_terminal: bool = False,
) -> tuple[JsonObject, str | None, Counter[str]]:
    deadline = time.monotonic() + case.maximum_minutes * 60
    last_marker: tuple[object, ...] | None = None
    last_change = time.monotonic()
    last_heartbeat = 0.0
    action_counts: Counter[str] = Counter()
    state = api.state(project_id)
    while True:
        now = time.monotonic()
        marker = _state_marker(state)
        if marker != last_marker:
            announce(_describe_state(state))
            last_marker = marker
            last_change = now
            last_heartbeat = now
        elif now - last_heartbeat >= 30:
            announce(f"still running | {_describe_state(state)}")
            last_heartbeat = now

        if goal(state):
            return state, None, action_counts

        run = cast(JsonObject, state.get("run", {}))
        status = str(run.get("status"))
        if status == "failure_paused":
            raise AcceptanceInvariantError(
                "run_failure_paused",
                (
                    "The real scenario entered failure_paused: "
                    f"{run.get('failure_code')!r}."
                ),
            )
        if status == "completed":
            raise AcceptanceInvariantError(
                "run_completed_before_checkpoint",
                "The Run completed before the scenario checkpoint was observed.",
            )
        if status in {"paused", "pause_requested"}:
            raise AcceptanceInvariantError(
                "unexpected_pause",
                "The Run paused before the scenario requested a checkpoint pause.",
            )
        creator_request = state.get("creator_input_request")
        if creator_request is not None and allow_creator_terminal:
            return state, "creator_decision_required", action_counts

        previous_state = state
        state, terminal = _perform_normal_actor_action(
            api,
            project_id=project_id,
            state=state,
            key_prefix=key_prefix,
            action_counts=action_counts,
            stop_before_book_successor_approval=(
                stop_before_book_successor_approval
            ),
        )
        if terminal is not None:
            return state, terminal, action_counts
        if state is not previous_state:
            continue

        if status == "waiting_for_user":
            raise AcceptanceInvariantError(
                "unexpected_user_wait",
                (
                    "No approved public actor action exists for wait reason "
                    f"{run.get('wait_reason_code')!r}."
                ),
            )
        diagnostics = api.diagnostics(project_id)
        if int(diagnostics.get("task_count") or 0) > case.maximum_agent_tasks:
            raise AcceptanceInvariantError(
                "agent_task_guard_exceeded",
                (
                    f"Scenario exceeded its {case.maximum_agent_tasks}-task safety "
                    "guard without reaching the semantic checkpoint."
                ),
            )
        if now >= deadline:
            raise AcceptanceInvariantError(
                "scenario_timeout",
                f"Scenario exceeded its {case.maximum_minutes}-minute safety window.",
            )
        if now - last_change >= 15 * 60:
            raise AcceptanceInvariantError(
                "no_authoritative_progress",
                "Authoritative state did not change for fifteen minutes.",
            )
        time.sleep(poll_seconds)
        state = api.state(project_id)


def _lineage_violations(rows: list[JsonObject], owner_key: str) -> list[str]:
    grouped: dict[str, list[JsonObject]] = {}
    for row in rows:
        grouped.setdefault(str(row[owner_key]), []).append(row)
    issues: list[str] = []
    for owner, lineage in grouped.items():
        ordered = sorted(lineage, key=lambda item: int(item["baseline_version"]))
        for index, row in enumerate(ordered, start=1):
            if int(row["baseline_version"]) != index:
                issues.append(f"{owner}: baseline versions are not contiguous")
                break
            expected_parent = None if index == 1 else ordered[index - 2]["id"]
            if row.get("parent_baseline_id") != expected_parent:
                issues.append(f"{owner}: baseline parent does not match v{index - 1}")
                break
    return issues


def _provider_failure(attempts: list[JsonObject]) -> bool:
    external_categories = {
        "authentication",
        "quota",
        "configuration",
        "capability",
        "transport",
        "timeout",
    }
    return any(
        item.get("status") == "failed"
        and str(item.get("error_category")) in external_categories
        for item in attempts
    )


def _assert_common_invariants(
    *,
    case: AcceptanceCase,
    profile_id: str,
    evidence: JsonObject,
    event_types: tuple[str, ...],
    final_run_status: str,
) -> list[JsonObject]:
    checks: list[JsonObject] = []

    def record(check_id: str, ok: bool, detail: str) -> None:
        checks.append({"id": check_id, "ok": ok, "detail": detail})

    tasks = cast(list[JsonObject], evidence["tasks"])
    task_kinds = {str(item["task_kind"]) for item in tasks}
    missing_tasks = sorted(set(case.required_task_kinds) - task_kinds)
    record(
        "required_producer_consumer_tasks",
        not missing_tasks,
        "all required task kinds present"
        if not missing_tasks
        else f"missing task kinds: {', '.join(missing_tasks)}",
    )
    missing_events = sorted(set(case.required_event_types) - set(event_types))
    record(
        "required_formal_events",
        not missing_events,
        "all required formal events present"
        if not missing_events
        else f"missing event types: {', '.join(missing_events)}",
    )
    forbidden = sorted(set(case.forbidden_event_types) & set(event_types))
    record(
        "forbidden_routes_absent",
        not forbidden,
        "no forbidden route was opened"
        if not forbidden
        else f"unexpected event types: {', '.join(forbidden)}",
    )
    wrong_profiles = sorted(
        {
            str(item.get("profile_id"))
            for item in tasks
            if item.get("profile_id") != profile_id
        }
    )
    record(
        "exact_profile_binding",
        not wrong_profiles,
        f"every task used {profile_id}"
        if not wrong_profiles
        else f"unexpected profiles: {', '.join(wrong_profiles)}",
    )
    deliveries = [
        item
        for item in tasks
        if item.get("status") == "succeeded"
        and item.get("delivery_state") not in {"applied", "discarded_stale"}
    ]
    pending_allowed = final_run_status == "paused" and all(
        item.get("delivery_state") == "pending" for item in deliveries
    )
    record(
        "successful_results_consumed",
        not deliveries or pending_allowed,
        (
            "successful results were applied or explicitly stale"
            if not deliveries
            else (
                "only the safe-boundary pause retains a pending completed action"
                if pending_allowed
                else "successful results remain in an illegal delivery state"
            )
        ),
    )
    lineage_issues = [
        *_lineage_violations(
            cast(list[JsonObject], evidence["book_baselines"]), "book_id"
        ),
        *_lineage_violations(
            cast(list[JsonObject], evidence["arc_baselines"]), "arc_id"
        ),
        *_lineage_violations(
            cast(list[JsonObject], evidence["chapter_baselines"]), "chapter_id"
        ),
    ]
    record(
        "immutable_baseline_lineage",
        not lineage_issues,
        "baseline versions and parents are contiguous"
        if not lineage_issues
        else "; ".join(lineage_issues),
    )
    correction_rounds = [
        int(item["automatic_correction_round"])
        for collection in (
            cast(list[JsonObject], evidence["tasks"]),
            cast(list[JsonObject], evidence["arc_parent_reviews"]),
            cast(list[JsonObject], evidence["book_parent_reviews"]),
        )
        for item in collection
        if item.get("automatic_correction_round") is not None
    ]
    record(
        "bounded_correction_round",
        all(round_number in {0, 1} for round_number in correction_rounds),
        "all correction lineage rounds are within 0..1",
    )
    book_ids = {
        str(item["id"])
        for item in cast(list[JsonObject], evidence["book_baselines"])
    }
    arc_ids = {
        str(item["id"])
        for item in cast(list[JsonObject], evidence["arc_baselines"])
    }
    broken_bindings = [
        str(item["id"])
        for item in cast(list[JsonObject], evidence["chapter_baselines"])
        if str(item["book_baseline_id"]) not in book_ids
        or str(item["arc_baseline_id"]) not in arc_ids
    ]
    record(
        "chapter_exact_parent_binding",
        not broken_bindings,
        "every formal Chapter binds concrete Book and Arc baselines"
        if not broken_bindings
        else f"broken Chapter baseline bindings: {', '.join(broken_bindings)}",
    )
    return checks


def _assert_derived_evidence_invariants(
    *,
    case: AcceptanceCase,
    evidence: JsonObject,
) -> tuple[list[JsonObject], str]:
    if not case.preserve_prose_when_evidence_only_repair_occurs:
        return [], "not_applicable"
    tasks = cast(list[JsonObject], evidence["tasks"])
    repair_tasks = [
        item
        for item in tasks
        if item.get("task_kind") == "chapter.repair.observation"
        and item.get("delivery_state") == "applied"
    ]
    if not repair_tasks:
        return (
            [
                {
                    "id": "derived_evidence_authority",
                    "ok": False,
                    "detail": (
                        "the scenario never exercised an applied evidence-only "
                        "Chapter repair"
                    ),
                }
            ],
            "evidence_only_repair_not_exercised",
        )
    reviews = {
        str(item["id"]): item
        for item in cast(list[JsonObject], evidence["chapter_reviews"])
    }
    submissions = {
        str(item["id"]): item
        for item in cast(list[JsonObject], evidence["chapter_submissions"])
    }
    baselines = cast(list[JsonObject], evidence["chapter_baselines"])
    violations: list[str] = []
    for task in repair_tasks:
        review_id = task.get("source_chapter_candidate_review_id")
        review = None if review_id is None else reviews.get(str(review_id))
        submission = (
            None
            if review is None
            else submissions.get(str(review.get("submission_id")))
        )
        matching = [
            baseline
            for baseline in baselines
            if baseline.get("chapter_id") == task.get("chapter_id")
            and baseline.get("prose_ref_id")
            == (None if submission is None else submission.get("draft_ref_id"))
        ]
        if review is None or submission is None or not matching:
            violations.append(str(task["id"]))
    return (
        [
            {
                "id": "derived_evidence_authority",
                "ok": not violations,
                "detail": (
                    "evidence-only repair preserved the frozen prose source"
                    if not violations
                    else (
                        "observation repair lost its exact frozen prose source: "
                        + ", ".join(violations)
                    )
                ),
            }
        ],
        "evidence_only_repair_exercised",
    )


@dataclass(frozen=True, slots=True)
class _HierarchicalChain:
    feedback_ids: frozenset[str]
    chapter_review_ids: frozenset[str]
    chapter_requests: tuple[JsonObject, ...]
    arc_reviews: tuple[JsonObject, ...]
    arc_requests: tuple[JsonObject, ...]
    book_reviews: tuple[JsonObject, ...]


def _hierarchical_chain(evidence: JsonObject) -> _HierarchicalChain:
    tasks = {
        str(item["id"]): item
        for item in cast(list[JsonObject], evidence["tasks"])
    }
    feedback_ids = {
        str(item["id"])
        for item in cast(list[JsonObject], evidence["feedback"])
        if item.get("feedback_kind") == "unsolicited"
        and item.get("route_layer") == "chapter"
        and item.get("status") == "applied"
    }
    chapter_review_ids = {
        str(item["id"])
        for item in cast(list[JsonObject], evidence["chapter_reviews"])
        if (
            task := tasks.get(str(item.get("evaluator_task_id")))
        ) is not None
        and str(task.get("source_feedback_id")) in feedback_ids
    }
    chapter_requests = [
        item
        for item in cast(
            list[JsonObject],
            evidence["chapter_arc_change_requests"],
        )
        if str(item.get("source_review_id")) in chapter_review_ids
    ]
    chapter_request_ids = {str(item["id"]) for item in chapter_requests}
    arc_reviews = [
        item
        for item in cast(list[JsonObject], evidence["arc_parent_reviews"])
        if str(item.get("request_id")) in chapter_request_ids
    ]
    arc_review_ids = {str(item["id"]) for item in arc_reviews}
    arc_requests = [
        item
        for item in cast(
            list[JsonObject],
            evidence["arc_book_change_requests"],
        )
        if str(item.get("source_arc_parent_review_id")) in arc_review_ids
    ]
    arc_request_ids = {str(item["id"]) for item in arc_requests}
    book_reviews = [
        item
        for item in cast(list[JsonObject], evidence["book_parent_reviews"])
        if str(item.get("request_id")) in arc_request_ids
    ]
    return _HierarchicalChain(
        feedback_ids=frozenset(feedback_ids),
        chapter_review_ids=frozenset(chapter_review_ids),
        chapter_requests=tuple(chapter_requests),
        arc_reviews=tuple(arc_reviews),
        arc_requests=tuple(arc_requests),
        book_reviews=tuple(book_reviews),
    )


def _hierarchical_terminal(
    *,
    state: JsonObject,
    evidence: JsonObject,
    initial: Checkpoint,
) -> str | None:
    run = cast(JsonObject, state.get("run", {}))
    if run.get("status") == "failure_paused":
        return None
    if state.get("creator_input_request") is not None:
        return "creator_decision_required"
    if _command_enabled(state, "approve_book"):
        book = cast(JsonObject, state.get("book", {}))
        if book.get("current_baseline_id") == cast(
            JsonObject, initial.state.get("book", {})
        ).get("current_baseline_id"):
            return "book_successor_approval_required"
    chain = _hierarchical_chain(evidence)
    if any(
        item.get("disposition") == "keep_book"
        and int(item.get("automatic_correction_round") or 0) == 1
        and item.get("predecessor_review_id") is not None
        for item in chain.book_reviews
    ):
        return "book_baseline_kept"
    return None


def _hierarchical_chain_violations(evidence: JsonObject) -> list[str]:
    chain = _hierarchical_chain(evidence)
    violations: list[str] = []
    if not chain.feedback_ids:
        violations.append("no applied public Chapter feedback")
    if not chain.chapter_review_ids:
        violations.append("no Chapter review bound to that feedback")
    if not chain.chapter_requests:
        violations.append("no Chapter-to-Arc request bound to that review")
    if not chain.arc_reviews:
        violations.append("no Arc review bound to that Chapter request")
    if not chain.arc_requests:
        violations.append("no Arc-to-Book request bound to that Arc review")
    if not chain.book_reviews:
        violations.append("no Book review bound to that Arc request")
    return violations


def _assert_hierarchical_invariants(
    *,
    initial: Checkpoint,
    evidence: JsonObject,
    terminal: str,
) -> list[JsonObject]:
    initial_book_ids = {
        str(item["id"])
        for item in cast(list[JsonObject], initial.evidence["book_baselines"])
    }
    initial_arc_ids = {
        str(item["id"])
        for item in cast(list[JsonObject], initial.evidence["arc_baselines"])
    }
    initial_chapter_ids = {
        str(item["id"])
        for item in cast(list[JsonObject], initial.evidence["chapter_baselines"])
    }
    final_book_ids = {
        str(item["id"])
        for item in cast(list[JsonObject], evidence["book_baselines"])
    }
    final_arc_ids = {
        str(item["id"])
        for item in cast(list[JsonObject], evidence["arc_baselines"])
    }
    final_chapter_ids = {
        str(item["id"])
        for item in cast(list[JsonObject], evidence["chapter_baselines"])
    }
    preserved = (
        initial_book_ids <= final_book_ids
        and initial_arc_ids <= final_arc_ids
        and initial_chapter_ids <= final_chapter_ids
    )
    chain_violations = _hierarchical_chain_violations(evidence)
    return [
        {
            "id": "formal_history_preserved",
            "ok": preserved,
            "detail": (
                "all pre-pressure formal baselines remain immutable rows"
                if preserved
                else "one or more pre-pressure baseline identities disappeared"
            ),
        },
        {
            "id": "chapter_to_arc_to_book_authority_route",
            "ok": not chain_violations,
            "detail": (
                (
                    "public feedback remained traceable through Chapter review, "
                    "Arc authority, and Book authority"
                )
                if not chain_violations
                else "; ".join(chain_violations)
            ),
        },
        {
            "id": "hierarchical_terminal",
            "ok": terminal
            in {
                "book_successor_approval_required",
                "creator_decision_required",
                "book_baseline_kept",
            },
            "detail": f"terminal={terminal}",
        },
    ]


def _profile_preflight(
    *,
    profile_path: Path,
    profile_id: str,
    run_probe: bool,
) -> JsonObject:
    catalog = ProfileCatalog(profile_path)
    selected_before = catalog.load().selected_profile_id
    try:
        stored = catalog.get_stored(profile_id)
    except ProfileConfigurationError as exc:
        raise AcceptanceConfigurationError(str(exc)) from exc
    if not stored.enabled or not stored.api_key.get_secret_value():
        raise AcceptanceConfigurationError(
            f"Profile {profile_id!r} must be enabled and contain a local API key."
        )
    if run_probe:
        try:
            evidence = asyncio.run(probe_stored_profile(stored))
        except ProfileCapabilityProbeError as exc:
            raise AcceptanceConfigurationError(str(exc)) from exc
        catalog.record_capability_evidence(
            profile_id=profile_id,
            evidence=evidence,
        )
    try:
        resolved = catalog.resolve(profile_id)
    except ProfileConfigurationError as exc:
        raise AcceptanceConfigurationError(
            f"{exc} Run: npm.cmd run profile:probe -- {profile_id}"
        ) from exc
    selected_after = catalog.load().selected_profile_id
    if selected_after != selected_before:
        raise AcceptanceInvariantError(
            "selected_profile_changed",
            "Real acceptance must not change the user's selected long-run Profile.",
        )
    snapshot = resolved.snapshot
    return {
        "profile_id": snapshot.profile_id,
        "api_family": snapshot.api_family,
        "model_id": snapshot.model_id,
        "configuration_fingerprint": stored.configuration_fingerprint,
        "profile_snapshot_fingerprint": snapshot.fingerprint,
        "capability_fingerprint": snapshot.capability_fingerprint,
        "capabilities": snapshot.capabilities.model_dump(mode="json"),
        "probe_executed": run_probe,
    }


def _open_application(
    *,
    database_path: Path,
    profile_path: Path,
    export_root: Path,
) -> TestClient:
    app = create_app(
        database_path=database_path,
        profile_path=profile_path,
        export_root=export_root,
        auto_migrate=True,
        run_engine_enabled=True,
    )
    return TestClient(app, raise_server_exceptions=False)


def _checkpoint(
    *,
    api: ProductApi,
    database_path: Path,
    project_id: str,
) -> Checkpoint:
    state = api.state(project_id)
    return Checkpoint(
        state=state,
        evidence=read_authority_evidence(database_path, project_id),
        event_types=_event_index(api.events(project_id)),
    )


def run_case(
    *,
    case: AcceptanceCase,
    case_sha256: str,
    profile_id: str,
    profile_path: Path,
    run_dir: Path,
    poll_seconds: float,
    announce: Callable[[str], None],
) -> JsonObject:
    case_started = datetime.now(UTC)
    case_slug = case.case_id.replace("-", "_")
    case_dir = run_dir / case_slug
    database_path = case_dir / "novelpilot.sqlite3"
    export_root = case_dir / "exports"
    project_id = f"accept-{case_slug}-{uuid.uuid4().hex[:10]}"
    issues: list[JsonObject] = []
    checks: list[JsonObject] = []
    action_counts: Counter[str] = Counter()
    first_checkpoint: Checkpoint | None = None
    final_checkpoint: Checkpoint | None = None
    terminal_observed: str | None = None
    restart_evidence: JsonObject | None = None

    try:
        with _open_application(
            database_path=database_path,
            profile_path=profile_path,
            export_root=export_root,
        ) as client:
            api = ProductApi(client)
            profile_document = api.profiles()
            profile = next(
                (
                    item
                    for item in cast(
                        list[JsonObject], profile_document.get("profiles", [])
                    )
                    if item.get("id") == profile_id
                ),
                None,
            )
            if (
                profile is None
                or profile.get("capability_status") != "ready"
                or not profile.get("has_api_key")
            ):
                raise AcceptanceInvariantError(
                    "profile_not_ready_in_product",
                    f"Profile {profile_id!r} is not ready through the public API.",
                )
            state = api.create_project(
                project_id=project_id,
                creator_brief=case.creator_brief,
                operation_mode=case.operation_mode,
                profile_id=profile_id,
                key=f"{project_id}:create",
            )
            state = api.start(project_id, state, key=f"{project_id}:start")
            state, _terminal, actions = _drive_until(
                api,
                project_id=project_id,
                case=case,
                key_prefix=project_id,
                goal=_first_chapter_committed,
                poll_seconds=poll_seconds,
                announce=lambda message: announce(f"[{case.case_id}] {message}"),
            )
            action_counts.update(actions)
            state = _pause_at_checkpoint(
                api,
                project_id=project_id,
                state=state,
                key_prefix=f"{project_id}:first-chapter",
                deadline=time.monotonic() + 30 * 60,
                poll_seconds=poll_seconds,
            )
            first_checkpoint = _checkpoint(
                api=api,
                database_path=database_path,
                project_id=project_id,
            )
            if case.terminal == "first_chapter_committed":
                terminal_observed = "first_chapter_committed"
                final_checkpoint = first_checkpoint
            elif case.terminal == "hierarchical_resolution":
                assert case.feedback_after_first_chapter is not None
                state = api.submit_feedback(
                    project_id,
                    case.feedback_after_first_chapter,
                    key=f"{project_id}:hierarchical-feedback",
                )
                action_counts["feedback"] += 1
                state = api.resume(
                    project_id,
                    state,
                    key=f"{project_id}:hierarchical-resume",
                )
                action_counts["resume"] += 1

                def hierarchical_goal(candidate: JsonObject) -> bool:
                    nonlocal terminal_observed
                    current_evidence = read_authority_evidence(
                        database_path, project_id
                    )
                    terminal_observed = _hierarchical_terminal(
                        state=candidate,
                        evidence=current_evidence,
                        initial=first_checkpoint,
                    )
                    return terminal_observed is not None

                state, driver_terminal, actions = _drive_until(
                    api,
                    project_id=project_id,
                    case=case,
                    key_prefix=f"{project_id}:pressure",
                    goal=hierarchical_goal,
                    poll_seconds=poll_seconds,
                    announce=lambda message: announce(
                        f"[{case.case_id}] {message}"
                    ),
                    stop_before_book_successor_approval=True,
                    allow_creator_terminal=True,
                )
                action_counts.update(actions)
                terminal_observed = terminal_observed or driver_terminal
                final_checkpoint = _checkpoint(
                    api=api,
                    database_path=database_path,
                    project_id=project_id,
                )
            else:
                # Durable restart deliberately closes the real FastAPI lifespan here.
                final_checkpoint = first_checkpoint

        if case.terminal == "continued_after_restart":
            assert first_checkpoint is not None
            before = _compact_state(first_checkpoint.state)
            before_task_ids = {
                str(item["id"])
                for item in cast(
                    list[JsonObject],
                    first_checkpoint.evidence["tasks"],
                )
            }
            with _open_application(
                database_path=database_path,
                profile_path=profile_path,
                export_root=export_root,
            ) as restarted_client:
                restarted_api = ProductApi(restarted_client)
                reopened = restarted_api.state(project_id)
                after = _compact_state(reopened)
                pointer_keys = (
                    "book_baseline_id",
                    "current_arc_id",
                    "current_arc_baseline_id",
                    "current_chapter_id",
                    "current_chapter_baseline_id",
                    "committed_chapter_count",
                )
                restart_evidence = {
                    "checkpoint_preserved": all(
                        before[key] == after[key] for key in pointer_keys
                    ),
                    "before": {key: before[key] for key in pointer_keys},
                    "after": {key: after[key] for key in pointer_keys},
                    "event_sequence_before": before["latest_event_sequence"],
                    "event_sequence_after": after["latest_event_sequence"],
                }
                if cast(JsonObject, reopened.get("run", {})).get("status") != "paused":
                    raise AcceptanceInvariantError(
                        "restart_pause_state_lost",
                        "The reopened application did not restore the durable paused Run.",
                    )
                reopened = restarted_api.resume(
                    project_id,
                    reopened,
                    key=f"{project_id}:restart-resume",
                )
                action_counts["resume"] += 1

                def continued_after_restart(candidate: JsonObject) -> bool:
                    current_evidence = read_authority_evidence(
                        database_path,
                        project_id,
                    )
                    return any(
                        str(item["id"]) not in before_task_ids
                        and item.get("status") == "succeeded"
                        and item.get("delivery_state")
                        in {"applied", "discarded_stale"}
                        for item in cast(
                            list[JsonObject],
                            current_evidence["tasks"],
                        )
                    )

                continued, _, actions = _drive_until(
                    restarted_api,
                    project_id=project_id,
                    case=case,
                    key_prefix=f"{project_id}:restart",
                    goal=continued_after_restart,
                    poll_seconds=poll_seconds,
                    announce=lambda message: announce(
                        f"[{case.case_id}] {message}"
                    ),
                )
                action_counts.update(actions)
                continued_status = str(
                    cast(JsonObject, continued.get("run", {})).get("status")
                )
                if continued_status not in {"waiting_for_user", "completed"}:
                    continued = _pause_at_checkpoint(
                        restarted_api,
                        project_id=project_id,
                        state=continued,
                        key_prefix=f"{project_id}:restart-progress",
                        deadline=time.monotonic() + 30 * 60,
                        poll_seconds=poll_seconds,
                    )
                terminal_observed = "continued_after_restart"
                final_checkpoint = _checkpoint(
                    api=restarted_api,
                    database_path=database_path,
                    project_id=project_id,
                )
                snapshot = restarted_api.snapshot(project_id)
                restart_evidence["final_snapshot_event_sequence"] = snapshot.get(
                    "last_domain_event_sequence"
                )
                restart_evidence["continued_after_restart"] = any(
                    str(item["id"]) not in before_task_ids
                    and item.get("status") == "succeeded"
                    and item.get("delivery_state")
                    in {"applied", "discarded_stale"}
                    for item in cast(
                        list[JsonObject],
                        final_checkpoint.evidence["tasks"],
                    )
                )

        assert final_checkpoint is not None
        final_run = cast(JsonObject, final_checkpoint.state.get("run", {}))
        checks.extend(
            _assert_common_invariants(
                case=case,
                profile_id=profile_id,
                evidence=final_checkpoint.evidence,
                event_types=final_checkpoint.event_types,
                final_run_status=str(final_run.get("status")),
            )
        )
        derived_checks, derived_outcome = _assert_derived_evidence_invariants(
            case=case,
            evidence=final_checkpoint.evidence,
        )
        checks.extend(derived_checks)
        if case.terminal == "continued_after_restart":
            assert restart_evidence is not None
            checks.extend(
                [
                    {
                        "id": "restart_checkpoint_preserved",
                        "ok": bool(restart_evidence["checkpoint_preserved"]),
                        "detail": "formal pointers survived lifespan restart",
                    },
                    {
                        "id": "restart_continued",
                        "ok": bool(restart_evidence["continued_after_restart"]),
                        "detail": "the reopened Run continued to formal completion",
                    },
                ]
            )
        if case.terminal == "hierarchical_resolution":
            assert first_checkpoint is not None and terminal_observed is not None
            checks.extend(
                _assert_hierarchical_invariants(
                    initial=first_checkpoint,
                    evidence=final_checkpoint.evidence,
                    terminal=terminal_observed,
                )
            )
            checks.append(
                {
                    "id": "accepted_hierarchical_terminal",
                    "ok": terminal_observed
                    in set(case.accepted_hierarchical_terminals),
                    "detail": f"terminal={terminal_observed}",
                }
            )
        if any(not bool(item["ok"]) for item in checks):
            first = next(item for item in checks if not bool(item["ok"]))
            issues.append(
                {
                    "code": "invariant_failed",
                    "invariant_id": first["id"],
                    "message": first["detail"],
                }
            )
    except AcceptanceInvariantError as exc:
        issues.append(
            {
                "code": exc.code,
                "message": exc.message,
            }
        )
        derived_outcome = "not_reached"
    except AcceptanceApiError as exc:
        issues.append(
            {
                "code": "product_api_error",
                "api_code": exc.code,
                "http_status": exc.status_code,
                "message": exc.message,
            }
        )
        derived_outcome = "not_reached"
    except (KeyError, TypeError, ValueError, sqlite3.Error) as exc:
        issues.append(
            {
                "code": "acceptance_contract_error",
                "message": str(exc),
            }
        )
        derived_outcome = "not_reached"

    if final_checkpoint is None and database_path.is_file():
        try:
            fallback_evidence = read_authority_evidence(database_path, project_id)
        except sqlite3.Error:
            fallback_evidence = {
                "tasks": [],
                "attempts": [],
                "book_baselines": [],
                "arc_baselines": [],
                "chapter_baselines": [],
            }
    else:
        fallback_evidence = (
            {}
            if final_checkpoint is None
            else final_checkpoint.evidence
        )
    attempts = cast(list[JsonObject], fallback_evidence.get("attempts", []))
    failure_class = (
        "external_provider"
        if _provider_failure(attempts)
        else "harness_or_domain"
        if issues
        else None
    )
    finished = datetime.now(UTC)
    return {
        "schema_id": "novelpilot-backend-real-scenario-v1",
        "case_id": case.case_id,
        "scenario_ids": case.scenario_ids,
        "case_sha256": case_sha256,
        "status": "passed" if not issues else "failed",
        "project_id": project_id,
        "started_at": case_started.isoformat(),
        "finished_at": finished.isoformat(),
        "elapsed_seconds": round((finished - case_started).total_seconds(), 3),
        "profile_id": profile_id,
        "terminal_expected": case.terminal,
        "terminal_observed": terminal_observed,
        "action_counts": dict(sorted(action_counts.items())),
        "final_authoritative_state": (
            None
            if final_checkpoint is None
            else _compact_state(final_checkpoint.state)
        ),
        "event_types": (
            []
            if final_checkpoint is None
            else list(final_checkpoint.event_types)
        ),
        "authority_evidence": fallback_evidence,
        "checks": checks,
        "derived_evidence_outcome": derived_outcome,
        "restart_evidence": restart_evidence,
        "issues": issues,
        "failure_class": failure_class,
        "technical_rescue_count": 0,
        "sensitive_content_omitted": True,
        "database_retained": database_path.is_file(),
        "database_relative_path": database_path.relative_to(run_dir).as_posix(),
    }


def run_suite(
    *,
    case_ids: Sequence[str],
    profile_id: str,
    profile_path: Path,
    report_root: Path,
    poll_seconds: float,
    run_probe: bool,
    announce: Callable[[str], None],
) -> tuple[Path, JsonObject]:
    if profile_id != DEFAULT_PROFILE_ID:
        raise AcceptanceConfigurationError(
            f"Engineering real acceptance is fixed to {DEFAULT_PROFILE_ID!r}."
        )
    if poll_seconds <= 0:
        raise AcceptanceConfigurationError("Poll interval must be positive.")
    cases = [load_case(case_id) for case_id in case_ids]
    started = datetime.now(UTC)
    run_id = f"{started.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    run_dir = report_root.resolve() / run_id
    try:
        profile_facts = _profile_preflight(
            profile_path=profile_path,
            profile_id=profile_id,
            run_probe=run_probe,
        )
    except (AcceptanceConfigurationError, AcceptanceInvariantError) as exc:
        preflight_failure: JsonObject = {
            "schema_id": "novelpilot-backend-real-acceptance-aggregate-v1",
            "run_id": run_id,
            "status": "failed",
            "started_at": started.isoformat(),
            "finished_at": datetime.now(UTC).isoformat(),
            "profile": {"profile_id": profile_id, "probe_executed": run_probe},
            "case_count": 0,
            "status_counts": {"passed": 0, "failed": 0},
            "preflight_issue": {
                "code": (
                    exc.code
                    if isinstance(exc, AcceptanceInvariantError)
                    else "profile_preflight_failed"
                ),
                "message": str(exc),
            },
            "technical_rescue_count": 0,
            "long_book_experiment_invoked": False,
            "sensitive_content_omitted": True,
        }
        _atomic_json(run_dir / "aggregate.json", preflight_failure)
        _atomic_json(
            report_root.resolve() / "latest-run.json",
            {
                "schema_id": "novelpilot-backend-real-acceptance-latest-v1",
                "run_id": run_id,
                "status": "failed",
                "run_directory": run_dir.relative_to(
                    report_root.resolve()
                ).as_posix(),
                "aggregate_path": (
                    run_dir.relative_to(report_root.resolve()) / "aggregate.json"
                ).as_posix(),
                "updated_at": datetime.now(UTC).isoformat(),
            },
        )
        raise
    frozen = {
        "run_id": run_id,
        "git_commit": _git_commit(),
        "working_tree_dirty": _working_tree_dirty(),
        "source_tree_sha256": _source_tree_sha256(),
        "framework_versions": _framework_versions(),
        "profile": profile_facts,
        "case_ids": list(case_ids),
        "public_api_only": True,
        "real_provider": True,
        "technical_rescue_allowed": False,
        "long_book_experiment_invoked": False,
    }
    _atomic_json(
        run_dir / "frozen-run.json",
        {
            "schema_id": "novelpilot-backend-real-acceptance-frozen-v1",
            "frozen_at": datetime.now(UTC).isoformat(),
            "frozen": frozen,
        },
    )

    def emit(message: str) -> None:
        _atomic_json(
            run_dir / "progress.json",
            {
                "schema_id": "novelpilot-backend-real-acceptance-progress-v1",
                "run_id": run_id,
                "status": "running",
                "updated_at": datetime.now(UTC).isoformat(),
                "last_message": message,
                "non_authoritative": True,
            },
        )
        announce(message)

    relative_run_dir = run_dir.relative_to(report_root.resolve())
    _atomic_json(
        report_root.resolve() / "latest-run.json",
        {
            "schema_id": "novelpilot-backend-real-acceptance-latest-v1",
            "run_id": run_id,
            "status": "running",
            "run_directory": relative_run_dir.as_posix(),
            "aggregate_path": None,
            "progress_path": (relative_run_dir / "progress.json").as_posix(),
            "updated_at": datetime.now(UTC).isoformat(),
        },
    )
    emit(
        f"Backend real acceptance {run_id} started | profile={profile_id} | "
        f"cases={','.join(case_ids)}"
    )
    reports: list[JsonObject] = []
    for index, (case, case_sha256) in enumerate(cases, start=1):
        emit(f"[{index}/{len(cases)}] {case.case_id} started")
        report = run_case(
            case=case,
            case_sha256=case_sha256,
            profile_id=profile_id,
            profile_path=profile_path,
            run_dir=run_dir,
            poll_seconds=poll_seconds,
            announce=emit,
        )
        reports.append(report)
        _atomic_json(run_dir / f"{case.case_id}.json", report)
        issue_codes = [
            str(item.get("code"))
            for item in cast(list[JsonObject], report.get("issues", []))
        ]
        suffix = "" if not issue_codes else f" | issues={','.join(issue_codes)}"
        emit(
            f"[{index}/{len(cases)}] {case.case_id} {report['status']} | "
            f"elapsed={report['elapsed_seconds']}s{suffix}"
        )
    counts = Counter(str(item["status"]) for item in reports)
    total_attempts = sum(
        len(
            cast(
                list[JsonObject],
                cast(JsonObject, item.get("authority_evidence", {})).get(
                    "attempts", []
                ),
            )
        )
        for item in reports
    )
    total_tokens = sum(
        int(attempt.get("total_tokens") or 0)
        for item in reports
        for attempt in cast(
            list[JsonObject],
            cast(JsonObject, item.get("authority_evidence", {})).get(
                "attempts", []
            ),
        )
    )
    aggregate: JsonObject = {
        "schema_id": "novelpilot-backend-real-acceptance-aggregate-v1",
        "run_id": run_id,
        "status": "passed" if counts["failed"] == 0 else "failed",
        "started_at": started.isoformat(),
        "finished_at": datetime.now(UTC).isoformat(),
        "profile": profile_facts,
        "source_tree_sha256": frozen["source_tree_sha256"],
        "case_count": len(reports),
        "status_counts": {
            "passed": counts["passed"],
            "failed": counts["failed"],
        },
        "total_attempts": total_attempts,
        "total_tokens": total_tokens,
        "cases": [
            {
                "case_id": item["case_id"],
                "scenario_ids": item["scenario_ids"],
                "status": item["status"],
                "project_id": item["project_id"],
                "terminal_observed": item["terminal_observed"],
                "failure_class": item["failure_class"],
                "issue_codes": [
                    issue.get("code")
                    for issue in cast(list[JsonObject], item["issues"])
                ],
            }
            for item in reports
        ],
        "technical_rescue_count": 0,
        "long_book_experiment_invoked": False,
        "sensitive_content_omitted": True,
    }
    _atomic_json(run_dir / "aggregate.json", aggregate)
    _atomic_json(
        report_root.resolve() / "latest-run.json",
        {
            "schema_id": "novelpilot-backend-real-acceptance-latest-v1",
            "run_id": run_id,
            "status": aggregate["status"],
            "run_directory": run_dir.relative_to(report_root.resolve()).as_posix(),
            "aggregate_path": (
                run_dir.relative_to(report_root.resolve()) / "aggregate.json"
            ).as_posix(),
            "progress_path": (
                run_dir.relative_to(report_root.resolve()) / "progress.json"
            ).as_posix(),
            "updated_at": datetime.now(UTC).isoformat(),
        },
    )
    final_message = (
        f"Backend real acceptance {run_id} {aggregate['status']} | "
        f"passed={counts['passed']} | failed={counts['failed']} | "
        f"report={run_dir / 'aggregate.json'}"
    )
    _atomic_json(
        run_dir / "progress.json",
        {
            "schema_id": "novelpilot-backend-real-acceptance-progress-v1",
            "run_id": run_id,
            "status": "finished",
            "updated_at": datetime.now(UTC).isoformat(),
            "last_message": final_message,
            "non_authoritative": True,
        },
    )
    announce(final_message)
    return run_dir, aggregate


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run targeted production-path backend scenarios with the fixed "
            "jemmy-gpt-5.4-mini engineering Profile."
        )
    )
    parser.add_argument(
        "--case",
        action="append",
        dest="case_ids",
        help="Run one named scenario; repeat to select several. Defaults to the full suite.",
    )
    parser.add_argument("--profile-id", default=DEFAULT_PROFILE_ID)
    parser.add_argument("--profile-config", type=Path, default=LLM_PROFILES_PATH)
    parser.add_argument("--report-root", type=Path, default=DEFAULT_REPORT_ROOT)
    parser.add_argument("--poll-seconds", type=float, default=0.5)
    parser.add_argument(
        "--use-current-capability-evidence",
        action="store_true",
        help=(
            "Do not issue the S0 probe calls. Intended only for debugging one scenario; "
            "the standard command always probes the exact Profile first."
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        _, aggregate = run_suite(
            case_ids=arguments.case_ids or DEFAULT_CASE_IDS,
            profile_id=arguments.profile_id,
            profile_path=arguments.profile_config.resolve(),
            report_root=arguments.report_root,
            poll_seconds=arguments.poll_seconds,
            run_probe=not arguments.use_current_capability_evidence,
            announce=lambda message: print(message, flush=True),
        )
    except (AcceptanceConfigurationError, AcceptanceInvariantError) as exc:
        print(f"Backend real acceptance preflight failed: {exc}", file=sys.stderr)
        return 2
    return 0 if aggregate["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
