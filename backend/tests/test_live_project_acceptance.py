from contextlib import nullcontext
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
from app.schemas.events import HarnessEvent
from scripts import live_project_acceptance as live_acceptance
from app.storage.events import append_event
from app.storage.json_files import write_json


def test_live_acceptance_case_has_a_stable_prompt_hash() -> None:
    case, prompt = live_acceptance._load_case(live_acceptance.DEFAULT_CASE_PATH)

    assert case["case_id"] == "phase16-two-chapter-normal-flow-v2"
    assert case["actor_policy_version"] == "recommended-only-v1"
    assert case["operation_mode"] == "participatory"
    assert case["first_arc"]["expected_target_chapter_count"] == 2
    assert "23:17" in prompt
    assert "0417" in prompt
    assert "林澈" in prompt
    assert "许青" in prompt


def test_natural_arc_case_reuses_the_real_benchmark_mother_prompt() -> None:
    path = (
        live_acceptance.ROOT_DIR
        / "scripts"
        / "live_acceptance_cases"
        / "benchmark_mother_natural_arc.json"
    )
    case, prompt = live_acceptance._load_case(path)

    assert case["case_id"] == "benchmark-mother-natural-first-arc-v1"
    assert case["first_arc"]["minimum_target_chapter_count"] == 6
    assert "《退潮前的十一分钟》" in prompt
    assert "预计写4～5万字、18～22章" in prompt
    assert "封站清点的第一晚" in prompt


def test_stable_fact_check_accepts_equivalent_time_notation() -> None:
    required = {
        "林澈": ("林澈",),
        "23:17": ("23:17", "二十三点十七分", "二十三时十七分"),
        "0417": ("0417",),
    }

    assert live_acceptance._missing_stable_facts(
        "林澈在二十三点十七分发现了编号 0417。",
        required,
    ) == []
    assert live_acceptance._missing_stable_facts(
        "林澈在 23：17 发现了编号 0417。",
        required,
    ) == []


def test_stable_fact_check_still_rejects_a_semantically_different_time() -> None:
    required = {
        "23:17": ("23:17", "二十三点十七分", "二十三时十七分"),
    }

    assert live_acceptance._missing_stable_facts(
        "事件发生在二十三点十八分。",
        required,
    ) == ["23:17"]


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


def test_live_acceptance_cleanup_stops_and_pauses_failed_isolated_project(
    tmp_path: Path,
    monkeypatch,
) -> None:
    metadata = SimpleNamespace(run_status="running")
    desired_states: list[tuple[str, bool]] = []

    monkeypatch.setattr(
        live_acceptance,
        "set_run_intent",
        lambda _path, *, desired_state, clear_provider_wait: desired_states.append(
            (desired_state, clear_provider_wait)
        ),
    )
    monkeypatch.setattr(
        live_acceptance.project_storage,
        "project_metadata_lock",
        lambda _path: nullcontext(),
    )
    monkeypatch.setattr(
        live_acceptance.project_storage,
        "read_project_metadata",
        lambda _path: metadata,
    )
    monkeypatch.setattr(
        live_acceptance.project_storage,
        "write_project_metadata",
        lambda _path, value: setattr(metadata, "run_status", value.run_status),
    )
    monkeypatch.setattr(
        live_acceptance,
        "read_run_control_state",
        lambda _path: SimpleNamespace(desired_state="stopped"),
    )

    live_acceptance._request_acceptance_project_stop(tmp_path)
    assert metadata.run_status == "running"
    live_acceptance._finalize_acceptance_project_stop(tmp_path)

    assert desired_states == [("stopped", True)]
    assert metadata.run_status == "paused"


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
            "write_run_control_state",
        ):
        assert forbidden not in script
    assert "_request_acceptance_project_stop(project_path)" in script
    assert 'desired_state="stopped"' in script


def test_live_report_aggregates_agent_evaluator_and_capability_usage(
    tmp_path: Path,
) -> None:
    write_json(
        tmp_path / "chapters/chapter-001/agent/a/a1/telemetry.json",
        {
            "activation_id": "a1",
            "candidate_run_id": "run-1",
            "role": "chapter",
            "phase": "chapter",
            "llm_calls": 2,
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "prompt_tokens_details": {"cached_tokens": 40},
                "total_tokens": 120,
            },
        },
    )
    write_json(
        tmp_path / "chapters/chapter-001/agent/a/a1/evaluation.json",
        {
            "evaluation_mode": "repair_verification",
            "telemetry": {
                "attempts": [
                    {
                        "call_type": "initial",
                        "usage": {
                            "prompt_tokens": 50,
                            "completion_tokens": 10,
                            "total_tokens": 60,
                        },
                        "usage_available": True,
                    },
                    {
                        "call_type": "validation_repair",
                        "usage": {
                            "prompt_tokens": 60,
                            "completion_tokens": 10,
                            "total_tokens": 70,
                        },
                        "usage_available": True,
                    },
                ]
            },
        },
    )
    report = live_acceptance._build_model_usage_report(
        tmp_path,
        {
            "calls": [
                {
                    "call_type": "tool_calling",
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 2,
                        "total_tokens": 12,
                    },
                    "usage_available": True,
                }
            ]
        },
    )

    assert report["usage_complete"] is True
    assert report["totals"] == {
        "prompt_tokens": 220,
        "completion_tokens": 42,
        "cached_tokens": 40,
        "total_tokens": 262,
    }
    call_types = {
        item["call_type"] for item in report["by_role_loop_phase_call_type"]
    }
    assert call_types == {
        "agent_turn",
        "evaluator_initial",
        "evaluator_validation_repair",
        "tool_calling",
    }


def test_live_recovery_ledger_records_semantic_and_activation_revisions(
    tmp_path: Path,
) -> None:
    for activation_id, started_at, semantic_revisions in (
        ("a1", "2026-01-01T00:00:00Z", 0),
        ("a2", "2026-01-01T00:01:00Z", 1),
    ):
        write_json(
            tmp_path / f"arcs/arc-001/agent/a/{activation_id}/telemetry.json",
            {
                "activation_id": activation_id,
                "candidate_run_id": "run-1",
                "role": "story_arc",
                "phase": "planning",
                "started_at": started_at,
                "outcome": "candidate",
                "llm_calls": 1,
                "activation_tool_schema_repairs": 0,
                "activation_transport_retries": 0,
                "candidate_budgets": {
                    "used_semantic_revisions": semantic_revisions
                },
                "usage": {"total_tokens": 100},
            },
        )
    write_json(
        tmp_path / "arcs/arc-001/agent/repair-chain.json",
        {
            "candidate_run_id": "run-1",
            "entries": [
                {
                    "activation_id": "a1",
                    "candidate_artifact_id": "candidate-1",
                    "open_issue_ids": ["issue-1"],
                },
                {
                    "activation_id": "a2",
                    "candidate_artifact_id": "candidate-2",
                    "open_issue_ids": [],
                },
            ],
        },
    )

    report = live_acceptance._build_recovery_ledger(tmp_path, checkpoints=[])

    assert report["counts"]["semantic_revision"] == 1
    assert report["counts"]["activation_restart_resume"] == 1
    semantic = next(
        item for item in report["entries"] if item["category"] == "semantic_revision"
    )
    assert semantic["reason_code"] == "evaluator_local_repair"
    assert semantic["model_reinvoked"] is True
    assert semantic["extra_tokens"] == 100


def test_live_recovery_ledger_records_profile_capability_transport_retries(
    tmp_path: Path,
) -> None:
    report = live_acceptance._build_recovery_ledger(
        tmp_path,
        checkpoints=[],
        profile_test={
            "calls": [
                {
                    "call_type": "tool_calling",
                    "transport_retries": 2,
                }
            ]
        },
    )

    assert report["counts"]["transport_retry"] == 2
    assert report["entries"][0]["reason_code"] == (
        "profile_capability_transport_provider_retry"
    )


def test_live_series_requires_fixed_inputs_and_writes_aggregate(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls = 0

    def fake_run(_options):
        nonlocal calls
        calls += 1
        return {
            "status": "passed",
            "case": {"prompt_sha256": "prompt-hash"},
            "profile": {
                "profile_id": "main",
                "model_snapshot": "model",
                "provider_snapshot": "openai-compatible",
                "capability_test": {
                    "capability_test": {"profile_fingerprint": "profile-hash"},
                    "usage": {"total_tokens": calls},
                },
            },
            "project": {"name": f"project-{calls}", "path": f"project-{calls}"},
            "terminal": {"benchmark_fixture_status": "frozen"},
            "token_usage": {
                "totals": {
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "cached_tokens": 50,
                    "total_tokens": 120,
                }
            },
            "reset_recovery_ledger": {
                "counts": {"semantic_revision": 1}
            },
        }

    monkeypatch.setattr(live_acceptance, "run_live_project_acceptance", fake_run)
    monkeypatch.setattr(
        live_acceptance,
        "_behavior_fingerprint",
        lambda *_args: "code-hash",
    )
    monkeypatch.setattr(live_acceptance, "OUTPUT_DIR", tmp_path)
    aggregate = live_acceptance.run_live_project_acceptance_series(
        live_acceptance.LiveProjectAcceptanceOptions(profile_id="main"),
        runs=3,
    )

    assert aggregate["consecutive_passes"] == 3
    assert aggregate["token_totals"]["total_tokens"] == 360
    assert aggregate["recovery_counts"]["semantic_revision"] == 3
    assert Path(aggregate["aggregate_report_path"]).is_file()


def test_live_acceptance_json_output_is_safe_for_legacy_windows_console(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(
        live_acceptance,
        "run_live_project_acceptance",
        lambda _options: {
            "status": "passed",
            "diagnostic": "t_reported−delta",
        },
    )

    assert live_acceptance.main(["--profile-id", "test-profile", "--json"]) == 0
    output = capsys.readouterr().out
    assert "\\u2212" in output
    assert "−" not in output


def test_bug_ledger_records_harness_issue_and_terminal_failure(tmp_path: Path) -> None:
    append_event(
        tmp_path,
        HarnessEvent(
            project_id="project-diagnostic",
            kind="agent_transport_retry",
            loop_layer="book",
            atomic_action="continue_book_discussion",
            status="requested",
            message="Provider connection will be retried.",
            payload={"category": "transport_provider", "retry": 1, "limit": 3},
        ),
    )

    ledger = live_acceptance._build_bug_ledger(
        tmp_path,
        failure="Book state did not advance.",
    )

    assert ledger["entry_count"] == 2
    assert ledger["counts_by_reason"] == {
        "transport_provider": 1,
        "run_exception": 1,
    }
    assert ledger["entries"][0]["model_reinvocation_expected"] is True


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
