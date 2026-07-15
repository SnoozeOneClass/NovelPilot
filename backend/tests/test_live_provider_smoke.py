import json
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.api import profiles as profiles_api
from app.api import projects as projects_api
from app.api import setup as setup_api
from app.core import config as core_config
from app.core import paths as core_paths
from app.harness import orchestrator as harness_orchestrator
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
    monkeypatch.setattr(agent_runtime, "call_llm", _fixture_agent_call_llm)
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
    monkeypatch.setattr(
        harness_orchestrator,
        "_is_evidence_quote_repairable",
        lambda _reasons: False,
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
    project_path = next((tmp_path / "output").glob("project-*"))
    report_path = project_path / "exports" / "live_smoke_report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))

    assert "missing=" in message
    assert "Inspect project:" in message
    assert "Last harness event: state_patch_rejected" in message
    assert "Last artifact: chapters/chapter-001/state_patch_rejection.json" in message
    assert "not present in chapter_final" in message
    assert report["status"] == "failed"
    assert report["failure"]["last_event"]["kind"] == "state_patch_rejected"
    assert report["failure"]["last_event"]["artifact_path"] == (
        "chapters/chapter-001/state_patch_rejection.json"
    )
    assert "not present in chapter_final" in (
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


def _fixture_call_llm(_profile: object, request: ChatRequest) -> ChatResult:
    action = str(request.metadata.get("atomic_action", "profile_test"))
    content_by_action = {
        "profile_test": "Profile works.",
        "continue_book_discussion": json.dumps(
            {
                "reply": "The direction is concrete and remains open to further discussion.",
                "direction_draft": _fixture_direction(),
                "discussion_summary": "A fair near-future mystery about earned trust.",
                "confirmed_decisions": ["Fair clues", "Earned trust", "Costly hope"],
                "superseded_decisions": [],
                "unresolved_questions": [],
                "assumptions": [],
                "contradictions": [],
                "question": None,
                "suggestions": [],
                "ready_status": "ready",
                "readiness_reason": "Stable direction and rolling freedoms are explicit.",
            }
        ),
        "synthesize_book_direction": json.dumps(
            {
                "direction_markdown": _fixture_direction(),
                "constraints": {
                    "confirmed": ["Fair clues", "Earned trust", "Costly hope"],
                    "must_preserve": ["Reveals change relationships."],
                    "must_avoid": ["No arbitrary technology solution."],
                    "creative_freedoms": ["Choose local arc routes from committed canon."],
                    "open_decisions": ["The exact final loss remains open."],
                },
                "confirmed_decision_coverage": [
                    {"decision": "Fair clues", "candidate_evidence": "visible clues"},
                    {"decision": "Earned trust", "candidate_evidence": "earned trust"},
                    {"decision": "Costly hope", "candidate_evidence": "hard-won hope"},
                ],
                "recommended_titles": [
                    {"title": "Smoke Fixture", "rationale": "Names the smoke fixture."},
                    {"title": "Hard-Won Hope", "rationale": "Centers the emotional promise."},
                    {"title": "Visible Clues", "rationale": "Signals the fair mystery."},
                ],
                "rolling_plan_markdown": _fixture_rolling_contract(),
            }
        ),
        "review_book_direction": json.dumps(
            {
                "summary": "The candidate preserves confirmed intent and rolling scope.",
                "issues": [],
                "signals": ["confirmed_decisions_preserved:passed", "rolling_scope:passed"],
            }
        ),
        "plan_current_arc": json.dumps(
            {
                "plan_markdown": "# Arc 1\n\nA rolling first arc focused on earned trust.",
                "target_chapter_count": 3,
            }
        ),
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
    result = _fixture_agent_call_llm(_profile, request)
    if result.tool_calls and result.tool_calls[0].name == "submit_chapter_candidate":
        call = result.tool_calls[0]
        arguments = dict(call.arguments)
        state_patch = dict(arguments["state_patch"])
        operations = [dict(item) for item in state_patch["operations"]]
        operations[0]["evidence_quotes"] = ["quote absent from final prose"]
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


def _fixture_direction() -> str:
    return (
        "# Book Direction\n\nThe novel is a grounded near-future coastal mystery about earned "
        "trust. Every reveal must follow visible clues and alter a meaningful relationship, so plot "
        "knowledge and emotional consequence advance together. The protagonist begins capable but "
        "isolated, then gains agency through difficult alliances. Victories carry durable personal "
        "costs while preserving hard-won hope. Technology remains limited, socially consequential, "
        "and unable to erase earlier choices. Later antagonists, local conflicts, and the exact final "
        "loss remain open for rolling planning from committed canon."
    )


def _fixture_rolling_contract() -> str:
    return (
        "# Rolling Story Arc Contract\n\nPlan only the current story arc from approved direction and "
        "committed canon. Give it one mystery advance, one relationship change, and one test of "
        "earned trust. After chapters commit, reconcile observations and state patches before "
        "planning the next arc. Return to the book loop only when an approved highest-level decision "
        "must change."
    )


def _project_tree_contains(project_path: Path, needle: str) -> bool:
    for path in project_path.rglob("*"):
        if path.is_file() and needle in path.read_text(encoding="utf-8"):
            return True
    return False
