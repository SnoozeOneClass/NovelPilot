from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.projects import (
    RETRY_BUDGET_SCOPE_VERSION,
    RetryBudgetScopeVersion,
)


AgentRole = Literal["book", "story_arc", "chapter"]
AgentLifecycle = Literal[
    "idle",
    "running",
    "waiting_user",
    "blocked",
    "completed",
    "failed",
]
FailureCategory = Literal[
    "transport_provider",
    "unsupported_capability",
    "malformed_model_output",
    "harness_conflict",
    "local_semantic",
    "cross_loop_semantic",
    "needs_user",
    "exhausted",
    "cancelled",
]
FailureScope = Literal["gateway", "profile", "harness", "book", "story_arc", "chapter"]


class AgentIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str = Field(min_length=1)
    role: AgentRole
    scope_id: str | None = None

    @model_validator(mode="after")
    def validate_scope(self) -> "AgentIdentity":
        if self.role == "book" and self.scope_id is not None:
            raise ValueError("Book Agent identity cannot have a nested scope ID.")
        if self.role != "book" and not self.scope_id:
            raise ValueError(f"{self.role} Agent identity requires a scope ID.")
        return self

    @property
    def key(self) -> str:
        return self.role if self.scope_id is None else f"{self.role}:{self.scope_id}"


class AgentBudgets(BaseModel):
    model_config = ConfigDict(extra="forbid")

    budget_scope_version: RetryBudgetScopeVersion = RETRY_BUDGET_SCOPE_VERSION
    max_turns: int = Field(ge=1, le=200)
    used_turns: int = Field(default=0, ge=0)
    tool_schema_repair_limit: int = Field(default=2, ge=0, le=20)
    used_tool_schema_repairs: int = Field(default=0, ge=0)
    semantic_revision_limit: int = Field(default=2, ge=0, le=20)
    used_semantic_revisions: int = Field(default=0, ge=0)
    transport_retry_limit: int = Field(default=3, ge=0, le=20)
    used_transport_retries: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_usage(self) -> "AgentBudgets":
        limits = [
            (self.used_turns, self.max_turns, "Agent turns"),
            (
                self.used_tool_schema_repairs,
                self.tool_schema_repair_limit,
                "Tool/schema repairs",
            ),
            (
                self.used_semantic_revisions,
                self.semantic_revision_limit,
                "semantic revisions",
            ),
            (
                self.used_transport_retries,
                self.transport_retry_limit,
                "transport retries",
            ),
        ]
        for used, limit, label in limits:
            if used > limit:
                raise ValueError(f"Used {label} cannot exceed its limit.")
        return self


class AgentState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    identity: AgentIdentity
    lifecycle: AgentLifecycle = "idle"
    candidate_run_id: str | None = None
    activation_id: str | None = None
    phase: str | None = None
    expected_revision: int | None = Field(default=None, ge=0)
    budgets: AgentBudgets | None = None
    last_checkpoint_id: str | None = None
    summary: str = Field(default="", max_length=20_000)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class FailureEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    failure_id: str = Field(default_factory=lambda: str(uuid4()))
    category: FailureCategory
    code: str = Field(min_length=1, max_length=128)
    scope: FailureScope
    recoverable: bool
    responsible_component: str = Field(min_length=1, max_length=200)
    identity: AgentIdentity | None = None
    candidate_run_id: str | None = None
    activation_id: str | None = None
    checkpoint: str | None = None
    candidate_revision: int | None = Field(default=None, ge=0)
    message: str = Field(min_length=1, max_length=4_000)
    evidence: list[str] = Field(default_factory=list, max_length=100)
    consumed_budgets: dict[str, int] = Field(default_factory=dict)
    remaining_budgets: dict[str, int] = Field(default_factory=dict)
    allowed_actions: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ToolExecutionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok", "error"]
    tool_name: str
    tool_call_id: str
    content: dict[str, Any] = Field(default_factory=dict)
    recoverable: bool = False
    error_code: str | None = None
    message: str = ""
    checkpoint_id: str | None = None
    terminal: bool = False
    artifact_paths: list[str] = Field(default_factory=list)
    allowed_actions: list[str] = Field(default_factory=list)
    replayed: bool = False


class ToolReplayRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    tool_name: str
    tool_call_id: str
    argument_digest: str = Field(min_length=64, max_length=64)
    result: ToolExecutionResult


class AgentRunResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome: Literal["candidate", "waiting_user", "blocked", "failed"]
    identity: AgentIdentity
    candidate_run_id: str
    activation_id: str
    turns_used: int
    terminal_result: ToolExecutionResult | None = None
    failure: FailureEnvelope | None = None
    model_snapshot: str | None = None
    provider_snapshot: str | None = None
    usage: dict[str, Any] = Field(default_factory=dict)


class EvaluationEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    locator: str = Field(min_length=1, max_length=1_000)
    excerpt: str = Field(min_length=1, max_length=4_000)


class EvaluationIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: str = Field(min_length=1, max_length=128)
    severity: Literal["warning", "blocking"]
    candidate_locator: str = Field(min_length=1, max_length=1_000)
    evidence_locator: str = Field(min_length=1, max_length=1_000)
    explanation: str = Field(min_length=1, max_length=4_000)


class EvaluationSignal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    value: int | float | bool | str
    evidence_locator: str = Field(min_length=1, max_length=1_000)


class UpstreamBlockerProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    owner: Literal["book", "story_arc"]
    contract_field: str = Field(min_length=1, max_length=1_000)
    contract_revision: int = Field(ge=1)
    committed_evidence_locator: str = Field(min_length=1, max_length=1_000)
    impossibility_reason: str = Field(min_length=1, max_length=4_000)


class EvaluationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    outcome: Literal["pass", "local_repair", "cross_loop_escalation", "needs_user"]
    contract_satisfied: bool
    summary: str = Field(min_length=1, max_length=8_000)
    issues: list[EvaluationIssue] = Field(max_length=100)
    signals: list[EvaluationSignal] = Field(max_length=100)
    repair_brief: str | None = Field(max_length=8_000)
    upstream_blocker: UpstreamBlockerProposal | None

    @model_validator(mode="after")
    def validate_outcome_payload(self) -> "EvaluationResult":
        if self.outcome == "pass" and (not self.contract_satisfied or self.issues):
            raise ValueError("A passing evaluation must satisfy the contract without issues.")
        if self.outcome == "local_repair" and not self.repair_brief:
            raise ValueError("Local repair requires a repair brief.")
        if self.outcome == "cross_loop_escalation" and self.upstream_blocker is None:
            raise ValueError("Cross-Loop escalation requires an upstream blocker.")
        if self.outcome != "cross_loop_escalation" and self.upstream_blocker is not None:
            raise ValueError("Upstream blocker is only valid for cross-Loop escalation.")
        return self


class EvaluationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    identity: AgentIdentity
    candidate_run_id: str | None = Field(default=None, min_length=1, max_length=200)
    checkpoint: str = Field(min_length=1, max_length=200)
    candidate_artifact_id: str = Field(min_length=1, max_length=1_000)
    candidate_revision: int = Field(ge=1)
    candidate_content: str = Field(min_length=1, max_length=500_000)
    evidence: list[EvaluationEvidence] = Field(max_length=200)
    deterministic_prechecks: dict[str, bool | int | float | str] = Field(
        default_factory=dict
    )
    rubric_version: str = Field(min_length=1, max_length=128)


class EvaluationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    evaluation_id: str = Field(default_factory=lambda: str(uuid4()))
    candidate_run_id: str | None = Field(default=None, min_length=1, max_length=200)
    input_fingerprint: str | None = Field(default=None, min_length=64, max_length=64)
    candidate_artifact_id: str
    candidate_revision: int = Field(ge=1)
    evaluator_profile_id: str
    evaluator_model_snapshot: str
    evaluator_provider_snapshot: str
    rubric_version: str
    result: EvaluationResult
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
