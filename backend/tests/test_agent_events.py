from app.harness.agents.events import project_agent_event


def test_agent_event_projection_exposes_safe_evidence_without_raw_arguments() -> None:
    projected = project_agent_event(
        {
            "kind": "agent_activation_failed",
            "activation_id": "activation-1",
            "candidate_run_id": "run-1",
            "code": "tool_schema_repair_exhausted",
            "cause_code": "candidate_patch_evidence_not_verbatim",
            "recoverable": True,
            "allowed_actions": ["retry_failed_run"],
            "message": "private provider response",
            "raw_arguments": {"api_key": "secret"},
            "evidence_paths": [
                "book/agent/a/activation-1/failure.json",
                "book/agent/a/activation-1/telemetry.json",
            ],
        }
    )

    assert projected is not None
    assert projected.status == "failed"
    assert projected.artifact_path == "book/agent/a/activation-1/failure.json"
    assert projected.routing_decision == "tool_schema_repair_exhausted"
    assert projected.payload["cause_code"] == "candidate_patch_evidence_not_verbatim"
    assert projected.payload["recoverable"] is True
    assert projected.payload["allowed_actions"] == ["retry_failed_run"]
    assert projected.payload["evidence_paths"] == [
        "book/agent/a/activation-1/failure.json",
        "book/agent/a/activation-1/telemetry.json",
    ]
    assert "message" not in projected.payload
    assert "raw_arguments" not in projected.payload
    assert "secret" not in str(projected.payload)


def test_agent_event_projection_ignores_unknown_internal_events() -> None:
    assert project_agent_event({"kind": "provider_hidden_reasoning"}) is None


def test_evaluation_event_exposes_counts_and_component_names_only() -> None:
    projected = project_agent_event(
        {
            "kind": "agent_evaluation_completed",
            "evaluation_id": "evaluation-1",
            "evaluation_mode": "repair_verification",
            "logical_candidate_revision": 3,
            "open_issue_count": 1,
            "resolved_issue_count": 2,
            "new_issue_count": 1,
            "late_discovery_count": 1,
            "allowed_components": ["draft"],
            "repair_brief": "private prose-level repair guidance",
            "candidate_hash": "private-hash",
        }
    )

    assert projected is not None
    assert projected.payload["evaluation_mode"] == "repair_verification"
    assert projected.payload["logical_candidate_revision"] == 3
    assert projected.payload["late_discovery_count"] == 1
    assert projected.payload["allowed_components"] == ["draft"]
    assert "repair_brief" not in projected.payload
    assert "candidate_hash" not in projected.payload
