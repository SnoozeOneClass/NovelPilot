from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict, SecretStr

from app.harness.agents.models import AgentIdentity
from app.harness.agents.persistence import json_document
from app.harness.agents.policy import ResolvedAgentPolicy
from app.harness.agents.registry import (
    ToolExecutionContext,
    ToolExecutionPlan,
    ToolRegistry,
    ToolSpec,
)
from app.harness.agents.runtime import AgentActivation, AgentRuntime
from app.harness.experiment_hooks import ExperimentHookRegistry, ExperimentHookSpec
from app.llm.gateway import ChatMessage, ToolCall
from app.schemas.experiments import ExperimentHookStrategy
from app.schemas.profiles import LlmProfile


class CandidateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str


def test_experiment_hook_observes_committed_tool_result_without_mutating_it(
    tmp_path: Path,
) -> None:
    observed = []

    def observer(payload):
        observed.append(payload)
        payload["artifact_paths"].clear()

    hooks = ExperimentHookRegistry()
    hooks.register(ExperimentHookSpec("candidate_observer", "tool_result", observer))
    registry = _registry(hooks)
    context = _context(
        tmp_path,
        ExperimentHookStrategy(mode="full", disabled_hook_ids=[]),
    )

    result = registry.execute(context, _call())

    assert result.status == "ok"
    assert result.artifact_paths == ["book/candidates/value.json"]
    assert (tmp_path / result.artifact_paths[0]).is_file()
    assert observed[0]["tool_name"] == "submit_candidate"
    assert observed[0]["artifact_paths"] == []


def test_ablation_disables_only_the_named_experiment_hook(tmp_path: Path) -> None:
    observed = []
    hooks = ExperimentHookRegistry()
    hooks.register(
        ExperimentHookSpec("candidate_observer", "tool_result", observed.append)
    )
    registry = _registry(hooks)
    context = _context(
        tmp_path,
        ExperimentHookStrategy(
            mode="ablation",
            disabled_hook_ids=["candidate_observer"],
        ),
    )

    result = registry.execute(context, _call())

    assert result.status == "ok"
    assert observed == []
    assert (tmp_path / "book" / "candidates" / "value.json").is_file()


def test_none_baseline_cannot_enter_agent_runtime(tmp_path: Path) -> None:
    activation = AgentActivation(
        project_path=tmp_path,
        identity=AgentIdentity(project_id="project-1", role="book"),
        candidate_run_id="none-run",
        phase="direction",
        expected_revision=0,
        allowed_tools=("submit_candidate",),
        system_prompt="Use the Tool.",
        messages=(ChatMessage(role="user", content="Create a candidate."),),
        policy=_policy(),
        experiment_strategy=ExperimentHookStrategy(mode="none"),
    )

    with pytest.raises(ValueError, match="direct generation"):
        AgentRuntime(_registry()).run(activation)


def _registry(hooks: ExperimentHookRegistry | None = None) -> ToolRegistry:
    registry = ToolRegistry(hooks)
    registry.register(
        ToolSpec(
            name="submit_candidate",
            version=1,
            description="Submit an experiment candidate.",
            input_model=CandidateInput,
            allowed_roles=frozenset({"book"}),
            handler=lambda _context, arguments: ToolExecutionPlan(
                content={"promotable": False},
                files={
                    "book/candidates/value.json": json_document(
                        arguments.model_dump(mode="json")
                    )
                },
                checkpoint_id="candidate:1",
                artifact_paths=["book/candidates/value.json"],
            ),
            read_only=False,
            terminal=True,
        )
    )
    return registry


def _context(
    project_path: Path,
    strategy: ExperimentHookStrategy,
) -> ToolExecutionContext:
    return ToolExecutionContext(
        project_path=project_path,
        identity=AgentIdentity(project_id="project-1", role="book"),
        candidate_run_id="run-1",
        activation_id="activation-1",
        tool_call_id="call-1",
        phase="direction",
        expected_revision=0,
        experiment_strategy=strategy,
    )


def _call() -> ToolCall:
    return ToolCall(
        id="call-1",
        name="submit_candidate",
        arguments={"text": "candidate"},
        raw_arguments='{"text":"candidate"}',
    )


def _policy() -> ResolvedAgentPolicy:
    profile = LlmProfile(
        id="main",
        name="Main",
        protocol="openai-compatible",
        base_url="https://api.example.com/v1",
        api_key=SecretStr("secret"),
        model="story-model",
    )
    return ResolvedAgentPolicy(
        role="book",
        profile=profile,
        evaluator_profile=profile,
        max_turns=2,
        tool_schema_repair_limit=1,
        semantic_revision_limit=1,
        transport_retry_limit=1,
    )
