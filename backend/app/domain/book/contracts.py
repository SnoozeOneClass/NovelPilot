from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.agents.contracts import EvaluationIssue


class BookSuggestion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    label: str
    message: str
    rationale: str = ""
    recommended: bool = False
    action: Literal["answer", "select_title"] = "answer"
    value: str | None = None

    @model_validator(mode="after")
    def _action_value(self) -> BookSuggestion:
        if self.action == "select_title" and not (self.value or "").strip():
            raise ValueError("A title suggestion requires a title value.")
        if self.action == "answer" and self.value is not None:
            raise ValueError("An ordinary suggestion cannot carry a control value.")
        return self


class BookSupersededDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    turn: int = Field(ge=1)
    decision: str
    replacement: str | None
    reason: str
    user_evidence: str


class BookDiscussionState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_id: Literal["book-discussion-state-v1"] = "book-discussion-state-v1"
    turn_count: int = Field(ge=0)
    direction_draft: str
    discussion_summary: str
    confirmed_decisions: list[str] = Field(default_factory=list)
    superseded_decisions: list[BookSupersededDecision] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    contradictions: list[str] = Field(default_factory=list)
    selected_title: str | None = None
    selected_title_source: Literal["recommended", "custom"] | None = None
    question: str | None = None
    suggestions: list[BookSuggestion] = Field(default_factory=list)
    readiness_status: Literal["awaiting_agent", "continue", "ready"]
    readiness_reason: str

    @model_validator(mode="after")
    def _discussion_state_boundary(self) -> BookDiscussionState:
        if (self.selected_title is None) != (self.selected_title_source is None):
            raise ValueError("A selected title and its source must be persisted together.")
        if self.readiness_status == "awaiting_agent":
            if self.question is not None or self.suggestions:
                raise ValueError("An awaiting-agent state cannot expose stale suggestions.")
            return self
        if self.readiness_status == "ready":
            if self.selected_title is None:
                raise ValueError("A ready Book discussion requires a selected title.")
            if self.question is not None or self.suggestions:
                raise ValueError("A ready Book discussion cannot expose another question.")
            return self
        if self.question is None or not 2 <= len(self.suggestions) <= 3:
            raise ValueError("A continuing Book discussion requires one question and 2-3 options.")
        return self


class BookTranscriptMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sequence: int = Field(ge=1)
    role: Literal["user", "assistant"]
    content: str

    @field_validator("content")
    @classmethod
    def _message_non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Book transcript messages must be non-blank.")
        return value


class BookTranscript(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_id: Literal["book-transcript-v1"] = "book-transcript-v1"
    messages: list[BookTranscriptMessage]


class RecordBookUserInputRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    book_id: str
    expected_workspace_lock_version: int = Field(ge=1)
    message: str
    suggestion_id: str | None = None

    @field_validator("message")
    @classmethod
    def _input_non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Book discussion input must be non-blank.")
        return value.strip()


class RecordBookUserInputResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    book_id: str
    workspace_lock_version: int
    selected_title: str | None


class ApplyBookDiscussionTaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    book_id: str
    task_id: str
    attempt_id: str
    expected_workspace_lock_version: int = Field(ge=1)


class ApplyBookDiscussionTaskResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    book_id: str
    task_id: str
    delivery: Literal["applied", "discarded_stale"]
    workspace_lock_version: int = Field(ge=1)
    readiness_status: Literal["continue", "ready"]
    selected_title: str | None


class ApplyBookCandidateTaskRequest(ApplyBookDiscussionTaskRequest):
    pass


class ApplyBookCandidateTaskResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    book_id: str
    task_id: str
    delivery: Literal["applied", "discarded_stale"]
    workspace_lock_version: int = Field(ge=1)


class BookCompletionRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    requirement_key: str = Field(
        min_length=1,
        pattern=r"^[a-z0-9][a-z0-9_.-]*$",
    )
    description: str = Field(min_length=1)
    evidence_expectation: str = Field(min_length=1)
    required: Literal[True] = True


class CompletionContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    completion_requirements: list[BookCompletionRequirement] = Field(
        min_length=1,
        description=(
            "Keyed semantic conditions that must all be satisfied before the "
            "novel can complete. Every requirement_key must be copied exactly "
            "into at least one Arc contract's completion_requirement_keys."
        ),
    )

    @model_validator(mode="after")
    def _unique_requirements(self) -> CompletionContract:
        keys = [item.requirement_key for item in self.completion_requirements]
        if len(keys) != len(set(keys)):
            raise ValueError("Book completion requirement keys must be unique.")
        return self


class BookCreativeConstraints(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    genre_reader_promise: str = Field(min_length=1)
    premise_story_engine: str = Field(min_length=1)
    stable_world_invariants: list[str] = Field(min_length=1)
    stable_character_invariants: list[str] = Field(min_length=1)
    core_selling_points: list[str] = Field(min_length=1)
    prohibited_outcomes: list[str] = Field(min_length=1)


class BookRollingPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    long_term_character_directions: list[str] = Field(min_length=1)
    whole_book_pacing_strategy: str = Field(min_length=1)
    ending_tendency: str = Field(min_length=1)
    arc_planning_guidelines: list[str] = Field(min_length=1)
    whole_book_scale_guidance: str = Field(
        min_length=1,
        description=(
            "Advisory whole-book scale guidance derived from creator intent. "
            "It is context for planning, never a Chapter-count, routing, approval, "
            "or completion predicate."
        ),
    )


class BookArcContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    whole_book_role: str = Field(
        min_length=1,
        description=(
            "The semantic role this Story Arc serves in the whole Book. Do not "
            "include Chapter allocation, titles, events, or scenes."
        ),
    )
    core_goal: str = Field(
        min_length=1,
        description="The Book-level outcome this Story Arc must achieve.",
    )
    handoff_from_previous: str = Field(
        min_length=1,
        description=(
            "How this Story Arc semantically receives the prior Arc or the Book "
            "opening. This is not a storage identity or an exact-copy protocol."
        ),
    )
    exit_conditions: list[str] = Field(
        min_length=1,
        description=(
            "Observable semantic conditions that permit this Story Arc to close. "
            "Do not prescribe Chapter counts or a Chapter-by-Chapter outline."
        ),
    )
    completion_requirement_keys: list[str] = Field(
        description=(
            "Book completion requirement keys whose semantic outcome this Story Arc "
            "is responsible for advancing or establishing. This assigns Book-level "
            "responsibility only; it does not prescribe Chapter counts, titles, events, "
            "or scenes. Every value must exactly match a requirement_key declared in "
            "the same candidate's completion_contract; never invent or paraphrase a key."
        ),
    )
    is_final: bool = Field(
        description="True only for the last planned Story Arc in the Book topology."
    )

    @field_validator(
        "whole_book_role",
        "core_goal",
        "handoff_from_previous",
    )
    @classmethod
    def _non_blank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Book Arc contract text must be non-blank.")
        return value

    @field_validator("exit_conditions")
    @classmethod
    def _non_blank_exit_conditions(cls, value: list[str]) -> list[str]:
        if any(not item.strip() for item in value):
            raise ValueError("Book Arc exit conditions must be non-blank.")
        return value

    @field_validator("completion_requirement_keys")
    @classmethod
    def _valid_completion_requirement_keys(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError(
                "Book Arc completion requirement responsibility keys must be unique."
            )
        if any(
            not item
            or not re.fullmatch(r"[a-z0-9][a-z0-9_.-]*", item)
            for item in value
        ):
            raise ValueError(
                "Book Arc completion requirement responsibility keys are invalid."
            )
        return value


class BookArcTopology(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    arcs: list[BookArcContract] = Field(
        min_length=1,
        description=(
            "Complete ordered whole-book Story Arc topology. List position defines "
            "the Harness-assigned one-based ordinal. Exactly the last Arc is final."
        ),
    )

    @model_validator(mode="after")
    def _one_final_last(self) -> BookArcTopology:
        final_positions = [
            index for index, contract in enumerate(self.arcs) if contract.is_final
        ]
        if final_positions != [len(self.arcs) - 1]:
            raise ValueError(
                "Book Arc topology requires exactly one final Arc in the last position."
            )
        return self


class BookArcTopologySuffix(BaseModel):
    """Model-authored mutable future suffix for a Book successor or repair."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    arcs: list[BookArcContract] = Field(
        default_factory=list,
        description=(
            "Only the mutable future Arc suffix. Do not repeat the Harness-frozen "
            "historical prefix, ordinals, baseline IDs, or storage metadata. A "
            "non-empty suffix has exactly one final Arc in its last position. An "
            "empty suffix is legal only when the Harness-preserved prefix can form "
            "the complete approved topology."
        ),
    )

    @model_validator(mode="after")
    def _non_empty_suffix_has_one_final_last(self) -> BookArcTopologySuffix:
        if not self.arcs:
            return self
        final_positions = [
            index for index, contract in enumerate(self.arcs) if contract.is_final
        ]
        if final_positions != [len(self.arcs) - 1]:
            raise ValueError(
                "A non-empty Book Arc topology suffix requires exactly one final "
                "Arc in the last position."
            )
        return self


class BookCandidatePack(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    direction: str = Field(
        min_length=1,
        description="Coherent whole-book direction synthesized from the frozen creator evidence.",
    )
    constraints: BookCreativeConstraints = Field(
        description="Explicit creative constraints for downstream Story Arc planning.",
    )
    selected_title: str = Field(
        min_length=1,
        description="Formal title already selected in the frozen Book discussion.",
    )
    rolling_plan: BookRollingPlan = Field(
        description="Whole-book rolling-plan strategy without pre-writing every chapter.",
    )
    completion_contract: CompletionContract
    arc_topology: BookArcTopology = Field(
        description=(
            "Ordered semantic Story Arc contracts owned by Book. It must not contain "
            "per-Arc Chapter counts, Chapter titles, events, scenes, or identities."
        )
    )

    @model_validator(mode="after")
    def _completion_requirements_have_exact_arc_owners(self) -> BookCandidatePack:
        requirement_keys = {
            item.requirement_key
            for item in self.completion_contract.completion_requirements
        }
        assigned_keys = {
            key
            for arc in self.arc_topology.arcs
            for key in arc.completion_requirement_keys
        }
        unknown = assigned_keys.difference(requirement_keys)
        if unknown:
            raise ValueError(
                "Arc completion_requirement_keys must exactly reuse keys from "
                "completion_contract; unknown keys: " + ", ".join(sorted(unknown))
            )
        missing = requirement_keys.difference(assigned_keys)
        if missing:
            raise ValueError(
                "Every completion_contract requirement_key must have at least one "
                "owning Arc; missing keys: " + ", ".join(sorted(missing))
            )
        return self


class BookSuccessorCandidateProposal(BaseModel):
    """Book revision output that never asks the model to copy frozen Arc history."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    direction: str = Field(min_length=1)
    constraints: BookCreativeConstraints
    selected_title: str = Field(min_length=1)
    rolling_plan: BookRollingPlan
    completion_contract: CompletionContract
    arc_topology_suffix: BookArcTopologySuffix


BookRepairComponent = Literal[
    "direction",
    "constraints",
    "rolling_plan",
    "completion_contract",
    "arc_topology",
]

BOOK_REPAIRABLE_COMPONENTS: tuple[BookRepairComponent, ...] = (
    "direction",
    "constraints",
    "rolling_plan",
    "completion_contract",
    "arc_topology",
)


class BookDirectionRepair(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    component: Literal["direction"]
    value: str = Field(min_length=1, description="Replacement whole-book direction.")


class BookConstraintsRepair(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    component: Literal["constraints"]
    value: BookCreativeConstraints = Field(
        description="Replacement creative constraints for downstream planning."
    )


class BookRollingPlanRepair(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    component: Literal["rolling_plan"]
    value: BookRollingPlan = Field(description="Replacement rolling-plan strategy.")


class BookCompletionContractRepair(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    component: Literal["completion_contract"]
    value: CompletionContract


class BookArcTopologyRepair(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    component: Literal["arc_topology"]
    value: BookArcTopologySuffix = Field(
        description=(
            "Replacement mutable future suffix only. The Harness preserves the "
            "frozen historical prefix and composes the complete topology."
        )
    )


BookRepairChange = Annotated[
    BookDirectionRepair
    | BookConstraintsRepair
    | BookRollingPlanRepair
    | BookCompletionContractRepair
    | BookArcTopologyRepair,
    Field(discriminator="component"),
]


class BookRepairPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    changes: list[BookRepairChange] = Field(
        min_length=1,
        max_length=5,
        description=(
            "Only Book components authorized by the repair contract in frozen context. "
            "Every returned replacement must differ from its current value. "
            "Omitted components are preserved by the Harness and must not be repeated."
        ),
    )

    @field_validator("changes")
    @classmethod
    def _unique_components(
        cls,
        value: list[BookRepairChange],
    ) -> list[BookRepairChange]:
        components = [change.component for change in value]
        if len(components) != len(set(components)):
            raise ValueError("A Book repair patch may change each component at most once.")
        return value


class BookEvaluationIssue(EvaluationIssue):
    """One EP1 blocker judged at Book authority."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    observed_components: list[BookRepairComponent] = Field(
        default_factory=list,
        description=(
            "Diagnostic Book candidate locations where this semantic issue was "
            "explicitly observed. This is not the repair authorization boundary; the "
            "Harness declares that same-layer envelope independently."
        ),
    )

    @field_validator("observed_components")
    @classmethod
    def _unique_observed_components(
        cls,
        value: list[BookRepairComponent],
    ) -> list[BookRepairComponent]:
        if len(value) != len(set(value)):
            raise ValueError("Book issue observed components must be unique.")
        return value


class BookRepairContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    authorized_components: list[BookRepairComponent] = Field(
        min_length=1,
        description=(
            "Harness-declared same-layer Book candidate envelope allowed to change "
            "during this one bounded local repair."
        ),
    )
    issues: list[BookEvaluationIssue] = Field(
        min_length=1,
        description="Complete frozen semantic issue ledger authorizing this repair.",
    )

    @field_validator("authorized_components")
    @classmethod
    def _unique_authorized_components(
        cls,
        value: list[BookRepairComponent],
    ) -> list[BookRepairComponent]:
        if len(value) != len(set(value)):
            raise ValueError("Book repair components must be unique.")
        return value


BookRequirementCoverageJudgmentKind = Literal[
    "aligned",
    "strength_mismatch",
    "infeasible",
]


class BookRequirementCoverageJudgment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    requirement_key: str = Field(
        min_length=1,
        pattern=r"^[a-z0-9][a-z0-9_.-]*$",
        description="One exact key from the current Book completion contract.",
    )
    judgment: BookRequirementCoverageJudgmentKind = Field(
        description=(
            "Whether closing the responsible Arc contracts under their exact goals and "
            "exit conditions necessarily establishes every indispensable named subject, "
            "action, exclusion, causal link, outcome strength, and evidence expectation "
            "in this Book completion requirement. Thematic compatibility or a broader "
            "category is a strength_mismatch, not aligned."
        ),
    )
    rationale: str = Field(
        min_length=1,
        description=(
            "For aligned, map every indispensable semantic atom to a responsible Arc goal "
            "or exit condition. For a non-aligned judgment, name the exact missing or "
            "infeasible atom; do not rely on matching or differing wording alone."
        ),
    )
    blocking_issue_code: str | None = Field(
        default=None,
        description=(
            "Required for strength_mismatch or infeasible and absent for aligned. It "
            "links this coverage judgment to one typed EP1 finding in the same result."
        ),
    )

    @model_validator(mode="after")
    def _issue_link_boundary(self) -> BookRequirementCoverageJudgment:
        linked = (
            self.blocking_issue_code is not None
            and bool(self.blocking_issue_code.strip())
        )
        if (self.judgment == "aligned") == linked:
            raise ValueError(
                "Only a non-aligned Book requirement coverage judgment requires a "
                "blocking issue code."
            )
        return self


class BookEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    decision: Literal["pass", "local_repair", "needs_user"] = Field(
        description=(
            "Use local_repair only when one bounded same-layer repair can resolve the "
            "findings; the Harness owns the repairable component envelope."
        ),
    )
    summary: str = Field(min_length=1, description="Evidence-based Book rubric assessment.")
    findings: list[BookEvaluationIssue] = Field(
        default_factory=list,
        description="Blocking EP1 findings only; never a literary-quality scorecard.",
    )
    requirement_coverage: list[BookRequirementCoverageJudgment] = Field(
        min_length=1,
        description=(
            "Exactly one semantic coverage judgment for every current Book completion "
            "requirement. The Harness validates the exact key set."
        ),
    )

    @field_validator("requirement_coverage")
    @classmethod
    def _unique_requirement_coverage(
        cls,
        value: list[BookRequirementCoverageJudgment],
    ) -> list[BookRequirementCoverageJudgment]:
        keys = [item.requirement_key for item in value]
        if len(keys) != len(set(keys)):
            raise ValueError("Book requirement coverage keys must be unique.")
        return value

    @model_validator(mode="after")
    def _repair_shape(self) -> BookEvaluation:
        if self.decision == "pass" and self.findings:
            raise ValueError("A passing Book evaluation cannot carry blockers.")
        if self.decision != "pass" and not self.findings:
            raise ValueError("A non-passing Book evaluation requires an EP1 blocker.")
        finding_codes = [finding.code for finding in self.findings]
        if len(finding_codes) != len(set(finding_codes)):
            raise ValueError("Book evaluation finding codes must be unique.")
        linked_codes = {
            item.blocking_issue_code
            for item in self.requirement_coverage
            if item.blocking_issue_code is not None
        }
        if not linked_codes.issubset(set(finding_codes)):
            raise ValueError(
                "Every non-aligned Book requirement must link to one typed EP1 finding."
            )
        if self.decision == "pass" and any(
            item.judgment != "aligned" for item in self.requirement_coverage
        ):
            raise ValueError(
                "A passing Book evaluation requires every completion requirement to "
                "be semantically aligned with its responsible Arc contracts."
            )
        if self.decision == "local_repair":
            if any(not finding.observed_components for finding in self.findings):
                raise ValueError(
                    "Every Book local-repair finding requires at least one observed "
                    "candidate component."
                )
            if any(
                finding.kind
                in {"creator_owned_unknown", "parent_authority_concern"}
                for finding in self.findings
            ):
                raise ValueError(
                    "Creator-owned and parent-authority blockers cannot be Book-local repair."
                )
        elif any(finding.observed_components for finding in self.findings):
            raise ValueError(
                "Book observed repair locations are legal only for local repair."
            )
        if self.decision == "needs_user":
            if any(
                finding.kind != "creator_owned_unknown"
                for finding in self.findings
            ):
                raise ValueError(
                    "Book needs_user may carry only concrete creator-owned unknowns."
                )
        elif any(
            finding.kind == "creator_owned_unknown" for finding in self.findings
        ):
            raise ValueError(
                "Creator-owned unknowns must use the Book needs_user decision."
            )
        if any(
            finding.kind == "parent_authority_concern"
            for finding in self.findings
        ):
            raise ValueError("Book is the top creative authority and has no parent route.")
        return self


class ApplyBookCandidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    book_id: str
    expected_workspace_lock_version: int = Field(ge=1)
    candidate: BookCandidatePack
    selected_title_source: Literal["recommended", "custom"] = "custom"


class ApplyBookCandidateResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    book_id: str
    workspace_id: str
    workspace_lock_version: int


class SubmitBookRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    book_id: str
    expected_workspace_lock_version: int = Field(ge=1)


class SubmitBookResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    book_id: str
    submission_id: str
    content_fingerprint: str


class RecordBookReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    book_id: str
    submission_id: str
    evaluator_task_id: str
    evaluator_attempt_id: str
    rubric_id: str
    rubric_version: int = Field(ge=1)
    deterministic_precheck: dict[str, object]


class RecordBookReviewResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    book_id: str
    submission_id: str
    review_id: str
    decision: Literal["pass", "local_repair", "needs_user"]


class ApproveBookRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    book_id: str
    submission_id: str
    review_id: str
    expected_current_baseline_id: str | None = None


class ApproveBookResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    book_id: str
    baseline_id: str
    baseline_version: int
    approved_title: str
