import json
from pathlib import Path

from app.api import exports as exports_api
from app.api import profiles as profiles_api
from app.api import projects as projects_api
from app.api import runs as runs_api
from app.api import setup as setup_api
from app.core import config as core_config
from app.core import paths as core_paths
from app.harness.agents import evaluator as agent_evaluator
from app.harness.agents import loop_runners
from app.harness.agents import runtime as agent_runtime
from app.llm.gateway import ChatRequest, ChatResult, ToolCall
from app.schemas.profiles import LlmProfileUpsert
from app.schemas.projects import CreateProjectRequest
from app.schemas.runs import RunAdvanceRequest
from app.schemas.setup import SetupApprovalRequest, SetupTurnRequest
from app.storage import profiles as profile_storage
from app.storage import projects as project_storage
from app.storage.events import read_events
from app.storage.json_files import read_json


def test_local_happy_path_creates_writes_commits_and_exports(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _isolate_runtime_paths(tmp_path, monkeypatch)
    monkeypatch.setattr(agent_runtime, "call_llm", _fixture_agent_call_llm)
    monkeypatch.setattr(agent_evaluator, "call_llm", _fixture_agent_call_llm)
    monkeypatch.setattr(loop_runners, "require_harness_capabilities", lambda _profile: None)
    monkeypatch.setattr(profile_storage, "require_harness_capabilities", lambda _profile: None)

    project = projects_api.create_project(
        CreateProjectRequest(operation_mode="full_auto")
    )
    profile = profiles_api.upsert_profile(
        LlmProfileUpsert(
            id="main",
            name="Fixture Provider",
            protocol="openai-compatible",
            base_url="https://api.example.com/v1",
            api_key="secret-key",
            model="fixture-model",
        )
    )
    profiles_api.select_profile(profile.id)

    setup_api.continue_setup_discussion(
        SetupTurnRequest(
            message=(
                "Build a fair mystery about earned trust and visible costs. "
                "Use 《Fixture Novel》 as the formal title."
            )
        )
    )
    candidate_state = setup_api.prepare_setup_review()
    assert candidate_state.candidate is not None
    setup_state = setup_api.approve_setup(
        SetupApprovalRequest(
            candidate_revision=candidate_state.candidate.revision,
            title="Fixture Novel",
        )
    )

    run_result = runs_api.start_run(RunAdvanceRequest(stop_after_chapter=True))
    export_result = exports_api.export_current_manuscript()

    project_path = Path(project.path)
    chapter_path = project_path / "chapters" / "chapter-001"
    characters = read_json(project_path / "canon" / "characters.json")
    manuscript = (project_path / export_result["artifact_path"]).read_text(encoding="utf-8")
    metadata = read_json(project_path / "project.json")
    events = read_events(project_path)

    assert setup_state.approved is True
    assert profile.has_api_key is True
    assert run_result["status"] == "idle"
    assert export_result["artifact_path"] == "exports/manuscript.md"
    assert "earned trust" in (project_path / "book" / "settings.md").read_text(
        encoding="utf-8"
    )
    assert (project_path / "arcs" / "arc-001" / "plan.md").exists()
    assert (chapter_path / "context_snapshot.json").exists()
    assert (chapter_path / "goal.md").exists()
    assert (chapter_path / "draft.md").exists()
    assert (chapter_path / "observations.json").exists()
    assert (chapter_path / "review.md").exists()
    assert (chapter_path / "verification.json").exists()
    assert (chapter_path / "final.md").exists()
    assert (chapter_path / "candidate_state_patch.json").exists()
    assert (chapter_path / "committed_state_patch.json").exists()
    assert any(
        item.get("semantic_state") == "The protagonist trusts companions."
        for item in characters["items"].values()
    )
    assert manuscript == "The protagonist trusts companions after the trial.\n"
    assert metadata["active_profile_id"] == "main"
    assert any(event.kind == "llm_output_delta" for event in events)
    assert events[-1].kind == "export_completed"
    assert not _project_tree_contains(project_path, "secret-key")
    assert not _project_tree_contains(project_path, "https://api.example.com/v1")


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


def _fixture_agent_call_llm(_profile: object, request: ChatRequest) -> ChatResult:
    if request.response_schema is not None:
        evaluation_input = json.loads(request.messages[1].content)
        dimensions = evaluation_input["rubric"]
        payload = {
            "outcome": "pass",
            "contract_satisfied": True,
            "summary": "The fixed candidate satisfies its contract.",
            "rubric_checks": [
                {
                    "status": "pass",
                    "evidence_hint": "The candidate satisfies the supplied contract.",
                    "explanation": "The fixture candidate satisfies this dimension.",
                }
                for _item in dimensions
            ],
            "prior_issue_checks": [],
            "new_issues": [],
            "signals": [],
            "repair_brief": None,
            "upstream_blocker": None,
        }
        return ChatResult(
            content=json.dumps(payload),
            structured_output=payload,
            finish_reason="stop",
            model_snapshot="fixture-model",
            provider_snapshot="openai-compatible",
        )

    tool_names = {tool.name for tool in request.tools}
    prior_calls = {
        call.name for message in request.messages for call in message.tool_calls
    }
    if "submit_book_discussion_update" in tool_names:
        name = "submit_book_discussion_update"
        arguments = _book_discussion_arguments()
    elif "submit_book_direction_candidate" in tool_names:
        name = "submit_book_direction_candidate"
        arguments = _book_direction_arguments()
    elif "submit_story_arc_candidate" in tool_names:
        name = "submit_story_arc_candidate"
        arguments = {
            "plan_markdown": "# Arc 1\n\nA rolling first arc focused on earned trust.",
            "target_chapter_count": 3,
            "change_summary": "Create the first rolling arc.",
        }
    elif "plan_chapter_candidate" in tool_names:
        name, arguments = _next_chapter_tool(prior_calls)
    else:
        raise AssertionError(f"Unexpected fixture request: {request}")

    call = ToolCall(
        id=f"fixture-{name}",
        name=name,
        arguments=arguments,
        raw_arguments=json.dumps(arguments),
    )
    return ChatResult(
        content="",
        tool_calls=[call],
        finish_reason="tool_call",
        model_snapshot="fixture-model",
        provider_snapshot="openai-compatible",
        usage={"input_tokens": 10, "output_tokens": 20, "total_tokens": 30},
    )


def _book_discussion_arguments() -> dict[str, object]:
    return {
        "reply": "The direction is concrete enough to review.",
        "direction_draft": _fixture_direction(),
        "discussion_summary": "A fair mystery about earned trust and visible costs.",
        "newly_confirmed_decisions": ["Fair clues", "Earned trust", "Visible costs"],
        "superseded_decisions": [],
        "unresolved_questions": [],
        "assumptions": [],
        "contradictions": [],
        "newly_selected_title": "Fixture Novel",
        "question": None,
        "suggestions": [],
        "readiness": {
            "status": "ready",
            "reason": "Stable promises and rolling freedoms are explicit.",
        },
    }


def _book_direction_arguments() -> dict[str, object]:
    return {
        "direction_markdown": _fixture_direction(),
        "constraints": {
            "must_avoid": ["No arbitrary solution."],
            "creative_freedoms": ["Choose the current arc antagonist from committed canon."],
            "open_decisions": [],
        },
        "comparison_titles": [
            {"title": "Visible Costs", "rationale": "Highlights the core promise."},
            {"title": "Earned Trust", "rationale": "Centers the emotional arc."},
        ],
        "rolling_plan_markdown": _fixture_rolling_contract(),
    }


def _next_chapter_tool(
    prior_calls: set[str],
) -> tuple[str, dict[str, object]]:
    if "plan_chapter_candidate" not in prior_calls:
        return "plan_chapter_candidate", {
            "plan_markdown": "# Chapter Goal\n\nProve earned trust.",
        }
    if "write_chapter_draft" not in prior_calls:
        return "write_chapter_draft", {
            "content": "The protagonist trusts companions after the trial.",
        }
    if "inspect_chapter_consistency" not in prior_calls:
        return "inspect_chapter_consistency", {}
    if "write_chapter_observations" not in prior_calls:
        return "write_chapter_observations", {
            "observations": {
                "events": [
                    {
                        "summary": "The protagonist chooses trust.",
                    }
                ],
                "character_changes": [],
                "relationship_changes": [],
                "world_fact_candidates": [],
                "foreshadowing_candidates": [],
            },
        }
    if "write_chapter_state_patch" not in prior_calls:
        return "write_chapter_state_patch", {
            "state_patch": {
                "operations": [
                    {
                        "change_kind": "establish",
                        "entity_kind": "character",
                        "entity_name": "protagonist",
                        "resulting_state": "The protagonist trusts companions.",
                        "evidence_hint": "trusts companions",
                        "rationale": "The candidate prose proves the trust change.",
                    }
                ],
            },
        }
    return "submit_chapter_candidate", {
        "summary": "The chapter makes the trust change visible.",
    }


def _fixture_direction() -> str:
    return (
        "# Book Direction\n\nThe novel is a grounded mystery about earned trust. Every reveal must "
        "follow visible clues and change a meaningful relationship, keeping plot knowledge and "
        "emotional consequence together. The protagonist begins capable but isolated and gains "
        "agency through difficult alliances. Victories carry durable personal costs without making "
        "hope feel false. Speculative tools cannot erase earlier choices. Later antagonists, local "
        "conflicts, and the exact final cost remain open for rolling planning from committed canon."
    )


def _fixture_rolling_contract() -> str:
    return (
        "# Rolling Story Arc Contract\n\nPlan only the current story arc from approved direction and "
        "committed canon. Give it one mystery advance, one relationship change, and one test of "
        "earned trust. After chapters commit, reconcile observations and patches before planning the "
        "next arc. Return to the book loop only when an approved highest-level decision must change."
    )


def _project_tree_contains(project_path: Path, needle: str) -> bool:
    for path in project_path.rglob("*"):
        if path.is_file() and needle in path.read_text(encoding="utf-8"):
            return True
    return False
