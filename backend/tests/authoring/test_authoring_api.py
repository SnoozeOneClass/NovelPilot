from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

import pytest
from app.authoring.domain.models import Instruction, RunStatus
from app.authoring.models import AuthoringMetadataDocument, AuthoringModelMetadata
from app.authoring.models.catalog import (
    CapabilityEvidence,
    ProfilesDocument,
    StoredProfile,
    encode_profiles_document,
)
from app.authoring.models.contracts import ProfileCapabilities
from app.authoring.models.transport import (
    ActivationRequestBudgetExhausted,
    ModelRequestBudgetExhausted,
    TransportRetryBudgetExhausted,
)
from app.authoring.tools import EpisodeDeps
from app.authoring.workers import EpisodeResult
from app.main import create_app
from fastapi.testclient import TestClient


def _app(tmp_path: Path):
    return create_app(
        database_path=tmp_path / "authoring.sqlite3",
        profile_path=tmp_path / "profiles.json",
        metadata_path=tmp_path / "metadata.json",
        fake=True,
    )


def _wait_for_status(client: TestClient, project_id: str, status: str) -> dict[str, object]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        response = client.get(f"/api/authoring/projects/{project_id}")
        assert response.status_code == 200
        payload = response.json()
        if payload["status"] == status:
            return payload
        time.sleep(0.02)
    raise AssertionError(f"project did not reach {status}")


def test_authoring_api_isolated_create_run_list_detail_and_exports(tmp_path: Path) -> None:
    app = _app(tmp_path)
    with TestClient(app) as client:
        created = client.post(
            "/api/authoring/projects",
            json={
                "brief": "一个守钟人必须在黎明前兑现承诺",
                "target_chapters": 1,
                "default_profile_id": "fake-profile",
            },
        )
        assert created.status_code == 201
        project_id = created.json()["project_id"]
        assert created.json()["status"] == "ready"
        assert created.json()["profile_bindings"] == {"default": "fake-profile"}
        rebound = client.put(
            f"/api/authoring/projects/{project_id}/profiles",
            json={
                "default_profile_id": "fake-profile",
                "writer_profile_id": "writer-profile",
                "editor_profile_id": "editor-profile",
            },
        )
        assert rebound.status_code == 200
        assert rebound.json()["profile_bindings"]["writer"] == "writer-profile"

        started = client.post(f"/api/authoring/projects/{project_id}/run")
        assert started.status_code == 200
        completed = _wait_for_status(client, project_id, "completed")
        assert completed["completed_chapters"] == 1
        assert completed["progress_percent"] == 100
        assert any(item["kind"] == "chapter_committed" for item in completed["recent_activity"])

        projects = client.get("/api/authoring/projects")
        assert projects.status_code == 200
        assert [item["project_id"] for item in projects.json()] == [project_id]

        markdown = client.get(f"/api/authoring/projects/{project_id}/export?format=markdown")
        text = client.get(f"/api/authoring/projects/{project_id}/export?format=txt")
        assert markdown.status_code == text.status_code == 200
        assert markdown.headers["content-disposition"] == 'attachment; filename="novel.md"'
        assert text.headers["content-disposition"] == 'attachment; filename="novel.txt"'
        assert markdown.headers["etag"]
        repeated = client.get(f"/api/authoring/projects/{project_id}/export?format=markdown")
        assert repeated.headers["etag"] == markdown.headers["etag"]
        assert repeated.content == markdown.content
        assert "第1章" in markdown.text

    assert (tmp_path / "authoring.sqlite3").exists()


def test_authoring_api_validates_dtos_and_uses_shared_error_envelopes(tmp_path: Path) -> None:
    with TestClient(_app(tmp_path)) as client:
        for body in (
            {"brief": "   "},
            {"brief": "idea", "target_words": 30_000_001},
            {"brief": "idea", "default_profile_id": "   "},
        ):
            response = client.post("/api/authoring/projects", json=body)
            assert response.status_code == 422
            assert response.json()["error"]["code"] == "request_validation_failed"

        missing = client.get("/api/authoring/projects/does-not-exist")
        assert missing.status_code == 404
        assert missing.json() == {
            "error": {
                "code": "http_error",
                "message": "Authoring project not found",
                "details": None,
            }
        }

        project_id = client.post(
            "/api/authoring/projects", json={"brief": "valid cursor test", "target_chapters": 1}
        ).json()["project_id"]
        invalid_cursor = client.get(
            f"/api/authoring/projects/{project_id}/events",
            headers={"Last-Event-ID": "not-an-integer"},
        )
        assert invalid_cursor.status_code == 400
        assert invalid_cursor.json()["error"]["message"] == "Last-Event-ID must be an integer"


@pytest.mark.parametrize(
    "error_type",
    [ActivationRequestBudgetExhausted, ModelRequestBudgetExhausted, TransportRetryBudgetExhausted],
)
def test_project_api_explains_saved_budget_failure_as_a_run_limit(
    tmp_path: Path, error_type: type[RuntimeError]
) -> None:
    app = _app(tmp_path)
    with TestClient(app) as client:
        created = client.post(
            "/api/authoring/projects",
            json={"brief": "budget failure display", "target_chapters": 1},
        )
        project_id = created.json()["project_id"]

        async def save_failure() -> None:
            await app.state.authoring_resources.store.set_status(
                project_id,
                RunStatus.FAILURE_PAUSED,
                failure_reason=f"{error_type.__name__}: configured request budget exhausted",
            )

        assert client.portal is not None
        client.portal.call(save_failure)
        response = client.get(f"/api/authoring/projects/{project_id}")
        assert response.status_code == 200
        project = response.json()
        assert project["status"] == "failure_paused"
        assert project["completed_chapters"] == 0
        assert "运行上限" in project["failure_reason"]
        assert "模型设置" not in project["failure_reason"]
        assert error_type.__name__ not in project["failure_reason"]


def test_profile_projection_hides_secrets_and_rejects_stale_authoring_metadata(
    tmp_path: Path,
) -> None:
    profile = StoredProfile(
        id="profile-a",
        display_name="Profile A",
        api_family="openai_responses",
        base_url="https://provider.invalid/v1",
        api_key="projection-secret",
        model_id="model-a",
        request_options={"max_tokens": 512},
    )
    evidence = CapabilityEvidence(
        checked_at="2026-09-04T00:00:00Z",
        profile_fingerprint=profile.configuration_fingerprint,
        source="pydantic-ai-capability-v1",
        capabilities=ProfileCapabilities(tool_calling=True),
    )
    profile = profile.model_copy(update={"capability_test": evidence})
    profiles_path = tmp_path / "profiles.json"
    profiles_path.write_bytes(
        encode_profiles_document(
            ProfilesDocument(selected_profile_id=profile.id, profiles=[profile])
        )
    )
    metadata_path = tmp_path / "metadata.json"
    metadata_path.write_text(
        AuthoringMetadataDocument(
            profiles=[
                AuthoringModelMetadata(
                    profile_id=profile.id,
                    configuration_fingerprint="0" * 64,
                    context_window=8_192,
                    max_output_tokens=512,
                )
            ]
        ).model_dump_json(),
        encoding="utf-8",
    )
    app = create_app(
        database_path=tmp_path / "authoring.sqlite3",
        profile_path=profiles_path,
        metadata_path=metadata_path,
        fake=True,
    )

    with TestClient(app) as client:
        response = client.get("/api/authoring/profiles")
        assert response.status_code == 200
        assert response.json() == [
            {
                "profile_id": "profile-a",
                "display_name": "Profile A",
                "model_id": "model-a",
                "capability_status": "stale",
                "context_window": None,
                "max_output_tokens": None,
            }
        ]
        assert "projection-secret" not in response.text
        assert "provider.invalid" not in response.text


def test_real_composition_validates_profile_and_metadata_when_creating(tmp_path: Path) -> None:
    profile = StoredProfile(
        id="profile-a",
        display_name="Profile A",
        api_family="openai_responses",
        base_url="https://provider.invalid/v1",
        api_key="validation-only-secret",
        model_id="model-a",
        request_options={"max_tokens": 512},
    )
    profile = profile.model_copy(
        update={
            "capability_test": CapabilityEvidence(
                checked_at="2000-01-01T00:00:00Z",
                profile_fingerprint=profile.configuration_fingerprint,
                source="pydantic-ai-capability-v1",
                capabilities=ProfileCapabilities(tool_calling=True),
            )
        }
    )
    profiles_path = tmp_path / "profiles.json"
    profiles_path.write_bytes(
        encode_profiles_document(
            ProfilesDocument(selected_profile_id=profile.id, profiles=[profile])
        )
    )
    metadata_path = tmp_path / "metadata.json"
    metadata_path.write_text(
        AuthoringMetadataDocument(
            profiles=[
                AuthoringModelMetadata(
                    profile_id=profile.id,
                    configuration_fingerprint=profile.configuration_fingerprint,
                    context_window=8_192,
                    max_output_tokens=512,
                )
            ]
        ).model_dump_json(),
        encoding="utf-8",
    )
    app = create_app(
        database_path=tmp_path / "authoring.sqlite3",
        profile_path=profiles_path,
        metadata_path=metadata_path,
    )
    with TestClient(app) as client:
        invalid = client.post(
            "/api/authoring/projects",
            json={"brief": "invalid binding", "default_profile_id": "missing"},
        )
        valid = client.post(
            "/api/authoring/projects",
            json={"brief": "valid binding", "default_profile_id": "profile-a"},
        )
        assert invalid.status_code == 422
        assert valid.status_code == 201


def test_sse_replays_persisted_sequence_from_last_event_id(tmp_path: Path) -> None:
    with TestClient(_app(tmp_path)) as client:
        created = client.post(
            "/api/authoring/projects",
            json={"brief": "replay", "target_chapters": 1},
        ).json()
        project_id = created["project_id"]
        first_cursor = created["latest_event_sequence"]
        client.post(f"/api/authoring/projects/{project_id}/run")
        _wait_for_status(client, project_id, "completed")

        replay = client.get(
            f"/api/authoring/projects/{project_id}/events?follow=false",
            headers={"Last-Event-ID": str(first_cursor)},
        )
        assert replay.status_code == 200
        ids = [
            int(line.removeprefix("id: "))
            for line in replay.text.splitlines()
            if line.startswith("id: ")
        ]
        assert ids
        assert min(ids) > first_cursor
        assert ids == sorted(ids)
        assert "event: authoring_event" in replay.text
        assert f'"project_id":"{project_id}"' in replay.text


def test_web_cancel_stops_active_process_run_without_more_chapters(tmp_path: Path) -> None:
    with TestClient(_app(tmp_path)) as client:
        created = client.post(
            "/api/authoring/projects",
            json={"brief": "cancel a long run", "target_chapters": 100},
        ).json()
        project_id = created["project_id"]
        assert client.post(f"/api/authoring/projects/{project_id}/run").status_code == 200
        cancelled = client.post(f"/api/authoring/projects/{project_id}/cancel")
        assert cancelled.status_code == 200
        assert cancelled.json()["project"]["status"] == "cancelled"
        chapters = cancelled.json()["project"]["completed_chapters"]
        time.sleep(0.1)
        after = client.get(f"/api/authoring/projects/{project_id}").json()
        assert after["status"] == "cancelled"
        assert after["completed_chapters"] == chapters


def test_web_pause_and_cancel_wake_active_worker_interrupt_event(tmp_path: Path) -> None:
    class BlockingWorker:
        def __init__(self) -> None:
            self.started = threading.Event()

        async def run(
            self,
            _instruction: Instruction,
            _deps: EpisodeDeps,
            cancel_event=None,
        ) -> EpisodeResult:
            assert cancel_event is not None
            self.started.set()
            await cancel_event.wait()
            raise asyncio.CancelledError

    app = _app(tmp_path)
    with TestClient(app) as client:
        for action, expected in (("pause", "paused"), ("cancel", "cancelled")):
            worker = BlockingWorker()
            app.state.authoring_resources.service.worker_runtime = worker
            project_id = client.post(
                "/api/authoring/projects",
                json={"brief": f"{action} active request", "target_chapters": 2},
            ).json()["project_id"]
            assert client.post(f"/api/authoring/projects/{project_id}/run").status_code == 200
            assert worker.started.wait(timeout=2)

            response = client.post(f"/api/authoring/projects/{project_id}/{action}")

            assert response.status_code == 200
            assert _wait_for_status(client, project_id, expected)["status"] == expected


def test_start_reports_when_another_process_owns_the_persisted_lease(tmp_path: Path) -> None:
    app = _app(tmp_path)
    with TestClient(app) as client:
        project_id = client.post(
            "/api/authoring/projects",
            json={"brief": "cross process", "target_chapters": 1},
        ).json()["project_id"]
        asyncio.run(
            app.state.authoring_resources.store.acquire_lease(
                project_id, "another-process", ttl_seconds=30
            )
        )

        response = client.post(f"/api/authoring/projects/{project_id}/run")

        assert response.status_code == 200
        assert response.json()["accepted"] is False
        assert response.json()["ownership"] == "owned_elsewhere"
