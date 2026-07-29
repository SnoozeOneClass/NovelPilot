from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.agents.contracts import (
    ArcChapterOutlineEntry,
    ArcClosureSignal,
    ArcStateTransition,
    EvaluationIssue,
)

ArcRepairComponent = Literal[
    "title",
    "desired_state_transition",
    "conflict_trajectory",
    "pacing_trajectory",
    "character_obligations",
    "foreshadowing_obligations",
    "prohibitions",
    "closure_signals",
    "chapter_outline",
]
ArcReviewDecision = Literal["pass", "local_repair", "escalate_to_book", "needs_user"]


class ArcTitleRepair(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    component: Literal["title"]
    value: str = Field(min_length=1, description="Replacement Story Arc title.")


class ArcStateTransitionRepair(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    component: Literal["desired_state_transition"]
    value: ArcStateTransition


class ArcConflictTrajectoryRepair(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    component: Literal["conflict_trajectory"]
    value: list[str] = Field(min_length=1)


class ArcPacingTrajectoryRepair(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    component: Literal["pacing_trajectory"]
    value: list[str] = Field(min_length=1)


class ArcCharacterObligationsRepair(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    component: Literal["character_obligations"]
    value: list[str] = Field(min_length=1)


class ArcForeshadowingObligationsRepair(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    component: Literal["foreshadowing_obligations"]
    value: list[str]


class ArcProhibitionsRepair(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    component: Literal["prohibitions"]
    value: list[str] = Field(min_length=1)


class ArcClosureSignalsRepair(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    component: Literal["closure_signals"]
    value: list[ArcClosureSignal] = Field(min_length=1)


class ArcChapterOutlineRepair(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    component: Literal["chapter_outline"]
    value: list[ArcChapterOutlineEntry]


ArcRepairChange = Annotated[
    ArcTitleRepair
    | ArcStateTransitionRepair
    | ArcConflictTrajectoryRepair
    | ArcPacingTrajectoryRepair
    | ArcCharacterObligationsRepair
    | ArcForeshadowingObligationsRepair
    | ArcProhibitionsRepair
    | ArcClosureSignalsRepair
    | ArcChapterOutlineRepair,
    Field(discriminator="component"),
]


class ArcRepairPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    changes: list[ArcRepairChange] = Field(
        min_length=1,
        max_length=5,
        description=(
            "Only Story Arc components authorized by the repair contract in frozen context. "
            "Every returned replacement must differ from its current value. "
            "Omitted components are preserved by the Harness and must not be repeated."
        ),
    )

    @field_validator("changes")
    @classmethod
    def _unique_components(
        cls,
        value: list[ArcRepairChange],
    ) -> list[ArcRepairChange]:
        components = [change.component for change in value]
        if len(components) != len(set(components)):
            raise ValueError("An Arc repair patch may change each component at most once.")
        return value


class ArcEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    decision: ArcReviewDecision = Field(
        description=(
            "Use local_repair only for a bounded Arc repair; use escalate_to_book when "
            "the approved Book direction itself must change."
        ),
    )
    summary: str = Field(min_length=1, description="Evidence-based Story Arc assessment.")
    issues: list[EvaluationIssue] = Field(default_factory=list)
    repair_scope: list[ArcRepairComponent] = Field(
        default_factory=list,
        description=(
            "Required and non-empty only when decision is local_repair; otherwise empty."
        ),
    )

    @model_validator(mode="after")
    def _decision_boundary(self) -> ArcEvaluation:
        if (self.decision == "local_repair") != bool(self.repair_scope):
            raise ValueError("Exactly local_repair requires a bounded Arc repair scope.")
        return self


class ArcRepairContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    authorized_components: list[ArcRepairComponent] = Field(min_length=1)
    issues: list[EvaluationIssue] = Field(default_factory=list)

    @field_validator("authorized_components")
    @classmethod
    def _unique_components(
        cls,
        value: list[ArcRepairComponent],
    ) -> list[ArcRepairComponent]:
        if len(value) != len(set(value)):
            raise ValueError("Arc repair components must be unique.")
        return value


class CreateStoryArcRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    book_id: str
    expected_book_baseline_id: str
    expected_canon_baseline_id: str
    expected_ordinal: int = Field(ge=1)
    source_progress_handoff_id: str | None = None
    source_task_id: str | None = None


class CreateStoryArcResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    book_id: str
    arc_id: str
    workspace_id: str
    ordinal: int = Field(ge=1)
    workspace_lock_version: int = 1


class RebaseStaleArcRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    book_id: str
    arc_id: str
    expected_workspace_lock_version: int = Field(ge=1)
    expected_book_baseline_id: str
    expected_arc_baseline_id: str | None
    expected_canon_baseline_id: str


class RebaseStaleArcResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    arc_id: str
    workspace_lock_version: int = Field(ge=1)
    base_arc_baseline_id: str | None
    book_baseline_id: str
    canon_baseline_id: str


class ApplyArcTaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    book_id: str
    arc_id: str
    task_id: str
    attempt_id: str
    expected_workspace_lock_version: int = Field(ge=1)


class ApplyArcTaskResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    arc_id: str
    task_id: str
    delivery: Literal["applied", "discarded_stale"]
    workspace_lock_version: int = Field(ge=1)


class SubmitArcRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    book_id: str
    arc_id: str
    expected_workspace_lock_version: int = Field(ge=1)


class SubmitArcResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    arc_id: str
    submission_id: str
    content_fingerprint: str


class RecordArcReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    book_id: str
    arc_id: str
    submission_id: str
    evaluator_task_id: str
    evaluator_attempt_id: str
    rubric_id: str
    rubric_version: int = Field(ge=1)
    deterministic_precheck: dict[str, object]


class RecordArcReviewResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    arc_id: str
    submission_id: str
    review_id: str
    decision: ArcReviewDecision
    approval_gate_id: str | None = None
    next_action: Literal[
        "auto_commit",
        "await_approval",
        "repair",
        "await_user",
        "escalated_to_book",
        "failure_paused",
    ]


class CommitArcAutoRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    book_id: str
    arc_id: str
    submission_id: str
    review_id: str
    expected_current_baseline_id: str | None = None


class ApproveArcRequest(CommitArcAutoRequest):
    approval_gate_id: str


class RejectArcGateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    book_id: str
    arc_id: str
    submission_id: str
    review_id: str
    approval_gate_id: str


class CommitArcResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    arc_id: str
    baseline_id: str
    baseline_version: int = Field(ge=1)
    closure_cumulative_chapter_count: int = Field(ge=1)
    authorization_kind: Literal["policy_auto", "human_approval"]
    lifecycle_status: Literal["active", "closing"]


class RejectArcGateResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    arc_id: str
    approval_gate_id: str
    rejected: bool = True
    workspace_lock_version: int = Field(ge=1)
