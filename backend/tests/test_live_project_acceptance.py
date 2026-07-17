from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from app.schemas.setup import (
    BookDirectionCandidate,
    BookDirectionConstraints,
    BookDirectionReview,
    BookTitleSuggestion,
    SetupReadinessSignal,
    SetupStateDocument,
    SetupSuggestion,
)
from scripts import live_project_acceptance as live_acceptance


def test_live_acceptance_case_has_a_stable_prompt_hash() -> None:
    case, prompt = live_acceptance._load_case(live_acceptance.DEFAULT_CASE_PATH)

    assert case["case_id"] == "phase16-two-chapter-normal-flow-v1"
    assert case["actor_policy_version"] == "recommended-only-v1"
    assert case["operation_mode"] == "participatory"
    assert case["first_arc"]["expected_target_chapter_count"] == 2
    assert "23:17" in prompt
    assert "0417" in prompt
    assert "林澈" in prompt
    assert "许青" in prompt


def test_live_acceptance_script_direct_entry_imports_without_starting_run() -> None:
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [
            str(root / "scripts" / "python.cmd"),
            str(root / "scripts" / "live_project_acceptance.py"),
            "--help",
        ],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert "--profile-id" in result.stdout


def test_recommended_only_actor_requires_exactly_one_public_recommendation() -> None:
    actor = live_acceptance.RecommendedOnlyActor("recommended-only-v1")
    state = SetupStateDocument(
        question="Which route?",
        suggestions=[
            SetupSuggestion(
                id="one",
                label="One",
                message="Choose one.",
                recommended=True,
            ),
            SetupSuggestion(
                id="two",
                label="Two",
                message="Choose two.",
            ),
        ],
    )

    selected = actor.select(state)

    assert selected.id == "one"
    assert actor.decisions == [
        {
            "gate": "book_question",
            "question": "Which route?",
            "question_sha256": live_acceptance._text_sha256("Which route?"),
            "suggestion_id": "one",
            "label": "One",
            "message": "Choose one.",
            "suggestion_text_sha256": live_acceptance._text_sha256("Choose one."),
            "recommended": True,
            "selection": "unique_model_recommendation",
            "api_command": "POST /api/setup/turn",
        }
    ]

    for flags in ((False, False), (True, True)):
        invalid = state.model_copy(deep=True)
        for suggestion, recommended in zip(invalid.suggestions, flags, strict=True):
            suggestion.recommended = recommended
        with pytest.raises(live_acceptance.LiveProjectAcceptanceError):
            actor.select(invalid)


def test_live_acceptance_preflight_rejects_competing_runnable_project(
    tmp_path: Path,
    monkeypatch,
) -> None:
    competitor = tmp_path / "project-competing"
    monkeypatch.setattr(
        live_acceptance.project_storage,
        "list_projects",
        lambda: [
            SimpleNamespace(
                name=competitor.name,
                path=str(competitor),
                metadata=SimpleNamespace(run_status="running"),
            )
        ],
    )
    monkeypatch.setattr(
        live_acceptance,
        "read_run_control_state",
        lambda _path: SimpleNamespace(desired_state="running"),
    )

    with pytest.raises(
        live_acceptance.LiveProjectAcceptanceError,
        match="idle RunHost queue",
    ):
        live_acceptance._assert_no_competing_runnable_projects()

    live_acceptance._assert_no_competing_runnable_projects(exclude=competitor)


def test_book_actor_uses_only_public_setup_actions(monkeypatch, tmp_path: Path) -> None:
    calls: list[tuple[str, str | int | None]] = []
    initial = SetupStateDocument()
    question = SetupStateDocument(
        question="Which title?",
        suggestions=[
            SetupSuggestion(
                id="title:1",
                label="Recommended Title",
                message="Use Recommended Title.",
                recommended=True,
            ),
            SetupSuggestion(
                id="title:2",
                label="Other Title",
                message="Use Other Title.",
            ),
        ],
    )
    ready = SetupStateDocument(
        selected_title="Recommended Title",
        title_selection_source="recommended",
        readiness=SetupReadinessSignal(status="ready", reason="Ready."),
    )
    reviewed = ready.model_copy(
        update={"candidate": _reviewed_candidate()},
        deep=True,
    )
    approved = reviewed.model_copy(
        update={"approved": True, "phase": "approved"},
        deep=True,
    )
    states = iter((question, ready))

    monkeypatch.setattr(live_acceptance.setup_api, "get_setup_state", lambda: initial)

    def continue_discussion(request):
        calls.append(("turn", request.message))
        return next(states)

    monkeypatch.setattr(
        live_acceptance.setup_api,
        "continue_setup_discussion",
        continue_discussion,
    )
    monkeypatch.setattr(
        live_acceptance.setup_api,
        "prepare_setup_review",
        lambda: calls.append(("review", None)) or reviewed,
    )
    monkeypatch.setattr(
        live_acceptance.setup_api,
        "approve_setup",
        lambda request: calls.append(("approve", request.candidate_revision)) or approved,
    )
    actor = live_acceptance.RecommendedOnlyActor("recommended-only-v1")

    result = live_acceptance._complete_book_setup(
        actor,
        "Creator brief.",
        tmp_path,
        [],
    )

    assert result.approved is True
    assert calls == [
        ("turn", "Creator brief."),
        ("turn", "Use Recommended Title."),
        ("review", None),
        ("approve", 1),
    ]
    assert actor.decisions[-1]["selection"] == "exact_reviewed_candidate"
    assert actor.decisions[-1]["api_command"] == "POST /api/setup/approve"


def test_live_actor_and_driver_have_no_production_enablement_or_fault_seam() -> None:
    root = Path(__file__).resolve().parents[2]
    script = (root / "scripts" / "live_project_acceptance.py").read_text(
        encoding="utf-8"
    )
    production = "\n".join(
        path.read_text(encoding="utf-8")
        for base in (root / "backend" / "app", root / "frontend" / "src")
        for path in base.rglob("*")
        if path.is_file() and path.suffix in {".py", ".ts", ".tsx"}
    )

    assert "RecommendedOnlyActor" not in production
    assert "recommended-only-v1" not in production
    for forbidden in (
        "monkeypatch",
        "fault_inject",
        "HarnessOrchestrator",
        "AgentRuntime",
        "call_llm",
        "write_project_metadata",
        "set_run_intent",
        "write_run_control_state",
    ):
        assert forbidden not in script


def _reviewed_candidate() -> BookDirectionCandidate:
    return BookDirectionCandidate(
        revision=1,
        direction_markdown="# Direction\n",
        constraints=BookDirectionConstraints(),
        confirmed_decision_coverage=[],
        recommended_titles=[
            BookTitleSuggestion(title=f"Title {index}", rationale="A rationale.")
            for index in range(1, 4)
        ],
        rolling_plan_markdown="# Rolling plan\n",
        review=BookDirectionReview(status="passed", summary="Passed."),
        direction_path="book/reviews/revision-0001/direction.md",
        constraints_path="book/reviews/revision-0001/constraints.json",
        title_suggestions_path="book/reviews/revision-0001/titles.json",
        rolling_plan_path="book/reviews/revision-0001/rolling.md",
        verification_path="book/reviews/revision-0001/verification.json",
        profile_id="main",
        model_snapshot="fixture-model",
        review_model_snapshot="fixture-model",
    )
