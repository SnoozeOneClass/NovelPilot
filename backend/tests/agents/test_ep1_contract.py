from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.agents.contracts import (
    ChapterEvaluationIssue,
    ChapterRepairVerificationIssue,
    ChapterRepairVerificationResult,
    EstablishedFactCandidate,
    EvaluationIssue,
    LayerEvaluationResult,
)
from app.domain.arc.contracts import ArcEvaluation, ArcEvaluationIssue
from app.domain.book.contracts import (
    BookEvaluation,
    BookEvaluationIssue,
    BookRepairContract,
)
from app.domain.evaluation import (
    ArcClosureEvaluation,
    ArcParentContractEvaluation,
    BookCompletionEvaluation,
    BookParentContractEvaluation,
    ChapterEvidenceCorrectionEvaluation,
    ChapterEvidenceTarget,
    CompletionRequirementStatus,
    CreatorInputNeed,
)


def test_ordinary_chapter_fact_is_open_world_and_has_no_truth_protocol() -> None:
    fact = EstablishedFactCandidate(
        statement="Xu Lan has never recorded this event during the prior twelve years.",
        evidence_hint=("The current Chapter establishes this through her archive inspection."),
    )
    assert fact.statement.startswith("Xu Lan")
    assert set(fact.model_dump()) == {"statement", "evidence_hint"}


def test_explicit_conflict_requires_two_affirmative_statements_not_prior_silence() -> None:
    with pytest.raises(ValidationError, match="affirmative candidate and formal"):
        EvaluationIssue(
            kind="explicit_conflict",
            code="record_conflict",
            subject="Xu Lan's archive",
            summary="Prior history does not mention a record.",
            evidence=["No earlier Chapter explicitly mentions the record."],
            candidate_claim="Xu Lan recorded the event.",
        )

    conflict = EvaluationIssue(
        kind="explicit_conflict",
        code="record_conflict",
        subject="Xu Lan's archive",
        summary="The candidate contradicts one formal Chapter.",
        evidence=[
            "The candidate says she never recorded it; Chapter 3 affirmatively shows the entry."
        ],
        candidate_claim="Xu Lan never recorded the event.",
        contrary_formal_statement="Formal Chapter 3 shows Xu Lan writing that event.",
    )
    assert conflict.kind == "explicit_conflict"


def test_quality_preference_is_not_an_ep1_blocker_kind() -> None:
    with pytest.raises(ValidationError):
        EvaluationIssue.model_validate(
            {
                "kind": "pacing_preference",
                "code": "slow_middle",
                "subject": "middle pacing",
                "summary": "The middle could be faster.",
                "evidence": ["This is a subjective preference."],
            }
        )


def test_closed_contract_and_strong_conclusion_require_their_named_evidence() -> None:
    with pytest.raises(ValidationError, match="explicit contract item"):
        EvaluationIssue(
            kind="contract_unfulfilled",
            code="missing_arc_exit",
            subject="Arc exit condition",
            summary="The candidate does not satisfy its assigned exit condition.",
            evidence=["The frozen Arc contract names the required transition."],
        )
    contract_issue = EvaluationIssue(
        kind="contract_unfulfilled",
        code="missing_arc_exit",
        subject="Arc exit condition",
        summary="The candidate does not satisfy its assigned exit condition.",
        evidence=["The frozen Arc contract names the required transition."],
        contract_item="The witness publicly retracts the altered testimony.",
    )
    assert contract_issue.contract_item is not None

    with pytest.raises(ValidationError, match="support gap"):
        EvaluationIssue(
            kind="unsupported_strong_conclusion",
            code="culpability_not_supported",
            subject="culpability conclusion",
            summary="The candidate declares one character guilty.",
            evidence=["The prose contains only a suspicion."],
        )
    conclusion_issue = EvaluationIssue(
        kind="unsupported_strong_conclusion",
        code="culpability_not_supported",
        subject="culpability conclusion",
        summary="The candidate declares one character guilty.",
        evidence=["The prose contains only a suspicion."],
        support_gap="No formal evidence excludes the remaining suspects.",
    )
    assert conclusion_issue.support_gap is not None


def test_ep1_kind_requires_minimum_evidence_without_rejecting_relevant_diagnostics() -> None:
    issue = EvaluationIssue(
        kind="contract_unfulfilled",
        code="completion_requirement_not_entailed",
        subject="current_incident_separation",
        summary="The owning Arc is broader than the exact completion requirement.",
        evidence=["The requirement names exact actors, actions, and a causal relationship."],
        contract_item=(
            "Tang Qiao places the page; Gu Xiangchao obstructs verification and causes "
            "the delay and fall."
        ),
        support_gap=(
            "The owning Arc only promises to clarify roles and does not require either "
            "specific action or the causal edge."
        ),
    )
    assert issue.kind == "contract_unfulfilled"
    assert issue.support_gap is not None

    conflict_with_contract_context = EvaluationIssue(
        kind="explicit_conflict",
        code="formal_contract_conflict",
        subject="responsibility attribution",
        summary="The candidate reverses the formal attribution.",
        evidence=["The candidate and formal contract name opposite actors."],
        candidate_claim="Tang Qiao obstructed verification.",
        contrary_formal_statement="The formal contract assigns obstruction to Gu Xiangchao.",
        contract_item="Preserve the formal responsibility attribution.",
    )
    assert conflict_with_contract_context.contract_item is not None

    with pytest.raises(ValidationError, match="supplied together"):
        EvaluationIssue(
            kind="parent_authority_concern",
            code="incomplete_diagnostic_pair",
            subject="Book responsibility",
            summary="Only one side of a diagnostic comparison was supplied.",
            evidence=["The lower layer raised a concern."],
            candidate_claim="The Arc cannot close under its assignment.",
        )

    with pytest.raises(ValidationError, match="Exactly creator_owned_unknown"):
        EvaluationIssue(
            kind="contract_unfulfilled",
            code="creator_route_smuggling",
            subject="Arc exit",
            summary="A diagnostic field must not manufacture a creator wait.",
            evidence=["The Arc exit remains unmet."],
            contract_item="Close the assigned Arc transition.",
            creator_question="Should the creator rewrite the Arc?",
        )


def test_derived_evidence_mismatch_names_both_projection_and_formal_prose() -> None:
    with pytest.raises(ValidationError, match="affirmative candidate and formal"):
        EvaluationIssue(
            kind="derived_evidence_mismatch",
            code="observation_overstates_prose",
            subject="Chapter observation",
            summary="The derived observation overstates what the prose establishes.",
            evidence=["The observation claims certainty."],
            candidate_claim="The witness confessed.",
        )

    mismatch = EvaluationIssue(
        kind="derived_evidence_mismatch",
        code="observation_overstates_prose",
        subject="Chapter observation",
        summary="The derived observation contradicts the formal prose.",
        evidence=["The observation says confession; the prose explicitly says denial."],
        candidate_claim="The witness confessed.",
        contrary_formal_statement="The formal Chapter states that the witness denied it.",
    )
    assert mismatch.kind == "derived_evidence_mismatch"


def test_layer_authority_and_creator_wait_decisions_fail_closed() -> None:
    chapter_parent = ChapterEvaluationIssue(
        kind="parent_authority_concern",
        code="arc_assignment_infeasible",
        subject="current Arc assignment",
        summary="The assignment conflicts with committed facts.",
        evidence=["The assignment requires an event prohibited by the current Arc."],
        observed_components=["plan"],
    )
    with pytest.raises(ValidationError, match="cannot authorize a Chapter-local repair"):
        LayerEvaluationResult(
            guidance_authority_judgment="not_present",
            decision="local_repair",
            summary="Attempted lower-layer repair.",
            issues=[chapter_parent],
        )

    escalation = LayerEvaluationResult(
        guidance_authority_judgment="requires_parent_review",
        decision="escalate_to_arc",
        summary="The diagnostic locations do not grant a local repair.",
        issues=[chapter_parent.model_copy(update={"observed_components": ["plan", "prose"]})],
    )
    assert escalation.decision == "escalate_to_arc"
    assert escalation.issues[0].observed_components == ["plan", "prose"]

    arc_parent = ArcEvaluationIssue(
        kind="parent_authority_concern",
        code="book_contract_concern",
        subject="current Book promise",
        summary="The Arc evidence warrants Book review.",
        evidence=["The Arc cannot resolve this without changing Book intent."],
    )
    with pytest.raises(ValidationError, match="must be escalated to Book"):
        ArcEvaluation(
            guidance_authority_judgment="not_present",
            decision="needs_user",
            summary="Incorrectly routed parent concern.",
            issues=[arc_parent],
        )

    creator_unknown = BookEvaluationIssue(
        kind="creator_owned_unknown",
        code="creator_choice_required",
        subject="ending preference",
        summary="Only the creator can choose the intended ending.",
        evidence=["Formal story facts do not encode this preference."],
        creator_question="Should the ending remain open or resolve the relationship?",
    )
    evaluation = BookEvaluation(
        decision="needs_user",
        summary="One creator-owned decision is required.",
        findings=[creator_unknown],
        requirement_coverage=[
            {
                "requirement_key": "ending_resolved",
                "judgment": "aligned",
                "rationale": "The current Arc ownership remains feasible.",
            }
        ],
    )
    assert evaluation.decision == "needs_user"


def test_guidance_authority_judgment_cannot_be_hidden_by_a_passing_candidate() -> None:
    with pytest.raises(
        ValidationError,
        match="Chapter guidance requiring parent authority must escalate to Arc",
    ):
        LayerEvaluationResult(
            guidance_authority_judgment="requires_parent_review",
            decision="pass",
            summary="The candidate ignored the incompatible guidance.",
        )

    with pytest.raises(
        ValidationError,
        match="Arc guidance requiring parent authority must escalate to Book",
    ):
        ArcEvaluation(
            guidance_authority_judgment="requires_parent_review",
            decision="pass",
            summary="The candidate ignored the incompatible guidance.",
        )


def test_one_decision_cannot_hide_mixed_issues_and_observations_are_diagnostic() -> None:
    book_issue = BookEvaluationIssue(
        kind="contract_unfulfilled",
        code="direction_missing",
        subject="Book direction",
        summary="The candidate omits its assigned direction.",
        evidence=["The frozen candidate does not contain the assigned direction."],
        contract_item="Preserve the evidence-led mystery direction.",
        observed_components=["direction"],
    )
    evaluation = BookEvaluation(
        decision="local_repair",
        summary="The issue was observed in direction, without granting authority.",
        findings=[book_issue],
        requirement_coverage=[
            {
                "requirement_key": "ending_resolved",
                "judgment": "aligned",
                "rationale": "The completion ownership remains aligned.",
            }
        ],
    )
    assert evaluation.findings[0].observed_components == ["direction"]
    repair_contract = BookRepairContract(
        authorized_components=[
            "direction",
            "constraints",
            "rolling_plan",
            "completion_contract",
            "arc_topology",
        ],
        issues=[book_issue],
    )
    assert repair_contract.authorized_components[-1] == "arc_topology"
    with pytest.raises(ValidationError, match="must be unique"):
        BookRepairContract(
            authorized_components=["direction", "direction"],
            issues=[book_issue],
        )

    arc_parent = ArcEvaluationIssue(
        kind="parent_authority_concern",
        code="book_promise_concern",
        subject="Book promise",
        summary="The Arc evidence requires Book review.",
        evidence=["The requested Arc outcome would alter a formal Book promise."],
    )
    arc_local = ArcEvaluationIssue(
        kind="contract_unfulfilled",
        code="arc_exit_missing",
        subject="Arc exit",
        summary="The Arc candidate also omits its own exit condition.",
        evidence=["The frozen Arc candidate does not perform the assigned transition."],
        contract_item="Complete the assigned Arc transition.",
    )
    with pytest.raises(ValidationError, match="only evidence-bound parent concerns"):
        ArcEvaluation(
            guidance_authority_judgment="not_present",
            decision="escalate_to_book",
            summary="Mixed local and parent issues cannot share one route.",
            issues=[arc_parent, arc_local],
        )
    with pytest.raises(ValidationError, match="observed_components"):
        ArcEvaluationIssue(
            kind="contract_unfulfilled",
            code="arc_exit_missing",
            subject="Arc exit",
            summary="The candidate omits the assigned exit.",
            evidence=["The frozen candidate does not complete the transition."],
            contract_item="Complete the assigned Arc transition.",
            observed_components=["chapter_outline"],
        )

    chapter_parent = ChapterEvaluationIssue(
        kind="parent_authority_concern",
        code="arc_assignment_concern",
        subject="Arc assignment",
        summary="The assignment requires Arc review.",
        evidence=["Formal Chapter evidence conflicts with the assigned Arc outcome."],
        observed_components=["plan"],
    )
    chapter_local = ChapterEvaluationIssue(
        kind="contract_unfulfilled",
        code="chapter_goal_missing",
        subject="Chapter goal",
        summary="The candidate omits its local goal.",
        evidence=["The frozen candidate does not perform the assigned Chapter action."],
        contract_item="Perform the assigned Chapter action.",
        observed_components=["prose"],
    )
    with pytest.raises(ValidationError, match="only direct-parent concerns"):
        LayerEvaluationResult(
            guidance_authority_judgment="not_present",
            decision="escalate_to_arc",
            summary="Mixed local and parent issues cannot share one route.",
            issues=[chapter_parent, chapter_local],
        )


def test_parent_and_evidence_routes_require_their_exact_ep1_issue_kind() -> None:
    parent_issue = EvaluationIssue(
        kind="parent_authority_concern",
        code="book_review_required",
        subject="current Book contract",
        summary="Arc evidence requires Book authority review.",
        evidence=["The Arc cannot satisfy its assignment without changing Book intent."],
    )
    mismatch_issue = EvaluationIssue(
        kind="derived_evidence_mismatch",
        code="observation_mismatch",
        subject="committed Chapter observation",
        summary="The observation contradicts its source prose.",
        evidence=["The observation says yes while the formal prose says no."],
        candidate_claim="The witness confessed.",
        contrary_formal_statement="The formal Chapter says the witness denied it.",
    )
    local_issue = EvaluationIssue(
        kind="contract_unfulfilled",
        code="arc_exit_missing",
        subject="Arc exit condition",
        summary="The current result does not fulfill its frozen exit condition.",
        evidence=["The frozen contract requires the witness to retract the statement."],
        contract_item="The witness retracts the altered statement.",
    )

    with pytest.raises(ValidationError, match="book_review_required.*atomic"):
        ArcParentContractEvaluation(
            arc_contract_judgment="remains_applicable",
            book_review_concern="book_review_required",
            chapter_evidence_concern="not_required",
            summary="The route omits its issue ledger.",
        )
    with pytest.raises(ValidationError, match="issues"):
        ArcClosureEvaluation.model_validate(
            {
                "outcome": {
                    "kind": "correct_chapter_evidence",
                    "summary": "The route omits its evidence mismatch.",
                    "target": {
                        "chapter_book_ordinal": 2,
                        "correction_goal": ("Correct the derived confession observation."),
                    },
                }
            }
        )
    with pytest.raises(
        ValidationError,
        match="arc_evidence_review_required.*atomic",
    ):
        BookParentContractEvaluation(
            book_contract_judgment="remains_applicable",
            arc_evidence_concern="arc_evidence_review_required",
            summary="The route omits its evidence mismatch.",
        )
    with pytest.raises(ValidationError, match="cannot carry ignored blockers"):
        ArcParentContractEvaluation(
            arc_contract_judgment="remains_applicable",
            book_review_concern="not_required",
            chapter_evidence_concern="not_required",
            summary="A keep decision tries to hide a blocker.",
            issues=[local_issue],
        )
    with pytest.raises(
        ValidationError,
        match="book_review_required keeps the current Arc contract applicable",
    ):
        ArcParentContractEvaluation(
            arc_contract_judgment="revision_warranted",
            book_review_concern="book_review_required",
            chapter_evidence_concern="not_required",
            summary="Two authority dispositions are selected.",
            issues=[parent_issue],
        )

    valid_parent = ArcParentContractEvaluation(
        arc_contract_judgment="remains_applicable",
        book_review_concern="book_review_required",
        chapter_evidence_concern="not_required",
        summary="The parent route and issue ledger agree.",
        issues=[parent_issue],
    )
    valid_evidence = BookParentContractEvaluation(
        book_contract_judgment="remains_applicable",
        arc_evidence_concern="arc_evidence_review_required",
        summary="The evidence route and issue ledger agree.",
        issues=[mismatch_issue],
    )
    assert valid_parent.book_review_concern == "book_review_required"
    assert valid_evidence.arc_evidence_concern == "arc_evidence_review_required"


def test_top_and_evidence_verification_tasks_reject_parent_authority_issues() -> None:
    parent_issue = EvaluationIssue(
        kind="parent_authority_concern",
        code="illegal_parent_route",
        subject="nonexistent parent authority",
        summary="This top-level or evidence-only task has no parent route.",
        evidence=["The task contract exposes no parent-authority disposition."],
    )

    with pytest.raises(ValidationError, match="no parent authority route"):
        BookCompletionEvaluation(
            requirement_statuses=[
                CompletionRequirementStatus(
                    requirement_key="ending",
                    status="unresolved",
                    rationale="The ending requirement is not yet satisfied.",
                )
            ],
            book_contract_judgment="remains_applicable",
            summary="An invalid parent issue is attached.",
            issues=[parent_issue],
        )
    with pytest.raises(ValidationError, match="cannot route a parent challenge"):
        ChapterEvidenceCorrectionEvaluation(
            observations_supported_by_frozen_prose=False,
            canon_intent_supported_by_frozen_prose=True,
            descendant_facts_remain_consistent=True,
            summary="An invalid parent issue is attached.",
            issues=[parent_issue],
        )

    creator_issue = EvaluationIssue(
        kind="creator_owned_unknown",
        code="creator_choice_required",
        subject="witness intent",
        summary="Only the creator can settle this intent.",
        evidence=["The formal prose deliberately leaves the intent unresolved."],
        creator_question="Did the witness intend to conceal the statement?",
    )
    with pytest.raises(ValidationError, match="cannot create a creator wait"):
        ChapterEvidenceCorrectionEvaluation(
            observations_supported_by_frozen_prose=False,
            canon_intent_supported_by_frozen_prose=True,
            descendant_facts_remain_consistent=True,
            summary="A Chapter-scoped verifier tries to create a user wait.",
            issues=[creator_issue],
        )


def test_successful_closure_and_completion_cannot_hide_ep1_blockers() -> None:
    local_issue = EvaluationIssue(
        kind="contract_unfulfilled",
        code="ending_missing",
        subject="formal ending requirement",
        summary="The frozen ending requirement is not fulfilled.",
        evidence=["The requirement calls for a public retraction."],
        contract_item="The witness publicly retracts the altered statement.",
    )
    with pytest.raises(ValidationError, match="issues"):
        ArcClosureEvaluation.model_validate(
            {
                "outcome": {
                    "kind": "closed",
                    "summary": "The closure claims success while carrying a blocker.",
                    "evidence": ["The formal Chapter establishes the exit state."],
                    "issues": [local_issue.model_dump(mode="json")],
                }
            }
        )
    with pytest.raises(ValidationError, match="satisfied Book completion"):
        BookCompletionEvaluation(
            requirement_statuses=[
                CompletionRequirementStatus(
                    requirement_key="ending",
                    status="satisfied",
                    evidence=["The formal closure establishes the ending."],
                    rationale="The frozen requirement is satisfied.",
                )
            ],
            book_contract_judgment="remains_applicable",
            summary="The completion claims success while carrying a blocker.",
            issues=[local_issue],
        )


def test_creator_wait_is_standalone_from_bounded_evidence_correction() -> None:
    question = "Did the witness knowingly conceal the altered statement?"
    creator_issue = EvaluationIssue(
        kind="creator_owned_unknown",
        code="creator_intent_required",
        subject="witness intent",
        summary="Committed evidence cannot determine creator-owned intent.",
        evidence=["The one bounded evidence correction has already been consumed."],
        creator_question=question,
    )
    need = CreatorInputNeed(
        controlled_fact="Whether the witness knowingly concealed the statement.",
        question=question,
        evidence=["Formal prose deliberately leaves the intent unresolved."],
    )

    with pytest.raises(ValidationError):
        ArcParentContractEvaluation(
            arc_contract_judgment="remains_applicable",
            book_review_concern="not_required",
            chapter_evidence_concern="chapter_evidence_review_required",
            chapter_evidence_target=ChapterEvidenceTarget(
                chapter_book_ordinal=2,
                correction_goal="Resolve the remaining creator-owned intent ambiguity.",
            ),
            summary="An invalid result combines evidence correction and creator wait.",
            issues=[creator_issue],
            creator_input_need=need,
        )

    evaluation = ArcParentContractEvaluation(
        arc_contract_judgment="unable_to_judge",
        book_review_concern="not_required",
        chapter_evidence_concern="not_required",
        summary="Only creator intent can resolve the recurrence.",
        issues=[creator_issue],
        creator_input_need=need,
    )
    assert evaluation.creator_input_need == need

    with pytest.raises(ValidationError):
        BookParentContractEvaluation(
            book_contract_judgment="remains_applicable",
            arc_evidence_concern="arc_evidence_review_required",
            summary="An invalid result combines evidence correction and creator wait.",
            issues=[creator_issue],
            creator_input_need=need,
        )
    book_wait = BookParentContractEvaluation(
        book_contract_judgment="unable_to_judge",
        arc_evidence_concern="not_required",
        summary="Only creator intent can resolve the Book-parent review.",
        issues=[creator_issue],
        creator_input_need=need,
    )
    assert book_wait.creator_input_need == need

    with pytest.raises(ValidationError):
        ArcClosureEvaluation.model_validate(
            {
                "outcome": {
                    "kind": "correct_chapter_evidence",
                    "summary": (
                        "An invalid closure combines evidence correction and creator wait."
                    ),
                    "target": {
                        "chapter_book_ordinal": 2,
                        "correction_goal": (
                            "Resolve the remaining creator-owned intent ambiguity."
                        ),
                    },
                    "issues": [creator_issue.model_dump(mode="json")],
                    "creator_input_need": need.model_dump(mode="json"),
                }
            }
        )
    closure_wait = ArcClosureEvaluation.model_validate(
        {
            "outcome": {
                "kind": "needs_user",
                "summary": "Only creator intent can resolve the closure review.",
                "issues": [creator_issue.model_dump(mode="json")],
                "creator_input_need": need.model_dump(mode="json"),
            }
        }
    )
    assert closure_wait.outcome.creator_input_need == need


def test_initial_chapter_result_cannot_encode_repair_recurrence() -> None:
    issue = {
        "kind": "contract_unfulfilled",
        "code": "chapter_goal_missing",
        "subject": "Chapter goal",
        "summary": "The repaired candidate still omits its goal.",
        "evidence": ["The frozen assignment remains unfulfilled."],
        "contract_item": "Perform the assigned Chapter action.",
        "observed_components": ["prose"],
        "recurrence": "persists_after_authorized_repair",
    }
    with pytest.raises(ValidationError, match="recurrence"):
        LayerEvaluationResult.model_validate(
            {
                "guidance_authority_judgment": "not_present",
                "decision": "local_repair",
                "summary": "Initial review cannot claim recurrence.",
                "issues": [issue],
            }
        )

    verification = ChapterRepairVerificationResult(
        guidance_authority_judgment="not_present",
        decision="local_repair",
        summary="Verification may classify recurrence.",
        issues=[ChapterRepairVerificationIssue.model_validate(issue)],
    )
    assert verification.issues[0].recurrence == "persists_after_authorized_repair"
