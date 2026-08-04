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
_LOCAL_REVIEW_ISSUE_KINDS = frozenset(
    {
        "explicit_conflict",
        "contract_unfulfilled",
        "unsupported_strong_conclusion",
    }
)


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

    arc_contract_judgment: ArcContractJudgment = Field(
        description=(
            "Directly adjudicate the exact inbound Chapter-to-Arc concern. "
            "remains_applicable means the requested effect can be handled below Book "
            "authority under the unchanged Arc; it must not mean only that the current "
            "Chapter candidate ignored the concern. When book_review_concern is "
            "book_review_required, use remains_applicable because the current Arc stays "
            "authoritative until Book decides. unable_to_judge is reserved for a concrete "
            "creator-owned input need."
        ),
    )
    book_review_concern: Literal[
        "not_required",
        "book_review_required",
    ] = Field(
        description=(
            "Judge whether honoring the exact inbound requested effect requires Book "
            "authority. A candidate that preserves the current Book by ignoring the request "
            "does not make Book review unnecessary."
        ),
    )
    chapter_evidence_concern: Literal[
        "not_required",
        "chapter_evidence_review_required",
    ] = Field(
        description=(
            "Select Chapter evidence review only as its own disposition. Keep this "
            "not_required for a creator wait."
        )
    )
    chapter_evidence_target: ChapterEvidenceTarget | None = Field(
        default=None,
        description="Present exactly when Chapter evidence review is required.",
    )
    summary: str = Field(min_length=1)
    issues: list[EvaluationIssue] = Field(default_factory=list)
    creator_input_need: CreatorInputNeed | None = Field(
        default=None,
        description=(
            "A standalone creator-owned need. It requires unable_to_judge and cannot "
            "be combined with revision, Book review, or Chapter evidence review."
        ),
    )

    @model_validator(mode="after")
    def _chapter_evidence_shape(self) -> ArcParentContractEvaluation:
        required = self.chapter_evidence_concern == "chapter_evidence_review_required"
        book_review_required = self.book_review_concern == "book_review_required"
        revision_warranted = self.arc_contract_judgment == "revision_warranted"
        creator_required = self.creator_input_need is not None
        if (
            book_review_required
            and self.arc_contract_judgment != "remains_applicable"
        ):
            raise ValueError(
                "book_review_required keeps the current Arc contract applicable "
                "until Book authority decides."
            )
        if required != (self.chapter_evidence_target is not None):
            raise ValueError(
                "chapter_evidence_review_required needs one semantic Chapter target."
            )
        _validate_issue_route(
            required=book_review_required,
            kind="parent_authority_concern",
            issues=self.issues,
            route_name="book_review_required",
        )
        _validate_issue_route(
            required=required,
            kind="derived_evidence_mismatch",
            issues=self.issues,
            route_name="chapter_evidence_review_required",
        )
        _validate_creator_input_boundary(
            unable_to_judge=self.arc_contract_judgment == "unable_to_judge",
            creator_input_need=self.creator_input_need,
            issues=self.issues,
            require_standalone=True,
        )
        if book_review_required and (revision_warranted or required or creator_required):
            raise ValueError("Arc parent review may select only one authority disposition.")
        if revision_warranted and (required or creator_required):
            raise ValueError("Arc revision cannot hide an evidence route or creator wait.")
        if self.arc_contract_judgment == "unable_to_judge" and (
            book_review_required or required
        ):
            raise ValueError("unable_to_judge cannot also select another authority route.")
        if creator_required and (book_review_required or revision_warranted or required):
            raise ValueError(
                "A creator input need is a standalone Arc-parent disposition and cannot "
                "also select revision, Book review, or Chapter evidence review."
            )
        if creator_required:
            _validate_only_issue_kinds(
                issues=self.issues,
                allowed=frozenset({"creator_owned_unknown"}),
                route_name="creator wait",
            )
        elif book_review_required:
            _validate_only_issue_kinds(
                issues=self.issues,
                allowed=frozenset({"parent_authority_concern"}),
                route_name="book_review_required",
            )
        elif required:
            _validate_only_issue_kinds(
                issues=self.issues,
                allowed=frozenset({"derived_evidence_mismatch"}),
                route_name="chapter_evidence_review_required",
            )
        elif revision_warranted:
            _validate_only_issue_kinds(
                issues=self.issues,
                allowed=_LOCAL_REVIEW_ISSUE_KINDS,
                route_name="Arc revision",
            )
        elif self.issues:
            raise ValueError("Keeping the Arc baseline cannot carry ignored blockers.")
        return self


class BookParentContractEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    book_contract_judgment: BookContractJudgment = Field(
        description=(
            "Directly adjudicate the exact inbound Arc-to-Book concern at top authority. "
            "remains_applicable means the requested effect is rejected or can be handled "
            "below Book authority without changing the Book baseline; it must not mean only "
            "that current Arc content ignored the concern."
        ),
    )
    arc_evidence_concern: Literal[
        "not_required",
        "arc_evidence_review_required",
    ] = Field(
        description=(
            "Judge whether the inbound concern is actually a derived Arc-evidence problem "
            "that must be corrected below Book rather than a Book contract decision."
        ),
    )
    summary: str = Field(min_length=1)
    issues: list[EvaluationIssue] = Field(default_factory=list)
    creator_input_need: CreatorInputNeed | None = Field(
        default=None,
        description=(
            "A standalone creator-owned need. It requires unable_to_judge and cannot "
            "be combined with Book revision or Arc evidence review."
        ),
    )

    @model_validator(mode="after")
    def _creator_input_boundary(self) -> BookParentContractEvaluation:
        evidence_required = self.arc_evidence_concern == "arc_evidence_review_required"
        revision_warranted = self.book_contract_judgment == "revision_warranted"
        creator_required = self.creator_input_need is not None
        _validate_no_issue_kind(
            kind="parent_authority_concern",
            issues=self.issues,
            message="Book authority has no parent route.",
        )
        _validate_issue_route(
            required=evidence_required,
            kind="derived_evidence_mismatch",
            issues=self.issues,
            route_name="arc_evidence_review_required",
        )
        _validate_creator_input_boundary(
            unable_to_judge=self.book_contract_judgment == "unable_to_judge",
            creator_input_need=self.creator_input_need,
            issues=self.issues,
            require_standalone=True,
        )
        if revision_warranted and (evidence_required or creator_required):
            raise ValueError("Book revision cannot hide an evidence route or creator wait.")
        if (
            self.book_contract_judgment == "unable_to_judge"
            and evidence_required
        ):
            raise ValueError("unable_to_judge cannot also select an evidence route.")
        if creator_required and (revision_warranted or evidence_required):
            raise ValueError(
                "A creator input need is a standalone Book-parent disposition and cannot "
                "also select revision or Arc evidence review."
            )
        if creator_required:
            _validate_only_issue_kinds(
                issues=self.issues,
                allowed=frozenset({"creator_owned_unknown"}),
                route_name="creator wait",
            )
        elif evidence_required:
            _validate_only_issue_kinds(
                issues=self.issues,
                allowed=frozenset({"derived_evidence_mismatch"}),
                route_name="arc_evidence_review_required",
            )
        elif revision_warranted:
            _validate_only_issue_kinds(
                issues=self.issues,
                allowed=_LOCAL_REVIEW_ISSUE_KINDS,
                route_name="Book revision",
            )
        elif self.issues:
            raise ValueError("Keeping the Book baseline cannot carry ignored blockers.")
        return self


class ArcClosureEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    signal_statuses: list[ContractSignalStatus] = Field(min_length=1)
    arc_contract_judgment: ArcContractJudgment = Field(
        description=(
            "Use unable_to_judge only for one standalone creator-owned input need."
        )
    )
    book_review_concern: Literal["not_required", "book_review_required"] = Field(
        description="Keep not_required for a creator wait or lower evidence review."
    )
    chapter_evidence_concern: Literal[
        "not_required",
        "chapter_evidence_review_required",
    ] = Field(
        description=(
            "Select Chapter evidence review only as its own disposition. Keep this "
            "not_required for a creator wait."
        )
    )
    chapter_evidence_target: ChapterEvidenceTarget | None = Field(
        default=None,
        description="Present exactly when Chapter evidence review is required.",
    )
    summary: str = Field(min_length=1)
    issues: list[EvaluationIssue] = Field(default_factory=list)
    creator_input_need: CreatorInputNeed | None = Field(
        default=None,
        description=(
            "A standalone creator-owned need. It requires unable_to_judge and cannot "
            "be combined with revision, Book review, or Chapter evidence review."
        ),
    )

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
        book_review_required = self.book_review_concern == "book_review_required"
        revision_warranted = self.arc_contract_judgment == "revision_warranted"
        creator_required = self.creator_input_need is not None
        if required != (self.chapter_evidence_target is not None):
            raise ValueError(
                "chapter_evidence_review_required needs one semantic Chapter target."
            )
        _validate_issue_route(
            required=book_review_required,
            kind="parent_authority_concern",
            issues=self.issues,
            route_name="book_review_required",
        )
        _validate_issue_route(
            required=required,
            kind="derived_evidence_mismatch",
            issues=self.issues,
            route_name="chapter_evidence_review_required",
        )
        _validate_creator_input_boundary(
            unable_to_judge=self.arc_contract_judgment == "unable_to_judge",
            creator_input_need=self.creator_input_need,
            issues=self.issues,
            require_standalone=True,
        )
        if book_review_required and (revision_warranted or required or creator_required):
            raise ValueError("Arc closure may select only one authority disposition.")
        if revision_warranted and (required or creator_required):
            raise ValueError("Arc revision cannot hide an evidence route or creator wait.")
        if self.arc_contract_judgment == "unable_to_judge" and (
            book_review_required or required
        ):
            raise ValueError("unable_to_judge cannot also select another authority route.")
        if creator_required and (book_review_required or revision_warranted or required):
            raise ValueError(
                "A creator input need is a standalone Arc-closure disposition and cannot "
                "also select revision, Book review, or Chapter evidence review."
            )
        if creator_required:
            _validate_only_issue_kinds(
                issues=self.issues,
                allowed=frozenset({"creator_owned_unknown"}),
                route_name="creator wait",
            )
        elif book_review_required:
            _validate_only_issue_kinds(
                issues=self.issues,
                allowed=frozenset({"parent_authority_concern"}),
                route_name="book_review_required",
            )
        elif required:
            _validate_only_issue_kinds(
                issues=self.issues,
                allowed=frozenset({"derived_evidence_mismatch"}),
                route_name="chapter_evidence_review_required",
            )
        elif revision_warranted:
            _validate_only_issue_kinds(
                issues=self.issues,
                allowed=_LOCAL_REVIEW_ISSUE_KINDS,
                route_name="Arc revision",
            )
        elif all(item.status == "satisfied" for item in self.signal_statuses):
            if self.issues:
                raise ValueError("A satisfied Arc closure cannot carry ignored blockers.")
        else:
            _validate_only_issue_kinds(
                issues=self.issues,
                allowed=_LOCAL_REVIEW_ISSUE_KINDS,
                route_name="Arc closure correction",
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

    @model_validator(mode="after")
    def _creator_input_boundary(self) -> BookCompletionEvaluation:
        creator_required = self.creator_input_need is not None
        _validate_no_issue_kind(
            kind="parent_authority_concern",
            issues=self.issues,
            message="Book completion has no parent authority route.",
        )
        _validate_creator_input_boundary(
            unable_to_judge=self.book_contract_judgment == "unable_to_judge",
            creator_input_need=self.creator_input_need,
            issues=self.issues,
        )
        _validate_no_issue_kind(
            kind="derived_evidence_mismatch",
            issues=self.issues,
            message="Book completion exposes no lower evidence-correction route.",
        )
        if (
            self.book_contract_judgment == "revision_warranted"
            and creator_required
        ):
            raise ValueError("Book revision cannot hide a creator wait.")
        if creator_required:
            _validate_only_issue_kinds(
                issues=self.issues,
                allowed=frozenset({"creator_owned_unknown"}),
                route_name="creator wait",
            )
        else:
            _validate_only_issue_kinds(
                issues=self.issues,
                allowed=_LOCAL_REVIEW_ISSUE_KINDS,
                route_name="Book completion correction",
            )
            if (
                self.book_contract_judgment == "remains_applicable"
                and all(
                    item.status == "satisfied"
                    for item in self.requirement_statuses
                )
                and self.issues
            ):
                raise ValueError(
                    "A satisfied Book completion cannot carry ignored blockers."
                )
        return self


class ChapterEvidenceCorrectionEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    observations_supported_by_frozen_prose: bool
    canon_intent_supported_by_frozen_prose: bool
    descendant_facts_remain_consistent: bool
    summary: str = Field(min_length=1)
    issues: list[EvaluationIssue] = Field(default_factory=list)

    @model_validator(mode="after")
    def _evidence_boundary(self) -> ChapterEvidenceCorrectionEvaluation:
        supported = (
            self.observations_supported_by_frozen_prose
            and self.canon_intent_supported_by_frozen_prose
            and self.descendant_facts_remain_consistent
        )
        if supported and self.issues:
            raise ValueError(
                "A passing evidence-correction verification cannot carry blockers."
            )
        if not supported and not self.issues:
            raise ValueError(
                "A failed evidence-correction verification requires an EP1 blocker."
            )
        _validate_no_issue_kind(
            kind="parent_authority_concern",
            issues=self.issues,
            message="Chapter evidence verification cannot route a parent challenge.",
        )
        _validate_no_issue_kind(
            kind="creator_owned_unknown",
            issues=self.issues,
            message="Chapter evidence verification cannot create a creator wait.",
        )
        return self


def _validate_creator_input_boundary(
    *,
    unable_to_judge: bool,
    creator_input_need: CreatorInputNeed | None,
    issues: list[EvaluationIssue],
    require_standalone: bool = False,
) -> None:
    if unable_to_judge and creator_input_need is None:
        raise ValueError(
            "unable_to_judge requires one concrete creator input need."
        )
    if require_standalone and creator_input_need is not None and not unable_to_judge:
        raise ValueError(
            "A standalone creator input need requires unable_to_judge."
        )
    has_creator_unknown = any(
        issue.kind == "creator_owned_unknown" for issue in issues
    )
    if (creator_input_need is not None) != has_creator_unknown:
        raise ValueError(
            "A creator input need and creator_owned_unknown blocker are atomic."
        )


def _validate_issue_route(
    *,
    required: bool,
    kind: str,
    issues: list[EvaluationIssue],
    route_name: str,
) -> None:
    present = any(issue.kind == kind for issue in issues)
    if required != present:
        raise ValueError(
            f"{route_name} and its {kind} EP1 issue are atomic."
        )


def _validate_no_issue_kind(
    *,
    kind: str,
    issues: list[EvaluationIssue],
    message: str,
) -> None:
    if any(issue.kind == kind for issue in issues):
        raise ValueError(message)


def _validate_only_issue_kinds(
    *,
    issues: list[EvaluationIssue],
    allowed: frozenset[str],
    route_name: str,
) -> None:
    illegal = {issue.kind for issue in issues}.difference(allowed)
    if illegal:
        raise ValueError(
            f"{route_name} cannot carry unrelated EP1 issue kinds: "
            f"{', '.join(sorted(illegal))}."
        )
