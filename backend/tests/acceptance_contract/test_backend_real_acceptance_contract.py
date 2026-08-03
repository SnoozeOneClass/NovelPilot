from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from scripts.backend_real_acceptance import (
    DEFAULT_CASE_IDS,
    DEFAULT_PROFILE_ID,
    AcceptanceCase,
    Checkpoint,
    _assert_derived_evidence_invariants,
    _hierarchical_chain_violations,
    _hierarchical_terminal,
    _lineage_violations,
    _open_application,
    load_case,
    read_authority_evidence,
)


pytestmark = pytest.mark.acceptance_contract


def test_default_real_scenarios_cover_s0_through_s5_without_internal_inputs() -> None:
    cases = [load_case(case_id)[0] for case_id in DEFAULT_CASE_IDS]

    assert {scenario_id for case in cases for scenario_id in case.scenario_ids} == {
        "S1",
        "S2",
        "S3",
        "S4",
        "S5",
    }
    assert DEFAULT_PROFILE_ID == "jemmy-gpt-5.4-mini"
    forbidden_input_keys = {
        "task_id",
        "attempt_id",
        "book_baseline_id",
        "arc_baseline_id",
        "chapter_baseline_id",
        "result_json",
        "provider_response",
    }
    for case in cases:
        serialized = case.model_dump(mode="json")
        assert forbidden_input_keys.isdisjoint(serialized)
        assert case.creator_brief.strip()
        if case.feedback_after_first_chapter is not None:
            assert case.feedback_after_first_chapter.content.strip()
    restart_case = load_case("base-vertical-v1")[0]
    assert restart_case.terminal == "continued_after_restart"
    assert restart_case.restart_after_first_chapter is True


def test_hierarchical_case_requires_public_feedback_and_explicit_terminal() -> None:
    payload = load_case("hierarchical-pressure-v1")[0].model_dump(mode="json")
    payload["feedback_after_first_chapter"] = None

    with pytest.raises(ValidationError):
        AcceptanceCase.model_validate(payload)


def test_baseline_lineage_oracle_checks_contiguous_parent_chain() -> None:
    valid = [
        {
            "id": "baseline-1",
            "chapter_id": "chapter-1",
            "baseline_version": 1,
            "parent_baseline_id": None,
        },
        {
            "id": "baseline-2",
            "chapter_id": "chapter-1",
            "baseline_version": 2,
            "parent_baseline_id": "baseline-1",
        },
    ]
    invalid = [
        *valid,
        {
            "id": "baseline-4",
            "chapter_id": "chapter-1",
            "baseline_version": 4,
            "parent_baseline_id": "baseline-2",
        },
    ]

    assert _lineage_violations(valid, "chapter_id") == []
    assert _lineage_violations(invalid, "chapter_id")


def test_evidence_only_repair_oracle_binds_back_to_frozen_prose() -> None:
    case = load_case("derived-evidence-v1")[0]
    evidence = {
        "tasks": [
            {
                "id": "repair-task",
                "task_kind": "chapter.repair.observation",
                "delivery_state": "applied",
                "chapter_id": "chapter-1",
                "source_chapter_candidate_review_id": "review-1",
            }
        ],
        "chapter_reviews": [
            {
                "id": "review-1",
                "submission_id": "submission-1",
            }
        ],
        "chapter_submissions": [
            {
                "id": "submission-1",
                "draft_ref_id": "frozen-prose-ref",
            }
        ],
        "chapter_baselines": [
            {
                "chapter_id": "chapter-1",
                "prose_ref_id": "frozen-prose-ref",
            }
        ],
    }

    checks, outcome = _assert_derived_evidence_invariants(
        case=case,
        evidence=evidence,
    )

    assert outcome == "evidence_only_repair_exercised"
    assert checks == [
        {
            "id": "derived_evidence_authority",
            "ok": True,
            "detail": "evidence-only repair preserved the frozen prose source",
        }
    ]


def test_evidence_only_repair_oracle_accepts_a_correct_first_pass() -> None:
    case = load_case("derived-evidence-v1")[0]

    checks, outcome = _assert_derived_evidence_invariants(
        case=case,
        evidence={"tasks": []},
    )

    assert outcome == "evidence_only_repair_not_needed"
    assert checks == [
        {
            "id": "derived_evidence_authority",
            "ok": True,
            "detail": "first-pass derived evidence required no evidence-only repair",
        }
    ]


def test_hierarchical_oracle_requires_one_traceable_feedback_to_book_chain() -> None:
    evidence = {
        "feedback": [
            {
                "id": "feedback-1",
                "feedback_kind": "unsolicited",
                "route_layer": "chapter",
                "status": "applied",
            }
        ],
        "tasks": [
            {
                "id": "evaluate-chapter-1",
                "source_feedback_id": "feedback-1",
            },
            {
                "id": "evaluate-book-parent-1",
                "task_kind": "evaluate.book_parent_contract",
                "scope_layer": "book",
                "arc_baseline_id": None,
                "subject_arc_baseline_id": "arc-v1",
                "source_book_parent_review_id": None,
            },
        ],
        "chapter_reviews": [
            {
                "id": "chapter-review-1",
                "evaluator_task_id": "evaluate-chapter-1",
            }
        ],
        "chapter_arc_change_requests": [
            {
                "id": "chapter-arc-request-1",
                "source_review_id": "chapter-review-1",
            }
        ],
        "arc_parent_reviews": [
            {
                "id": "arc-review-1",
                "request_id": "chapter-arc-request-1",
                "target_arc_baseline_id": "arc-v1",
            }
        ],
        "arc_book_change_requests": [
            {
                "id": "arc-book-request-1",
                "source_arc_parent_review_id": "arc-review-1",
            }
        ],
        "book_parent_reviews": [
            {
                "id": "book-review-1",
                "request_id": "arc-book-request-1",
                "source_task_id": "evaluate-book-parent-1",
                "subject_arc_baseline_id": "arc-v1",
                "correction_lineage_id": "book-lineage-1",
            }
        ],
        "arc_baselines": [
            {"id": "arc-v1", "parent_baseline_id": None},
            {"id": "arc-v2", "parent_baseline_id": "arc-v1"},
        ],
    }

    assert _hierarchical_chain_violations(evidence) == []
    evidence["book_parent_reviews"][0].update(
        {
            "disposition": "keep_book",
            "automatic_correction_round": 0,
            "predecessor_review_id": None,
        }
    )
    state = {
        "book": {"current_baseline_id": "book-v1"},
        "run": {"status": "running"},
        "commands": [],
    }
    initial = Checkpoint(
        state={"book": {"current_baseline_id": "book-v1"}},
        evidence={},
        event_types=(),
    )
    assert (
        _hierarchical_terminal(
            state=state,
            evidence=evidence,
            initial=initial,
        )
        is None
    )
    evidence["book_parent_reviews"].append(
        {
            "id": "book-review-2",
            "request_id": "arc-book-request-1",
            "source_task_id": "evaluate-book-parent-2",
            "subject_arc_baseline_id": "arc-v2",
            "disposition": "keep_book",
            "automatic_correction_round": 1,
            "predecessor_review_id": "book-review-1",
            "correction_lineage_id": "book-lineage-1",
        }
    )
    evidence["tasks"].append(
        {
            "id": "evaluate-book-parent-2",
            "task_kind": "evaluate.book_parent_contract",
            "scope_layer": "book",
            "arc_baseline_id": None,
            "subject_arc_baseline_id": "arc-v2",
            "source_book_parent_review_id": "book-review-1",
        }
    )
    assert _hierarchical_chain_violations(evidence) == []
    assert (
        _hierarchical_terminal(
            state=state,
            evidence=evidence,
            initial=initial,
        )
        == "book_baseline_kept"
    )
    evidence["book_parent_reviews"][1]["subject_arc_baseline_id"] = "arc-v1"
    evidence["tasks"][2]["subject_arc_baseline_id"] = "arc-v1"
    assert _hierarchical_chain_violations(evidence) == [
        "Book review book-review-2 round 1 subject is not its exact Arc successor"
    ]
    evidence["book_parent_reviews"] = [
        {
            "id": "unrelated-book-review",
            "request_id": "different-request",
        }
    ]
    assert _hierarchical_chain_violations(evidence) == [
        "no Book review bound to that Arc request"
    ]


def test_hierarchical_case_never_accepts_failure_pause_as_resolution() -> None:
    case = load_case("hierarchical-pressure-v1")[0]
    assert "explicit_failure_pause" not in case.accepted_hierarchical_terminals
    payload = case.model_dump(mode="json")
    payload["accepted_hierarchical_terminals"].append("explicit_failure_pause")

    with pytest.raises(ValidationError):
        AcceptanceCase.model_validate(payload)


def test_real_runner_does_not_import_or_launch_four_slot_experiment() -> None:
    source = (
        Path(__file__).resolve().parents[3] / "scripts" / "backend_real_acceptance.py"
    ).read_text(encoding="utf-8")

    assert "live_book_observation_series" not in source
    assert "experiment:live-book" not in source


def test_read_only_evidence_projection_accepts_fresh_production_schema(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "scenario.sqlite3"
    with _open_application(
        database_path=database_path,
        profile_path=tmp_path / "profiles.local.json",
        export_root=tmp_path / "exports",
    ):
        evidence = read_authority_evidence(database_path, "missing-project")

    assert all(rows == [] for rows in evidence.values())
