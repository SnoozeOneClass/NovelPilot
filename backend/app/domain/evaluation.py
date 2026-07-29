from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.agents.contracts import EvaluationIssue

EvidenceStatus = Literal["satisfied", "unresolved", "contradicted"]
ArcContractJudgment = Literal[
    "remains_applicable",
    "revision_warranted",
    "unable_to_judge",
]
BookContractJudgment = Literal[
    "remains_applicable",
    "revision_warranted",
    "unable_to_judge",
]


class CreatorInputNeed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    controlled_fact: str = Field(
        min_length=1,
        description=(
            "The concrete fact, preference, clarification, approval, or direction "
            "decision that only the creator controls."
        ),
    )
    question: str = Field(
        min_length=1,
        description="One specific question that would resolve the missing creator input.",
    )
    evidence: list[str] = Field(
        min_length=1,
        description="Why committed evidence cannot resolve this question without the user.",
    )


class ContractSignalStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    signal_key: str = Field(min_length=1)
    status: EvidenceStatus
    evidence: list[str] = Field(
        default_factory=list,
        description="Committed Chapter or Canon evidence relevant to this signal.",
    )
    rationale: str = Field(min_length=1)


class CompletionRequirementStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    requirement_key: str = Field(min_length=1)
    status: EvidenceStatus
    evidence: list[str] = Field(default_factory=list)
    rationale: str = Field(min_length=1)


class ChapterEvidenceTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    chapter_book_ordinal: int = Field(
        ge=1,
        description=(
            "Human-visible Chapter ordinal whose observations or Canon intent "
            "need evidence-only correction; never a storage identity."
        ),
    )
    correction_goal: str = Field(
        min_length=1,
        description=(
            "The precise observations/Canon evidence concern to correct without "
            "changing the approved plan or prose."
        ),
    )


class ArcParentContractEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    arc_contract_judgment: ArcContractJudgment
    book_review_concern: Literal["not_required", "book_review_required"]
    chapter_evidence_concern: Literal[
        "not_required",
        "chapter_evidence_review_required",
    ]
    chapter_evidence_target: ChapterEvidenceTarget | None = None
    summary: str = Field(min_length=1)
    issues: list[EvaluationIssue] = Field(default_factory=list)
    creator_input_need: CreatorInputNeed | None = None

    @model_validator(mode="after")
    def _chapter_evidence_shape(self) -> ArcParentContractEvaluation:
        required = self.chapter_evidence_concern == "chapter_evidence_review_required"
        if required != (self.chapter_evidence_target is not None):
            raise ValueError(
                "chapter_evidence_review_required needs one semantic Chapter target."
            )
        return self


class BookParentContractEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    book_contract_judgment: BookContractJudgment
    arc_evidence_concern: Literal["not_required", "arc_evidence_review_required"]
    summary: str = Field(min_length=1)
    issues: list[EvaluationIssue] = Field(default_factory=list)
    creator_input_need: CreatorInputNeed | None = None


class ArcClosureEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    signal_statuses: list[ContractSignalStatus] = Field(min_length=1)
    arc_contract_judgment: ArcContractJudgment
    book_review_concern: Literal["not_required", "book_review_required"]
    chapter_evidence_concern: Literal[
        "not_required",
        "chapter_evidence_review_required",
    ]
    chapter_evidence_target: ChapterEvidenceTarget | None = None
    summary: str = Field(min_length=1)
    issues: list[EvaluationIssue] = Field(default_factory=list)
    creator_input_need: CreatorInputNeed | None = None

    @field_validator("signal_statuses")
    @classmethod
    def _unique_signal_keys(
        cls, value: list[ContractSignalStatus]
    ) -> list[ContractSignalStatus]:
        keys = [item.signal_key for item in value]
        if len(keys) != len(set(keys)):
            raise ValueError("Arc closure signal keys must be unique.")
        return value

    @model_validator(mode="after")
    def _chapter_evidence_shape(self) -> ArcClosureEvaluation:
        required = self.chapter_evidence_concern == "chapter_evidence_review_required"
        if required != (self.chapter_evidence_target is not None):
            raise ValueError(
                "chapter_evidence_review_required needs one semantic Chapter target."
            )
        return self


class BookCompletionEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    requirement_statuses: list[CompletionRequirementStatus] = Field(min_length=1)
    book_contract_judgment: BookContractJudgment
    summary: str = Field(min_length=1)
    issues: list[EvaluationIssue] = Field(default_factory=list)
    creator_input_need: CreatorInputNeed | None = None

    @field_validator("requirement_statuses")
    @classmethod
    def _unique_requirement_keys(
        cls, value: list[CompletionRequirementStatus]
    ) -> list[CompletionRequirementStatus]:
        keys = [item.requirement_key for item in value]
        if len(keys) != len(set(keys)):
            raise ValueError("Book completion requirement keys must be unique.")
        return value


class ChapterEvidenceCorrectionEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    observations_supported_by_frozen_prose: bool
    canon_intent_supported_by_frozen_prose: bool
    descendant_facts_remain_consistent: bool
    summary: str = Field(min_length=1)
    issues: list[EvaluationIssue] = Field(default_factory=list)
    creator_input_need: CreatorInputNeed | None = None
