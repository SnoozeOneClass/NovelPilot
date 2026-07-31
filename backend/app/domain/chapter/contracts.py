from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.agents.contracts import (
    ChapterEvaluationIssue,
    ChapterObservationResult,
    ChapterRepairComponent,
)

ChapterComponent = Literal[
    "plan",
    "draft",
    "observations",
    "repair_plan",
    "repair_prose",
    "repair_observations",
]
ChapterReviewDecision = Literal[
    "pass",
    "local_repair",
    "escalate_to_arc",
]
ChapterRepairStage = Literal[
    "primary_semantic",
    "derived_dependency_closure",
]


class ChapterRepairContract(BaseModel):
    """Harness-owned repair authority shared by Domain delivery and Route."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        serialize_by_alias=True,
    )

    contract_schema: Literal["chapter-repair-contract-v5"] = Field(
        default="chapter-repair-contract-v5",
        alias="schema",
    )
    repair_stage: ChapterRepairStage
    authorized_components: list[ChapterRepairComponent] = Field(min_length=1)
    issues: list[ChapterEvaluationIssue] = Field(min_length=1)
    issue_fingerprints: list[str] = Field(min_length=1)
    stalled_issue_fingerprints: list[str] = Field(default_factory=list)

    @field_validator("authorized_components")
    @classmethod
    def _unique_components(
        cls,
        value: list[ChapterRepairComponent],
    ) -> list[ChapterRepairComponent]:
        if len(value) != len(set(value)):
            raise ValueError("Chapter repair components must be unique.")
        return value

    @field_validator("issue_fingerprints", "stalled_issue_fingerprints")
    @classmethod
    def _valid_fingerprints(cls, value: list[str]) -> list[str]:
        if any(not fingerprint.strip() for fingerprint in value):
            raise ValueError("Chapter repair fingerprints must be non-blank.")
        return value

    @field_validator("stalled_issue_fingerprints")
    @classmethod
    def _unique_stalled_fingerprints(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("Stalled Chapter repair fingerprints must be unique.")
        return value

    @model_validator(mode="after")
    def _stage_scope(self) -> ChapterRepairContract:
        scope = set(self.authorized_components)
        issue_scope = {
            component
            for issue in self.issues
            for component in issue.affected_components
        }
        if scope != issue_scope:
            raise ValueError(
                "Chapter repair authorization must equal the typed issue component union."
            )
        if "plan" in scope and scope != {"plan"}:
            raise ValueError(
                "A Chapter plan repair must be the only authorized component."
            )
        if self.repair_stage == "derived_dependency_closure" and not scope <= {
            "observations",
            "canon",
        }:
            raise ValueError(
                "A derived dependency closure may change only observations or Canon."
            )
        if len(self.issue_fingerprints) != len(self.issues):
            raise ValueError(
                "Each Chapter repair issue requires one frozen semantic fingerprint."
            )
        return self


class CreateChapterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    book_id: str
    arc_id: str
    expected_book_baseline_id: str
    expected_arc_baseline_id: str
    expected_canon_baseline_id: str


class CreateChapterResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    chapter_id: str
    workspace_id: str
    book_ordinal: int = Field(ge=1)
    arc_ordinal: int = Field(ge=1)
    workspace_lock_version: int = Field(ge=1)


class RebaseStaleChapterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    book_id: str
    arc_id: str
    chapter_id: str
    expected_workspace_lock_version: int = Field(ge=1)
    expected_book_baseline_id: str
    expected_arc_baseline_id: str
    expected_chapter_baseline_id: str | None
    expected_canon_baseline_id: str


class RebaseStaleChapterResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    chapter_id: str
    workspace_lock_version: int = Field(ge=1)
    base_chapter_baseline_id: str | None
    book_baseline_id: str
    arc_baseline_id: str
    canon_baseline_id: str


class ApplyChapterTaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    chapter_id: str
    task_id: str
    attempt_id: str
    expected_workspace_lock_version: int = Field(ge=1)


class ApplyChapterTaskResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    chapter_id: str
    task_id: str
    component: ChapterComponent
    delivery: Literal["applied", "discarded_stale"]
    workspace_lock_version: int = Field(ge=1)


class SubmitChapterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    chapter_id: str
    expected_workspace_lock_version: int = Field(ge=1)


class SubmitChapterResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    chapter_id: str
    submission_id: str
    content_fingerprint: str


class RecordChapterReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    chapter_id: str
    submission_id: str
    evaluator_task_id: str
    evaluator_attempt_id: str
    rubric_id: str
    rubric_version: int = Field(ge=1)


class RecordChapterReviewResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    chapter_id: str
    submission_id: str
    review_id: str
    decision: ChapterReviewDecision


class CommitChapterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    chapter_id: str
    submission_id: str
    review_id: str
    expected_current_chapter_baseline_id: str | None = None
    expected_canon_baseline_id: str


class CommitChapterResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    chapter_id: str
    chapter_baseline_id: str
    chapter_baseline_version: int = Field(ge=1)
    canon_before_id: str
    canon_after_id: str
    canon_changed: bool
    arc_closure_due: bool


class ChapterTextView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    chapter_id: str
    chapter_title: str
    prose: str

    @field_validator("chapter_title", "prose")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Committed Chapter title and prose must be non-blank.")
        return value


class CommittedChapterObservationSource(BaseModel):
    """Harness-owned authority binding for one committed observation document."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    chapter_id: str
    chapter_baseline_id: str
    prose_ref_id: str
    prose_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class CommittedEstablishedFact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    fact_ordinal: int = Field(ge=1)
    statement: str = Field(min_length=1)
    evidence_hint: str = Field(min_length=1)


class CommittedChapterObservation(BaseModel):
    """Correctable historical index derived from one exact formal Chapter prose."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_id: Literal["chapter-committed-observation-v1"] = (
        "chapter-committed-observation-v1"
    )
    source: CommittedChapterObservationSource
    summary: str = Field(min_length=1)
    established_facts: list[CommittedEstablishedFact]


def bind_committed_chapter_observation(
    *,
    candidate: ChapterObservationResult,
    chapter_id: str,
    chapter_baseline_id: str,
    prose_ref_id: str,
    prose_sha256: str,
) -> CommittedChapterObservation:
    """Bind untrusted semantic candidates to exact Harness-owned formal authority."""

    return CommittedChapterObservation(
        source=CommittedChapterObservationSource(
            chapter_id=chapter_id,
            chapter_baseline_id=chapter_baseline_id,
            prose_ref_id=prose_ref_id,
            prose_sha256=prose_sha256,
        ),
        summary=candidate.summary.strip(),
        established_facts=[
            CommittedEstablishedFact(
                fact_ordinal=ordinal,
                statement=fact.statement,
                evidence_hint=fact.evidence_hint,
            )
            for ordinal, fact in enumerate(candidate.established_facts, start=1)
        ],
    )
