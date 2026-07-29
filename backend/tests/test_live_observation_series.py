from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scripts.live_book_observation_series import (
    ObservationConfigurationError,
    load_case,
    run_series,
)


class FakeObservationApi:
    def __init__(self) -> None:
        self.projects: dict[str, dict[str, Any]] = {}
        self.actions: list[tuple[str, str]] = []

    def profiles(self) -> dict[str, Any]:
        return {
            "selected_profile_id": "grok-4.5",
            "profiles": [
                {
                    "id": "grok-4.5",
                    "display_name": "Grok 4.5",
                    "api_family": "openai_responses",
                    "base_url": "https://provider.invalid/v1",
                    "model_id": "grok-4.5",
                    "request_options": {"reasoning_effort": "high"},
                    "enabled": True,
                    "has_api_key": True,
                    "capability_status": "ready",
                    "capabilities": {
                        "text_output": True,
                        "text_streaming": True,
                        "native_json_schema": True,
                        "tool_calling": False,
                        "usage_reporting": True,
                        "contract_version": 1,
                    },
                    "configuration_fingerprint": "a" * 64,
                    "capability_fingerprint": "b" * 64,
                }
            ],
        }

    def create_project(
        self,
        *,
        project_id: str,
        prompt: str,
        mode: str,
        profile_id: str,
        key: str,
    ) -> dict[str, Any]:
        assert prompt.startswith("我有一个悬疑小说构思")
        assert profile_id == "grok-4.5"
        assert key.endswith(":create")
        self.projects[project_id] = {"stage": 0, "mode": mode}
        self.actions.append((project_id, "create_project"))
        return self._state(project_id)

    def start_run(self, *, project_id: str, lock_version: int, key: str) -> dict[str, Any]:
        assert lock_version == 1
        assert key.endswith(":start")
        self.projects[project_id]["stage"] = 1
        self.actions.append((project_id, "start_run"))
        return self._state(project_id)

    def get_state(self, project_id: str) -> dict[str, Any]:
        return self._state(project_id)

    def send_book_input(
        self,
        *,
        project_id: str,
        workspace_lock_version: int,
        message: str,
        suggestion_id: str,
        key: str,
    ) -> dict[str, Any]:
        assert workspace_lock_version == 1
        assert message == "采用推荐书名。"
        assert suggestion_id == "suggestion-1"
        assert ":book-input:" in key
        self.projects[project_id]["stage"] = 2
        self.actions.append((project_id, "book_input"))
        return self._state(project_id)

    def approve_book(self, *, project_id: str, key: str) -> dict[str, Any]:
        assert key.endswith(":book-approve")
        mode = self.projects[project_id]["mode"]
        self.projects[project_id]["stage"] = 3 if mode == "participatory" else 4
        self.actions.append((project_id, "book_approval"))
        return self._state(project_id)

    def approve_arc(
        self,
        *,
        project_id: str,
        key: str,
    ) -> dict[str, Any]:
        assert ":arc-approve:" in key
        self.projects[project_id]["stage"] = 4
        self.actions.append((project_id, "arc_approval"))
        return self._state(project_id)

    def diagnostics(self, project_id: str) -> dict[str, Any]:
        assert self.projects[project_id]["stage"] == 4
        return {
            "project_id": project_id,
            "run_id": f"{project_id}:run",
            "task_count": 1,
            "attempt_count": 1,
            "arc_count": 1,
            "completion_id": f"{project_id}:completion",
            "completion_version": 1,
            "attempts": [
                {
                    "task_id": f"{project_id}:task",
                    "task_kind": "evaluate.book_completion",
                    "attempt_id": f"{project_id}:attempt",
                    "attempt_number": 1,
                    "attempt_status": "succeeded",
                    "retry_kind": "initial",
                    "provider_request_count": 1,
                    "transport_retry_count": 0,
                    "model_request_count": 1,
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "total_tokens": 15,
                    "error_code": None,
                    "error_category": None,
                }
            ],
        }

    def events(self, project_id: str) -> list[dict[str, Any]]:
        return [
            {
                "sequence": 1,
                "event_id": f"{project_id}:event",
                "event_type": "book.completed",
                "aggregate_type": "book",
                "aggregate_id": f"{project_id}:book",
                "occurred_at_ms": 1,
            }
        ]

    def snapshot(self, project_id: str) -> dict[str, Any]:
        return {"project_id": project_id, "chapters": [{"book_ordinal": 1}] * 20}

    def export(self, project_id: str) -> dict[str, Any]:
        return {
            "project_id": project_id,
            "content_sha256": "c" * 64,
            "snapshot_fingerprint": "d" * 64,
            "byte_count": 100,
            "path": f"{project_id}.md",
        }

    def _state(self, project_id: str) -> dict[str, Any]:
        value = self.projects[project_id]
        stage = int(value["stage"])
        mode = str(value["mode"])
        if stage == 0:
            status, wait_reason, commands = "waiting_for_user", "not_started", [
                {"command_id": "start_run", "enabled": True}
            ]
        elif stage == 1:
            status, wait_reason, commands = "waiting_for_user", "book_direction_input", [
                {"command_id": "send_book_input", "enabled": True}
            ]
        elif stage == 2:
            status, wait_reason, commands = "waiting_for_user", "book_approval_required", [
                {"command_id": "approve_book", "enabled": True}
            ]
        elif stage == 3:
            status, wait_reason, commands = "waiting_for_user", "arc_approval_required", [
                {"command_id": "approve_arc", "enabled": True}
            ]
        else:
            status, wait_reason, commands = "completed", None, []
        task_kind_by_stage = {
            1: "book.discuss",
            2: "evaluate.book",
            3: "arc.plan",
            4: "evaluate.book_completion",
        }
        task_kind = task_kind_by_stage.get(stage)
        return {
            "project": {
                "project_id": project_id,
                "operation_mode": mode,
                "lifecycle_status": "completed" if stage == 4 else "active",
                "committed_chapter_count": 20 if stage == 4 else 0,
            },
            "run": {
                "run_id": f"{project_id}:run",
                "status": status,
                "wait_reason_code": wait_reason,
                "failure_code": None,
                "lock_version": 1,
            },
            "book": {
                "book_id": f"{project_id}:book",
                "lifecycle_status": "completed" if stage == 4 else "active",
                "current_baseline_id": None if stage < 3 else f"{project_id}:book-baseline",
                "workspace_state": "approved" if stage >= 3 else "drafting",
                "workspace_lock_version": 1,
                "discussion": {
                    "turn_count": 1,
                    "suggestions": [
                        {
                            "id": "suggestion-1",
                            "message": "采用推荐书名。",
                            "recommended": True,
                        }
                    ],
                },
            },
            "current_arc": {
                "arc_id": f"{project_id}:arc",
                "ordinal": 1,
                "lifecycle_status": "completed" if stage == 4 else "planning",
                "workspace_state": "approved" if stage == 4 else "planning",
                "closure_cumulative_chapter_count": 20,
            },
            "current_chapter": None,
            "latest_event_sequence": stage,
            "commands": commands,
            "recent_tasks": (
                []
                if task_kind is None
                else [
                    {
                        "task_kind": task_kind,
                        "task_status": "succeeded",
                        "delivery_state": "applied",
                        "attempt_number": 1,
                        "attempt_status": "succeeded",
                        "retry_kind": "initial",
                        "provider_request_count": 1,
                        "transport_retry_count": 0,
                    }
                ]
            ),
        }


class OutsideAdvisoryRangeObservationApi(FakeObservationApi):
    def _state(self, project_id: str) -> dict[str, Any]:
        state = super()._state(project_id)
        if int(self.projects[project_id]["stage"]) == 4:
            state["project"]["committed_chapter_count"] = 1
        return state


class FailurePausedObservationApi(FakeObservationApi):
    def start_run(
        self, *, project_id: str, lock_version: int, key: str
    ) -> dict[str, Any]:
        assert lock_version == 1
        assert key.endswith(":start")
        self.projects[project_id]["stage"] = 5
        self.actions.append((project_id, "start_run"))
        return self._state(project_id)

    def diagnostics(self, project_id: str) -> dict[str, Any]:
        assert self.projects[project_id]["stage"] == 5
        return {
            "project_id": project_id,
            "run_id": f"{project_id}:run",
            "task_count": 1,
            "attempt_count": 1,
            "arc_count": 0,
            "completion_id": None,
            "completion_version": None,
            "attempts": [
                {
                    "task_id": f"{project_id}:task",
                    "task_kind": "discuss.book",
                    "attempt_id": f"{project_id}:attempt",
                    "attempt_number": 6,
                    "attempt_status": "failed",
                    "retry_kind": "transport_retry",
                    "provider_request_count": 6,
                    "transport_retry_count": 5,
                    "model_request_count": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "total_tokens": 0,
                    "error_code": "provider_timeout",
                    "error_category": "execution",
                }
            ],
        }

    def events(self, project_id: str) -> list[dict[str, Any]]:
        assert self.projects[project_id]["stage"] == 5
        return []

    def _state(self, project_id: str) -> dict[str, Any]:
        value = self.projects[project_id]
        if int(value["stage"]) != 5:
            return super()._state(project_id)
        mode = str(value["mode"])
        return {
            "project": {
                "project_id": project_id,
                "operation_mode": mode,
                "lifecycle_status": "active",
                "committed_chapter_count": 0,
            },
            "run": {
                "run_id": f"{project_id}:run",
                "status": "failure_paused",
                "wait_reason_code": None,
                "failure_code": "provider_timeout",
                "lock_version": 2,
            },
            "book": {
                "book_id": f"{project_id}:book",
                "lifecycle_status": "drafting",
                "current_baseline_id": None,
                "workspace_state": "drafting",
                "workspace_lock_version": 1,
                "discussion": {"turn_count": 0, "suggestions": []},
            },
            "current_arc": None,
            "current_chapter": None,
            "latest_event_sequence": 2,
            "commands": [],
            "recent_tasks": [
                {
                    "task_kind": "book.discuss",
                    "task_status": "failed",
                    "delivery_state": "failed",
                    "attempt_number": 6,
                    "attempt_status": "failed",
                    "retry_kind": "transport_retry",
                    "provider_request_count": 6,
                    "transport_retry_count": 5,
                }
            ],
        }


class SlowObservationApi(FakeObservationApi):
    def start_run(
        self, *, project_id: str, lock_version: int, key: str
    ) -> dict[str, Any]:
        assert lock_version == 1
        assert key.endswith(":start")
        self.projects[project_id]["stage"] = 6
        self.projects[project_id]["poll_count"] = 0
        self.actions.append((project_id, "start_run"))
        return self._state(project_id)

    def get_state(self, project_id: str) -> dict[str, Any]:
        project = self.projects[project_id]
        project["poll_count"] = int(project["poll_count"]) + 1
        if int(project["poll_count"]) >= 3:
            project["stage"] = 4
        return self._state(project_id)

    def _state(self, project_id: str) -> dict[str, Any]:
        value = self.projects[project_id]
        if int(value["stage"]) != 6:
            return super()._state(project_id)
        mode = str(value["mode"])
        return {
            "project": {
                "project_id": project_id,
                "operation_mode": mode,
                "lifecycle_status": "active",
                "committed_chapter_count": 2,
            },
            "run": {
                "run_id": f"{project_id}:run",
                "status": "running",
                "wait_reason_code": None,
                "failure_code": None,
                "lock_version": 2,
            },
            "book": {
                "book_id": f"{project_id}:book",
                "lifecycle_status": "active",
                "current_baseline_id": f"{project_id}:book-baseline",
                "workspace_state": "approved",
                "workspace_lock_version": 1,
                "discussion": {"turn_count": 1, "suggestions": []},
            },
            "current_arc": {
                "arc_id": f"{project_id}:arc",
                "ordinal": 1,
                "lifecycle_status": "active",
                "workspace_state": "approved",
                "closure_cumulative_chapter_count": 20,
            },
            "current_chapter": {
                "chapter_id": f"{project_id}:chapter:3",
                "book_ordinal": 3,
                "lifecycle_status": "drafting",
                "workspace_state": "drafting",
            },
            "latest_event_sequence": 6,
            "commands": [],
            "recent_tasks": [
                {
                    "task_kind": "chapter.draft",
                    "task_status": "running",
                    "delivery_state": "pending",
                    "attempt_number": 2,
                    "attempt_status": "running",
                    "retry_kind": "transport_retry",
                    "provider_request_count": 2,
                    "transport_retry_count": 1,
                }
            ],
        }


class HeartbeatThenCrashObservationApi(SlowObservationApi):
    def get_state(self, project_id: str) -> dict[str, Any]:
        project = self.projects[project_id]
        project["poll_count"] = int(project["poll_count"]) + 1
        if int(project["poll_count"]) >= 3:
            raise RuntimeError("runner crashed after heartbeat")
        return self._state(project_id)


def test_frozen_series_runs_exact_mode_schedule_without_rescue(tmp_path: Path) -> None:
    case = load_case("benchmark-mother-natural-book-v1")
    api = FakeObservationApi()
    announcements: list[str] = []
    tick = 0.0

    def monotonic() -> float:
        nonlocal tick
        tick += 0.01
        return tick

    series_dir, aggregate = run_series(
        api=api,
        case=case,
        profile_id=None,
        runs=4,
        report_root=tmp_path,
        sleep_seconds=0.001,
        sleep=lambda _seconds: None,
        monotonic=monotonic,
        announce=announcements.append,
    )

    assert aggregate["status_counts"] == {"completed": 4, "failed": 0, "not_run": 0}
    assert aggregate["profile_id"] == "grok-4.5"
    assert aggregate["technical_rescue_count"] == 0
    assert [item["mode"] for item in aggregate["slots"]] == list(case.schedule)
    assert len(api.projects) == 4
    actions_by_mode = {
        mode: [action for project_id, action in api.actions if f"-{mode.replace('_', '-')}" in project_id]
        for mode in case.schedule
    }
    assert "arc_approval" not in actions_by_mode["full_auto"]
    assert actions_by_mode["participatory"].count("arc_approval") == 2
    assert (series_dir / "aggregate.json").is_file()
    assert len(list(series_dir.glob("slot-*.json"))) == 4
    state = json.loads((series_dir / "series.json").read_text(encoding="utf-8"))
    assert state["status"] == "finished"
    assert state["analysis_ready"] is True
    assert state["active_observation"] is None
    latest = json.loads(
        (tmp_path / "latest-series.json").read_text(encoding="utf-8")
    )
    assert latest["series_id"] == aggregate["series_id"]
    assert latest["status"] == "finished"
    assert latest["aggregate_path"].endswith("/aggregate.json")
    assert any("started | profile=grok-4.5" in line for line in announcements)
    assert any("[1/4 full_auto] slot started" in line for line in announcements)
    assert any(
        "task=book.discuss#1/succeeded" in line for line in announcements
    )
    assert any(
        "actor submitted recommended Book input" in line
        for line in announcements
    )
    assert any(
        "[2/4 participatory] actor approved Arc 1 with its derived outline checkpoint"
        in line
        for line in announcements
    )
    assert announcements[-1].endswith(
        "finished | completed=4 | failed=0 | not_run=0"
    )
    assert all("%" not in line for line in announcements)
    assert all(case.prompt not in line for line in announcements)


def test_advisory_chapter_range_is_recorded_without_failing_a_completed_run(
    tmp_path: Path,
) -> None:
    series_dir, aggregate = run_series(
        api=OutsideAdvisoryRangeObservationApi(),
        case=load_case("benchmark-mother-natural-book-v1"),
        profile_id=None,
        runs=4,
        report_root=tmp_path,
    )

    assert aggregate["status_counts"] == {
        "completed": 4,
        "failed": 0,
        "not_run": 0,
    }
    assert aggregate["issue_index"] == []
    reports = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(series_dir.glob("slot-*.json"))
    ]
    assert [report["completion"]["chapter_count"] for report in reports] == [
        1,
        1,
        1,
        1,
    ]
    assert all(
        report["completion"]["within_advisory_chapter_range"] is False
        for report in reports
    )
    assert all(report["issues"] == [] for report in reports)


def test_frozen_series_rejects_partial_run_count(tmp_path: Path) -> None:
    with pytest.raises(ObservationConfigurationError, match="exactly four"):
        run_series(
            api=FakeObservationApi(),
            case=load_case("benchmark-mother-natural-book-v1"),
            profile_id="grok-4.5",
            runs=3,
            report_root=tmp_path,
        )


def test_frozen_series_rejects_non_positive_heartbeat(tmp_path: Path) -> None:
    with pytest.raises(ObservationConfigurationError, match="Heartbeat.*positive"):
        run_series(
            api=FakeObservationApi(),
            case=load_case("benchmark-mother-natural-book-v1"),
            profile_id=None,
            runs=4,
            report_root=tmp_path,
            heartbeat_seconds=0,
        )


def test_product_failures_finalize_handoff_without_technical_rescue(
    tmp_path: Path,
) -> None:
    series_dir, aggregate = run_series(
        api=FailurePausedObservationApi(),
        case=load_case("benchmark-mother-natural-book-v1"),
        profile_id=None,
        runs=4,
        report_root=tmp_path,
    )

    assert aggregate["status_counts"] == {
        "completed": 0,
        "failed": 4,
        "not_run": 0,
    }
    assert aggregate["technical_rescue_count"] == 0
    assert {item["code"] for item in aggregate["issue_index"]} == {
        "run_failure_paused"
    }
    state = json.loads((series_dir / "series.json").read_text(encoding="utf-8"))
    assert state["status"] == "finished"
    assert state["recorded_slot_count"] == 4
    aggregate_on_disk = json.loads(
        (series_dir / "aggregate.json").read_text(encoding="utf-8")
    )
    assert {
        item["code"] for item in aggregate_on_disk["issue_index"]
    } == {"run_failure_paused"}


def test_unexpected_runner_crash_leaves_durable_running_marker(
    tmp_path: Path,
) -> None:
    class CrashingObservationApi(FakeObservationApi):
        def create_project(
            self,
            *,
            project_id: str,
            prompt: str,
            mode: str,
            profile_id: str,
            key: str,
        ) -> dict[str, Any]:
            raise RuntimeError("runner crashed")

    with pytest.raises(RuntimeError, match="runner crashed"):
        run_series(
            api=CrashingObservationApi(),
            case=load_case("benchmark-mother-natural-book-v1"),
            profile_id=None,
            runs=4,
            report_root=tmp_path,
        )

    latest = json.loads(
        (tmp_path / "latest-series.json").read_text(encoding="utf-8")
    )
    assert latest["status"] == "running"
    series_dir = tmp_path / latest["series_directory"]
    state = json.loads((series_dir / "series.json").read_text(encoding="utf-8"))
    assert state["status"] == "running"
    assert state["recorded_slot_count"] == 0
    assert state["analysis_ready"] is False
    assert state["active_observation"]["kind"] == "slot_started"


def test_unchanged_long_running_state_emits_truthful_heartbeat(
    tmp_path: Path,
) -> None:
    announcements: list[str] = []
    tick = -30.0

    def monotonic() -> float:
        nonlocal tick
        tick += 30.0
        return tick

    series_dir, aggregate = run_series(
        api=SlowObservationApi(),
        case=load_case("benchmark-mother-natural-book-v1"),
        profile_id=None,
        runs=4,
        report_root=tmp_path,
        sleep_seconds=0.001,
        heartbeat_seconds=60.0,
        sleep=lambda _seconds: None,
        monotonic=monotonic,
        announce=announcements.append,
    )

    assert aggregate["status_counts"]["completed"] == 4
    heartbeats = [line for line in announcements if "still running" in line]
    assert len(heartbeats) == 4
    assert all("Chapter 3" in line for line in heartbeats)
    assert all("task=chapter.draft#2/running" in line for line in heartbeats)
    assert all("transport_retries=1" in line for line in heartbeats)
    assert all("committed=2" in line for line in heartbeats)
    assert all("elapsed=00:" in line for line in heartbeats)
    assert all("%" not in line for line in announcements)
    state = json.loads((series_dir / "series.json").read_text(encoding="utf-8"))
    assert state["active_observation"] is None


def test_heartbeat_is_persisted_if_runner_crashes_before_slot_report(
    tmp_path: Path,
) -> None:
    tick = -30.0

    def monotonic() -> float:
        nonlocal tick
        tick += 30.0
        return tick

    with pytest.raises(RuntimeError, match="after heartbeat"):
        run_series(
            api=HeartbeatThenCrashObservationApi(),
            case=load_case("benchmark-mother-natural-book-v1"),
            profile_id=None,
            runs=4,
            report_root=tmp_path,
            sleep_seconds=0.001,
            heartbeat_seconds=60.0,
            sleep=lambda _seconds: None,
            monotonic=monotonic,
        )

    latest = json.loads(
        (tmp_path / "latest-series.json").read_text(encoding="utf-8")
    )
    series_dir = tmp_path / latest["series_directory"]
    state = json.loads((series_dir / "series.json").read_text(encoding="utf-8"))
    assert state["status"] == "running"
    assert state["recorded_slot_count"] == 0
    assert state["active_observation"]["kind"] == "heartbeat"
    assert "Chapter 3" in state["active_observation"]["message"]
    assert state["active_observation"]["non_authoritative"] is True
