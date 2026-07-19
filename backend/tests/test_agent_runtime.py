from dataclasses import replace
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from app.harness.agents.domain_tools import build_default_tool_registry
from app.harness.agents.models import AgentBudgets, AgentIdentity, AgentState
from app.harness.agents.persistence import activation_relative, json_document, read_agent_state
from app.harness.agents.persistence import save_agent_state
from app.harness.agents.policy import ResolvedAgentPolicy
from app.harness.agents.registry import (
    ToolExecutionContext,
    ToolExecutionPlan,
    ToolHandlerError,
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


class RetryCandidateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    accept: bool = False


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
    assert state.budgets.used_tool_schema_repairs == 0
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
    assert telemetry["activation_tool_schema_repairs"] == 1
    assert telemetry["retry_budget_scope_version"] == "action-local-v1"
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
        "direction_markdown": "# Direction\n\nA fair-play mystery with bounded secrets.",
        "constraints": {
            "must_avoid": [],
            "creative_freedoms": [],
            "open_decisions": [],
        },
        "comparison_titles": [
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
        expected_candidate_revision=1,
        control_data={
            "confirmed_decisions": ["Clues remain fair."],
            "selected_title": "The First Tide",
        },
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
    request_path = next((tmp_path / "book" / "agent" / "a").glob("*/request.json"))
    assert read_json(request_path)["expected_candidate_revision"] == 1


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


def test_new_activation_resets_local_usage_and_preserves_semantic_chain(
    tmp_path: Path,
) -> None:
    activation = _activation(tmp_path)
    save_agent_state(
        tmp_path,
        AgentState(
            identity=activation.identity,
            lifecycle="idle",
            candidate_run_id=activation.candidate_run_id,
            budgets=AgentBudgets(
                max_turns=4,
                used_turns=4,
                tool_schema_repair_limit=2,
                used_tool_schema_repairs=2,
                semantic_revision_limit=2,
                used_semantic_revisions=1,
                transport_retry_limit=2,
                used_transport_retries=2,
            ),
        ),
    )
    responses = iter(
        [
            _tool_response(
                ToolCall(
                    id="fresh-invalid",
                    name="submit_candidate",
                    arguments={},
                    raw_arguments="{}",
                )
            ),
            _tool_response(
                ToolCall(
                    id="fresh-valid",
                    name="submit_candidate",
                    arguments={"revision": 1},
                    raw_arguments='{"revision":1}',
                )
            ),
        ]
    )

    result = AgentRuntime(
        _registry_with_inspector(),
        chat_call=lambda _profile, _request: next(responses),
    ).run(activation)

    assert result.outcome == "candidate"
    assert result.turns_used == 2
    state = read_agent_state(tmp_path, activation.identity)
    assert state.budgets is not None
    assert state.budgets.used_turns == 2
    assert state.budgets.used_tool_schema_repairs == 0
    assert state.budgets.used_transport_retries == 0
    assert state.budgets.used_semantic_revisions == 1


def test_tool_repair_limit_is_independent_after_successful_tool_action(
    tmp_path: Path,
) -> None:
    responses = iter(
        [
            _tool_response(
                ToolCall(
                    id="first-invalid",
                    name="submit_candidate",
                    arguments={},
                    raw_arguments="{}",
                )
            ),
            _tool_response(
                ToolCall(
                    id="inspect-ok",
                    name="inspect_context",
                    arguments={"revision": 1},
                    raw_arguments='{"revision":1}',
                )
            ),
            _tool_response(
                ToolCall(
                    id="second-invalid",
                    name="submit_candidate",
                    arguments={},
                    raw_arguments="{}",
                )
            ),
            _tool_response(
                ToolCall(
                    id="second-valid",
                    name="submit_candidate",
                    arguments={"revision": 1},
                    raw_arguments='{"revision":1}',
                )
            ),
        ]
    )
    activation = _activation(tmp_path)
    activation = replace(
        activation,
        allowed_tools=("inspect_context", "submit_candidate"),
        policy=activation.policy.model_copy(
            update={"max_turns": 6, "tool_schema_repair_limit": 1}
        ),
    )

    result = AgentRuntime(
        _registry_with_inspector(),
        chat_call=lambda _profile, _request: next(responses),
    ).run(activation)

    assert result.outcome == "candidate"
    telemetry_path = next(
        (tmp_path / "book" / "agent" / "a").glob("*/telemetry.json")
    )
    telemetry = read_json(telemetry_path)
    assert telemetry["activation_tool_schema_repairs"] == 2


def test_tool_repair_limit_still_bounds_one_continuous_failed_action(
    tmp_path: Path,
) -> None:
    def invalid_response(call_id: str) -> ChatResult:
        return _tool_response(
            ToolCall(
                id=call_id,
                name="submit_candidate",
                arguments={},
                raw_arguments="{}",
            )
        )

    responses = iter(
        [invalid_response("invalid-1"), invalid_response("invalid-2")]
    )
    activation = _activation(tmp_path)
    activation = replace(
        activation,
        policy=activation.policy.model_copy(
            update={"tool_schema_repair_limit": 1}
        ),
    )

    result = AgentRuntime(
        _registry_with_inspector(),
        chat_call=lambda _profile, _request: next(responses),
    ).run(activation)

    assert result.outcome == "failed"
    assert result.failure is not None
    assert result.failure.code == "tool_schema_repair_exhausted"
    assert result.failure.cause_code == "invalid_tool_arguments"
    assert result.failure.recoverable is True
    assert result.failure.allowed_actions == [
        "retry:submit_candidate",
        "retry_failed_run",
    ]
    assert result.failure.consumed_budgets["tool_schema_repairs"] == 1
    assert result.failure.remaining_budgets["tool_schema_repairs"] == 0


def test_chapter_retry_preserves_candidate_workspace_and_original_failure(
    tmp_path: Path,
) -> None:
    registry = ToolRegistry()

    def seed_candidate(context, _arguments):
        draft_path = activation_relative(context.identity, context.activation_id) / "c" / "draft.md"
        return ToolExecutionPlan(
            content={"summary": "Draft preserved."},
            files={draft_path.as_posix(): "The bell rang once.\n"},
            artifact_paths=[draft_path.as_posix()],
            allowed_actions=["submit_candidate"],
        )

    def submit_candidate(context, arguments):
        assert isinstance(arguments, RetryCandidateInput)
        draft_path = activation_relative(context.identity, context.activation_id) / "c" / "draft.md"
        if not arguments.accept:
            raise ToolHandlerError(
                "candidate_patch_evidence_not_verbatim",
                "State-patch evidence is not verbatim.",
                recoverable=True,
                artifact_paths=[draft_path.as_posix()],
                allowed_actions=["retry:submit_candidate"],
            )
        assert (context.project_path / draft_path).read_text(encoding="utf-8") == (
            "The bell rang once.\n"
        )
        return ToolExecutionPlan(
            content={"summary": "Candidate accepted."},
            checkpoint_id="chapter:chapter-001:1",
            artifact_paths=[draft_path.as_posix()],
        )

    registry.register(
        ToolSpec(
            name="seed_candidate",
            version=1,
            description="Seed a Chapter candidate.",
            input_model=RetryCandidateInput,
            allowed_roles=frozenset({"chapter"}),
            allowed_phases=frozenset({"chapter"}),
            handler=seed_candidate,
            read_only=False,
        )
    )
    registry.register(
        ToolSpec(
            name="submit_candidate",
            version=1,
            description="Submit a Chapter candidate.",
            input_model=RetryCandidateInput,
            allowed_roles=frozenset({"chapter"}),
            allowed_phases=frozenset({"chapter"}),
            handler=submit_candidate,
            read_only=False,
            terminal=True,
        )
    )
    base = _activation(tmp_path)
    activation = replace(
        base,
        identity=AgentIdentity(
            project_id="project-1",
            role="chapter",
            scope_id="chapter-001",
        ),
        candidate_run_id="chapter-chain-1",
        phase="chapter",
        allowed_tools=("seed_candidate", "submit_candidate"),
        policy=base.policy.model_copy(
            update={
                "role": "chapter",
                "max_turns": 7,
                "tool_schema_repair_limit": 2,
            }
        ),
    )
    first_responses = iter(
        [
            _tool_response(
                ToolCall(
                    id="seed",
                    name="seed_candidate",
                    arguments={"accept": False},
                    raw_arguments='{"accept":false}',
                )
            ),
            _tool_response(
                ToolCall(
                    id="reject-1",
                    name="submit_candidate",
                    arguments={"accept": False},
                    raw_arguments='{"accept":false}',
                )
            ),
            _tool_response(
                ToolCall(
                    id="reject-2",
                    name="submit_candidate",
                    arguments={"accept": False},
                    raw_arguments='{"accept":false}',
                )
            ),
            _tool_response(
                ToolCall(
                    id="reject-3",
                    name="submit_candidate",
                    arguments={"accept": False},
                    raw_arguments='{"accept":false}',
                )
            ),
        ]
    )

    failed = AgentRuntime(
        registry,
        chat_call=lambda _profile, _request: next(first_responses),
    ).run(activation)

    assert failed.outcome == "failed"
    assert failed.failure is not None
    assert failed.failure.cause_code == "candidate_patch_evidence_not_verbatim"
    assert failed.failure.recoverable is True
    assert failed.failure.allowed_actions == [
        "retry:submit_candidate",
        "retry_failed_run",
    ]
    assert any(path.endswith("/c/draft.md") for path in failed.failure.evidence)

    completed = AgentRuntime(
        registry,
        chat_call=lambda _profile, _request: _tool_response(
            ToolCall(
                id="accept-after-retry",
                name="submit_candidate",
                arguments={"accept": True},
                raw_arguments='{"accept":true}',
            )
        ),
    ).run(activation)

    assert completed.outcome == "candidate"
    assert completed.candidate_run_id == failed.candidate_run_id
    assert completed.activation_id != failed.activation_id
    restored_draft = (
        tmp_path
        / activation_relative(activation.identity, completed.activation_id)
        / "c"
        / "draft.md"
    )
    assert restored_draft.read_text(encoding="utf-8") == "The bell rang once.\n"
    state = read_agent_state(tmp_path, activation.identity)
    assert state.budgets is not None
    assert state.budgets.used_tool_schema_repairs == 0


def test_provider_retry_limit_is_independent_for_each_llm_request(
    tmp_path: Path,
) -> None:
    attempts = 0

    def fake_call(_profile, _request):
        nonlocal attempts
        attempts += 1
        if attempts in {1, 3}:
            raise RuntimeError("temporary provider failure")
        if attempts == 2:
            return _tool_response(
                ToolCall(
                    id="inspect-after-retry",
                    name="inspect_context",
                    arguments={"revision": 1},
                    raw_arguments='{"revision":1}',
                )
            )
        return _tool_response(
            ToolCall(
                id="submit-after-second-retry",
                name="submit_candidate",
                arguments={"revision": 1},
                raw_arguments='{"revision":1}',
            )
        )

    activation = _activation(tmp_path)
    activation = replace(
        activation,
        allowed_tools=("inspect_context", "submit_candidate"),
        policy=activation.policy.model_copy(
            update={"transport_retry_limit": 1}
        ),
    )

    result = AgentRuntime(
        _registry_with_inspector(),
        chat_call=fake_call,
    ).run(activation)

    assert attempts == 4
    assert result.outcome == "candidate"
    assert result.turns_used == 2
    telemetry_path = next(
        (tmp_path / "book" / "agent" / "a").glob("*/telemetry.json")
    )
    telemetry = read_json(telemetry_path)
    assert telemetry["activation_transport_retries"] == 2


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


def _registry_with_inspector() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="inspect_context",
            version=1,
            description="Inspect context before another candidate submission.",
            input_model=RevisionInput,
            allowed_roles=frozenset({"book"}),
            handler=lambda _context, arguments: ToolExecutionPlan(
                content={"revision": arguments.revision}
            ),
            read_only=True,
            terminal=False,
        )
    )
    registry.register(_terminal_spec())
    return registry


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
