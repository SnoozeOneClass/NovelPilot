from dataclasses import replace
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from app.harness.agents.domain_tools import build_default_tool_registry
from app.harness.agents.models import AgentBudgets, AgentIdentity, AgentState
from app.harness.agents.persistence import json_document, read_agent_state
from app.harness.agents.persistence import save_agent_state
from app.harness.agents.policy import ResolvedAgentPolicy
from app.harness.agents.registry import (
    ToolExecutionContext,
    ToolExecutionPlan,
    ToolRegistry,
    ToolSpec,
)
from app.harness.agents.runtime import AgentActivation, AgentRuntime
from app.llm.gateway import ChatMessage, ChatResult, ToolCall
from app.schemas.profiles import LlmProfile
from app.storage.json_files import read_json


class RevisionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision: int = Field(ge=1)


class TextInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str


def test_agent_runtime_repairs_invalid_tool_call_and_stops_on_checkpoint(tmp_path) -> None:
    registry = ToolRegistry()
    registry.register(_terminal_spec())
    responses = iter(
        [
            _tool_response(
                ToolCall(
                    id="call-invalid",
                    name="submit_candidate",
                    arguments={},
                    raw_arguments="{}",
                ),
                usage={
                    "input_tokens": 100,
                    "output_tokens": 10,
                    "details": {"cached_tokens": 3},
                },
            ),
            _tool_response(
                ToolCall(
                    id="call-valid",
                    name="submit_candidate",
                    arguments={"revision": 1},
                    raw_arguments='{"revision":1}',
                ),
                usage={
                    "input_tokens": 120,
                    "output_tokens": 20,
                    "details": {"cached_tokens": 2},
                },
            ),
        ]
    )
    runtime = AgentRuntime(registry, chat_call=lambda _profile, _request: next(responses))

    result = runtime.run(_activation(tmp_path))

    assert result.outcome == "candidate"
    assert result.turns_used == 2
    assert result.terminal_result is not None
    assert result.terminal_result.checkpoint_id == "book-direction:1"
    assert read_json(tmp_path / "book" / "candidates" / "direction-1.json") == {
        "revision": 1
    }
    state = read_agent_state(tmp_path, AgentIdentity(project_id="project-1", role="book"))
    assert state.lifecycle == "completed"
    assert state.budgets is not None
    assert state.budgets.used_tool_schema_repairs == 1
    assert result.usage == {
        "input_tokens": 220,
        "output_tokens": 30,
        "details": {"cached_tokens": 5},
    }
    telemetry_path = next(
        (tmp_path / "book" / "agent" / "a").glob("*/telemetry.json")
    )
    telemetry = read_json(telemetry_path)
    assert telemetry["outcome"] == "candidate"
    assert telemetry["llm_calls"] == 2
    assert telemetry["tool_calls"] == 2
    assert telemetry["tool_errors"] == 1
    assert telemetry["validation_failures"] == 1
    assert telemetry["activation_turns"] == 2
    assert telemetry["usage"] == result.usage


def test_restart_preserves_wait_and_fails_interrupted_activation_closed(
    tmp_path: Path,
) -> None:
    waiting_identity = AgentIdentity(project_id="project-1", role="book")
    save_agent_state(
        tmp_path,
        AgentState(
            identity=waiting_identity,
            lifecycle="waiting_user",
            candidate_run_id="waiting-run",
            budgets=AgentBudgets(max_turns=20, used_turns=4),
            last_checkpoint_id="user-decision:1",
        ),
    )

    waiting = read_agent_state(tmp_path, waiting_identity)

    assert waiting.lifecycle == "waiting_user"
    assert waiting.candidate_run_id == "waiting-run"
    assert waiting.budgets is not None
    assert waiting.budgets.used_turns == 4

    interrupted_identity = AgentIdentity(
        project_id="project-1",
        role="chapter",
        scope_id="chapter-001",
    )
    save_agent_state(
        tmp_path,
        AgentState(
            identity=interrupted_identity,
            lifecycle="running",
            candidate_run_id="interrupted-run",
            activation_id="activation-1",
            budgets=AgentBudgets(max_turns=30, used_turns=6),
        ),
    )

    interrupted = read_agent_state(tmp_path, interrupted_identity)

    assert interrupted.lifecycle == "failed"
    assert "interrupted" in interrupted.summary
    assert interrupted.budgets is not None
    assert interrupted.budgets.used_turns == 6
    assert read_json(
        tmp_path / "chapters" / "chapter-001" / "agent" / "state.json"
    )["lifecycle"] == "failed"


def test_book_direction_missing_markdown_is_repaired_within_bounded_agent_run(
    tmp_path: Path,
) -> None:
    valid_arguments = {
        "expected_revision": 1,
        "candidate_revision": 1,
        "direction_markdown": "# Direction\n\nA fair-play mystery with bounded secrets.",
        "constraints": {
            "confirmed": ["Clues remain fair."],
            "must_preserve": [],
            "must_avoid": [],
            "creative_freedoms": [],
            "open_decisions": [],
        },
        "confirmed_decision_coverage": [
            {
                "decision": "Clues remain fair.",
                "candidate_evidence": "The direction explicitly requires fair-play clues.",
            }
        ],
        "recommended_titles": [
            {"title": "The First Tide", "rationale": "Names the opening mystery."},
            {"title": "Eleven Minutes", "rationale": "Names the central time gap."},
            {"title": "The Closed Window", "rationale": "Names a recurring clue."},
        ],
        "rolling_plan_markdown": "Plan only the first active story arc.",
    }
    missing_markdown = {
        key: value for key, value in valid_arguments.items() if key != "direction_markdown"
    }
    responses = iter(
        [
            _tool_response(
                ToolCall(
                    id="missing-direction",
                    name="submit_book_direction_candidate",
                    arguments=missing_markdown,
                    raw_arguments="{}",
                )
            ),
            _tool_response(
                ToolCall(
                    id="repaired-direction",
                    name="submit_book_direction_candidate",
                    arguments=valid_arguments,
                    raw_arguments="{}",
                )
            ),
        ]
    )
    runtime = AgentRuntime(
        build_default_tool_registry(),
        chat_call=lambda _profile, _request: next(responses),
    )
    activation = replace(
        _activation(tmp_path),
        candidate_run_id="book-direction-repair",
        allowed_tools=("submit_book_direction_candidate",),
    )

    result = runtime.run(activation)

    assert result.outcome == "candidate"
    assert result.turns_used == 2
    assert result.terminal_result is not None
    assert result.terminal_result.tool_name == "submit_book_direction_candidate"
    assert result.terminal_result.content["promotable"] is False
    assert not (tmp_path / "book" / "direction.md").exists()
    telemetry_path = next((tmp_path / "book" / "agent" / "a").glob("*/telemetry.json"))
    telemetry = read_json(telemetry_path)
    assert telemetry["validation_failures"] == 1
    assert telemetry["tool_errors"] == 1


def test_write_tool_idempotency_replays_same_result_and_rejects_changed_arguments(
    tmp_path,
) -> None:
    calls = 0

    def handler(_context, arguments):
        nonlocal calls
        calls += 1
        assert isinstance(arguments, RevisionInput)
        return ToolExecutionPlan(
            content={"revision": arguments.revision},
            files={
                "book/candidates/value.json": json_document(
                    {"revision": arguments.revision}
                )
            },
            checkpoint_id=f"candidate:{arguments.revision}",
            artifact_paths=["book/candidates/value.json"],
        )

    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="submit_candidate",
            version=1,
            description="Submit a candidate.",
            input_model=RevisionInput,
            allowed_roles=frozenset({"book"}),
            handler=handler,
            read_only=False,
            terminal=True,
        )
    )
    context = ToolExecutionContext(
        project_path=tmp_path,
        identity=AgentIdentity(project_id="project-1", role="book"),
        candidate_run_id="run-1",
        activation_id="activation-1",
        tool_call_id="call-1",
        phase="direction",
        expected_revision=1,
    )
    first_call = ToolCall(
        id="call-1",
        name="submit_candidate",
        arguments={"revision": 1},
        raw_arguments='{"revision":1}',
    )

    first = registry.execute(context, first_call)
    replay = registry.execute(context, first_call)
    conflict = registry.execute(
        context,
        first_call.model_copy(
            update={"arguments": {"revision": 2}, "raw_arguments": '{"revision":2}'}
        ),
    )

    assert calls == 1
    assert first.status == "ok"
    assert replay.replayed is True
    assert conflict.status == "error"
    assert conflict.error_code == "idempotency_conflict"


def test_agent_runtime_rejects_terminal_tool_without_checkpoint(tmp_path) -> None:
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="submit_candidate",
            version=1,
            description="Submit a candidate.",
            input_model=RevisionInput,
            allowed_roles=frozenset({"book"}),
            handler=lambda _context, _arguments: ToolExecutionPlan(content={"ok": True}),
            read_only=False,
            terminal=True,
        )
    )
    response = _tool_response(
        ToolCall(
            id="call-1",
            name="submit_candidate",
            arguments={"revision": 1},
            raw_arguments='{"revision":1}',
        )
    )
    runtime = AgentRuntime(registry, chat_call=lambda _profile, _request: response)

    result = runtime.run(_activation(tmp_path))

    assert result.outcome == "failed"
    assert result.failure is not None
    assert result.failure.code == "terminal_checkpoint_missing"
    telemetry_path = next(
        (tmp_path / "book" / "agent" / "a").glob("*/telemetry.json")
    )
    assert read_json(telemetry_path)["outcome"] == "failed"


def test_transport_retry_does_not_consume_an_agent_turn(tmp_path) -> None:
    registry = ToolRegistry()
    registry.register(_terminal_spec())
    attempts = 0

    def fake_call(_profile, _request):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary provider failure")
        return _tool_response(
            ToolCall(
                id="call-1",
                name="submit_candidate",
                arguments={"revision": 1},
                raw_arguments='{"revision":1}',
            )
        )

    result = AgentRuntime(registry, chat_call=fake_call).run(_activation(tmp_path))

    assert attempts == 2
    assert result.outcome == "candidate"
    assert result.turns_used == 1
    state = read_agent_state(tmp_path, AgentIdentity(project_id="project-1", role="book"))
    assert state.budgets is not None
    assert state.budgets.used_transport_retries == 1


def test_registry_enforces_role_allowlist() -> None:
    registry = ToolRegistry()
    registry.register(_terminal_spec())

    try:
        registry.resolve(role="chapter", phase="direction", names=["submit_candidate"])
    except ValueError as exc:
        assert "not allowed" in str(exc)
    else:
        raise AssertionError("Chapter Agent unexpectedly received a Book Tool.")


def test_runtime_redacts_profile_secrets_before_tool_candidate_write(tmp_path) -> None:
    registry = ToolRegistry()

    def handler(_context, arguments):
        assert isinstance(arguments, TextInput)
        return ToolExecutionPlan(
            content={"summary": "Candidate saved."},
            files={"book/candidates/redacted.json": json_document(arguments.model_dump())},
            checkpoint_id="redacted:1",
            artifact_paths=["book/candidates/redacted.json"],
        )

    registry.register(
        ToolSpec(
            name="submit_candidate",
            version=1,
            description="Submit candidate text.",
            input_model=TextInput,
            allowed_roles=frozenset({"book"}),
            handler=handler,
            read_only=False,
            terminal=True,
        )
    )
    response = _tool_response(
        ToolCall(
            id="call-secret",
            name="submit_candidate",
            arguments={
                "text": "provider echoed secret and https://api.example.com/v1"
            },
            raw_arguments=(
                '{"text":"provider echoed secret and https://api.example.com/v1"}'
            ),
        )
    )
    result = AgentRuntime(registry, chat_call=lambda _profile, _request: response).run(
        _activation(tmp_path)
    )

    assert result.outcome == "candidate"
    payload = read_json(tmp_path / "book" / "candidates" / "redacted.json")
    assert payload == {"text": "provider echoed [redacted] and [redacted]"}
    transcript = next((tmp_path / "book" / "agent" / "a").glob("*/transcript.jsonl"))
    rendered = transcript.read_text(encoding="utf-8")
    assert "https://api.example.com/v1" not in rendered
    assert '"secret"' not in rendered


def _terminal_spec() -> ToolSpec:
    def handler(_context, arguments):
        assert isinstance(arguments, RevisionInput)
        path = f"book/candidates/direction-{arguments.revision}.json"
        return ToolExecutionPlan(
            content={"revision": arguments.revision, "summary": "Candidate saved."},
            files={path: json_document({"revision": arguments.revision})},
            checkpoint_id=f"book-direction:{arguments.revision}",
            artifact_paths=[path],
        )

    return ToolSpec(
        name="submit_candidate",
        version=1,
        description="Submit a candidate.",
        input_model=RevisionInput,
        allowed_roles=frozenset({"book"}),
        handler=handler,
        read_only=False,
        terminal=True,
    )


def _activation(project_path: Path) -> AgentActivation:
    profile = LlmProfile(
        id="main",
        name="Main",
        protocol="openai-compatible",
        base_url="https://api.example.com/v1",
        api_key=SecretStr("secret"),
        model="story-model",
    )
    return AgentActivation(
        project_path=project_path,
        identity=AgentIdentity(project_id="project-1", role="book"),
        candidate_run_id="candidate-run-1",
        phase="direction",
        expected_revision=1,
        allowed_tools=("submit_candidate",),
        system_prompt="Use the Tool.",
        messages=(ChatMessage(role="user", content="Create the candidate."),),
        policy=ResolvedAgentPolicy(
            role="book",
            profile=profile,
            evaluator_profile=profile,
            max_turns=4,
            tool_schema_repair_limit=2,
            semantic_revision_limit=2,
            transport_retry_limit=2,
        ),
    )


def _tool_response(
    call: ToolCall,
    *,
    usage: dict[str, object] | None = None,
) -> ChatResult:
    return ChatResult(
        content="",
        tool_calls=[call],
        finish_reason="tool_call",
        usage=usage or {},
        model_snapshot="story-model",
        provider_snapshot="openai-compatible",
    )
