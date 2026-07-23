from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app


def _project_payload() -> dict[str, object]:
    return {
        "project_id": "api-project",
        "creator_brief": "写一部长篇悬疑小说，并保持人物动机一致。",
        "operation_mode": "participatory",
    }


def test_project_projection_and_explicit_run_control(tmp_path: Path) -> None:
    app = create_app(
        database_path=tmp_path / "api.sqlite3",
        profile_path=tmp_path / "profiles.json",
        export_root=tmp_path / "exports",
        run_engine_enabled=False,
    )

    with TestClient(app) as client:
        created = client.post(
            "/api/projects",
            headers={"Idempotency-Key": "create-api-project"},
            json=_project_payload(),
        )
        assert created.status_code == 201
        created_body = created.json()
        assert created_body["replayed"] is False
        state = created_body["state"]
        assert state["project"]["project_id"] == "api-project"
        assert state["project"]["operation_mode"] == "participatory"
        assert state["run"]["status"] == "waiting_for_user"
        assert state["commands"][0]["command_id"] == "start_run"
        assert state["commands"][0]["enabled"] is True
        first_sequence = state["latest_event_sequence"]

        replay = client.post(
            "/api/projects",
            headers={"Idempotency-Key": "create-api-project"},
            json=_project_payload(),
        )
        assert replay.status_code == 201
        assert replay.json()["replayed"] is True

        first_read = client.get("/api/projects/api-project")
        second_read = client.get("/api/projects/api-project")
        assert first_read.status_code == 200
        assert second_read.json() == first_read.json()
        assert first_read.json()["latest_event_sequence"] == first_sequence

        diagnostics = client.get("/api/projects/api-project/diagnostics")
        assert diagnostics.status_code == 200
        assert diagnostics.json() == {
            "project_id": "api-project",
            "run_id": state["run"]["run_id"],
            "task_count": 0,
            "attempt_count": 0,
            "arc_count": 0,
            "completion_id": None,
            "completion_version": None,
            "attempts": [],
        }

        started = client.post(
            "/api/projects/api-project/run/start",
            headers={"Idempotency-Key": "start-api-project"},
            json={"expected_lock_version": state["run"]["lock_version"]},
        )
        assert started.status_code == 200
        started_state = started.json()["state"]
        assert started_state["run"]["status"] == "running"
        assert started_state["run"]["started_at_ms"] is not None

        stale = client.post(
            "/api/projects/api-project/run/start",
            headers={"Idempotency-Key": "stale-start"},
            json={"expected_lock_version": state["run"]["lock_version"]},
        )
        assert stale.status_code == 409
        assert stale.json()["error"]["code"] == "command_precondition_failed"

        page = client.get("/api/projects/api-project/events?after=0")
        assert page.status_code == 200
        event_types = [event["event_type"] for event in page.json()["events"]]
        assert event_types == ["project.created", "run.started"]

        projects = client.get("/api/projects")
        assert projects.status_code == 200
        assert [item["project_id"] for item in projects.json()] == ["api-project"]


def test_api_errors_use_one_envelope(tmp_path: Path) -> None:
    app = create_app(
        database_path=tmp_path / "errors.sqlite3",
        profile_path=tmp_path / "profiles.json",
        export_root=tmp_path / "exports",
        run_engine_enabled=False,
    )

    with TestClient(app) as client:
        missing_header = client.post("/api/projects", json=_project_payload())
        assert missing_header.status_code == 422
        assert missing_header.json()["error"]["code"] == "request_validation_failed"

        missing_project = client.get("/api/projects/does-not-exist")
        assert missing_project.status_code == 404
        assert missing_project.json() == {
            "error": {
                "code": "domain_object_not_found",
                "message": "does-not-exist",
                "details": None,
            }
        }
