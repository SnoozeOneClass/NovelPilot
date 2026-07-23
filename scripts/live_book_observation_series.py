from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import subprocess
import sys
import time
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol, Sequence, cast

import httpx


ROOT_DIR = Path(__file__).resolve().parents[1]
CASE_DIR = ROOT_DIR / "scripts" / "live_acceptance_cases"
DEFAULT_REPORT_ROOT = ROOT_DIR / "data" / "live-observations"
EXPECTED_SCHEDULE = ("full_auto", "participatory", "full_auto", "participatory")

JsonObject = dict[str, Any]
Mode = Literal["full_auto", "participatory"]


class ObservationConfigurationError(RuntimeError):
    """The versioned observation contract or local preflight is invalid."""


class ObservationApiError(RuntimeError):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.status_code = status_code
        self.code = code
        self.message = message


class ObservationApiUnavailable(RuntimeError):
    """The local product API cannot be reached, so later slots cannot run safely."""


@dataclass(frozen=True, slots=True)
class ObservationCase:
    case_id: str
    prompt_path: Path
    prompt_sha256: str
    prompt: str
    schedule: tuple[Mode, ...]
    minimum_chapters: int
    maximum_chapters: int
    maximum_slot_hours: int
    actor_policy: JsonObject
    harness_contract: JsonObject
    raw: JsonObject


class ObservationApi(Protocol):
    def profiles(self) -> JsonObject: ...

    def create_project(
        self, *, project_id: str, prompt: str, mode: Mode, profile_id: str, key: str
    ) -> JsonObject: ...

    def start_run(self, *, project_id: str, lock_version: int, key: str) -> JsonObject: ...

    def get_state(self, project_id: str) -> JsonObject: ...

    def send_book_input(
        self,
        *,
        project_id: str,
        workspace_lock_version: int,
        message: str,
        suggestion_id: str,
        key: str,
    ) -> JsonObject: ...

    def approve_book(self, *, project_id: str, key: str) -> JsonObject: ...

    def approve_arc(
        self,
        *,
        project_id: str,
        target_chapter_count: int | None,
        key: str,
    ) -> JsonObject: ...

    def diagnostics(self, project_id: str) -> JsonObject: ...

    def events(self, project_id: str) -> list[JsonObject]: ...

    def snapshot(self, project_id: str) -> JsonObject: ...

    def export(self, project_id: str) -> JsonObject: ...


class HttpObservationApi:
    def __init__(self, base_url: str) -> None:
        self._client = httpx.Client(base_url=base_url.rstrip("/"), timeout=30.0)

    def close(self) -> None:
        self._client.close()

    def _request(
        self,
        method: str,
        path: str,
        *,
        key: str | None = None,
        body: JsonObject | None = None,
    ) -> JsonObject:
        headers = {} if key is None else {"Idempotency-Key": key}
        try:
            response = self._client.request(method, path, headers=headers, json=body)
        except httpx.TransportError as exc:
            raise ObservationApiUnavailable("The local NovelPilot API is unavailable.") from exc
        if response.is_success:
            value = response.json()
            if not isinstance(value, dict):
                raise ObservationApiUnavailable("The local API returned a non-object response.")
            return cast(JsonObject, value)
        try:
            envelope = response.json()
            error = envelope.get("error", {}) if isinstance(envelope, dict) else {}
            code = str(error.get("code", "api_request_failed"))
            message = str(error.get("message", f"HTTP {response.status_code}"))
        except (ValueError, TypeError):
            code = "api_request_failed"
            message = f"HTTP {response.status_code}"
        raise ObservationApiError(response.status_code, code, message)

    def profiles(self) -> JsonObject:
        return self._request("GET", "/api/profiles")

    def create_project(
        self, *, project_id: str, prompt: str, mode: Mode, profile_id: str, key: str
    ) -> JsonObject:
        result = self._request(
            "POST",
            "/api/projects",
            key=key,
            body={
                "project_id": project_id,
                "creator_brief": prompt,
                "operation_mode": mode,
                "default_profile_id": profile_id,
            },
        )
        return cast(JsonObject, result["state"])

    def start_run(self, *, project_id: str, lock_version: int, key: str) -> JsonObject:
        result = self._request(
            "POST",
            f"/api/projects/{project_id}/run/start",
            key=key,
            body={"expected_lock_version": lock_version},
        )
        return cast(JsonObject, result["state"])

    def get_state(self, project_id: str) -> JsonObject:
        return self._request("GET", f"/api/projects/{project_id}")

    def send_book_input(
        self,
        *,
        project_id: str,
        workspace_lock_version: int,
        message: str,
        suggestion_id: str,
        key: str,
    ) -> JsonObject:
        result = self._request(
            "POST",
            f"/api/projects/{project_id}/book/input",
            key=key,
            body={
                "expected_workspace_lock_version": workspace_lock_version,
                "message": message,
                "suggestion_id": suggestion_id,
            },
        )
        return cast(JsonObject, result["state"])

    def approve_book(self, *, project_id: str, key: str) -> JsonObject:
        result = self._request(
            "POST", f"/api/projects/{project_id}/book/approve", key=key
        )
        return cast(JsonObject, result["state"])

    def approve_arc(
        self,
        *,
        project_id: str,
        target_chapter_count: int | None,
        key: str,
    ) -> JsonObject:
        result = self._request(
            "POST",
            f"/api/projects/{project_id}/arc/approve",
            key=key,
            body={"target_chapter_count": target_chapter_count},
        )
        return cast(JsonObject, result["state"])

    def diagnostics(self, project_id: str) -> JsonObject:
        return self._request("GET", f"/api/projects/{project_id}/diagnostics")

    def events(self, project_id: str) -> list[JsonObject]:
        cursor = 0
        events: list[JsonObject] = []
        while True:
            page = self._request(
                "GET", f"/api/projects/{project_id}/events?after={cursor}&limit=500"
            )
            batch = cast(list[JsonObject], page.get("events", []))
            events.extend(batch)
            next_cursor = int(page.get("next_cursor", cursor))
            if not batch or next_cursor <= cursor:
                return events
            cursor = next_cursor

    def snapshot(self, project_id: str) -> JsonObject:
        return self._request("GET", f"/api/projects/{project_id}/snapshot")

    def export(self, project_id: str) -> JsonObject:
        return self._request("POST", f"/api/projects/{project_id}/export")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_case(case_id: str) -> ObservationCase:
    manifest_path = CASE_DIR / f"{case_id.replace('-', '_')}.json"
    if not manifest_path.is_file():
        raise ObservationConfigurationError(f"Unknown observation case: {case_id}")
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise ObservationConfigurationError("Observation case schema version must be 1.")
    if raw.get("case_id") != case_id:
        raise ObservationConfigurationError("Observation case identity does not match its file.")
    prompt_path = (ROOT_DIR / str(raw.get("prompt_path", ""))).resolve()
    if not prompt_path.is_relative_to(ROOT_DIR) or not prompt_path.is_file():
        raise ObservationConfigurationError("Observation prompt path is invalid.")
    prompt_sha256 = str(raw.get("prompt_sha256", ""))
    if _sha256_file(prompt_path) != prompt_sha256:
        raise ObservationConfigurationError("Observation prompt hash does not match the manifest.")
    schedule = tuple(raw.get("mode_schedule", ()))
    if schedule != EXPECTED_SCHEDULE:
        raise ObservationConfigurationError(
            "The observation schedule must remain full_auto, participatory, full_auto, participatory."
        )
    chapter_range = cast(JsonObject, raw.get("expected_chapter_range", {}))
    minimum = int(chapter_range.get("minimum", 0))
    maximum = int(chapter_range.get("maximum", 0))
    if (minimum, maximum) != (18, 22):
        raise ObservationConfigurationError("The frozen whole-book range must be 18-22 Chapters.")
    actor_policy = cast(JsonObject, raw.get("actor_policy", {}))
    harness_contract = cast(JsonObject, raw.get("harness_contract", {}))
    if actor_policy.get("id") != "recommended-first-public-api-v1":
        raise ObservationConfigurationError("The actor policy is not the approved v1 policy.")
    return ObservationCase(
        case_id=case_id,
        prompt_path=prompt_path,
        prompt_sha256=prompt_sha256,
        prompt=prompt_path.read_text(encoding="utf-8"),
        schedule=cast(tuple[Mode, ...], schedule),
        minimum_chapters=minimum,
        maximum_chapters=maximum,
        maximum_slot_hours=int(raw.get("maximum_slot_hours", 12)),
        actor_policy=actor_policy,
        harness_contract=harness_contract,
        raw=cast(JsonObject, raw),
    )


def _git_commit() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT_DIR,
        check=False,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip() if completed.returncode == 0 else "unavailable"


def _working_tree_dirty() -> bool:
    completed = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=ROOT_DIR,
        check=False,
        capture_output=True,
        text=True,
    )
    return completed.returncode != 0 or bool(completed.stdout.strip())


def _source_tree_sha256() -> str:
    roots = (
        ROOT_DIR / "backend" / "app",
        ROOT_DIR / "backend" / "alembic",
        ROOT_DIR / "frontend" / "src",
        ROOT_DIR / "scripts",
    )
    files = [
        path
        for root in roots
        for path in root.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and not path.name.endswith(".pyc")
    ]
    files.extend(
        path
        for path in (
            ROOT_DIR / "alembic.ini",
            ROOT_DIR / "pyproject.toml",
            ROOT_DIR / "package.json",
            ROOT_DIR / "frontend" / "package.json",
            ROOT_DIR / "frontend" / "package-lock.json",
        )
        if path.is_file()
    )
    digest = hashlib.sha256()
    for path in sorted(set(files), key=lambda item: item.as_posix()):
        relative = path.relative_to(ROOT_DIR).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        payload = path.read_bytes()
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


def _framework_versions() -> JsonObject:
    names = ("pydantic-ai-slim", "pydantic", "sqlalchemy", "fastapi")
    versions: JsonObject = {"python": sys.version.split()[0]}
    for name in names:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = "unavailable"
    return versions


def _profile_facts(profile: JsonObject) -> JsonObject:
    return {
        "profile_id": profile.get("id"),
        "display_name": profile.get("display_name"),
        "api_family": profile.get("api_family"),
        "model_id": profile.get("model_id"),
        "request_options": profile.get("request_options", {}),
        "configuration_fingerprint": profile.get("configuration_fingerprint"),
        "capability_fingerprint": profile.get("capability_fingerprint"),
        "capabilities": profile.get("capabilities"),
    }


def _preflight_profile(api: ObservationApi, profile_id: str) -> JsonObject:
    document = api.profiles()
    profiles = cast(list[JsonObject], document.get("profiles", []))
    profile = next((item for item in profiles if item.get("id") == profile_id), None)
    if profile is None:
        raise ObservationConfigurationError(f"Profile {profile_id!r} does not exist.")
    capabilities = cast(JsonObject, profile.get("capabilities") or {})
    if not profile.get("enabled") or profile.get("capability_status") != "ready":
        raise ObservationConfigurationError(f"Profile {profile_id!r} is not capability-ready.")
    if not capabilities.get("text_streaming") or not capabilities.get("native_json_schema"):
        raise ObservationConfigurationError(
            f"Profile {profile_id!r} lacks text_streaming or native_json_schema."
        )
    if not profile.get("has_api_key"):
        raise ObservationConfigurationError(f"Profile {profile_id!r} has no API key.")
    return profile


def _command_enabled(state: JsonObject, command_id: str) -> bool:
    commands = cast(list[JsonObject], state.get("commands", []))
    return any(
        item.get("command_id") == command_id and item.get("enabled") is True
        for item in commands
    )


def _compact_state(state: JsonObject) -> JsonObject:
    project = cast(JsonObject, state.get("project", {}))
    run = cast(JsonObject, state.get("run", {}))
    book = cast(JsonObject, state.get("book", {}))
    arc = cast(JsonObject, state.get("current_arc") or {})
    chapter = cast(JsonObject, state.get("current_chapter") or {})
    return {
        "project_lifecycle_status": project.get("lifecycle_status"),
        "run_status": run.get("status"),
        "wait_reason_code": run.get("wait_reason_code"),
        "failure_code": run.get("failure_code"),
        "book_lifecycle_status": book.get("lifecycle_status"),
        "book_baseline_id": book.get("current_baseline_id"),
        "committed_chapter_count": project.get("committed_chapter_count", 0),
        "current_arc_id": arc.get("arc_id"),
        "current_arc_ordinal": arc.get("ordinal"),
        "current_arc_status": arc.get("lifecycle_status"),
        "current_chapter_id": chapter.get("chapter_id"),
        "current_chapter_ordinal": chapter.get("book_ordinal"),
        "latest_event_sequence": state.get("latest_event_sequence", 0),
    }


def _attempt_metrics(diagnostics: JsonObject) -> JsonObject:
    attempts = cast(list[JsonObject], diagnostics.get("attempts", []))
    task_kinds = Counter(str(item.get("task_kind")) for item in attempts)
    statuses = Counter(str(item.get("attempt_status")) for item in attempts)
    retry_kinds = Counter(str(item.get("retry_kind")) for item in attempts)
    return {
        "task_count": diagnostics.get("task_count", 0),
        "attempt_count": diagnostics.get("attempt_count", 0),
        "arc_count": diagnostics.get("arc_count", 0),
        "task_kinds": dict(sorted(task_kinds.items())),
        "attempt_statuses": dict(sorted(statuses.items())),
        "retry_kinds": dict(sorted(retry_kinds.items())),
        "provider_request_count": sum(int(item.get("provider_request_count") or 0) for item in attempts),
        "transport_retry_count": sum(int(item.get("transport_retry_count") or 0) for item in attempts),
        "model_request_count": sum(int(item.get("model_request_count") or 0) for item in attempts),
        "input_tokens": sum(int(item.get("input_tokens") or 0) for item in attempts),
        "output_tokens": sum(int(item.get("output_tokens") or 0) for item in attempts),
        "total_tokens": sum(int(item.get("total_tokens") or 0) for item in attempts),
        "typed_output_extra_requests": sum(
            max(0, int(item.get("model_request_count") or 0) - 1) for item in attempts
        ),
        "semantic_repair_task_count": sum(
            1
            for item in attempts
            if ".repair" in str(item.get("task_kind"))
            or ".revise" in str(item.get("task_kind"))
        ),
        "failed_attempts": [
            {
                "task_id": item.get("task_id"),
                "task_kind": item.get("task_kind"),
                "attempt_id": item.get("attempt_id"),
                "attempt_number": item.get("attempt_number"),
                "error_code": item.get("error_code"),
                "error_category": item.get("error_category"),
                "http_status": item.get("http_status"),
                "error_ref_id": item.get("error_ref_id"),
                "diagnostic_ref_id": item.get("diagnostic_ref_id"),
            }
            for item in attempts
            if item.get("attempt_status") == "failed"
        ],
    }


def _provider_is_hard_unavailable(diagnostics: JsonObject) -> bool:
    attempts = cast(list[JsonObject], diagnostics.get("attempts", []))
    unavailable_categories = {"authentication", "quota", "configuration", "capability"}
    return any(
        item.get("attempt_status") == "failed"
        and item.get("error_category") in unavailable_categories
        for item in attempts
    )


def _gate_events(events: list[JsonObject]) -> list[JsonObject]:
    markers = ("approval", "baseline_committed", "completed", "user_input")
    return [
        {
            "sequence": item.get("sequence"),
            "event_id": item.get("event_id"),
            "event_type": item.get("event_type"),
            "aggregate_type": item.get("aggregate_type"),
            "aggregate_id": item.get("aggregate_id"),
            "occurred_at_ms": item.get("occurred_at_ms"),
        }
        for item in events
        if any(marker in str(item.get("event_type")) for marker in markers)
    ]


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


def _issue(code: str, message: str) -> JsonObject:
    return {"code": code, "message": message}


def run_observation_slot(
    *,
    api: ObservationApi,
    case: ObservationCase,
    profile_id: str,
    frozen: JsonObject,
    series_id: str,
    slot: int,
    mode: Mode,
    sleep_seconds: float,
    sleep: Any = time.sleep,
    monotonic: Any = time.monotonic,
) -> tuple[JsonObject, bool]:
    started_wall = datetime.now(UTC)
    started_monotonic = monotonic()
    project_id = f"obs-{series_id}-{slot:02d}-{mode.replace('_', '-')}"
    issues: list[JsonObject] = []
    gates: list[JsonObject] = []
    transitions: list[JsonObject] = []
    action_counts: Counter[str] = Counter()
    final_state: JsonObject = {}
    diagnostics: JsonObject = {"attempts": []}
    events: list[JsonObject] = []
    snapshot: JsonObject | None = None
    export_result: JsonObject | None = None
    stop_series = False

    try:
        state = api.create_project(
            project_id=project_id,
            prompt=case.prompt,
            mode=mode,
            profile_id=profile_id,
            key=f"{series_id}:{slot}:create",
        )
        action_counts["create_project"] += 1
        run = cast(JsonObject, state["run"])
        state = api.start_run(
            project_id=project_id,
            lock_version=int(run["lock_version"]),
            key=f"{series_id}:{slot}:start",
        )
        action_counts["start_run"] += 1
        deadline = started_monotonic + case.maximum_slot_hours * 60 * 60
        last_marker: tuple[object, ...] | None = None
        while True:
            compact = _compact_state(state)
            marker = tuple(compact.values())
            if marker != last_marker:
                transitions.append(
                    {
                        "observed_at": datetime.now(UTC).isoformat(),
                        **compact,
                    }
                )
                last_marker = marker
            run = cast(JsonObject, state.get("run", {}))
            status = str(run.get("status"))
            if status == "completed":
                break
            if status == "failure_paused":
                issues.append(
                    _issue(
                        "run_failure_paused",
                        f"Run stopped with failure code {run.get('failure_code')!r}.",
                    )
                )
                break
            if status in {"paused", "pause_requested"}:
                issues.append(
                    _issue(
                        "unexpected_pause",
                        "Run entered a pause state; the observation actor did not resume it.",
                    )
                )
                break
            if monotonic() >= deadline:
                issues.append(
                    _issue(
                        "slot_timeout",
                        f"Slot exceeded the frozen {case.maximum_slot_hours}-hour observation window.",
                    )
                )
                break

            if _command_enabled(state, "send_book_input"):
                book = cast(JsonObject, state.get("book", {}))
                discussion = cast(JsonObject, book.get("discussion", {}))
                suggestions = cast(list[JsonObject], discussion.get("suggestions", []))
                suggestion = next(
                    (item for item in suggestions if item.get("recommended") is True),
                    suggestions[0] if suggestions else None,
                )
                if suggestion is None:
                    issues.append(
                        _issue(
                            "book_actor_input_missing",
                            "Book input was enabled without an offered product suggestion.",
                        )
                    )
                    break
                turn = int(discussion.get("turn_count", 0))
                state = api.send_book_input(
                    project_id=project_id,
                    workspace_lock_version=int(book["workspace_lock_version"]),
                    message=str(suggestion["message"]),
                    suggestion_id=str(suggestion["id"]),
                    key=f"{series_id}:{slot}:book-input:{turn}",
                )
                action_counts["book_input"] += 1
                gates.append(
                    {
                        "kind": "book_input",
                        "turn": turn,
                        "suggestion_id": suggestion.get("id"),
                        "recommended": suggestion.get("recommended") is True,
                    }
                )
                continue

            if _command_enabled(state, "approve_book"):
                state = api.approve_book(
                    project_id=project_id,
                    key=f"{series_id}:{slot}:book-approve",
                )
                action_counts["book_approval"] += 1
                gates.append({"kind": "book_approval"})
                continue

            if _command_enabled(state, "approve_arc"):
                if mode != "participatory":
                    issues.append(
                        _issue(
                            "full_auto_arc_gate",
                            "A persistent Arc approval gate appeared in full-auto mode.",
                        )
                    )
                    break
                arc = cast(JsonObject, state.get("current_arc") or {})
                target = arc.get("recommended_target_chapter_count")
                state = api.approve_arc(
                    project_id=project_id,
                    target_chapter_count=None if target is None else int(target),
                    key=f"{series_id}:{slot}:arc-approve:{arc.get('arc_id')}",
                )
                action_counts["arc_approval"] += 1
                gates.append(
                    {
                        "kind": "arc_approval",
                        "arc_id": arc.get("arc_id"),
                        "arc_ordinal": arc.get("ordinal"),
                        "target_chapter_count": target,
                    }
                )
                continue

            if status == "waiting_for_user":
                issues.append(
                    _issue(
                        "unexpected_product_gate",
                        f"No approved actor action exists for wait reason {run.get('wait_reason_code')!r}.",
                    )
                )
                break
            sleep(sleep_seconds)
            state = api.get_state(project_id)

        final_state = _compact_state(state)
        diagnostics = api.diagnostics(project_id)
        events = api.events(project_id)
        if str(cast(JsonObject, state.get("run", {})).get("status")) == "completed":
            chapter_count = int(
                cast(JsonObject, state.get("project", {})).get("committed_chapter_count", 0)
            )
            if not case.minimum_chapters <= chapter_count <= case.maximum_chapters:
                issues.append(
                    _issue(
                        "chapter_count_out_of_range",
                        f"Completed Book has {chapter_count} Chapters; expected 18-22.",
                    )
                )
            if diagnostics.get("completion_id") is None:
                issues.append(
                    _issue("completion_identity_missing", "Completed Run has no completion identity.")
                )
            try:
                snapshot = api.snapshot(project_id)
                export_result = api.export(project_id)
                action_counts["export_markdown"] += 1
            except ObservationApiError as exc:
                issues.append(_issue("export_failed", f"{exc.code}: {exc.message}"))
        stop_series = _provider_is_hard_unavailable(diagnostics)
    except ObservationApiUnavailable as exc:
        issues.append(_issue("local_api_unavailable", str(exc)))
        stop_series = True
    except ObservationApiError as exc:
        issues.append(_issue("product_api_error", f"{exc.code}: {exc.message}"))
    except (KeyError, TypeError, ValueError) as exc:
        issues.append(_issue("observation_contract_error", str(exc)))

    finished_wall = datetime.now(UTC)
    attempt_metrics = _attempt_metrics(diagnostics)
    status: Literal["completed", "failed"] = "completed" if not issues else "failed"
    report: JsonObject = {
        "schema_id": "novelpilot-live-book-observation-v1",
        "series_id": series_id,
        "slot": slot,
        "status": status,
        "project_id": project_id,
        "mode": mode,
        "started_at": started_wall.isoformat(),
        "finished_at": finished_wall.isoformat(),
        "elapsed_seconds": round(monotonic() - started_monotonic, 3),
        "frozen": frozen,
        "final_authoritative_state": final_state,
        "action_counts": dict(sorted(action_counts.items())),
        "gate_log": gates,
        "state_transitions": transitions,
        "event_count": len(events),
        "gate_events": _gate_events(events),
        "attempt_metrics": attempt_metrics,
        "attempts": diagnostics.get("attempts", []),
        "completion": {
            "completion_id": diagnostics.get("completion_id"),
            "completion_version": diagnostics.get("completion_version"),
        },
        "snapshot": snapshot,
        "export": export_result,
        "issues": issues,
        "technical_rescue_performed": False,
        "technical_rescue_count": 0,
        "actor_policy_id": case.actor_policy.get("id"),
    }
    return report, stop_series


def run_series(
    *,
    api: ObservationApi,
    case: ObservationCase,
    profile_id: str,
    runs: int,
    report_root: Path,
    sleep_seconds: float = 2.0,
    sleep: Any = time.sleep,
    monotonic: Any = time.monotonic,
) -> tuple[Path, JsonObject]:
    if runs != len(case.schedule) or runs != 4:
        raise ObservationConfigurationError("This frozen series requires exactly four runs.")
    profile = _preflight_profile(api, profile_id)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    series_id = f"{timestamp}-{uuid.uuid4().hex[:8]}"
    series_dir = (report_root / series_id).resolve()
    frozen: JsonObject = {
        "git_commit": _git_commit(),
        "working_tree_dirty": _working_tree_dirty(),
        "source_tree_sha256": _source_tree_sha256(),
        "case_id": case.case_id,
        "prompt_sha256": case.prompt_sha256,
        "profile": _profile_facts(profile),
        "framework_versions": _framework_versions(),
        "harness_contract": case.harness_contract,
        "actor_policy": case.actor_policy,
        "mode_schedule": list(case.schedule),
    }
    _atomic_json(
        series_dir / "frozen-series.json",
        {
            "schema_id": "novelpilot-live-book-series-frozen-v1",
            "series_id": series_id,
            "frozen_at": datetime.now(UTC).isoformat(),
            "frozen": frozen,
        },
    )

    reports: list[JsonObject] = []
    stop_series = False
    for index, mode in enumerate(case.schedule, start=1):
        if stop_series:
            report = {
                "schema_id": "novelpilot-live-book-observation-v1",
                "series_id": series_id,
                "slot": index,
                "status": "not_run",
                "project_id": None,
                "mode": mode,
                "frozen": frozen,
                "issues": [
                    _issue(
                        "provider_or_api_unavailable",
                        "A prior slot established that further real calls cannot safely start.",
                    )
                ],
                "technical_rescue_performed": False,
                "technical_rescue_count": 0,
                "actor_policy_id": case.actor_policy.get("id"),
            }
        else:
            report, stop_series = run_observation_slot(
                api=api,
                case=case,
                profile_id=profile_id,
                frozen=frozen,
                series_id=series_id,
                slot=index,
                mode=mode,
                sleep_seconds=sleep_seconds,
                sleep=sleep,
                monotonic=monotonic,
            )
        reports.append(report)
        _atomic_json(series_dir / f"slot-{index:02d}-{mode}.json", report)

    status_counts = Counter(str(item["status"]) for item in reports)
    issue_index = [
        {"slot": item["slot"], "mode": item["mode"], **issue}
        for item in reports
        for issue in cast(list[JsonObject], item.get("issues", []))
    ]
    aggregate: JsonObject = {
        "schema_id": "novelpilot-live-book-series-aggregate-v1",
        "series_id": series_id,
        "case_id": case.case_id,
        "profile_id": profile_id,
        "mode_schedule": list(case.schedule),
        "status_counts": {
            "completed": status_counts.get("completed", 0),
            "failed": status_counts.get("failed", 0),
            "not_run": status_counts.get("not_run", 0),
        },
        "slots": [
            {
                "slot": item["slot"],
                "mode": item["mode"],
                "status": item["status"],
                "project_id": item.get("project_id"),
                "elapsed_seconds": item.get("elapsed_seconds"),
                "total_tokens": cast(JsonObject, item.get("attempt_metrics", {})).get(
                    "total_tokens", 0
                ),
                "transport_retry_count": cast(
                    JsonObject, item.get("attempt_metrics", {})
                ).get("transport_retry_count", 0),
                "typed_output_extra_requests": cast(
                    JsonObject, item.get("attempt_metrics", {})
                ).get("typed_output_extra_requests", 0),
                "semantic_repair_task_count": cast(
                    JsonObject, item.get("attempt_metrics", {})
                ).get("semantic_repair_task_count", 0),
            }
            for item in reports
        ],
        "issue_index": issue_index,
        "technical_rescue_count": 0,
        "engineering_acceptance_dependency": False,
        "note": "This is a factual observation index, not a 4/4 success verdict.",
    }
    _atomic_json(series_dir / "aggregate.json", aggregate)
    return series_dir, aggregate


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the frozen four-slot whole-book observation through public NovelPilot APIs."
    )
    parser.add_argument("--case", required=True)
    parser.add_argument("--profile-id", required=True)
    parser.add_argument("--runs", required=True, type=int)
    parser.add_argument(
        "--api-base-url",
        default=os.environ.get("NOVELPILOT_API_BASE_URL", "http://127.0.0.1:8010"),
    )
    parser.add_argument("--report-root", type=Path, default=DEFAULT_REPORT_ROOT)
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.poll_seconds <= 0:
        raise ObservationConfigurationError("--poll-seconds must be positive.")
    case = load_case(arguments.case)
    api = HttpObservationApi(arguments.api_base_url)
    try:
        series_dir, aggregate = run_series(
            api=api,
            case=case,
            profile_id=arguments.profile_id,
            runs=arguments.runs,
            report_root=arguments.report_root,
            sleep_seconds=arguments.poll_seconds,
        )
    finally:
        api.close()
    print(f"Observation reports: {series_dir}")
    print(json.dumps(aggregate["status_counts"], ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
