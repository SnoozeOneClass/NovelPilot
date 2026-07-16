from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import SecretStr, ValidationError

from app.core import config
from app.harness.agents.models import AgentRole
from app.harness.agents.policy import ResolvedAgentPolicy
from app.schemas.experiments import (
    ExperimentArmRequest,
    ExperimentHookStrategy,
    ExperimentRunConfigurationRequest,
)
from app.schemas.profiles import LlmProfile
from app.schemas.projects import AgentPolicy, ProjectMetadata
from app.storage import experiment_runs
from app.storage.json_files import read_json


FIXTURE_ID = "fixture-00000000-0000-0000-0000-000000000001"
CHECKPOINT_FINGERPRINT = "a" * 64


def test_run_configuration_freezes_each_loop_agent_and_evaluator_binding(
    tmp_path: Path,
    monkeypatch,
) -> None:
    output_dir = tmp_path / "output"
    metadata = ProjectMetadata(
        project_id="project-1",
        active_profile_id="book-profile",
        agent_policy=AgentPolicy(
            book_profile_id="book-profile",
            story_arc_profile_id="arc-profile",
            chapter_profile_id="chapter-profile",
            book_max_turns=21,
            story_arc_max_turns=22,
            chapter_max_turns=33,
            semantic_revision_limit=3,
        ),
    )
    policies = {
        "book": _policy("book", "book-profile"),
        "story_arc": _policy("story_arc", "arc-profile"),
        "chapter": _policy("chapter", "chapter-profile"),
    }
    monkeypatch.setattr(config, "OUTPUT_DIR", output_dir)
    monkeypatch.setattr(
        experiment_runs,
        "verify_fixture",
        lambda _path: SimpleNamespace(
            checkpoint=SimpleNamespace(
                checkpoint_fingerprint=CHECKPOINT_FINGERPRINT
            )
        ),
    )
    monkeypatch.setattr(
        experiment_runs,
        "read_project_metadata",
        lambda _project_path: metadata,
    )
    monkeypatch.setattr(
        experiment_runs,
        "resolve_agent_policy",
        lambda _metadata, role: policies[role],
    )

    response = experiment_runs.create_run_configuration(
        tmp_path / "project",
        _request(),
    )

    configuration = response.configuration
    bindings = {
        (binding.role, binding.purpose): binding.profile_id
        for binding in configuration.model_bindings
    }
    assert bindings == {
        ("book", "agent"): "book-profile",
        ("book", "evaluator"): "book-profile-evaluator",
        ("story_arc", "agent"): "arc-profile",
        ("story_arc", "evaluator"): "arc-profile-evaluator",
        ("chapter", "agent"): "chapter-profile",
        ("chapter", "evaluator"): "chapter-profile-evaluator",
    }
    assert configuration.agent_policy.book_max_turns == 21
    assert configuration.agent_policy.story_arc_max_turns == 22
    assert configuration.agent_policy.chapter_max_turns == 33
    assert configuration.agent_policy.semantic_revision_limit == 3
    assert configuration.schemas.context_policy_version == "context-policy-v1"
    assert configuration.schemas.evaluation_schema_version == "evaluation-v1"
    assert configuration.schemas.telemetry_schema_version == 2
    assert configuration.schemas.retry_budget_scope_version == "action-local-v1"
    assert configuration.schemas.tool_registry
    assert [arm.strategy.mode for arm in configuration.arms] == [
        "none",
        "full",
        "ablation",
    ]
    assert configuration.arms[0].strategy.none_baseline_version == "direct-v1"

    config_path = (
        output_dir
        / "experiments"
        / "runs"
        / configuration.run_id
        / "config.json"
    )
    stored = read_json(config_path)
    serialized = config_path.read_text(encoding="utf-8")
    assert stored["configuration_fingerprint"] == configuration.configuration_fingerprint
    assert "secret-" not in serialized
    assert "api_key" not in serialized
    assert "base_url" not in serialized
    assert not (output_dir / "experiments" / ".creating" / configuration.run_id).exists()


def test_run_configuration_cleans_staging_when_publication_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    metadata = ProjectMetadata(project_id="project-1", active_profile_id="main")
    output_dir = tmp_path / "output"
    policy = _policy("book", "main")
    monkeypatch.setattr(config, "OUTPUT_DIR", output_dir)
    monkeypatch.setattr(
        experiment_runs,
        "verify_fixture",
        lambda _path: SimpleNamespace(
            checkpoint=SimpleNamespace(
                checkpoint_fingerprint=CHECKPOINT_FINGERPRINT
            )
        ),
    )
    monkeypatch.setattr(
        experiment_runs,
        "read_project_metadata",
        lambda _project_path: metadata,
    )
    monkeypatch.setattr(
        experiment_runs,
        "resolve_agent_policy",
        lambda _metadata, role: policy.model_copy(update={"role": role}),
    )
    def fail_write(*_args, **_kwargs) -> None:
        raise OSError("injected failure")

    monkeypatch.setattr(experiment_runs, "write_json", fail_write)

    with pytest.raises(OSError, match="injected failure"):
        experiment_runs.create_run_configuration(tmp_path / "project", _request())

    creating_root = output_dir / "experiments" / ".creating"
    assert not creating_root.exists() or list(creating_root.iterdir()) == []
    assert not (output_dir / "experiments" / "runs").exists()


@pytest.mark.parametrize(
    ("mode", "disabled_hook_ids"),
    [
        ("none", ["semantic_repair"]),
        ("full", ["semantic_repair"]),
        ("ablation", []),
        ("ablation", ["z", "a"]),
        ("ablation", ["same", "same"]),
    ],
)
def test_hook_strategy_rejects_ambiguous_or_noncanonical_modes(
    mode: str,
    disabled_hook_ids: list[str],
) -> None:
    with pytest.raises(ValidationError):
        ExperimentHookStrategy(
            mode=mode,  # type: ignore[arg-type]
            disabled_hook_ids=disabled_hook_ids,
        )


def _request() -> ExperimentRunConfigurationRequest:
    return ExperimentRunConfigurationRequest(
        fixture_id=FIXTURE_ID,
        arms=[
            ExperimentArmRequest(
                arm_id="none",
                strategy=ExperimentHookStrategy(mode="none"),
            ),
            ExperimentArmRequest(
                arm_id="full",
                strategy=ExperimentHookStrategy(mode="full"),
            ),
            ExperimentArmRequest(
                arm_id="without_semantic_repair",
                strategy=ExperimentHookStrategy(
                    mode="ablation",
                    disabled_hook_ids=["semantic_repair"],
                ),
            ),
        ],
    )


def _policy(role: AgentRole, profile_id: str) -> ResolvedAgentPolicy:
    return ResolvedAgentPolicy(
        role=role,
        profile=_profile(profile_id),
        evaluator_profile=_profile(f"{profile_id}-evaluator"),
        max_turns=30,
        tool_schema_repair_limit=2,
        semantic_revision_limit=2,
        transport_retry_limit=3,
    )


def _profile(profile_id: str) -> LlmProfile:
    return LlmProfile(
        id=profile_id,
        name=profile_id,
        protocol="openai-compatible",
        base_url=f"https://{profile_id}.example.com/v1",
        api_key=SecretStr(f"secret-{profile_id}"),
        model=f"model-{profile_id}",
    )
