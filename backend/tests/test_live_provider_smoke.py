import json
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.api import profiles as profiles_api
from app.api import projects as projects_api
from app.api import setup as setup_api
from app.core import config as core_config
from app.core import paths as core_paths
from app.harness import orchestrator
from app.harness.loops import book as book_loop
from app.llm.gateway import ChatRequest, ChatResult
from app.schemas.profiles import LlmProfileUpsert
from app.schemas.projects import CreateProjectRequest
from app.storage import profiles as profile_storage
from app.storage import projects as project_storage
from app.storage.events import read_events

from scripts.live_provider_smoke import (
    LiveProviderSmokeError,
    LiveProviderSmokeOptions,
    run_smoke,
)


def test_live_provider_smoke_runs_fixture_flow_and_restores_active_state(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _isolate_runtime_paths(tmp_path, monkeypatch)
    monkeypatch.setattr(profiles_api, "call_llm", _fixture_call_llm)
    monkeypatch.setattr(book_loop, "call_llm", _fixture_call_llm)
    monkeypatch.setattr(orchestrator, "call_llm", _fixture_call_llm)

    previous_project = projects_api.create_project(
        CreateProjectRequest(title="Existing Novel", operation_mode="participatory")
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
    monkeypatch.setattr(profiles_api, "call_llm", _fixture_call_llm_with_bad_patch)
    monkeypatch.setattr(book_loop, "call_llm", _fixture_call_llm_with_bad_patch)
    monkeypatch.setattr(orchestrator, "call_llm", _fixture_call_llm_with_bad_patch)
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
    project_path = next((tmp_path / "output").glob("Smoke Failure Fixture*"))
    report_path = project_path / "exports" / "live_smoke_report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))

    assert "missing=" in message
    assert "Inspect project:" in message
    assert "Last harness event: state_patch_rejected" in message
    assert "Last artifact: chapters/chapter-001/state_patch_rejection.json" in message
    assert "State patch generator output could not be parsed as JSON." in message
    assert report["status"] == "failed"
    assert report["failure"]["last_event"]["kind"] == "state_patch_rejected"
    assert report["failure"]["last_event"]["artifact_path"] == (
        "chapters/chapter-001/state_patch_rejection.json"
    )
    assert "State patch generator output could not be parsed as JSON." in (
        report["failure"]["artifact_reasons"][0]
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
    monkeypatch.setattr(profiles_api, "call_llm", _fixture_call_llm)
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

    def fail_answer(_request):
        raise HTTPException(
            status_code=502,
            detail="setup failed with secret-key at https://api.example.com/v1",
        )

    monkeypatch.setattr(setup_api, "answer_setup_question", fail_answer)

    with pytest.raises(LiveProviderSmokeError) as exc:
        run_smoke(LiveProviderSmokeOptions(profile_id="main", title="Smoke Setup Failure Fixture"))

    message = str(exc.value)
    project_path = next((tmp_path / "output").glob("Smoke Setup Failure Fixture*"))
    report_path = project_path / "exports" / "live_smoke_report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report_payload = json.dumps(report, ensure_ascii=False)
    events = read_events(project_path)

    assert "Failed to answer book setup question" in message
    assert "[redacted]" in message
    assert "secret-key" not in message
    assert "https://api.example.com/v1" not in message
    assert report["status"] == "failed"
    assert "Failed to answer book setup question" in report["failure"]["message"]
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


def _fixture_call_llm(_profile: object, request: ChatRequest) -> ChatResult:
    action = str(request.metadata.get("atomic_action", "profile_test"))
    content_by_action = {
        "profile_test": "Profile works.",
        "personalize_setup_question": (
            '{"title":"Focused decision","prompt":"Choose the next stable book constraint.",'
            '"options":['
            '{"label":"A","description":"Keep pressure personal."},'
            '{"label":"B","description":"Keep clues visible."},'
            '{"label":"C","description":"Keep the ending hopeful."}]}'
        ),
        "plan_current_arc": "# Arc 1\n\nA rolling first arc focused on earned trust.",
        "generate_chapter_goal": (
            "# Chapter Goal\n\nProve the protagonist can trust companions without breaking continuity."
        ),
        "draft_chapter": "The protagonist trusts companions after the trial.",
        "extract_candidate_observations": (
            '{"schema_version":1,"status":"candidate","based_on":"chapters/chapter-001/draft.md",'
            '"events":[{"summary":"The protagonist chooses trust."}],'
            '"character_changes":[{"id":"protagonist","belief":"trusts companions"}],'
            '"relationship_changes":[],"world_fact_candidates":[],'
            '"foreshadowing_candidates":[],"requires_commit":true}'
        ),
        "semantic_review": (
            "# Review\n\nThe draft satisfies the chapter contract and keeps state changes explicit."
        ),
        "verify_chapter": (
            '{"goal_satisfied":true,"commit_allowed":true,"routing_decision":"commit",'
            '"signals":[{"name":"chapter_contract","status":"passed",'
            '"evidence":"The trust shift is visible in the draft."}],'
            '"reasons":[]}'
        ),
        "generate_candidate_state_patch": (
            '{"schema_version":1,"status":"candidate","based_on":{},'
            '"operations":[{"op":"upsert","target_file":"canon/characters.json",'
            '"target_id":"protagonist","expected_version":1,'
            '"value":{"belief":"trusts companions"},'
            '"evidence":[{"file":"chapters/chapter-001/final.md",'
            '"quote":"trusts companions"}],'
            '"rationale":"The committed chapter states that the protagonist trusts companions."}]}'
        ),
    }
    return ChatResult(
        content=content_by_action.get(action, f"# {action}\n"),
        model_snapshot="fixture-model",
        provider_snapshot="openai-compatible",
    )


def _fixture_call_llm_with_bad_patch(_profile: object, request: ChatRequest) -> ChatResult:
    if str(request.metadata.get("atomic_action", "")) == "generate_candidate_state_patch":
        return ChatResult(
            content="No canon changes.",
            model_snapshot="fixture-model",
            provider_snapshot="openai-compatible",
        )
    return _fixture_call_llm(_profile, request)


def _project_tree_contains(project_path: Path, needle: str) -> bool:
    for path in project_path.rglob("*"):
        if path.is_file() and needle in path.read_text(encoding="utf-8"):
            return True
    return False
