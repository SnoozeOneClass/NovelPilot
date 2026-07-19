import json

import pytest
from fastapi import HTTPException

from app.api import profiles as profiles_api
from app.harness.run_control import begin_active_runner, end_active_runner
from app.llm.gateway import ChatResult, ToolCall
from app.schemas.projects import ProjectMetadata
from app.schemas.profiles import LlmProfileUpsert
from app.storage.json_files import read_json, write_json
from app.storage import profiles as profile_storage


def test_profile_public_view_masks_api_key(tmp_path, monkeypatch) -> None:
    profile_path = tmp_path / "llm-profiles.local.json"
    monkeypatch.setattr(profile_storage, "LLM_PROFILES_PATH", profile_path)

    profile_storage.upsert_profile(
        LlmProfileUpsert(
            id="main",
            name="Main Provider",
            protocol="openai-compatible",
            base_url="https://api.example.com/v1",
            api_key="secret-key",
            model="example-model",
            request_options={"reasoning_effort": "high", "max_completion_tokens": 12000},
        )
    )

    public_document = profile_storage.list_public_profiles()
    stored = json.loads(profile_path.read_text(encoding="utf-8"))

    assert public_document.profiles[0].has_api_key is True
    assert public_document.profiles[0].request_options == {
        "reasoning_effort": "high",
        "max_completion_tokens": 12000,
    }
    assert not hasattr(public_document.profiles[0], "api_key")
    assert stored["profiles"][0]["api_key"] == "secret-key"
    assert stored["profiles"][0]["request_options"] == {
        "reasoning_effort": "high",
        "max_completion_tokens": 12000,
    }


def test_profile_update_preserves_existing_api_key(tmp_path, monkeypatch) -> None:
    profile_path = tmp_path / "llm-profiles.local.json"
    monkeypatch.setattr(profile_storage, "LLM_PROFILES_PATH", profile_path)

    profile_storage.upsert_profile(
        LlmProfileUpsert(
            id="main",
            name="Main Provider",
            protocol="openai-compatible",
            base_url="https://api.example.com/v1",
            api_key="secret-key",
            model="example-model",
        )
    )
    profile_storage.upsert_profile(
        LlmProfileUpsert(
            id="main",
            name="Renamed Provider",
            protocol="openai-compatible",
            base_url="https://api.example.com/v1",
            model="better-model",
        )
    )

    stored = json.loads(profile_path.read_text(encoding="utf-8"))

    assert stored["profiles"][0]["name"] == "Renamed Provider"
    assert stored["profiles"][0]["model"] == "better-model"
    assert stored["profiles"][0]["request_options"] == {}
    assert stored["profiles"][0]["api_key"] == "secret-key"


def test_profile_update_preserves_existing_request_options(tmp_path, monkeypatch) -> None:
    profile_path = tmp_path / "llm-profiles.local.json"
    monkeypatch.setattr(profile_storage, "LLM_PROFILES_PATH", profile_path)
    profile_storage.upsert_profile(
        LlmProfileUpsert(
            id="main",
            name="Main Provider",
            protocol="openai-compatible",
            base_url="https://api.example.com/v1",
            api_key="secret-key",
            model="example-model",
            request_options={"reasoning_effort": "medium"},
        )
    )

    profile_storage.upsert_profile(
        LlmProfileUpsert(
            id="main",
            name="Renamed Provider",
            protocol="openai-compatible",
            base_url="https://api.example.com/v1",
            model="better-model",
        )
    )

    stored = json.loads(profile_path.read_text(encoding="utf-8"))
    assert stored["profiles"][0]["request_options"] == {"reasoning_effort": "medium"}


def test_profile_selection_updates_active_project_metadata(tmp_path, monkeypatch) -> None:
    profile_path = tmp_path / "llm-profiles.local.json"
    project_path = tmp_path / "novel"
    project_path.mkdir()
    write_json(project_path / "project.json", ProjectMetadata(title="Novel").model_dump(mode="json"))
    monkeypatch.setattr(profile_storage, "LLM_PROFILES_PATH", profile_path)
    monkeypatch.setattr(profiles_api, "get_active_project_path", lambda: project_path)

    profile_storage.upsert_profile(
        LlmProfileUpsert(
            id="main",
            name="Main Provider",
            protocol="openai-compatible",
            base_url="https://api.example.com/v1",
            api_key="secret-key",
            model="example-model",
        )
    )
    profile_storage.upsert_profile(
        LlmProfileUpsert(
            id="alt",
            name="Alt Provider",
            protocol="anthropic-compatible",
            base_url="https://api.example.com",
            api_key="secret-key",
            model="alt-model",
        )
    )

    profiles_api.select_profile("alt")
    metadata = read_json(project_path / "project.json")

    assert metadata["active_profile_id"] == "alt"


def test_profile_selection_rejects_active_runner_before_mutating_state(
    tmp_path,
    monkeypatch,
) -> None:
    profile_path = tmp_path / "llm-profiles.local.json"
    project_path = tmp_path / "novel"
    project_path.mkdir()
    write_json(
        project_path / "project.json",
        ProjectMetadata(title="Novel", active_profile_id="main").model_dump(mode="json"),
    )
    monkeypatch.setattr(profile_storage, "LLM_PROFILES_PATH", profile_path)
    monkeypatch.setattr(profiles_api, "get_active_project_path", lambda: project_path)
    for profile_id in ["main", "alt"]:
        profile_storage.upsert_profile(
            LlmProfileUpsert(
                id=profile_id,
                name=f"{profile_id} Provider",
                protocol="openai-compatible",
                base_url="https://api.example.com/v1",
                api_key="secret-key",
                model="example-model",
            )
        )

    assert begin_active_runner(project_path) is True
    try:
        with pytest.raises(HTTPException) as caught:
            profiles_api.select_profile("alt")
    finally:
        end_active_runner(project_path)

    assert caught.value.status_code == 409
    assert profile_storage.load_profiles().active_profile_id == "main"
    assert read_json(project_path / "project.json")["active_profile_id"] == "main"


def test_profile_upsert_rejects_active_runner_before_writing_global_profile(
    tmp_path,
    monkeypatch,
) -> None:
    profile_path = tmp_path / "llm-profiles.local.json"
    project_path = tmp_path / "novel"
    project_path.mkdir()
    write_json(project_path / "project.json", ProjectMetadata(title="Novel").model_dump(mode="json"))
    monkeypatch.setattr(profile_storage, "LLM_PROFILES_PATH", profile_path)
    monkeypatch.setattr(profiles_api, "get_active_project_path", lambda: project_path)
    profile_storage.upsert_profile(
        LlmProfileUpsert(
            id="main",
            name="Main Provider",
            protocol="openai-compatible",
            base_url="https://api.example.com/v1",
            api_key="secret-key",
            model="example-model",
        )
    )

    assert begin_active_runner(project_path) is True
    try:
        with pytest.raises(HTTPException) as caught:
            profiles_api.upsert_profile(
                LlmProfileUpsert(
                    id="alt",
                    name="Alt Provider",
                    protocol="openai-compatible",
                    base_url="https://api.example.com/v1",
                    api_key="alt-secret",
                    model="alt-model",
                )
            )
    finally:
        end_active_runner(project_path)

    assert caught.value.status_code == 409
    assert [profile.id for profile in profile_storage.load_profiles().profiles] == ["main"]


def test_profile_connection_test_calls_configured_provider(tmp_path, monkeypatch) -> None:
    profile_path = tmp_path / "llm-profiles.local.json"
    monkeypatch.setattr(profile_storage, "LLM_PROFILES_PATH", profile_path)
    profile_storage.upsert_profile(
        LlmProfileUpsert(
            id="main",
            name="Main Provider",
            protocol="openai-compatible",
            base_url="https://api.example.com/v1",
            api_key="secret-key",
            model="example-model",
        )
    )
    captured_modes: list[str] = []

    def fake_call_llm(profile, request):
        captured_modes.append(request.execution_mode)
        assert request.profile_id == "main"
        assert request.stream is False
        assert request.request_options == {}
        if request.execution_mode == "tools":
            return ChatResult(
                content="",
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        name="novelpilot_capability_echo",
                        arguments={"value": "ok"},
                        raw_arguments='{"value":"ok"}',
                    )
                ],
                finish_reason="tool_call",
                model_snapshot="example-model",
                provider_snapshot="openai-compatible",
            )
        return ChatResult(
            content='{"supported":true}',
            structured_output={"supported": True},
            model_snapshot="example-model",
            provider_snapshot="openai-compatible",
        )

    monkeypatch.setattr(profiles_api, "call_llm", fake_call_llm)

    result = profiles_api.test_profile("main")

    assert captured_modes == ["tools", "structured_result"]
    assert result.ok is True
    assert result.message == "Tool Calling and Structured Output are available."
    assert result.model_snapshot == "example-model"
    assert result.capability_test.ready_for_harness is True
    stored = profile_storage.get_profile("main")
    assert stored.capability_test is not None
    assert stored.capability_test.ready_for_harness is True


def test_profile_connection_test_retries_transient_provider_failure(
    tmp_path, monkeypatch
) -> None:
    profile_path = tmp_path / "llm-profiles.local.json"
    monkeypatch.setattr(profile_storage, "LLM_PROFILES_PATH", profile_path)
    monkeypatch.setattr(profiles_api, "_PROFILE_RETRY_DELAY_SECONDS", 0)
    profile_storage.upsert_profile(
        LlmProfileUpsert(
            id="main",
            name="Main Provider",
            protocol="openai-compatible",
            base_url="https://api.example.com/v1",
            api_key="secret-key",
            model="example-model",
        )
    )
    attempts = 0

    def flaky_call_llm(_profile, request):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("OpenAI-compatible provider returned 500: upstream EOF")
        if request.execution_mode == "tools":
            return ChatResult(
                content="",
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        name="novelpilot_capability_echo",
                        arguments={"value": "ok"},
                        raw_arguments='{"value":"ok"}',
                    )
                ],
                finish_reason="tool_call",
                model_snapshot="example-model",
                provider_snapshot="openai-compatible",
            )
        return ChatResult(
            content='{"supported":true}',
            structured_output={"supported": True},
            model_snapshot="example-model",
            provider_snapshot="openai-compatible",
        )

    monkeypatch.setattr(profiles_api, "call_llm", flaky_call_llm)

    result = profiles_api.test_profile("main")

    assert attempts == 3
    assert result.calls[0]["transport_retries"] == 1
    assert result.calls[1]["transport_retries"] == 0


def test_profile_connection_test_rejects_disabled_profile(tmp_path, monkeypatch) -> None:
    profile_path = tmp_path / "llm-profiles.local.json"
    monkeypatch.setattr(profile_storage, "LLM_PROFILES_PATH", profile_path)
    profile_storage.upsert_profile(
        LlmProfileUpsert(
            id="main",
            name="Main Provider",
            protocol="openai-compatible",
            base_url="https://api.example.com/v1",
            api_key="secret-key",
            model="example-model",
            enabled=False,
        )
    )

    with pytest.raises(HTTPException) as exc:
        profiles_api.test_profile("main")

    assert exc.value.status_code == 400
    assert exc.value.detail == "Profile is disabled."


def test_profile_connection_test_maps_provider_errors(tmp_path, monkeypatch) -> None:
    profile_path = tmp_path / "llm-profiles.local.json"
    monkeypatch.setattr(profile_storage, "LLM_PROFILES_PATH", profile_path)
    profile_storage.upsert_profile(
        LlmProfileUpsert(
            id="main",
            name="Main Provider",
            protocol="openai-compatible",
            base_url="https://api.example.com/v1",
            api_key="secret-key",
            model="example-model",
        )
    )
    monkeypatch.setattr(
        profiles_api,
        "call_llm",
        lambda _profile, _request: (_ for _ in ()).throw(
            RuntimeError("provider leaked secret-key via https://api.example.com/v1")
        ),
    )

    with pytest.raises(HTTPException) as exc:
        profiles_api.test_profile("main")

    assert exc.value.status_code == 502
    assert "provider leaked" in str(exc.value.detail)
    assert "[redacted]" in str(exc.value.detail)
    assert "secret-key" not in str(exc.value.detail)
    assert "https://api.example.com/v1" not in str(exc.value.detail)
