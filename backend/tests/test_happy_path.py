import json
import re
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
    assert characters["items"]["protagonist"]["belief"] == "trusts companions"
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


def _fixture_call_llm(_profile: object, request: ChatRequest) -> ChatResult:
    action = str(request.metadata.get("atomic_action", "unknown"))
    content_by_action = {
        "continue_book_discussion": json.dumps(
            {
                "reply": "The direction is concrete enough to review, and discussion may continue.",
                "direction_draft": _fixture_direction(),
                "discussion_summary": "A fair mystery about earned trust and visible costs.",
                "confirmed_decisions": ["Fair clues", "Earned trust", "Visible costs"],
                "superseded_decisions": [],
                "unresolved_questions": [],
                "assumptions": [],
                "contradictions": [],
                "question": None,
                "suggestions": [],
                "ready_status": "ready",
                "readiness_reason": "Stable promises and rolling freedoms are explicit.",
            }
        ),
        "synthesize_book_direction": json.dumps(
            {
                "direction_markdown": _fixture_direction(),
                "constraints": {
                    "confirmed": ["Fair clues", "Earned trust", "Visible costs"],
                    "must_preserve": ["Reveals alter meaningful relationships."],
                    "must_avoid": ["No arbitrary solution."],
                    "creative_freedoms": ["Choose the current arc antagonist from committed canon."],
                    "open_decisions": [],
                },
                "confirmed_decision_coverage": [
                    {"decision": "Fair clues", "candidate_evidence": "visible clues"},
                    {"decision": "Earned trust", "candidate_evidence": "earned trust"},
                    {"decision": "Visible costs", "candidate_evidence": "personal costs"},
                ],
                "recommended_titles": [
                    {"title": "Fixture Novel", "rationale": "Names the fixture clearly."},
                    {"title": "Visible Costs", "rationale": "Highlights the core promise."},
                    {"title": "Earned Trust", "rationale": "Centers the emotional arc."},
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


def _fixture_agent_call_llm(_profile: object, request: ChatRequest) -> ChatResult:
    if request.response_schema is not None:
        evaluation_input = json.loads(request.messages[1].content)
        dimensions = evaluation_input["rubric"]["dimensions"]
        candidate_components = [
            key for key in evaluation_input["candidate"] if key != "kind"
        ]
        evidence_locator = f"candidate.{candidate_components[0]}"
        payload = {
            "schema_version": 2,
            "outcome": "pass",
            "contract_satisfied": True,
            "summary": "The fixed candidate satisfies its contract.",
            "rubric_checks": [
                {
                    "dimension_id": item["dimension_id"],
                    "status": "pass",
                    "evidence_locator": evidence_locator,
                    "explanation": "The fixture candidate satisfies this dimension.",
                }
                for item in dimensions
            ],
            "prior_issue_checks": [],
            "new_issues": [],
            "signals": [],
            "repair_brief": None,
            "repair_scope": [],
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
        arguments = _book_discussion_arguments(
            _expected_revision(request),
            _selected_title(request),
        )
    elif "submit_book_direction_candidate" in tool_names:
        name = "submit_book_direction_candidate"
        arguments = _book_direction_arguments(
            _expected_revision(request),
            _selected_title(request),
        )
    elif "submit_story_arc_candidate" in tool_names:
        name = "submit_story_arc_candidate"
        arguments = {
            "expected_revision": 0,
            "intent": "create",
            "arc_id": "arc-001",
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


def _book_discussion_arguments(
    expected_revision: int,
    selected_title: str,
) -> dict[str, object]:
    return {
        "expected_revision": expected_revision,
        "reply": "The direction is concrete enough to review.",
        "direction_draft": _fixture_direction(),
        "discussion_summary": "A fair mystery about earned trust and visible costs.",
        "confirmed_decisions": ["Fair clues", "Earned trust", "Visible costs"],
        "superseded_decisions": [],
        "unresolved_questions": [],
        "assumptions": [],
        "contradictions": [],
        "selected_title": selected_title,
        "question": None,
        "suggestions": [],
        "readiness": {
            "status": "ready",
            "reason": "Stable promises and rolling freedoms are explicit.",
        },
    }


def _book_direction_arguments(
    expected_revision: int,
    selected_title: str,
) -> dict[str, object]:
    title_decision = f"正式书名：《{selected_title}》"
    return {
        "expected_revision": expected_revision,
        "candidate_revision": 1,
        "direction_markdown": _fixture_direction(),
        "constraints": {
            "confirmed": [
                "Fair clues",
                "Earned trust",
                "Visible costs",
                title_decision,
            ],
            "must_preserve": ["Reveals alter meaningful relationships."],
            "must_avoid": ["No arbitrary solution."],
            "creative_freedoms": ["Choose the current arc antagonist from committed canon."],
            "open_decisions": [],
        },
        "confirmed_decision_coverage": [
            {"decision": "Fair clues", "candidate_evidence": "visible clues"},
            {"decision": "Earned trust", "candidate_evidence": "earned trust"},
            {"decision": "Visible costs", "candidate_evidence": "personal costs"},
            {"decision": title_decision, "candidate_evidence": selected_title},
        ],
        "recommended_titles": [
            {"title": selected_title, "rationale": "The user-confirmed formal title."},
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
            "chapter_id": "chapter-001",
            "expected_revision": 0,
            "plan_revision": 1,
            "plan_markdown": "# Chapter Goal\n\nProve earned trust.",
        }
    if "write_chapter_draft" not in prior_calls:
        return "write_chapter_draft", {
            "chapter_id": "chapter-001",
            "expected_revision": 0,
            "plan_revision": 1,
            "draft_revision": 1,
            "mode": "write",
            "content": "The protagonist trusts companions after the trial.",
        }
    if "inspect_chapter_consistency" not in prior_calls:
        return "inspect_chapter_consistency", {
            "chapter_id": "chapter-001",
            "expected_revision": 0,
            "draft_revision": 1,
        }
    if "write_chapter_observations" not in prior_calls:
        return "write_chapter_observations", {
            "chapter_id": "chapter-001",
            "expected_revision": 0,
            "draft_revision": 1,
            "observations": {
                "events": [
                    {
                        "summary": "The protagonist chooses trust.",
                        "evidence_quote": "trusts companions",
                    }
                ],
                "character_changes": [],
                "relationship_changes": [],
                "world_fact_candidates": [],
                "foreshadowing_candidates": [],
                "requires_commit": True,
            },
        }
    if "write_chapter_state_patch" not in prior_calls:
        return "write_chapter_state_patch", {
            "chapter_id": "chapter-001",
            "expected_revision": 0,
            "draft_revision": 1,
            "state_patch": {
                "operations": [
                    {
                        "op": "upsert",
                        "target_file": "canon/characters.json",
                        "target_id": "protagonist",
                        "value_fields": [
                            {"key": "belief", "json_value": '"trusts companions"'}
                        ],
                        "evidence_quotes": ["trusts companions"],
                        "rationale": "The candidate prose proves the trust change.",
                    }
                ],
            },
        }
    return "submit_chapter_candidate", {
        "chapter_id": "chapter-001",
        "expected_revision": 0,
        "candidate_revision": 1,
        "plan_revision": 1,
        "draft_revision": 1,
        "summary": "The chapter makes the trust change visible.",
    }


def _expected_revision(request: ChatRequest) -> int:
    for message in reversed(request.messages):
        match = re.search(r"expected_revision[=:](\d+)", message.content)
        if match is not None:
            return int(match.group(1))
        try:
            payload = json.loads(message.content)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and isinstance(payload.get("state_revision"), int):
            return int(payload["state_revision"])
    raise AssertionError("Fixture request did not expose its Harness revision.")


def _selected_title(request: ChatRequest) -> str:
    content = "\n".join(message.content for message in request.messages)
    for pattern in (
        r'"selected_title"\s*:\s*"([^"]+)"',
        r"《([^》]+)》",
    ):
        match = re.search(pattern, content)
        if match is not None:
            return match.group(1)
    raise AssertionError("Fixture request did not expose the confirmed formal title.")


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
