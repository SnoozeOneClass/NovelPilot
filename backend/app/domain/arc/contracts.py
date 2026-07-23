from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.agents.contracts import EvaluationIssue

ArcPurpose = Literal["regular", "final"]
ArcRepairComponent = Literal[
    "title",
    "purpose",
    "beats",
    "target_chapter_count",
    "completion_signals",
]
ArcReviewDecision = Literal["pass", "local_repair", "escalate_to_book", "needs_user"]


class ArcTitleRepair(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    component: Literal["title"]
    value: str = Field(min_length=1, description="Replacement Story Arc title.")


class ArcPurposeRepair(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    component: Literal["purpose"]
    value: str = Field(min_length=1, description="Replacement Story Arc purpose.")


class ArcBeatsRepair(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    component: Literal["beats"]
    value: list[str] = Field(min_length=1, description="Replacement ordered Story Arc beats.")


class ArcTargetChapterCountRepair(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    component: Literal["target_chapter_count"]
    value: int = Field(ge=1, le=30)


class ArcCompletionSignalsRepair(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    component: Literal["completion_signals"]
    value: list[str] = Field(
        min_length=1,
        description="Replacement observable Story Arc completion conditions.",
    )


ArcRepairChange = Annotated[
    ArcTitleRepair
    | ArcPurposeRepair
    | ArcBeatsRepair
    | ArcTargetChapterCountRepair
    | ArcCompletionSignalsRepair,
    Field(discriminator="component"),
]


class ArcRepairPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    changes: list[ArcRepairChange] = Field(
        min_length=1,
        max_length=5,
        description=(
            "Only Story Arc components authorized by the repair contract in frozen context. "
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
    purpose: ArcPurpose = "regular"
    source_task_id: str | None = None


class CreateStoryArcResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    book_id: str
    arc_id: str
    workspace_id: str
    ordinal: int = Field(ge=1)
    purpose: ArcPurpose
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
    target_chapter_count: int = Field(ge=1, le=30)


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
    target_chapter_count: int = Field(ge=1, le=30)
    authorization_kind: Literal["policy_auto", "human_approval"]
    lifecycle_status: Literal["active", "completed"]


class RejectArcGateResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    arc_id: str
    approval_gate_id: str
    rejected: bool = True
    workspace_lock_version: int = Field(ge=1)
