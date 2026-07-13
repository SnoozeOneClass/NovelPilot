import json
from pathlib import Path

from app.api import exports as exports_api
from app.api import profiles as profiles_api
from app.api import projects as projects_api
from app.api import runs as runs_api
from app.api import setup as setup_api
from app.core import config as core_config
from app.core import paths as core_paths
from app.harness import orchestrator
from app.harness.loops import book as book_loop
from app.llm.gateway import ChatRequest, ChatResult
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
    monkeypatch.setattr(book_loop, "call_llm", _fixture_call_llm)
    monkeypatch.setattr(orchestrator, "call_llm", _fixture_call_llm)

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
        SetupTurnRequest(message="Build a fair mystery about earned trust and visible costs.")
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
