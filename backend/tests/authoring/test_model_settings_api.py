from __future__ import annotations

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote

import pytest
from app.authoring.domain.models import WorkerRole
from app.authoring.models import AuthoringMetadataDocument
from app.authoring.models import settings as settings_module
from app.authoring.models.catalog import (
    CapabilityEvidence,
    ProfileCatalog,
    StoredProfile,
)
from app.authoring.models.contracts import ProfileCapabilities
from app.authoring.models.probe import ProfileCapabilityProbeError
from app.main import create_app
from fastapi.testclient import TestClient

ROOT = "/api/authoring/model-settings"
SECRET = "settings-only-credential-47ab"


def _body(**changes: object) -> dict[str, object]:
    return {
        "display_name": "Writer A",
        "api_family": "openai_responses",
        "base_url": "https://provider.invalid/v1",
        "model_id": "opaque-writer",
        "api_key": SECRET,
        "request_options": {"max_tokens": 2048, "reasoning_effort": "high"},
        "context_window": 32768,
        "max_output_tokens": 2048,
        "input_price_per_million": 1.25,
        "output_price_per_million": 5,
        "cache_price_per_million": 0.5,
        **changes,
    }


def _evidence(profile: StoredProfile) -> CapabilityEvidence:
    return CapabilityEvidence(
        checked_at="2026-09-09T00:00:00Z",
        profile_fingerprint=profile.configuration_fingerprint,
        source="pydantic-ai-capability-v1",
        capabilities=ProfileCapabilities(
            text_streaming=True, native_json_schema=True, tool_calling=True
        ),
    )


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    async def successful_probe(
        profile: StoredProfile, *, require_tool_calling: bool
    ) -> CapabilityEvidence:
        assert require_tool_calling is True
        return _evidence(profile)

    monkeypatch.setattr(settings_module, "probe_stored_profile", successful_probe)
    app = create_app(
        database_path=tmp_path / "authoring.sqlite3",
        profile_path=tmp_path / "profiles.json",
        metadata_path=tmp_path / "metadata.json",
    )
    with TestClient(app, raise_server_exceptions=False) as value:
        yield value


def _ready(client: TestClient, profile_id: str = "writer-a") -> dict[str, object]:
    saved = client.put(f"{ROOT}/profiles/{profile_id}", json=_body())
    assert saved.status_code == 200, saved.text
    validated = client.post(f"{ROOT}/profiles/{profile_id}/validate")
    assert validated.status_code == 200, validated.text
    return validated.json()


def test_settings_http_round_trip_default_and_beginner_projection(
    client: TestClient, tmp_path: Path
) -> None:
    assert client.get(ROOT).json() == {"selected_profile_id": None, "profiles": []}
    saved = client.put(f"{ROOT}/profiles/writer-a", json=_body())
    assert saved.status_code == 200, saved.text
    assert saved.json() == {
        "selected_profile_id": None,
        "profiles": [
            {
                "profile_id": "writer-a",
                "display_name": "Writer A",
                "api_family": "openai_responses",
                "base_url": "https://provider.invalid/v1",
                "model_id": "opaque-writer",
                "enabled": True,
                "has_api_key": True,
                "request_options": {"max_tokens": 2048, "reasoning_effort": "high"},
                "capability_status": "missing",
                "context_window": 32768,
                "max_output_tokens": 2048,
                "input_price_per_million": 1.25,
                "output_price_per_million": 5,
                "cache_price_per_million": 0.5,
                "metadata_version": 1,
            }
        ],
    }
    premature = client.put(f"{ROOT}/default", json={"profile_id": "writer-a"})
    assert premature.status_code == 422
    checked = client.post(f"{ROOT}/profiles/writer-a/validate")
    assert checked.status_code == 200, checked.text
    assert checked.json()["profiles"][0]["capability_status"] == "ready"
    default = client.put(f"{ROOT}/default", json={"profile_id": "writer-a"})
    assert default.status_code == 200
    assert default.json()["selected_profile_id"] == "writer-a"
    loaded = client.get(ROOT)
    assert loaded.json() == default.json()
    for response in (saved, checked, default, loaded):
        assert SECRET not in response.text
        assert '"api_key"' not in response.text
        assert "fingerprint" not in response.text
        assert "capability_test" not in response.text

    beginner = client.get("/api/authoring/profiles")
    assert beginner.json() == [
        {
            "profile_id": "writer-a",
            "display_name": "Writer A",
            "model_id": "opaque-writer",
            "capability_status": "ready",
            "context_window": 32768,
            "max_output_tokens": 2048,
        }
    ]
    assert SECRET not in beginner.text
    assert "provider.invalid" not in beginner.text
    created = client.post("/api/authoring/projects", json={"brief": "use the selected model"})
    assert created.status_code == 201, created.text
    assert created.json()["profile_bindings"] == {}
    assert ProfileCatalog(tmp_path / "profiles.json").load().selected_profile_id == "writer-a"
    assert SECRET not in (tmp_path / "metadata.json").read_text("utf-8")


@pytest.mark.parametrize("profile_id", ["team/model", "team/model/validate"])
def test_settings_routes_round_trip_encoded_profile_ids(
    client: TestClient, profile_id: str
) -> None:
    profile_url = f"{ROOT}/profiles/{quote(profile_id, safe='')}"
    saved = client.put(profile_url, json=_body())
    assert saved.status_code == 200, saved.text
    assert saved.json()["profiles"][0]["profile_id"] == profile_id
    edited = client.put(profile_url, json=_body(api_key="", display_name="Renamed model"))
    assert edited.status_code == 200, edited.text
    validated = client.post(f"{profile_url}/validate")
    assert validated.status_code == 200, validated.text
    assert validated.json()["profiles"][0]["capability_status"] == "ready"
    assert validated.json()["profiles"][0]["profile_id"] == profile_id
    selected = client.put(f"{ROOT}/default", json={"profile_id": profile_id})
    assert selected.status_code == 200, selected.text
    assert client.get(ROOT).json()["selected_profile_id"] == profile_id
    created = client.post(
        "/api/authoring/projects",
        json={"brief": "use an encoded model assignment", "default_profile_id": profile_id},
    )
    assert created.status_code == 201, created.text
    assert created.json()["profile_bindings"] == {"default": profile_id}


@pytest.mark.parametrize("key_edit", [None, "", "   ", "omitted"])
def test_blank_key_edits_preserve_key_evidence_and_unrelated_profiles(
    client: TestClient, tmp_path: Path, key_edit: str | None
) -> None:
    _ready(client)
    _ready(client, "other")
    catalog = ProfileCatalog(tmp_path / "profiles.json")
    before = catalog.get_stored("other")
    metadata_before = AuthoringMetadataDocument.model_validate_json(
        (tmp_path / "metadata.json").read_bytes()
    ).profiles[1]
    body = _body(api_key=key_edit, display_name="Renamed writer", context_window=65536)
    if key_edit == "omitted":
        del body["api_key"]
    saved = client.put(f"{ROOT}/profiles/writer-a", json=body)
    assert saved.status_code == 200, saved.text
    changed = saved.json()["profiles"][0]
    assert changed["capability_status"] == "ready"
    assert changed["has_api_key"] is True
    assert changed["metadata_version"] == 2
    assert catalog.get_stored("writer-a").api_key.get_secret_value() == SECRET
    assert catalog.get_stored("other") == before
    assert (
        AuthoringMetadataDocument.model_validate_json(
            (tmp_path / "metadata.json").read_bytes()
        ).profiles[1]
        == metadata_before
    )
    repeated = client.put(f"{ROOT}/profiles/writer-a", json=body)
    assert repeated.json() == saved.json()
    assert SECRET not in saved.text
    assert not list(tmp_path.glob(".*.tmp"))


@pytest.mark.parametrize(
    "change",
    [
        {"api_key": "replaced-credential-f102"},
        {"model_id": "updated-model"},
        {"base_url": "https://another.invalid/v1"},
        {"request_options": {"max_tokens": 2048, "reasoning_effort": "low"}},
    ],
)
def test_connection_and_credential_changes_invalidate_evidence(
    client: TestClient, tmp_path: Path, change: dict[str, object]
) -> None:
    _ready(client)
    catalog = ProfileCatalog(tmp_path / "profiles.json")
    before = catalog.get_stored("writer-a")
    changed = client.put(f"{ROOT}/profiles/writer-a", json=_body(**change))
    assert changed.status_code == 200
    assert changed.json()["profiles"][0]["capability_status"] == "missing"
    assert catalog.get_stored("writer-a").capability_test is None
    if "api_key" in change:
        assert (
            before.configuration_fingerprint
            == catalog.get_stored("writer-a").configuration_fingerprint
        )
    assert client.put(f"{ROOT}/default", json={"profile_id": "writer-a"}).status_code == 422
    assert client.post(f"{ROOT}/profiles/writer-a/validate").status_code == 200


@pytest.mark.parametrize(
    "change",
    [
        {"context_window": 2048},
        {"context_window": 8000},
        {
            "max_output_tokens": 32000,
            "request_options": {"max_tokens": 32000},
            "context_window": 32256,
        },
        {"max_output_tokens": 2048.5},
        {"request_options": {"max_tokens": 1024}},
        {"request_options": {"max_tokens": True}},
        {"request_options": {"max_retries": 100}},
        {"request_options": {"extra_body": {SECRET: {"api_key": SECRET}}}},
        {"request_options": {"extra_body": [{"api_key": SECRET}]}},
        {"request_options": {"extra_body": [{"some_value": SECRET}]}},
        {"base_url": f"https://provider.invalid/v1?api_key={SECRET}"},
        {"base_url": f"https://{SECRET}@provider.invalid/v1"},
        {"api_key": {SECRET: "invalid-secret-type"}},
        {"input_price_per_million": -1},
        {"api_family": SECRET},
        {"capability_test": {"profile_fingerprint": SECRET}},
        {"configuration_fingerprint": SECRET},
        {SECRET: "unexpected-secret-bearing-field"},
    ],
)
def test_bad_settings_and_secret_bearing_validation_errors_leave_both_files_unchanged(
    client: TestClient, tmp_path: Path, caplog: pytest.LogCaptureFixture, change: dict[str, object]
) -> None:
    _ready(client)
    profile_before = (tmp_path / "profiles.json").read_bytes()
    metadata_before = (tmp_path / "metadata.json").read_bytes()
    response = client.put(f"{ROOT}/profiles/writer-a", json=_body(**change))
    assert response.status_code == 422, response.text
    assert SECRET not in response.text + caplog.text
    assert (tmp_path / "profiles.json").read_bytes() == profile_before
    assert (tmp_path / "metadata.json").read_bytes() == metadata_before


def test_new_enabled_profile_requires_key_and_rejects_client_evidence(client: TestClient) -> None:
    body = _body()
    del body["api_key"]
    assert client.put(f"{ROOT}/profiles/new", json=body).status_code == 422
    assert client.get(ROOT).json()["profiles"] == []
    body["enabled"] = False
    saved = client.put(f"{ROOT}/profiles/new", json=body)
    assert saved.status_code == 200
    assert saved.json()["profiles"][0]["has_api_key"] is False
    assert client.post(f"{ROOT}/profiles/new/validate").status_code == 422
    assert client.put(f"{ROOT}/default", json={"profile_id": "new"}).status_code == 422
    assert (
        client.post(f"{ROOT}/profiles/new/validate", json={"capability_test": SECRET}).status_code
        == 422
    )


def test_anthropic_settings_preserve_protocol_and_options(client: TestClient) -> None:
    body = _body(api_family="anthropic_messages", base_url="https://provider.invalid")
    saved = client.put(f"{ROOT}/profiles/anthropic", json=body)
    assert saved.status_code == 200, saved.text
    assert saved.json()["profiles"][0]["api_family"] == "anthropic_messages"
    assert saved.json()["profiles"][0]["request_options"] == body["request_options"]
    assert client.post(f"{ROOT}/profiles/anthropic/validate").status_code == 200


def test_incomplete_probe_evidence_does_not_become_ready(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def incomplete(profile: StoredProfile, **_kwargs: object) -> CapabilityEvidence:
        return _evidence(profile).model_copy(
            update={"capabilities": ProfileCapabilities(tool_calling=False)}
        )

    monkeypatch.setattr(settings_module, "probe_stored_profile", incomplete)
    assert client.put(f"{ROOT}/profiles/writer-a", json=_body()).status_code == 200
    before = (tmp_path / "profiles.json").read_bytes()
    assert client.post(f"{ROOT}/profiles/writer-a/validate").status_code == 422
    assert (tmp_path / "profiles.json").read_bytes() == before


@pytest.mark.parametrize("failure", [ProfileCapabilityProbeError, RuntimeError, ValueError])
def test_probe_failure_never_saves_evidence_or_exposes_error_secrets(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure: type[Exception],
) -> None:
    async def fail(*_args: object, **_kwargs: object) -> CapabilityEvidence:
        raise failure(f"provider echoed {SECRET}") from RuntimeError(SECRET)

    monkeypatch.setattr(settings_module, "probe_stored_profile", fail)
    assert client.put(f"{ROOT}/profiles/writer-a", json=_body()).status_code == 200
    before = (tmp_path / "profiles.json").read_bytes()
    response = client.post(f"{ROOT}/profiles/writer-a/validate")
    assert response.status_code == (500 if failure is RuntimeError else 422)
    assert SECRET not in response.text + caplog.text
    assert (tmp_path / "profiles.json").read_bytes() == before


@pytest.mark.parametrize(
    "change",
    [
        {"api_key": "key-changed-during-probe-09ad"},
        {"model_id": "model-changed-during-probe"},
        {"context_window": 65536},
    ],
)
def test_probe_cannot_publish_after_concurrent_profile_or_metadata_edit(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: dict[str, object]
) -> None:
    entered = threading.Event()
    release = threading.Event()

    async def delayed(profile: StoredProfile, *, require_tool_calling: bool) -> CapabilityEvidence:
        assert require_tool_calling
        entered.set()
        assert await asyncio.to_thread(release.wait, 5)
        return _evidence(profile)

    monkeypatch.setattr(settings_module, "probe_stored_profile", delayed)
    assert client.put(f"{ROOT}/profiles/writer-a", json=_body()).status_code == 200
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(client.post, f"{ROOT}/profiles/writer-a/validate")
        try:
            assert entered.wait(5)
            saved = client.put(f"{ROOT}/profiles/writer-a", json=_body(**change))
            assert saved.status_code == 200
            after_edit = (tmp_path / "profiles.json").read_bytes()
        finally:
            release.set()
        rejected = pending.result(timeout=5)
    assert rejected.status_code == 409, rejected.text
    assert (tmp_path / "profiles.json").read_bytes() == after_edit
    assert ProfileCatalog(tmp_path / "profiles.json").get_stored("writer-a").capability_test is None


@pytest.mark.parametrize("change", [{"model_id": "changed-model"}, {"display_name": "Renamed"}])
def test_interrupted_two_file_update_cannot_become_ready(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: dict[str, object]
) -> None:
    _ready(client)
    real_write = settings_module.atomic_write

    def fail_metadata(path: Path, value: bytes) -> None:
        if path.name == "metadata.json":
            raise OSError(f"storage failure with {SECRET}")
        real_write(path, value)

    monkeypatch.setattr(settings_module, "atomic_write", fail_metadata)
    response = client.put(f"{ROOT}/profiles/writer-a", json=_body(context_window=65536, **change))
    assert response.status_code == 503
    assert SECRET not in response.text
    assert client.get(ROOT).json()["profiles"][0]["capability_status"] != "ready"
    assert client.put(f"{ROOT}/default", json={"profile_id": "writer-a"}).status_code == 422
    assert ProfileCatalog(tmp_path / "profiles.json").get_stored("writer-a").capability_test is None


def test_simultaneous_profile_saves_preserve_each_other(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_write = threading.Event()
    release = threading.Event()
    real_write = settings_module.atomic_write

    def pause_first_write(path: Path, value: bytes) -> None:
        if path.name == "profiles.json" and not first_write.is_set():
            first_write.set()
            assert release.wait(5)
        real_write(path, value)

    monkeypatch.setattr(settings_module, "atomic_write", pause_first_write)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(client.put, f"{ROOT}/profiles/first", json=_body())
        try:
            assert first_write.wait(5)
            second = executor.submit(client.put, f"{ROOT}/profiles/second", json=_body())
        finally:
            release.set()
        assert first.result(timeout=5).status_code == 200
        assert second.result(timeout=5).status_code == 200
    assert [item["profile_id"] for item in client.get(ROOT).json()["profiles"]] == [
        "first",
        "second",
    ]


def test_default_rejects_disabled_stale_or_missing_metadata(
    client: TestClient, tmp_path: Path
) -> None:
    _ready(client)
    assert client.put(f"{ROOT}/profiles/writer-a", json=_body(enabled=False)).status_code == 200
    assert client.get("/api/authoring/profiles").json()[0]["capability_status"] == "stale"
    assert client.put(f"{ROOT}/default", json={"profile_id": "writer-a"}).status_code == 422
    assert client.put(f"{ROOT}/profiles/writer-a", json=_body()).status_code == 200
    metadata_path = tmp_path / "metadata.json"
    original = metadata_path.read_bytes()
    metadata = json.loads(original)
    metadata["profiles"][0]["configuration_fingerprint"] = "0" * 64
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    assert client.get(ROOT).json()["profiles"][0]["capability_status"] == "stale"
    assert client.put(f"{ROOT}/default", json={"profile_id": "writer-a"}).status_code == 422
    assert client.post(f"{ROOT}/profiles/writer-a/validate").status_code == 422
    metadata_path.write_text('{"schema_version": 1, "profiles": []}', encoding="utf-8")
    assert client.put(f"{ROOT}/default", json={"profile_id": "writer-a"}).status_code == 422
    assert client.get(ROOT).json()["profiles"][0]["context_window"] is None


def test_settings_edit_does_not_change_an_already_frozen_episode_selection(
    client: TestClient, tmp_path: Path
) -> None:
    _ready(client)
    client.put(f"{ROOT}/default", json={"profile_id": "writer-a"})
    created = client.post("/api/authoring/projects", json={"brief": "freeze a model selection"})
    resources = client.app.state.authoring_resources
    selected = asyncio.run(
        resources.profile_resolver.resolve(
            resources.store, created.json()["project_id"], WorkerRole.WRITER
        )
    )
    try:
        fingerprint = selected.snapshot.fingerprint
        response = client.put(
            f"{ROOT}/profiles/writer-a",
            json=_body(model_id="new-episode-model", api_key="next-episode-secret-9812"),
        )
        assert response.status_code == 200
        assert selected.snapshot.fingerprint == fingerprint
        assert selected.snapshot.model_id == "opaque-writer"
        assert selected.redaction_secrets == (SECRET,)
    finally:
        asyncio.run(selected.aclose())


def test_episode_preflight_waits_for_one_complete_settings_update(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ready(client)
    staged = threading.Event()
    release = threading.Event()
    real_write = settings_module.atomic_write

    def pause_after_catalog_write(path: Path, value: bytes) -> None:
        real_write(path, value)
        if path.name == "profiles.json" and not staged.is_set():
            staged.set()
            assert release.wait(5)

    monkeypatch.setattr(settings_module, "atomic_write", pause_after_catalog_write)
    resolver = client.app.state.authoring_resources.profile_resolver
    with ThreadPoolExecutor(max_workers=2) as executor:
        save = executor.submit(
            client.put,
            f"{ROOT}/profiles/writer-a",
            json=_body(display_name="Renamed writer", context_window=65536),
        )
        try:
            assert staged.wait(5)
            preflight = executor.submit(resolver.validate_bindings, {"default": "writer-a"})
            with pytest.raises(TimeoutError):
                preflight.result(timeout=0.2)
        finally:
            release.set()
        assert save.result(timeout=5).status_code == 200
        assert preflight.result(timeout=5) is None


def test_paused_project_switch_preserves_role_fallbacks_and_validates_effective_binding(
    client: TestClient,
) -> None:
    for profile_id in ("primary", "replacement", "role-fallback"):
        _ready(client, profile_id)
    bindings = {
        "default_profile_id": "primary",
        "architect_profile_id": "primary",
        "writer_profile_id": "primary",
        "editor_profile_id": "replacement",
        "arbiter_profile_id": "primary",
        # Every role override wins over this generic fallback, so the generic
        # primary/fallback equality must not reject an otherwise usable binding.
        "fallback_profile_id": "primary",
        "architect_fallback_profile_id": "role-fallback",
        "writer_fallback_profile_id": "role-fallback",
        "editor_fallback_profile_id": "role-fallback",
        "arbiter_fallback_profile_id": "role-fallback",
    }
    created = client.post(
        "/api/authoring/projects", json={"brief": "preserve paused assignments", **bindings}
    )
    assert created.status_code == 201, created.text
    project_id = created.json()["project_id"]
    previous = created.json()["profile_bindings"]
    assert previous["fallback:writer"] == "role-fallback"
    assert client.post(f"/api/authoring/projects/{project_id}/pause").status_code == 200
    updated = client.put(
        f"/api/authoring/projects/{project_id}/profiles",
        json={**bindings, "writer_profile_id": "replacement"},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["project_id"] == project_id
    assert updated.json()["status"] == "paused"
    assert updated.json()["profile_bindings"] == {**previous, "writer": "replacement"}
    invalid = client.put(
        f"/api/authoring/projects/{project_id}/profiles",
        json={**bindings, "writer_profile_id": "role-fallback"},
    )
    assert invalid.status_code == 422
    assert "writer" in invalid.json()["error"]["message"]
    assert client.get(f"/api/authoring/projects/{project_id}").json()["profile_bindings"] == {
        **previous,
        "writer": "replacement",
    }
