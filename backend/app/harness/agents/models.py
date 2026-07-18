from datetime import UTC, datetime
from typing import Annotated, Any, Literal
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
    semantic_revision_limit: int = Field(default=10, ge=0, le=20)
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
    cause_code: str | None = Field(default=None, min_length=1, max_length=128)
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


CandidateKind = Literal["book_direction", "story_arc", "chapter"]
CandidateComponentName = Literal[
    "direction",
    "constraints",
    "confirmed_decision_coverage",
    "recommended_titles",
    "rolling_plan",
    "plan",
    "target_chapter_count",
    "change_summary",
    "draft",
    "observations",
    "state_patch",
]
EvaluationMode = Literal["initial", "repair_verification"]


class BookCandidateSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["book_direction"] = "book_direction"
    direction: str = Field(min_length=1, max_length=100_000)
    constraints: dict[str, Any]
    confirmed_decision_coverage: list[dict[str, Any]] = Field(max_length=500)
    recommended_titles: list[dict[str, Any]] = Field(min_length=3, max_length=5)
    rolling_plan: str = Field(min_length=1, max_length=50_000)


class StoryArcCandidateSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["story_arc"] = "story_arc"
    plan: str = Field(min_length=1, max_length=50_000)
    target_chapter_count: int = Field(ge=1, le=30)
    change_summary: str = Field(min_length=1, max_length=8_000)


class ChapterCandidateSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["chapter"] = "chapter"
    plan: str = Field(min_length=1, max_length=50_000)
    draft: str = Field(min_length=1, max_length=500_000)
    observations: dict[str, Any]
    state_patch: dict[str, Any]


CandidateSnapshot = Annotated[
    BookCandidateSnapshot | StoryArcCandidateSnapshot | ChapterCandidateSnapshot,
    Field(discriminator="kind"),
]


class EvaluationRubricDimension(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    dimension_id: str = Field(min_length=1, max_length=128)
    instruction: str = Field(min_length=1, max_length=2_000)


class EvaluationRubricSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str = Field(min_length=1, max_length=128)
    candidate_kind: CandidateKind
    dimensions: list[EvaluationRubricDimension] = Field(min_length=1, max_length=30)

    @model_validator(mode="after")
    def validate_dimensions(self) -> "EvaluationRubricSnapshot":
        dimension_ids = [item.dimension_id for item in self.dimensions]
        if len(dimension_ids) != len(set(dimension_ids)):
            raise ValueError("Evaluation rubric dimension IDs must be unique.")
        return self


class EvaluationRubricCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dimension_id: str = Field(min_length=1, max_length=128)
    status: Literal["pass", "warning", "blocking"]
    evidence_locator: str = Field(min_length=1, max_length=1_000)
    explanation: str = Field(min_length=1, max_length=4_000)


class PriorIssueCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    issue_id: str = Field(min_length=1, max_length=128)
    status: Literal["resolved", "remaining"]
    evidence_locator: str = Field(min_length=1, max_length=1_000)
    explanation: str = Field(min_length=1, max_length=4_000)


class EvaluationIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    issue_id: str | None = Field(default=None, min_length=1, max_length=128)
    discovery: Literal["initial_discovery", "late_discovery"] = "initial_discovery"
    category: str = Field(min_length=1, max_length=128)
    severity: Literal["warning", "blocking"]
    candidate_locator: str = Field(min_length=1, max_length=1_000)
    evidence_locator: str = Field(min_length=1, max_length=1_000)
    explanation: str = Field(min_length=1, max_length=4_000)


class NewEvaluationIssue(BaseModel):
    """A provider finding whose stable identity is assigned by the Harness."""

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

    schema_version: Literal[1, 2]
    outcome: Literal["pass", "local_repair", "cross_loop_escalation", "needs_user"]
    contract_satisfied: bool
    summary: str = Field(min_length=1, max_length=8_000)
    issues: list[EvaluationIssue] = Field(max_length=100)
    signals: list[EvaluationSignal] = Field(max_length=100)
    repair_brief: str | None = Field(max_length=8_000)
    upstream_blocker: UpstreamBlockerProposal | None
    rubric_checks: list[EvaluationRubricCheck] = Field(default_factory=list, max_length=30)
    prior_issue_checks: list[PriorIssueCheck] = Field(default_factory=list, max_length=100)
    new_issue_ids: list[str] = Field(default_factory=list, max_length=100)
    resolved_issue_ids: list[str] = Field(default_factory=list, max_length=100)
    repair_scope: list[CandidateComponentName] = Field(default_factory=list, max_length=20)

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


class ModelEvaluationResult(BaseModel):
    """Strict provider-facing v2 result before Harness issue-ledger normalization."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[2]
    outcome: Literal["pass", "local_repair", "cross_loop_escalation", "needs_user"]
    contract_satisfied: bool
    summary: str = Field(min_length=1, max_length=8_000)
    rubric_checks: list[EvaluationRubricCheck] = Field(min_length=1, max_length=30)
    prior_issue_checks: list[PriorIssueCheck] = Field(max_length=100)
    new_issues: list[NewEvaluationIssue] = Field(max_length=100)
    signals: list[EvaluationSignal] = Field(max_length=100)
    repair_brief: str | None = Field(max_length=8_000)
    repair_scope: list[CandidateComponentName] = Field(max_length=20)
    upstream_blocker: UpstreamBlockerProposal | None

    @model_validator(mode="after")
    def validate_outcome_payload(self) -> "ModelEvaluationResult":
        if self.outcome == "local_repair" and not self.repair_brief:
            raise ValueError("Local repair requires a repair brief.")
        if self.outcome == "cross_loop_escalation" and self.upstream_blocker is None:
            raise ValueError("Cross-Loop escalation requires an upstream blocker.")
        if self.outcome != "cross_loop_escalation" and self.upstream_blocker is not None:
            raise ValueError("Upstream blocker is only valid for cross-Loop escalation.")
        return self


class EvaluationHistoryEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    evaluation_id: str = Field(min_length=1, max_length=200)
    candidate_revision: int = Field(ge=1)
    candidate_artifact_id: str = Field(min_length=1, max_length=1_000)
    component_fingerprints: dict[CandidateComponentName, str]
    result: EvaluationResult


class RepairContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[2] = 2
    evaluation_id: str = Field(min_length=1, max_length=200)
    source_activation_id: str = Field(min_length=1, max_length=200)
    source_candidate_artifact_id: str = Field(min_length=1, max_length=1_000)
    source_candidate_revision: int = Field(ge=1)
    next_candidate_revision: int = Field(ge=2)
    open_issue_ids: list[str] = Field(min_length=1, max_length=100)
    repair_brief: str = Field(min_length=1, max_length=8_000)
    allowed_components: list[CandidateComponentName] = Field(min_length=1, max_length=20)
    source_component_fingerprints: dict[CandidateComponentName, str]

    @model_validator(mode="after")
    def validate_revision_delta(self) -> "RepairContract":
        if self.next_candidate_revision != self.source_candidate_revision + 1:
            raise ValueError("Repair contract must advance one logical candidate revision.")
        if len(self.open_issue_ids) != len(set(self.open_issue_ids)):
            raise ValueError("Repair contract open issue IDs must be unique.")
        if len(self.allowed_components) != len(set(self.allowed_components)):
            raise ValueError("Repair contract allowed components must be unique.")
        if not set(self.allowed_components).issubset(
            self.source_component_fingerprints
        ):
            raise ValueError("Repair contract authorizes an unknown candidate component.")
        if any(
            len(fingerprint) != 64
            for fingerprint in self.source_component_fingerprints.values()
        ):
            raise ValueError("Repair contract component fingerprints must be SHA-256.")
        return self


class EvaluationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[2] = 2
    identity: AgentIdentity
    candidate_run_id: str | None = Field(default=None, min_length=1, max_length=200)
    checkpoint: str = Field(min_length=1, max_length=200)
    candidate_artifact_id: str = Field(min_length=1, max_length=1_000)
    candidate_revision: int = Field(ge=1)
    mode: EvaluationMode = "initial"
    candidate: CandidateSnapshot
    component_fingerprints: dict[CandidateComponentName, str]
    evidence: list[EvaluationEvidence] = Field(max_length=200)
    deterministic_prechecks: dict[str, bool | int | float | str] = Field(
        default_factory=dict
    )
    rubric: EvaluationRubricSnapshot
    review_history: list[EvaluationHistoryEntry] = Field(default_factory=list, max_length=21)
    expected_repair: RepairContract | None = None

    @model_validator(mode="after")
    def validate_evaluation_context(self) -> "EvaluationInput":
        if self.rubric.candidate_kind != self.candidate.kind:
            raise ValueError("Evaluation rubric does not match the candidate kind.")
        if self.mode == "initial":
            if self.review_history or self.expected_repair is not None:
                raise ValueError("Initial evaluation cannot contain repair history.")
            if self.candidate_revision != 1:
                raise ValueError("Initial logical candidate revision must be 1.")
        else:
            if not self.review_history or self.expected_repair is None:
                raise ValueError("Repair verification requires history and a repair contract.")
            if self.expected_repair.next_candidate_revision != self.candidate_revision:
                raise ValueError("Repair contract revision does not match the candidate.")
            expected_revisions = list(range(1, self.candidate_revision))
            actual_revisions = [item.candidate_revision for item in self.review_history]
            if actual_revisions != expected_revisions:
                raise ValueError("Repair verification requires complete sequential history.")
            prior = self.review_history[-1]
            prior_issue_ids = {
                issue.issue_id for issue in prior.result.issues if issue.issue_id is not None
            }
            contract = self.expected_repair
            if (
                contract.evaluation_id != prior.evaluation_id
                or contract.source_candidate_artifact_id != prior.candidate_artifact_id
                or contract.source_candidate_revision != prior.candidate_revision
                or contract.source_component_fingerprints
                != prior.component_fingerprints
                or set(contract.open_issue_ids) != prior_issue_ids
            ):
                raise ValueError("Repair contract does not match the review-history head.")
        if set(self.component_fingerprints) != set(self.candidate.model_dump(exclude={"kind"})):
            raise ValueError("Candidate component fingerprints are incomplete or unknown.")
        if any(len(value) != 64 for value in self.component_fingerprints.values()):
            raise ValueError("Candidate component fingerprints must be SHA-256.")
        return self

    @property
    def rubric_version(self) -> str:
        return self.rubric.version


class RepairChainEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    activation_id: str = Field(min_length=1, max_length=200)
    candidate_artifact_id: str = Field(min_length=1, max_length=1_000)
    candidate_revision: int = Field(ge=1)
    component_fingerprints: dict[CandidateComponentName, str]
    evaluation_id: str = Field(min_length=1, max_length=200)
    evaluation_path: str = Field(min_length=1, max_length=1_000)
    changed_components: list[CandidateComponentName] = Field(default_factory=list, max_length=20)
    open_issue_ids: list[str] = Field(default_factory=list, max_length=100)
    resolved_issue_ids: list[str] = Field(default_factory=list, max_length=100)
    new_issue_ids: list[str] = Field(default_factory=list, max_length=100)


class RepairChain(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[2] = 2
    identity: AgentIdentity
    candidate_run_id: str = Field(min_length=1, max_length=200)
    candidate_kind: CandidateKind
    semantic_revision_limit: int = Field(ge=0, le=20)
    used_semantic_revisions: int = Field(default=0, ge=0, le=20)
    entries: list[RepairChainEntry] = Field(default_factory=list, max_length=21)
    review_history: list[EvaluationHistoryEntry] = Field(default_factory=list, max_length=21)
    pending_repair: RepairContract | None = None


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
    evaluation_mode: EvaluationMode = "initial"
    result: EvaluationResult
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
