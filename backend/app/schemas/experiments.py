from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.projects import (
    RETRY_BUDGET_SCOPE_VERSION,
    AgentPolicy,
    BenchmarkFixtureLifecycle,
    ProjectKind,
    RetryBudgetScopeVersion,
)


class _StrictExperimentModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExperimentHookStrategy(_StrictExperimentModel):
    schema_version: Literal[1] = 1
    mode: Literal["none", "full", "ablation"]
    disabled_hook_ids: list[str] = Field(default_factory=list, max_length=100)
    none_baseline_version: Literal["direct-v1"] = "direct-v1"

    @model_validator(mode="after")
    def validate_mode(self) -> "ExperimentHookStrategy":
        normalized = sorted(set(self.disabled_hook_ids))
        if normalized != self.disabled_hook_ids:
            raise ValueError("Disabled experiment hook IDs must be unique and sorted.")
        if self.mode in {"none", "full"} and self.disabled_hook_ids:
            raise ValueError(f"{self.mode} strategy cannot disable named hooks.")
        if self.mode == "ablation" and not self.disabled_hook_ids:
            raise ValueError("Ablation strategy must disable at least one named hook.")
        return self


class ExperimentFixtureFile(_StrictExperimentModel):
    path: str
    byte_size: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ExperimentFixtureCheckpoint(_StrictExperimentModel):
    source_project_name: str
    source_project_id: str
    source_title: str | None
    active_arc_id: str
    completed_arc_ids: list[str] = Field(default_factory=list)
    warmup_chapter_ids: list[str] = Field(default_factory=list)
    recommended_target_chapter_count: int = Field(ge=1, le=30)
    target_chapter_count: int = Field(ge=1, le=30)
    checkpoint_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class ExperimentFixtureManifest(_StrictExperimentModel):
    schema_version: Literal[1] = 1
    fixture_version: Literal["fixture-v1"] = "fixture-v1"
    fixture_id: str = Field(
        pattern=(
            r"^fixture-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
            r"[0-9a-f]{4}-[0-9a-f]{12}$"
        )
    )
    created_at: datetime
    checkpoint: ExperimentFixtureCheckpoint
    direct_prompt_path: Literal["direct_prompt.md"] = "direct_prompt.md"
    files: list[ExperimentFixtureFile] = Field(default_factory=list)


class ExperimentFixtureIssue(_StrictExperimentModel):
    code: str
    message: str


class ExperimentFixtureSummary(_StrictExperimentModel):
    fixture_version: Literal["fixture-v1"] = "fixture-v1"
    integrity_verified: Literal[True] = True
    fixture_id: str = Field(
        pattern=(
            r"^fixture-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
            r"[0-9a-f]{4}-[0-9a-f]{12}$"
        )
    )
    created_at: datetime
    relative_path: str
    checkpoint: ExperimentFixtureCheckpoint


class ExperimentFixtureStatus(_StrictExperimentModel):
    project_kind: ProjectKind = "novel"
    lifecycle: BenchmarkFixtureLifecycle | None = None
    eligible: bool
    issues: list[ExperimentFixtureIssue] = Field(default_factory=list)
    checkpoint: ExperimentFixtureCheckpoint | None = None
    existing_fixture: ExperimentFixtureSummary | None = None


class ExperimentFixtureCreateResponse(_StrictExperimentModel):
    created: bool
    fixture: ExperimentFixtureSummary


class ExperimentFixtureTransition(_StrictExperimentModel):
    status: Literal["freeze_failed", "frozen"]
    fixture: ExperimentFixtureSummary | None = None
    failure_code: str | None = None
    failure_message: str | None = None


class ExperimentArmRequest(_StrictExperimentModel):
    arm_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    strategy: ExperimentHookStrategy


class ExperimentRunConfigurationRequest(_StrictExperimentModel):
    fixture_id: str = Field(
        pattern=(
            r"^fixture-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
            r"[0-9a-f]{4}-[0-9a-f]{12}$"
        )
    )
    arms: list[ExperimentArmRequest] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_arms(self) -> "ExperimentRunConfigurationRequest":
        arm_ids = [arm.arm_id for arm in self.arms]
        if len(arm_ids) != len(set(arm_ids)):
            raise ValueError("Experiment arm IDs must be unique.")
        return self


class ExperimentModelBinding(_StrictExperimentModel):
    role: Literal["book", "story_arc", "chapter"]
    purpose: Literal["agent", "evaluator"]
    profile_id: str
    protocol: Literal["openai-compatible", "anthropic-compatible"]
    model: str
    profile_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class ExperimentSchemaSnapshot(_StrictExperimentModel):
    tool_registry: dict[str, int]
    context_policy_version: Literal["context-policy-v1"] = "context-policy-v1"
    evaluation_schema_version: Literal["evaluation-v1"] = "evaluation-v1"
    telemetry_schema_version: Literal[2] = 2
    retry_budget_scope_version: RetryBudgetScopeVersion = RETRY_BUDGET_SCOPE_VERSION


class ExperimentRunConfiguration(_StrictExperimentModel):
    schema_version: Literal[1] = 1
    run_id: str = Field(
        pattern=(
            r"^experiment-run-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
            r"[0-9a-f]{4}-[0-9a-f]{12}$"
        )
    )
    created_at: datetime
    fixture_id: str = Field(
        pattern=(
            r"^fixture-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
            r"[0-9a-f]{4}-[0-9a-f]{12}$"
        )
    )
    checkpoint_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    agent_policy: AgentPolicy
    model_bindings: list[ExperimentModelBinding]
    schemas: ExperimentSchemaSnapshot
    arms: list[ExperimentArmRequest]
    configuration_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class ExperimentRunConfigurationResponse(_StrictExperimentModel):
    configuration: ExperimentRunConfiguration
