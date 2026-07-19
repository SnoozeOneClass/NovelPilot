import json
import re
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.api import profiles as profiles_api
from app.api import projects as projects_api
from app.api import setup as setup_api
from app.core import config as core_config
from app.core import paths as core_paths
from app.harness.agents import evaluator as agent_evaluator
from app.harness.agents import runtime as agent_runtime
from app.llm.gateway import ChatRequest, ChatResult, ToolCall
from app.schemas.profiles import LlmProfileUpsert
from app.schemas.projects import CreateProjectRequest
from app.storage import profiles as profile_storage
from app.storage import projects as project_storage
from app.storage.events import read_events

from scripts.live_provider_smoke import (
    LiveProviderSmokeError,
    LiveProviderSmokeOptions,
    _test_profile,
    run_smoke,
)
from tests.test_happy_path import _fixture_agent_call_llm


def test_live_provider_smoke_runs_fixture_flow_and_restores_active_state(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _isolate_runtime_paths(tmp_path, monkeypatch)
    monkeypatch.setattr(profiles_api, "call_llm", _capability_fixture_call_llm)
    monkeypatch.setattr(agent_runtime, "call_llm", _live_fixture_agent_call_llm)
    monkeypatch.setattr(agent_evaluator, "call_llm", _fixture_agent_call_llm)

    previous_project = projects_api.create_project(
        CreateProjectRequest(operation_mode="participatory")
    )
    profile_storage.upsert_profile(
        LlmProfileUpsert(
            id="alt",
            name="Alt Provider",
            protocol="anthropic-compatible",
            base_url="https://api.example.com",
            api_key="alt-secret",
            model="alt-model",
        )
    )
    profile_storage.upsert_profile(
        LlmProfileUpsert(
            id="main",
            name="Main Provider",
            protocol="openai-compatible",
            base_url="https://api.example.com/v1",
            api_key="secret-key",
            model="fixture-model",
        )
    )

    result = run_smoke(LiveProviderSmokeOptions(profile_id="main", title="Smoke Fixture"))

    project_path = Path(result.project_path)
    report_path = project_path / "exports" / "live_smoke_report.json"

    assert result.status == "passed"
    assert result.profile_id == "main"
    assert result.run_status == "idle"
    assert result.event_count > 0
    assert (project_path / "chapters" / "chapter-001" / "final.md").exists()
    assert report_path.exists()
    assert result.artifacts["smoke_report"] == str(report_path)
    assert project_storage.get_active_project_path() == Path(previous_project.path)
    assert profile_storage.load_profiles().active_profile_id == "alt"
    assert not _project_tree_contains(project_path, "secret-key")
    assert not _project_tree_contains(project_path, "https://api.example.com/v1")


def test_live_provider_smoke_requires_configured_profile(tmp_path: Path, monkeypatch) -> None:
    _isolate_runtime_paths(tmp_path, monkeypatch)

    with pytest.raises(LiveProviderSmokeError) as exc:
        run_smoke(LiveProviderSmokeOptions(profile_id=None, title="Smoke Fixture"))

    assert exc.value.exit_code == 2
    assert "No active LLM profile" in str(exc.value)


def test_live_provider_smoke_skip_reuses_current_capability_result(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _isolate_runtime_paths(tmp_path, monkeypatch)
    monkeypatch.setattr(profiles_api, "call_llm", _capability_fixture_call_llm)
    profile_storage.upsert_profile(
        LlmProfileUpsert(
            id="main",
            name="Main Provider",
            protocol="openai-compatible",
            base_url="https://api.example.com/v1",
            api_key="secret-key",
            model="fixture-model",
        )
    )
    profiles_api.test_profile("main")

    result = _test_profile("main", True, [])

    assert result.ok is True
    assert result.capability_test.ready_for_harness is True


def test_live_provider_smoke_redacts_profile_test_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _isolate_runtime_paths(tmp_path, monkeypatch)
    monkeypatch.setattr(
        profiles_api,
        "call_llm",
        lambda _profile, _request: (_ for _ in ()).throw(
            RuntimeError("provider echoed secret-key at https://api.example.com/v1")
        ),
    )
    profile_storage.upsert_profile(
        LlmProfileUpsert(
            id="main",
            name="Main Provider",
            protocol="openai-compatible",
            base_url="https://api.example.com/v1",
            api_key="secret-key",
            model="fixture-model",
        )
    )

    with pytest.raises(LiveProviderSmokeError) as exc:
        run_smoke(LiveProviderSmokeOptions(profile_id="main", title="Smoke Failure Fixture"))

    message = str(exc.value)
    assert "Failed to test LLM profile" in message
    assert "[redacted]" in message
    assert "secret-key" not in message
    assert "https://api.example.com/v1" not in message


def test_live_provider_smoke_failure_reports_harness_context(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _isolate_runtime_paths(tmp_path, monkeypatch)
    monkeypatch.setattr(profiles_api, "call_llm", _capability_fixture_call_llm)
    monkeypatch.setattr(agent_runtime, "call_llm", _fixture_call_llm_with_bad_patch)
    monkeypatch.setattr(agent_evaluator, "call_llm", _fixture_agent_call_llm)
    profile_storage.upsert_profile(
        LlmProfileUpsert(
            id="main",
            name="Main Provider",
            protocol="openai-compatible",
            base_url="https://api.example.com/v1",
            api_key="secret-key",
            model="fixture-model",
        )
    )

    with pytest.raises(LiveProviderSmokeError) as exc:
        run_smoke(LiveProviderSmokeOptions(profile_id="main", title="Smoke Failure Fixture"))

    message = str(exc.value)
    project_path = next((tmp_path / "output").glob("project-*"))
    report_path = project_path / "exports" / "live_smoke_report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))

    assert "missing=" in message
    assert "Inspect project:" in message
    assert "Last harness event: run_failed" in message
    assert "no uniquely bindable support" in message
    assert report["status"] == "failed"
    assert report["failure"]["last_event"]["kind"] == "run_failed"
    assert report["failure"]["last_event"]["artifact_path"] is None
    assert "no uniquely bindable support" in (
        report["failure"]["last_event"]["message"]
    )
    report_payload = json.dumps(report, ensure_ascii=False)
    assert "secret-key" not in report_payload
    assert "https://api.example.com/v1" not in report_payload
    assert profile_storage.load_profiles().active_profile_id == "main"
    assert project_storage.get_active_project_path() is None


def test_live_provider_smoke_failure_reports_setup_action_errors(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _isolate_runtime_paths(tmp_path, monkeypatch)
    monkeypatch.setattr(profiles_api, "call_llm", _capability_fixture_call_llm)
    profile_storage.upsert_profile(
        LlmProfileUpsert(
            id="main",
            name="Main Provider",
            protocol="openai-compatible",
            base_url="https://api.example.com/v1",
            api_key="secret-key",
            model="fixture-model",
        )
    )

    def fail_discussion(_request):
        raise HTTPException(
            status_code=502,
            detail="setup failed with secret-key at https://api.example.com/v1",
        )

    monkeypatch.setattr(setup_api, "continue_setup_discussion", fail_discussion)

    with pytest.raises(LiveProviderSmokeError) as exc:
        run_smoke(LiveProviderSmokeOptions(profile_id="main", title="Smoke Setup Failure Fixture"))

    message = str(exc.value)
    project_path = next((tmp_path / "output").glob("project-*"))
    report_path = project_path / "exports" / "live_smoke_report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report_payload = json.dumps(report, ensure_ascii=False)
    events = read_events(project_path)

    assert "Failed to continue book direction discussion" in message
    assert "[redacted]" in message
    assert "secret-key" not in message
    assert "https://api.example.com/v1" not in message
    assert report["status"] == "failed"
    assert "Failed to continue book direction discussion" in report["failure"]["message"]
    assert "secret-key" not in report_payload
    assert "https://api.example.com/v1" not in report_payload
    assert not any(event.kind in {"run_started", "run_resumed"} for event in events)
    assert project_storage.get_active_project_path() is None


def _isolate_runtime_paths(tmp_path: Path, monkeypatch) -> None:
    config_dir = tmp_path / "config"
    output_dir = tmp_path / "output"
    active_project_path = config_dir / "active-project.local.json"
    llm_profiles_path = config_dir / "llm-profiles.local.json"

    monkeypatch.setattr(core_config, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(core_config, "OUTPUT_DIR", output_dir)
    monkeypatch.setattr(core_config, "ACTIVE_PROJECT_PATH", active_project_path)
    monkeypatch.setattr(core_config, "LLM_PROFILES_PATH", llm_profiles_path)
    monkeypatch.setattr(core_paths, "OUTPUT_DIR", output_dir)
    monkeypatch.setattr(project_storage, "OUTPUT_DIR", output_dir)
    monkeypatch.setattr(project_storage, "ACTIVE_PROJECT_PATH", active_project_path)
    monkeypatch.setattr(profile_storage, "LLM_PROFILES_PATH", llm_profiles_path)


def _capability_fixture_call_llm(
    _profile: object,
    request: ChatRequest,
) -> ChatResult:
    if any(tool.name == "novelpilot_capability_echo" for tool in request.tools):
        call = ToolCall(
            id="capability-tool",
            name="novelpilot_capability_echo",
            arguments={"value": "ok"},
            raw_arguments='{"value":"ok"}',
        )
        return ChatResult(
            content="",
            tool_calls=[call],
            finish_reason="tool_call",
            model_snapshot="fixture-model",
            provider_snapshot="openai-compatible",
        )
    if (
        request.response_schema is not None
        and request.response_schema.name == "novelpilot_capability_result"
    ):
        return ChatResult(
            content='{"supported":true}',
            structured_output={"supported": True},
            finish_reason="stop",
            model_snapshot="fixture-model",
            provider_snapshot="openai-compatible",
        )
    return _fixture_agent_call_llm(_profile, request)


def _fixture_call_llm_with_bad_patch(_profile: object, request: ChatRequest) -> ChatResult:
    result = _live_fixture_agent_call_llm(_profile, request)
    if result.tool_calls and result.tool_calls[0].name == "write_chapter_state_patch":
        call = result.tool_calls[0]
        arguments = dict(call.arguments)
        state_patch = dict(arguments["state_patch"])
        operations = [dict(item) for item in state_patch["operations"]]
        operations[0] = {
            "change_kind": "establish",
            "entity_kind": "world_fact",
            "entity_name": "月球补给站",
            "resulting_state": "月球补给站已经永久关闭。",
            "evidence_hint": "月球补给站在陨石雨中永久关闭。",
            "rationale": "陨石雨摧毁了月球补给站。",
        }
        state_patch["operations"] = operations
        arguments["state_patch"] = state_patch
        bad_call = call.model_copy(
            update={
                "arguments": arguments,
                "raw_arguments": json.dumps(arguments),
            }
        )
        return result.model_copy(update={"tool_calls": [bad_call]})
    return result


def _live_fixture_agent_call_llm(
    profile: object,
    request: ChatRequest,
) -> ChatResult:
    result = _fixture_agent_call_llm(profile, request)
    if not result.tool_calls or result.tool_calls[0].name != "submit_book_discussion_update":
        return result
    conversation = "\n".join(message.content for message in request.messages)
    title_match = re.search(r"正式书名确定为《([^》]+)》", conversation)
    if title_match is None:
        return result
    call = result.tool_calls[0]
    arguments = {**call.arguments, "newly_selected_title": title_match.group(1)}
    return result.model_copy(
        update={
            "tool_calls": [
                call.model_copy(
                    update={
                        "arguments": arguments,
                        "raw_arguments": json.dumps(arguments, ensure_ascii=False),
                    }
                )
            ]
        }
    )


def _project_tree_contains(project_path: Path, needle: str) -> bool:
    for path in project_path.rglob("*"):
        if path.is_file() and needle in path.read_text(encoding="utf-8"):
            return True
    return False
