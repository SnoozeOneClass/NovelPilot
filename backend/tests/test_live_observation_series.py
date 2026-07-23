from __future__ import annotations

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
        target_chapter_count: int | None,
        key: str,
    ) -> dict[str, Any]:
        assert target_chapter_count == 20
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
                    "task_kind": "book.assess_progress_or_completion",
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
                "recommended_target_chapter_count": 20,
            },
            "current_chapter": None,
            "latest_event_sequence": stage,
            "commands": commands,
        }


def test_frozen_series_runs_exact_mode_schedule_without_rescue(tmp_path: Path) -> None:
    case = load_case("benchmark-mother-natural-book-v1")
    api = FakeObservationApi()
    tick = 0.0

    def monotonic() -> float:
        nonlocal tick
        tick += 0.01
        return tick

    series_dir, aggregate = run_series(
        api=api,
        case=case,
        profile_id="grok-4.5",
        runs=4,
        report_root=tmp_path,
        sleep_seconds=0.001,
        sleep=lambda _seconds: None,
        monotonic=monotonic,
    )

    assert aggregate["status_counts"] == {"completed": 4, "failed": 0, "not_run": 0}
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


def test_frozen_series_rejects_partial_run_count(tmp_path: Path) -> None:
    with pytest.raises(ObservationConfigurationError, match="exactly four"):
        run_series(
            api=FakeObservationApi(),
            case=load_case("benchmark-mother-natural-book-v1"),
            profile_id="grok-4.5",
            runs=3,
            report_root=tmp_path,
        )
