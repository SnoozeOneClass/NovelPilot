from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.db.uow import StoreSession
from app.store.arcs import (
    ArcBaselineRecord,
    ArcRecord,
    ArcSubmissionRecord,
    ArcWorkspaceRecord,
)
from app.store.authority import BookParentReviewRecord
from app.store.books import BookBaselineRecord, BookRecord, BookWorkspaceRecord
from app.store.change_requests import ArcBookChangeRequestRecord


class BookParentCaseError(RuntimeError):
    """The frozen Book-parent cause and review subject do not form one legal case."""

    def __init__(self, invariant: str, message: str) -> None:
        super().__init__(message)
        self.invariant = invariant


@dataclass(frozen=True, slots=True)
class BookParentCaseBinding:
    project_id: str
    book_id: str
    request_id: str
    target_book_baseline_id: str
    subject_arc_baseline_id: str | None
    canon_baseline_id: str
    workspace_lock_version: int
    workspace_work_cycle_id: str
    correction_lineage_id: str
    correction_lineage_origin: Literal["review_initiated", "user_initiated"]
    automatic_correction_round: Literal[0, 1]
    predecessor_review_id: str | None
    source_feedback_id: str | None


@dataclass(frozen=True, slots=True)
class BookParentReviewCase:
    binding: BookParentCaseBinding
    change: ArcBookChangeRequestRecord
    book: BookRecord
    book_baseline: BookBaselineRecord
    book_workspace: BookWorkspaceRecord
    source_arc: ArcRecord
    source_arc_workspace: ArcWorkspaceRecord
    origin_arc_baseline_id: str | None
    subject_arc_baseline: ArcBaselineRecord | None
    predecessor_review: BookParentReviewRecord | None
    successor_submission: ArcSubmissionRecord | None


def _invalid(invariant: str, message: str) -> BookParentCaseError:
    return BookParentCaseError(invariant, message)


async def _origin_arc_baseline_id(
    session: StoreSession,
    *,
    change: ArcBookChangeRequestRecord,
    source_arc: ArcRecord,
) -> str | None:
    source_count = sum(
        (
            change.source_candidate_review_id is not None,
            change.source_arc_parent_review_id is not None,
            change.source_arc_closure_review_id is not None,
        )
    )
    if source_count != 1:
        raise _invalid(
            "book_parent_origin_unique",
            "Arc-to-Book request must preserve exactly one immutable origin.",
        )

    if change.source_candidate_review_id is not None:
        if change.source_candidate_submission_id is None:
            raise _invalid(
                "book_parent_candidate_origin_complete",
                "Candidate-origin request lost its frozen Arc submission.",
            )
        submission = await session.arcs.get_submission(
            project_id=change.project_id,
            submission_id=change.source_candidate_submission_id,
        )
        candidate_review = await session.arcs.get_review(
            project_id=change.project_id,
            review_id=change.source_candidate_review_id,
        )
        if (
            submission is None
            or candidate_review is None
            or submission.arc_id != source_arc.id
            or candidate_review.arc_id != source_arc.id
            or candidate_review.submission_id != submission.id
            or candidate_review.decision != "escalate_to_book"
            or change.evidence_ref_id != candidate_review.detail_ref_id
        ):
            raise _invalid(
                "book_parent_candidate_origin_exact",
                "Candidate-origin request no longer matches its frozen submission and review.",
            )
        return submission.base_arc_baseline_id

    if change.source_arc_parent_review_id is not None:
        parent_review = await session.arc_parent_reviews.get(
            project_id=change.project_id,
            review_id=change.source_arc_parent_review_id,
        )
        if (
            parent_review is None
            or parent_review.arc_id != source_arc.id
            or parent_review.disposition != "book_review_required"
            or change.evidence_ref_id != parent_review.detail_ref_id
        ):
            raise _invalid(
                "book_parent_arc_review_origin_exact",
                "Arc-parent origin no longer matches the immutable request evidence.",
            )
        return parent_review.target_arc_baseline_id

    assert change.source_arc_closure_review_id is not None
    closure_review = await session.arc_closure_reviews.get(
        project_id=change.project_id,
        review_id=change.source_arc_closure_review_id,
    )
    if (
        closure_review is None
        or closure_review.arc_id != source_arc.id
        or closure_review.disposition != "book_review_required"
        or change.evidence_ref_id != closure_review.detail_ref_id
    ):
        raise _invalid(
            "book_parent_arc_closure_origin_exact",
            "Arc-closure origin no longer matches the immutable request evidence.",
        )
    return closure_review.arc_baseline_id


async def _validate_user_feedback(
    session: StoreSession,
    *,
    binding: BookParentCaseBinding,
    exhausted_review_id: str,
) -> None:
    if binding.source_feedback_id is None:
        raise _invalid(
            "book_parent_user_feedback_required",
            "User-initiated Book-parent review has no applied feedback identity.",
        )
    feedback = await session.feedback.get(
        project_id=binding.project_id,
        feedback_id=binding.source_feedback_id,
    )
    if (
        feedback is None
        or feedback.status != "applied"
        or feedback.feedback_kind != "correction_wait_response"
        or feedback.route_layer != "book"
        or feedback.book_id != binding.book_id
        or feedback.book_parent_review_id != exhausted_review_id
        or feedback.resulting_correction_lineage_id
        != binding.correction_lineage_id
    ):
        raise _invalid(
            "book_parent_user_feedback_exact",
            "User feedback does not authorize this exact Book-parent lineage.",
        )


async def resolve_book_parent_review_case(
    session: StoreSession,
    *,
    binding: BookParentCaseBinding,
) -> BookParentReviewCase:
    """Resolve one exact Book-parent case without changing authoritative state."""

    change = await session.changes.get_arc_book(
        project_id=binding.project_id,
        request_id=binding.request_id,
    )
    project = await session.projects.get(binding.project_id)
    book = await session.books.get_for_project(binding.project_id)
    workspace = await session.books.get_workspace(
        project_id=binding.project_id,
        book_id=binding.book_id,
    )
    baseline = await session.books.get_baseline(
        project_id=binding.project_id,
        book_id=binding.book_id,
        baseline_id=binding.target_book_baseline_id,
    )
    if (
        change is None
        or change.book_id != binding.book_id
        or change.status not in {"open", "reviewed"}
        or project is None
        or project.current_canon_baseline_id != binding.canon_baseline_id
        or book is None
        or book.id != binding.book_id
        or book.lifecycle_status != "active"
        or book.current_completion_id is not None
        or book.current_baseline_id != binding.target_book_baseline_id
        or change.target_book_baseline_id != binding.target_book_baseline_id
        or baseline is None
        or workspace is None
        or workspace.lock_version != binding.workspace_lock_version
        or workspace.work_cycle_id != binding.workspace_work_cycle_id
    ):
        raise _invalid(
            "book_parent_case_authority_current",
            "Book-parent target Book, Canon, or workspace cycle is stale.",
        )

    source_arc = await session.arcs.get(
        project_id=binding.project_id,
        arc_id=change.arc_id,
    )
    source_workspace = await session.arcs.get_workspace(
        project_id=binding.project_id,
        arc_id=change.arc_id,
    )
    if (
        source_arc is None
        or source_arc.book_id != binding.book_id
        or source_workspace is None
        or source_arc.current_baseline_id != binding.subject_arc_baseline_id
    ):
        raise _invalid(
            "book_parent_subject_current",
            "Frozen Book-parent Arc subject is not the current formal Arc head.",
        )
    subject_baseline = (
        None
        if binding.subject_arc_baseline_id is None
        else await session.arcs.get_baseline(
            project_id=binding.project_id,
            arc_id=source_arc.id,
            baseline_id=binding.subject_arc_baseline_id,
        )
    )
    if binding.subject_arc_baseline_id is not None and subject_baseline is None:
        raise _invalid(
            "book_parent_subject_exists",
            "Frozen Book-parent Arc subject baseline does not exist.",
        )

    origin_baseline_id = await _origin_arc_baseline_id(
        session,
        change=change,
        source_arc=source_arc,
    )
    predecessor = (
        None
        if binding.predecessor_review_id is None
        else await session.book_parent_reviews.get(
            project_id=binding.project_id,
            review_id=binding.predecessor_review_id,
        )
    )
    if change.latest_parent_review_id != binding.predecessor_review_id or (
        binding.predecessor_review_id is not None and predecessor is None
    ):
        raise _invalid(
            "book_parent_predecessor_current",
            "Frozen Book-parent predecessor is not the request's latest review.",
        )

    successor_submission: ArcSubmissionRecord | None = None
    if binding.automatic_correction_round == 0:
        if binding.correction_lineage_origin == "review_initiated":
            if (
                predecessor is not None
                or binding.source_feedback_id is not None
                or change.status != "open"
                or origin_baseline_id != binding.subject_arc_baseline_id
            ):
                raise _invalid(
                    "book_parent_initial_round_exact",
                    "Initial Book-parent review must bind the immutable origin at the current Arc head.",
                )
        else:
            if (
                predecessor is None
                or change.status != "reviewed"
                or predecessor.disposition != "waiting_for_user"
                or predecessor.resolution_owner != "creator"
                or predecessor.user_question_ref_id is None
                or predecessor.subject_arc_baseline_id
                != binding.subject_arc_baseline_id
            ):
                raise _invalid(
                    "book_parent_user_round_exact",
                    "User-initiated Book-parent review is not bound to its creator wait and current subject.",
                )
            await _validate_user_feedback(
                session,
                binding=binding,
                exhausted_review_id=predecessor.id,
            )
    else:
        if (
            predecessor is None
            or change.status != "reviewed"
            or predecessor.request_id != change.id
            or predecessor.correction_lineage_id != binding.correction_lineage_id
            or predecessor.correction_lineage_origin
            != binding.correction_lineage_origin
            or predecessor.automatic_correction_round != 0
            or predecessor.source_feedback_id != binding.source_feedback_id
            or binding.subject_arc_baseline_id is None
            or subject_baseline is None
        ):
            raise _invalid(
                "book_parent_successor_predecessor_exact",
                "Round-one Book-parent review is not linked to its exact round-zero predecessor and formal subject.",
            )
        if binding.correction_lineage_origin == "user_initiated":
            if predecessor.source_exhausted_review_id is None:
                raise _invalid(
                    "book_parent_user_successor_origin",
                    "User round-one Book-parent review lost its creator-wait root.",
                )
            await _validate_user_feedback(
                session,
                binding=binding,
                exhausted_review_id=predecessor.source_exhausted_review_id,
            )
        elif binding.source_feedback_id is not None:
            raise _invalid(
                "book_parent_review_successor_feedback",
                "Review-initiated round one cannot bind user feedback.",
            )

        expected_revision_origin = (
            "user_initiated"
            if binding.correction_lineage_origin == "user_initiated"
            else (
                "initial"
                if predecessor.subject_arc_baseline_id is None
                else "automatic_arc_recovery"
            )
        )
        successor_submission = await session.arcs.get_submission(
            project_id=binding.project_id,
            submission_id=subject_baseline.submission_id,
        )
        successor_review = await session.arcs.get_review(
            project_id=binding.project_id,
            review_id=subject_baseline.review_id,
        )
        if (
            source_workspace.state != "idle"
            or source_workspace.source_book_parent_review_id != predecessor.id
            or source_workspace.correction_lineage_id
            != binding.correction_lineage_id
            or source_workspace.correction_lineage_origin
            != binding.correction_lineage_origin
            or source_workspace.automatic_correction_round != 1
            or source_workspace.revision_origin != expected_revision_origin
            or source_workspace.base_arc_baseline_id != subject_baseline.id
            or subject_baseline.parent_baseline_id
            != predecessor.subject_arc_baseline_id
            or subject_baseline.revision_origin != expected_revision_origin
            or subject_baseline.book_baseline_id
            != binding.target_book_baseline_id
            or subject_baseline.canon_baseline_id != binding.canon_baseline_id
            or successor_submission is None
            or successor_submission.arc_id != source_arc.id
            or successor_submission.workspace_id != source_workspace.id
            or successor_submission.work_cycle_id != source_workspace.work_cycle_id
            or successor_submission.base_arc_baseline_id
            != predecessor.subject_arc_baseline_id
            or successor_submission.disposition != "promoted"
            or successor_review is None
            or successor_review.submission_id != successor_submission.id
            or successor_review.decision != "pass"
            or (
                project.operation_mode == "full_auto"
                and subject_baseline.authorization_kind != "policy_auto"
            )
            or (
                project.operation_mode == "participatory"
                and (
                    subject_baseline.authorization_kind != "human_approval"
                    or subject_baseline.approval_gate_id is None
                    or subject_baseline.approval_id is None
                )
            )
        ):
            raise _invalid(
                "book_parent_successor_chain_exact",
                "Round-one Book-parent subject is not the reviewed and authorized Arc result of its predecessor correction.",
            )

    return BookParentReviewCase(
        binding=binding,
        change=change,
        book=book,
        book_baseline=baseline,
        book_workspace=workspace,
        source_arc=source_arc,
        source_arc_workspace=source_workspace,
        origin_arc_baseline_id=origin_baseline_id,
        subject_arc_baseline=subject_baseline,
        predecessor_review=predecessor,
        successor_submission=successor_submission,
    )
