import json
from itertools import product
from pathlib import Path
from typing import get_args

import pytest

from app.harness.agents.models import FailureCategory
from app.harness.flow_router import (
    RUNNING_FLOW_DECISIONS,
    RunFacts,
    route_run,
)
from app.schemas.projects import RunStatus
from app.schemas.readiness import RunNextActionId
from app.storage.readiness import (
    FAILURE_ACTION_BY_CATEGORY,
    FailureRecoveryAction,
    NormalizedFailureCategory,
)


EXPECTED_FAILURE_ACTIONS = {
    "transport_provider": "retry_provider_connection",
    "provider_auth": "retry_failed_run",
    "profile_configuration": "retry_failed_run",
    "unsupported_capability": "inspect_failure",
    "malformed_model_output": "retry_failed_run",
    "harness_conflict": "inspect_failure",
    "local_semantic": "retry_failed_run",
    "cross_loop_semantic": "inspect_failure",
    "needs_user": "inspect_failure",
    "exhausted": "inspect_failure",
    "cancelled": "inspect_failure",
    "harness_failure": "inspect_failure",
}
EXPECTED_BOUNDARIES = {
    "request",
    "agent_response",
    "tool_persistence",
    "candidate_finalization",
    "evaluation_response",
    "evaluation_persistence",
    "projection",
    "promotion_prepare",
    "canon_update",
    "promotion_commit",
    "run_intent",
    "host_claim",
    "stale_cleanup",
}


@pytest.mark.parametrize(
    ("category", "expected_action"),
    sorted(EXPECTED_FAILURE_ACTIONS.items()),
)
def test_normalized_failure_category_has_one_recovery_action(
    category: str,
    expected_action: str,
) -> None:
    assert FAILURE_ACTION_BY_CATEGORY[category] == expected_action


def test_failure_action_table_is_exhaustive_for_typed_categories() -> None:
    normalized_categories = set(get_args(NormalizedFailureCategory))
    agent_categories = set(get_args(FailureCategory))
    recovery_actions = set(get_args(FailureRecoveryAction))
    readiness_actions = set(get_args(RunNextActionId))

    assert normalized_categories == set(EXPECTED_FAILURE_ACTIONS)
    assert set(FAILURE_ACTION_BY_CATEGORY) == normalized_categories
    assert agent_categories <= normalized_categories
    assert set(FAILURE_ACTION_BY_CATEGORY.values()) == recovery_actions
    assert recovery_actions <= readiness_actions


def test_flow_router_table_covers_every_status_and_control_combination() -> None:
    statuses = set(get_args(RunStatus))
    assert set(RUNNING_FLOW_DECISIONS) == statuses

    for desired_state, project_status, provider_retry_due in product(
        ("stopped", "running"),
        sorted(statuses),
        (False, True),
    ):
        decision = route_run(
            RunFacts(
                desired_state=desired_state,
                project_status=project_status,
                provider_retry_due=provider_retry_due,
            )
        )
        if desired_state == "stopped":
            assert decision == "stop"
        elif project_status == "waiting_for_provider" and provider_retry_due:
            assert decision == "advance"
        else:
            assert decision == RUNNING_FLOW_DECISIONS[project_status]


def test_failure_coverage_manifest_owns_every_category_and_boundary() -> None:
    tests_root = Path(__file__).parent
    manifest = json.loads(
        (tests_root / "fixtures" / "failure_coverage_manifest.json").read_text(
            encoding="utf-8"
        )
    )
    rows = manifest["rows"]
    known_test_source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in tests_root.glob("test_*.py")
    )

    assert manifest["schema_version"] == 1
    assert manifest["external_calls_allowed"] is False
    assert set(manifest["categories"]) == set(EXPECTED_FAILURE_ACTIONS)
    assert set(manifest["boundaries"]) == EXPECTED_BOUNDARIES
    assert {row["category"] for row in rows} == set(manifest["categories"])
    assert {row["boundary"] for row in rows} == EXPECTED_BOUNDARIES
    assert {row["owner"] for row in rows} >= {
        "gateway",
        "profile",
        "harness",
        "book",
        "story_arc",
        "chapter",
        "evaluator",
        "control_plane",
    }
    assert len({row["id"] for row in rows}) == len(rows)
    for row in rows:
        assert row["permitted_action"] in set(get_args(RunNextActionId))
        assert row["retry_scope"]
        assert row["terminal_class"]
        assert row["invariants"]
        test_id = row.get("test_id")
        unsupported = row.get("unsupported_rationale")
        assert bool(test_id) != bool(unsupported)
        if test_id:
            assert f"def {test_id}(" in known_test_source
