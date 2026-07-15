import json

from app.storage import profiles as profile_storage
from app.storage.json_files import read_json
from app.llm.gateway import ChatResult, ToolCall
from scripts import configure_llm_profile, test_llm_profile


def test_profile_cli_reads_api_key_from_env_and_selects_profile(
    tmp_path,
    monkeypatch,
) -> None:
    profile_path = tmp_path / "llm-profiles.local.json"
    monkeypatch.setattr(profile_storage, "LLM_PROFILES_PATH", profile_path)
    monkeypatch.setattr(configure_llm_profile, "LLM_PROFILES_PATH", profile_path)
    monkeypatch.setenv("NOVELPILOT_API_KEY", "secret-key")

    result = configure_llm_profile.configure_profile(
        profile_id="main",
        name="Main Provider",
        protocol="openai-compatible",
        base_url="https://api.example.com/v1",
        model="example-model",
        api_key_env="NOVELPILOT_API_KEY",
        enabled=True,
        select=True,
    )

    stored = read_json(profile_path)

    assert result.profile.id == "main"
    assert result.profile.has_api_key is True
    assert result.active_profile_id == "main"
    assert stored["active_profile_id"] == "main"
    assert stored["profiles"][0]["api_key"] == "secret-key"


def test_profile_cli_update_preserves_existing_api_key(
    tmp_path,
    monkeypatch,
) -> None:
    profile_path = tmp_path / "llm-profiles.local.json"
    monkeypatch.setattr(profile_storage, "LLM_PROFILES_PATH", profile_path)
    monkeypatch.setattr(configure_llm_profile, "LLM_PROFILES_PATH", profile_path)
    monkeypatch.setenv("NOVELPILOT_API_KEY", "secret-key")
    configure_llm_profile.configure_profile(
        profile_id="main",
        name="Main Provider",
        protocol="openai-compatible",
        base_url="https://api.example.com/v1",
        model="example-model",
        api_key_env="NOVELPILOT_API_KEY",
        request_options={"reasoning_effort": "high"},
        enabled=True,
        select=True,
    )

    result = configure_llm_profile.configure_profile(
        profile_id="main",
        name="Renamed Provider",
        protocol="openai-compatible",
        base_url="https://api.example.com/v1",
        model="better-model",
        api_key_env=None,
        enabled=True,
        select=False,
    )

    stored = read_json(profile_path)

    assert result.profile.name == "Renamed Provider"
    assert result.profile.model == "better-model"
    assert result.profile.request_options == {"reasoning_effort": "high"}
    assert stored["profiles"][0]["api_key"] == "secret-key"
    assert stored["profiles"][0]["request_options"] == {"reasoning_effort": "high"}


def test_profile_cli_accepts_arbitrary_request_options_json(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    profile_path = tmp_path / "llm-profiles.local.json"
    monkeypatch.setattr(profile_storage, "LLM_PROFILES_PATH", profile_path)
    monkeypatch.setattr(configure_llm_profile, "LLM_PROFILES_PATH", profile_path)
    monkeypatch.setenv("NOVELPILOT_API_KEY", "secret-key")

    exit_code = configure_llm_profile.main(
        [
            "--id",
            "main",
            "--name",
            "Main Provider",
            "--protocol",
            "anthropic-compatible",
            "--base-url",
            "https://api.example.com",
            "--model",
            "example-model",
            "--api-key-env",
            "NOVELPILOT_API_KEY",
            "--request-options-json",
            '{"max_tokens":24000,"thinking":{"type":"enabled","budget_tokens":8000}}',
            "--json",
        ]
    )
    output = capsys.readouterr().out
    payload = json.loads(output)

    assert exit_code == 0
    assert payload["profile"]["request_options"] == {
        "max_tokens": 24000,
        "thinking": {"type": "enabled", "budget_tokens": 8000},
    }


def test_profile_cli_json_output_does_not_expose_api_key(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    profile_path = tmp_path / "llm-profiles.local.json"
    monkeypatch.setattr(profile_storage, "LLM_PROFILES_PATH", profile_path)
    monkeypatch.setattr(configure_llm_profile, "LLM_PROFILES_PATH", profile_path)
    monkeypatch.setenv("NOVELPILOT_API_KEY", "secret-key")

    exit_code = configure_llm_profile.main(
        [
            "--id",
            "main",
            "--name",
            "Main Provider",
            "--protocol",
            "openai-compatible",
            "--base-url",
            "https://api.example.com/v1",
            "--model",
            "example-model",
            "--api-key-env",
            "NOVELPILOT_API_KEY",
            "--select",
            "--json",
        ]
    )
    output = capsys.readouterr().out
    payload = json.loads(output)

    assert exit_code == 0
    assert "secret-key" not in output
    assert payload["profile"]["has_api_key"] is True
    assert "api_key" not in payload["profile"]


def test_profile_cli_fails_when_api_key_env_is_blank(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    profile_path = tmp_path / "llm-profiles.local.json"
    monkeypatch.setattr(profile_storage, "LLM_PROFILES_PATH", profile_path)
    monkeypatch.setattr(configure_llm_profile, "LLM_PROFILES_PATH", profile_path)
    monkeypatch.setenv("NOVELPILOT_API_KEY", " ")

    exit_code = configure_llm_profile.main(
        [
            "--id",
            "main",
            "--name",
            "Main Provider",
            "--protocol",
            "openai-compatible",
            "--base-url",
            "https://api.example.com/v1",
            "--model",
            "example-model",
            "--api-key-env",
            "NOVELPILOT_API_KEY",
            "--json",
        ]
    )
    output = capsys.readouterr().out

    assert exit_code == 1
    assert "Environment variable is not set or is blank" in output


def test_profile_test_cli_uses_active_profile_and_hides_secrets(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    profile_path = tmp_path / "llm-profiles.local.json"
    monkeypatch.setattr(profile_storage, "LLM_PROFILES_PATH", profile_path)
    monkeypatch.setattr(configure_llm_profile, "LLM_PROFILES_PATH", profile_path)
    monkeypatch.setenv("NOVELPILOT_API_KEY", "secret-key")
    configure_llm_profile.configure_profile(
        profile_id="main",
        name="Main Provider",
        protocol="openai-compatible",
        base_url="https://api.example.com/v1",
        model="example-model",
        api_key_env="NOVELPILOT_API_KEY",
        enabled=True,
        select=True,
    )

    def fake_call_llm(_profile, request):
        if request.tools:
            return ChatResult(
                content="",
                tool_calls=[
                    ToolCall(
                        id="capability-tool",
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

    monkeypatch.setattr(test_llm_profile.profiles_api, "call_llm", fake_call_llm)

    exit_code = test_llm_profile.main(["--json"])
    output = capsys.readouterr().out
    payload = json.loads(output)

    assert exit_code == 0
    assert payload["ok"] is True
    assert payload["profile_id"] == "main"
    assert "secret-key" not in output
    assert "https://api.example.com/v1" not in output
    assert payload["message"] == "Tool Calling and Structured Output are available."


def test_profile_test_cli_redacts_provider_errors(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    profile_path = tmp_path / "llm-profiles.local.json"
    monkeypatch.setattr(profile_storage, "LLM_PROFILES_PATH", profile_path)
    monkeypatch.setattr(configure_llm_profile, "LLM_PROFILES_PATH", profile_path)
    monkeypatch.setenv("NOVELPILOT_API_KEY", "secret-key")
    configure_llm_profile.configure_profile(
        profile_id="main",
        name="Main Provider",
        protocol="openai-compatible",
        base_url="https://api.example.com/v1",
        model="example-model",
        api_key_env="NOVELPILOT_API_KEY",
        enabled=True,
        select=True,
    )
    monkeypatch.setattr(
        test_llm_profile.profiles_api,
        "call_llm",
        lambda _profile, _request: (_ for _ in ()).throw(
            RuntimeError("provider leaked secret-key via https://api.example.com/v1")
        ),
    )

    exit_code = test_llm_profile.main(["--profile-id", "main", "--json"])
    output = capsys.readouterr().out
    payload = json.loads(output)

    assert exit_code == 1
    assert payload["status"] == "failed"
    assert "secret-key" not in output
    assert "https://api.example.com/v1" not in output
    assert "[redacted]" in payload["message"]


def test_profile_test_cli_fails_without_active_profile(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    profile_path = tmp_path / "llm-profiles.local.json"
    monkeypatch.setattr(profile_storage, "LLM_PROFILES_PATH", profile_path)

    exit_code = test_llm_profile.main(["--json"])
    output = capsys.readouterr().out

    assert exit_code == 2
    assert "No active LLM profile is configured" in output
