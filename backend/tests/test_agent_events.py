from app.harness.agents.events import project_agent_event


def test_agent_event_projection_exposes_safe_evidence_without_raw_arguments() -> None:
    projected = project_agent_event(
        {
            "kind": "agent_activation_failed",
            "activation_id": "activation-1",
            "candidate_run_id": "run-1",
            "code": "tool_schema_repair_exhausted",
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
    assert projected.payload["evidence_paths"] == [
        "book/agent/a/activation-1/failure.json",
        "book/agent/a/activation-1/telemetry.json",
    ]
    assert "message" not in projected.payload
    assert "raw_arguments" not in projected.payload
    assert "secret" not in str(projected.payload)


def test_agent_event_projection_ignores_unknown_internal_events() -> None:
    assert project_agent_event({"kind": "provider_hidden_reasoning"}) is None
