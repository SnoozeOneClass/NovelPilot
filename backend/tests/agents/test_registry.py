from __future__ import annotations

import asyncio

from pydantic_ai import ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from app.agents.contracts import (
    ACTIVATION_TIMEOUT_MS,
    CONNECT_TIMEOUT_MS,
    POOL_TIMEOUT_MS,
    READ_TIMEOUT_MS,
    WRITE_TIMEOUT_MS,
    ProfileCapabilities,
    ProfileSnapshot,
)
from app.agents.registry import DEFAULT_TASK_REGISTRY
from app.agents.roles import build_agent


def _profile() -> ProfileSnapshot:
    return ProfileSnapshot.create(
        profile_id="test-profile",
        display_name="Test Profile",
        api_family="openai_responses",
        base_url="https://provider.example/v1",
        model_id="opaque-model",
        capabilities=ProfileCapabilities(text_streaming=True, native_json_schema=True),
    )


def test_registry_freezes_s1_output_and_t1_without_domain_tools() -> None:
    profile = _profile()
    native = DEFAULT_TASK_REGISTRY.freeze_plan(
        task_id="task-native",
        project_id="project-a",
        run_id="run-a",
        task_key="book:evaluate:1",
        action_key="evaluate.book",
        role="evaluator",
        task_kind="evaluate.book",
        contract_version=1,
        book_id="book-a",
        canon_baseline_id="canon-a",
        semantic_goal="Evaluate the frozen candidate.",
        prompt="Evaluate this candidate.",
        context_manifest={"candidate_ref": "candidate-a"},
        profile_snapshot=profile,
        workspace_lock_version=1,
    )
    prose = DEFAULT_TASK_REGISTRY.freeze_plan(
        task_id="task-prose",
        project_id="project-a",
        run_id="run-a",
        task_key="chapter:draft:1",
        action_key="chapter.draft",
        role="chapter_writer",
        task_kind="chapter.draft",
        contract_version=1,
        book_id="book-a",
        arc_id="arc-a",
        chapter_id="chapter-a",
        canon_baseline_id="canon-a",
        semantic_goal="Write the frozen chapter plan.",
        prompt="Write chapter prose only.",
        context_manifest={"plan_ref": "plan-a"},
        profile_snapshot=profile,
        book_baseline_id="book-baseline-a",
        arc_baseline_id="arc-baseline-a",
    )

    assert native.output_mode == "native_json_schema"
    assert native.required_capabilities == ("native_json_schema",)
    assert native.model_request_limit == 2
    assert prose.output_mode == "text_streaming"
    assert prose.required_capabilities == ("text_streaming",)
    assert prose.model_request_limit == 1
    assert native.toolset == prose.toolset == ()
    assert (
        native.connect_timeout_ms,
        native.pool_timeout_ms,
        native.write_timeout_ms,
        native.read_timeout_ms,
        native.activation_timeout_ms,
    ) == (
        CONNECT_TIMEOUT_MS,
        POOL_TIMEOUT_MS,
        WRITE_TIMEOUT_MS,
        READ_TIMEOUT_MS,
        ACTIVATION_TIMEOUT_MS,
    )


def test_role_agent_has_no_function_tools_and_no_shared_history() -> None:
    seen_message_counts: list[int] = []

    def response(messages: list[object], info: AgentInfo) -> ModelResponse:
        seen_message_counts.append(len(messages))
        assert info.model_request_parameters.function_tools == []
        assert info.model_request_parameters.output_mode == "native"
        return ModelResponse(
            parts=[
                TextPart(
                    '{"decision":"pass","summary":"All checks pass.","findings":[],"repair_contract":null}'
                )
            ]
        )

    definition = DEFAULT_TASK_REGISTRY.get(
        role="evaluator",
        task_kind="evaluate.book",
        contract_version=1,
    )
    first = build_agent(model=FunctionModel(response), definition=definition)
    second = build_agent(model=FunctionModel(response), definition=definition)

    asyncio.run(first.run("First frozen candidate."))
    asyncio.run(second.run("Second frozen candidate."))

    assert seen_message_counts == [1, 1]
