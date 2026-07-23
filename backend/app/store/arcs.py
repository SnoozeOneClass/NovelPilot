from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import cast

from sqlalchemy import RowMapping, exists, func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from app.db.schema import (
    arc_approval_gates,
    arc_approvals,
    arc_baselines,
    arc_book_change_requests,
    arc_review_submissions,
    arc_reviews,
    arc_workspaces,
    chapters,
    story_arcs,
)


@dataclass(frozen=True, slots=True)
class ArcRecord:
    id: str
    project_id: str
    book_id: str
    ordinal: int
    purpose: str
    lifecycle_status: str
    current_baseline_id: str | None
    created_at_ms: int
    updated_at_ms: int
    completed_at_ms: int | None


@dataclass(frozen=True, slots=True)
class ArcWorkspaceRecord:
    id: str
    project_id: str
    book_id: str
    arc_id: str
    state: str
    lock_version: int
    base_arc_baseline_id: str | None
    book_baseline_id: str
    canon_baseline_id: str
    prior_arc_id: str | None
    prior_arc_baseline_id: str | None
    plan_ref_id: str | None
    recommended_target_chapter_count: int | None
    repair_policy_id: str
    semantic_repair_count: int
    semantic_repair_limit: int
    stale_reason_code: str | None
    stale_at_ms: int | None
    created_at_ms: int
    updated_at_ms: int
    guidance_ref_id: str | None = None


@dataclass(frozen=True, slots=True)
class ArcSubmissionRecord:
    id: str
    project_id: str
    book_id: str
    arc_id: str
    workspace_id: str
    workspace_lock_version: int
    base_arc_baseline_id: str | None
    book_baseline_id: str
    canon_baseline_id: str
    prior_arc_id: str | None
    prior_arc_baseline_id: str | None
    purpose: str
    plan_ref_id: str
    recommended_target_chapter_count: int
    content_manifest_ref_id: str
    content_fingerprint: str
    disposition: str
    close_reason_code: str | None
    created_at_ms: int
    closed_at_ms: int | None


@dataclass(frozen=True, slots=True)
class ArcReviewRecord:
    id: str
    project_id: str
    book_id: str
    arc_id: str
    submission_id: str
    evaluator_task_id: str
    evaluator_attempt_id: str
    decision: str
    rubric_id: str
    rubric_version: int
    precheck_ref_id: str
    detail_ref_id: str
    repair_contract_ref_id: str | None
    created_at_ms: int


@dataclass(frozen=True, slots=True)
class ArcApprovalGateRecord:
    id: str
    project_id: str
    book_id: str
    arc_id: str
    submission_id: str
    review_id: str
    reason: str
    state: str
    created_at_ms: int
    closed_at_ms: int | None


@dataclass(frozen=True, slots=True)
class ArcApprovalRecord:
    id: str
    project_id: str
    book_id: str
    arc_id: str
    gate_id: str
    submission_id: str
    review_id: str
    decision: str
    target_chapter_count: int | None
    created_at_ms: int


@dataclass(frozen=True, slots=True)
class ArcBaselineRecord:
    id: str
    project_id: str
    book_id: str
    arc_id: str
    baseline_version: int
    parent_baseline_id: str | None
    submission_id: str
    review_id: str
    book_baseline_id: str
    canon_baseline_id: str
    prior_arc_id: str | None
    prior_arc_baseline_id: str | None
    purpose: str
    plan_ref_id: str
    recommended_target_chapter_count: int
    target_chapter_count: int
    authorization_kind: str
    approval_gate_id: str | None
    approval_id: str | None
    created_at_ms: int


@dataclass(frozen=True, slots=True)
class ArcBookChangeRequestRecord:
    id: str
    project_id: str
    book_id: str
    arc_id: str
    source_submission_id: str
    source_review_id: str
    target_book_baseline_id: str
    evidence_ref_id: str
    status: str
    created_at_ms: int


def _arc_record(row: RowMapping) -> ArcRecord:
    return ArcRecord(
        id=cast(str, row["id"]),
        project_id=cast(str, row["project_id"]),
        book_id=cast(str, row["book_id"]),
        ordinal=cast(int, row["ordinal"]),
        purpose=cast(str, row["purpose"]),
        lifecycle_status=cast(str, row["lifecycle_status"]),
        current_baseline_id=cast(str | None, row["current_baseline_id"]),
        created_at_ms=cast(int, row["created_at_ms"]),
        updated_at_ms=cast(int, row["updated_at_ms"]),
        completed_at_ms=cast(int | None, row["completed_at_ms"]),
    )


def _workspace_record(row: RowMapping) -> ArcWorkspaceRecord:
    return ArcWorkspaceRecord(
        id=cast(str, row["id"]),
        project_id=cast(str, row["project_id"]),
        book_id=cast(str, row["book_id"]),
        arc_id=cast(str, row["arc_id"]),
        state=cast(str, row["state"]),
        lock_version=cast(int, row["lock_version"]),
        base_arc_baseline_id=cast(str | None, row["base_arc_baseline_id"]),
        book_baseline_id=cast(str, row["book_baseline_id"]),
        canon_baseline_id=cast(str, row["canon_baseline_id"]),
        prior_arc_id=cast(str | None, row["prior_arc_id"]),
        prior_arc_baseline_id=cast(str | None, row["prior_arc_baseline_id"]),
        plan_ref_id=cast(str | None, row["plan_ref_id"]),
        recommended_target_chapter_count=cast(
            int | None, row["recommended_target_chapter_count"]
        ),
        repair_policy_id=cast(str, row["repair_policy_id"]),
        semantic_repair_count=cast(int, row["semantic_repair_count"]),
        semantic_repair_limit=cast(int, row["semantic_repair_limit"]),
        stale_reason_code=cast(str | None, row["stale_reason_code"]),
        stale_at_ms=cast(int | None, row["stale_at_ms"]),
        created_at_ms=cast(int, row["created_at_ms"]),
        updated_at_ms=cast(int, row["updated_at_ms"]),
        guidance_ref_id=cast(str | None, row["guidance_ref_id"]),
    )


def _submission_record(row: RowMapping) -> ArcSubmissionRecord:
    return ArcSubmissionRecord(
        id=cast(str, row["id"]),
        project_id=cast(str, row["project_id"]),
        book_id=cast(str, row["book_id"]),
        arc_id=cast(str, row["arc_id"]),
        workspace_id=cast(str, row["workspace_id"]),
        workspace_lock_version=cast(int, row["workspace_lock_version"]),
        base_arc_baseline_id=cast(str | None, row["base_arc_baseline_id"]),
        book_baseline_id=cast(str, row["book_baseline_id"]),
        canon_baseline_id=cast(str, row["canon_baseline_id"]),
        prior_arc_id=cast(str | None, row["prior_arc_id"]),
        prior_arc_baseline_id=cast(str | None, row["prior_arc_baseline_id"]),
        purpose=cast(str, row["purpose"]),
        plan_ref_id=cast(str, row["plan_ref_id"]),
        recommended_target_chapter_count=cast(
            int, row["recommended_target_chapter_count"]
        ),
        content_manifest_ref_id=cast(str, row["content_manifest_ref_id"]),
        content_fingerprint=cast(str, row["content_fingerprint"]),
        disposition=cast(str, row["disposition"]),
        close_reason_code=cast(str | None, row["close_reason_code"]),
        created_at_ms=cast(int, row["created_at_ms"]),
        closed_at_ms=cast(int | None, row["closed_at_ms"]),
    )


def _review_record(row: RowMapping) -> ArcReviewRecord:
    return ArcReviewRecord(
        id=cast(str, row["id"]),
        project_id=cast(str, row["project_id"]),
        book_id=cast(str, row["book_id"]),
        arc_id=cast(str, row["arc_id"]),
        submission_id=cast(str, row["submission_id"]),
        evaluator_task_id=cast(str, row["evaluator_task_id"]),
        evaluator_attempt_id=cast(str, row["evaluator_attempt_id"]),
        decision=cast(str, row["decision"]),
        rubric_id=cast(str, row["rubric_id"]),
        rubric_version=cast(int, row["rubric_version"]),
        precheck_ref_id=cast(str, row["precheck_ref_id"]),
        detail_ref_id=cast(str, row["detail_ref_id"]),
        repair_contract_ref_id=cast(str | None, row["repair_contract_ref_id"]),
        created_at_ms=cast(int, row["created_at_ms"]),
    )


def _gate_record(row: RowMapping) -> ArcApprovalGateRecord:
    return ArcApprovalGateRecord(
        id=cast(str, row["id"]),
        project_id=cast(str, row["project_id"]),
        book_id=cast(str, row["book_id"]),
        arc_id=cast(str, row["arc_id"]),
        submission_id=cast(str, row["submission_id"]),
        review_id=cast(str, row["review_id"]),
        reason=cast(str, row["reason"]),
        state=cast(str, row["state"]),
        created_at_ms=cast(int, row["created_at_ms"]),
        closed_at_ms=cast(int | None, row["closed_at_ms"]),
    )


def _baseline_record(row: RowMapping) -> ArcBaselineRecord:
    return ArcBaselineRecord(
        id=cast(str, row["id"]),
        project_id=cast(str, row["project_id"]),
        book_id=cast(str, row["book_id"]),
        arc_id=cast(str, row["arc_id"]),
        baseline_version=cast(int, row["baseline_version"]),
        parent_baseline_id=cast(str | None, row["parent_baseline_id"]),
        submission_id=cast(str, row["submission_id"]),
        review_id=cast(str, row["review_id"]),
        book_baseline_id=cast(str, row["book_baseline_id"]),
        canon_baseline_id=cast(str, row["canon_baseline_id"]),
        prior_arc_id=cast(str | None, row["prior_arc_id"]),
        prior_arc_baseline_id=cast(str | None, row["prior_arc_baseline_id"]),
        purpose=cast(str, row["purpose"]),
        plan_ref_id=cast(str, row["plan_ref_id"]),
        recommended_target_chapter_count=cast(
            int, row["recommended_target_chapter_count"]
        ),
        target_chapter_count=cast(int, row["target_chapter_count"]),
        authorization_kind=cast(str, row["authorization_kind"]),
        approval_gate_id=cast(str | None, row["approval_gate_id"]),
        approval_id=cast(str | None, row["approval_id"]),
        created_at_ms=cast(int, row["created_at_ms"]),
    )


class ArcRepository:
    def __init__(self, connection: AsyncConnection) -> None:
        self._connection = connection

    async def insert(self, record: ArcRecord) -> None:
        await self._connection.execute(story_arcs.insert().values(**asdict(record)))

    async def get(self, *, project_id: str, arc_id: str) -> ArcRecord | None:
        row = (
            await self._connection.execute(
                select(story_arcs).where(
                    story_arcs.c.project_id == project_id,
                    story_arcs.c.id == arc_id,
                )
            )
        ).mappings().one_or_none()
        return None if row is None else _arc_record(row)

    async def list_for_book(self, *, project_id: str, book_id: str) -> list[ArcRecord]:
        rows = (
            await self._connection.execute(
                select(story_arcs)
                .where(
                    story_arcs.c.project_id == project_id,
                    story_arcs.c.book_id == book_id,
                )
                .order_by(story_arcs.c.ordinal)
            )
        ).mappings()
        return [_arc_record(row) for row in rows]

    async def get_unfinished_for_book(
        self, *, project_id: str, book_id: str
    ) -> ArcRecord | None:
        row = (
            await self._connection.execute(
                select(story_arcs).where(
                    story_arcs.c.project_id == project_id,
                    story_arcs.c.book_id == book_id,
                    story_arcs.c.lifecycle_status.in_(("planning", "active")),
                )
            )
        ).mappings().one_or_none()
        return None if row is None else _arc_record(row)

    async def get_latest_for_book(
        self, *, project_id: str, book_id: str
    ) -> ArcRecord | None:
        row = (
            await self._connection.execute(
                select(story_arcs)
                .where(
                    story_arcs.c.project_id == project_id,
                    story_arcs.c.book_id == book_id,
                )
                .order_by(story_arcs.c.ordinal.desc())
                .limit(1)
            )
        ).mappings().one_or_none()
        return None if row is None else _arc_record(row)

    async def next_ordinal(self, *, book_id: str) -> int:
        current = await self._connection.scalar(
            select(func.coalesce(func.max(story_arcs.c.ordinal), 0)).where(
                story_arcs.c.book_id == book_id
            )
        )
        return cast(int, current) + 1

    async def insert_workspace(self, record: ArcWorkspaceRecord) -> None:
        await self._connection.execute(arc_workspaces.insert().values(**asdict(record)))

    async def get_workspace(
        self, *, project_id: str, arc_id: str
    ) -> ArcWorkspaceRecord | None:
        row = (
            await self._connection.execute(
                select(arc_workspaces).where(
                    arc_workspaces.c.project_id == project_id,
                    arc_workspaces.c.arc_id == arc_id,
                )
            )
        ).mappings().one_or_none()
        return None if row is None else _workspace_record(row)

    async def compare_and_set_workspace(
        self, *, record: ArcWorkspaceRecord, expected_lock_version: int
    ) -> bool:
        values = asdict(record)
        for key in ("id", "project_id", "book_id", "arc_id"):
            values.pop(key)
        result = await self._connection.execute(
            update(arc_workspaces)
            .where(
                arc_workspaces.c.id == record.id,
                arc_workspaces.c.project_id == record.project_id,
                arc_workspaces.c.arc_id == record.arc_id,
                arc_workspaces.c.lock_version == expected_lock_version,
            )
            .values(**values)
        )
        return result.rowcount == 1

    async def find_pending_submission(
        self, *, project_id: str, arc_id: str
    ) -> ArcSubmissionRecord | None:
        row = (
            await self._connection.execute(
                select(arc_review_submissions).where(
                    arc_review_submissions.c.project_id == project_id,
                    arc_review_submissions.c.arc_id == arc_id,
                    arc_review_submissions.c.disposition == "pending",
                )
            )
        ).mappings().one_or_none()
        return None if row is None else _submission_record(row)

    async def get_submission(
        self, *, project_id: str, submission_id: str
    ) -> ArcSubmissionRecord | None:
        row = (
            await self._connection.execute(
                select(arc_review_submissions).where(
                    arc_review_submissions.c.project_id == project_id,
                    arc_review_submissions.c.id == submission_id,
                )
            )
        ).mappings().one_or_none()
        return None if row is None else _submission_record(row)

    async def insert_submission(self, record: ArcSubmissionRecord) -> None:
        await self._connection.execute(
            arc_review_submissions.insert().values(**asdict(record))
        )

    async def close_submission(
        self,
        *,
        project_id: str,
        submission_id: str,
        disposition: str,
        reason_code: str,
        closed_at_ms: int,
    ) -> bool:
        result = await self._connection.execute(
            update(arc_review_submissions)
            .where(
                arc_review_submissions.c.project_id == project_id,
                arc_review_submissions.c.id == submission_id,
                arc_review_submissions.c.disposition == "pending",
            )
            .values(
                disposition=disposition,
                close_reason_code=reason_code,
                closed_at_ms=closed_at_ms,
            )
        )
        return result.rowcount == 1

    async def insert_review(self, record: ArcReviewRecord) -> None:
        await self._connection.execute(arc_reviews.insert().values(**asdict(record)))

    async def get_review(
        self, *, project_id: str, review_id: str
    ) -> ArcReviewRecord | None:
        row = (
            await self._connection.execute(
                select(arc_reviews).where(
                    arc_reviews.c.project_id == project_id,
                    arc_reviews.c.id == review_id,
                )
            )
        ).mappings().one_or_none()
        return None if row is None else _review_record(row)

    async def get_latest_review(
        self, *, project_id: str, arc_id: str
    ) -> ArcReviewRecord | None:
        row = (
            await self._connection.execute(
                select(arc_reviews)
                .where(
                    arc_reviews.c.project_id == project_id,
                    arc_reviews.c.arc_id == arc_id,
                )
                .order_by(arc_reviews.c.created_at_ms.desc(), arc_reviews.c.id.desc())
                .limit(1)
            )
        ).mappings().one_or_none()
        return None if row is None else _review_record(row)

    async def find_passed_pending_without_gate(
        self, *, project_id: str, book_id: str
    ) -> tuple[ArcSubmissionRecord, ArcReviewRecord] | None:
        row = (
            await self._connection.execute(
                select(
                    arc_review_submissions.c.id.label("submission_id"),
                    arc_reviews.c.id.label("review_id"),
                )
                .join(
                    arc_reviews,
                    (arc_reviews.c.project_id == arc_review_submissions.c.project_id)
                    & (arc_reviews.c.submission_id == arc_review_submissions.c.id),
                )
                .where(
                    arc_review_submissions.c.project_id == project_id,
                    arc_review_submissions.c.book_id == book_id,
                    arc_review_submissions.c.disposition == "pending",
                    arc_reviews.c.decision == "pass",
                    ~exists(
                        select(arc_approval_gates.c.id).where(
                            arc_approval_gates.c.project_id
                            == arc_review_submissions.c.project_id,
                            arc_approval_gates.c.submission_id
                            == arc_review_submissions.c.id,
                        )
                    ),
                )
                .limit(1)
            )
        ).mappings().one_or_none()
        if row is None:
            return None
        submission = await self.get_submission(
            project_id=project_id,
            submission_id=cast(str, row["submission_id"]),
        )
        review = await self.get_review(
            project_id=project_id,
            review_id=cast(str, row["review_id"]),
        )
        assert submission is not None and review is not None
        return submission, review

    async def insert_approval_gate(self, record: ArcApprovalGateRecord) -> None:
        await self._connection.execute(
            arc_approval_gates.insert().values(**asdict(record))
        )

    async def get_approval_gate(
        self, *, project_id: str, gate_id: str
    ) -> ArcApprovalGateRecord | None:
        row = (
            await self._connection.execute(
                select(arc_approval_gates).where(
                    arc_approval_gates.c.project_id == project_id,
                    arc_approval_gates.c.id == gate_id,
                )
            )
        ).mappings().one_or_none()
        return None if row is None else _gate_record(row)

    async def find_pending_gate(
        self, *, project_id: str, arc_id: str
    ) -> ArcApprovalGateRecord | None:
        row = (
            await self._connection.execute(
                select(arc_approval_gates).where(
                    arc_approval_gates.c.project_id == project_id,
                    arc_approval_gates.c.arc_id == arc_id,
                    arc_approval_gates.c.state == "pending",
                )
            )
        ).mappings().one_or_none()
        return None if row is None else _gate_record(row)

    async def close_approval_gate(
        self,
        *,
        project_id: str,
        gate_id: str,
        state: str,
        closed_at_ms: int,
    ) -> bool:
        result = await self._connection.execute(
            update(arc_approval_gates)
            .where(
                arc_approval_gates.c.project_id == project_id,
                arc_approval_gates.c.id == gate_id,
                arc_approval_gates.c.state == "pending",
            )
            .values(state=state, closed_at_ms=closed_at_ms)
        )
        return result.rowcount == 1

    async def insert_approval(self, record: ArcApprovalRecord) -> None:
        await self._connection.execute(arc_approvals.insert().values(**asdict(record)))

    async def next_baseline_version(self, *, arc_id: str) -> int:
        current = await self._connection.scalar(
            select(func.coalesce(func.max(arc_baselines.c.baseline_version), 0)).where(
                arc_baselines.c.arc_id == arc_id
            )
        )
        return cast(int, current) + 1

    async def get_baseline_version(
        self, *, project_id: str, arc_id: str, baseline_id: str
    ) -> int | None:
        return cast(
            int | None,
            await self._connection.scalar(
                select(arc_baselines.c.baseline_version).where(
                    arc_baselines.c.project_id == project_id,
                    arc_baselines.c.arc_id == arc_id,
                    arc_baselines.c.id == baseline_id,
                )
            ),
        )

    async def get_baseline(
        self, *, project_id: str, arc_id: str, baseline_id: str
    ) -> ArcBaselineRecord | None:
        row = (
            await self._connection.execute(
                select(arc_baselines).where(
                    arc_baselines.c.project_id == project_id,
                    arc_baselines.c.arc_id == arc_id,
                    arc_baselines.c.id == baseline_id,
                )
            )
        ).mappings().one_or_none()
        return None if row is None else _baseline_record(row)

    async def insert_baseline(self, record: ArcBaselineRecord) -> None:
        await self._connection.execute(arc_baselines.insert().values(**asdict(record)))

    async def compare_and_set_current_baseline(
        self,
        *,
        project_id: str,
        arc_id: str,
        expected_baseline_id: str | None,
        new_baseline_id: str,
        updated_at_ms: int,
        lifecycle_status: str = "active",
        completed_at_ms: int | None = None,
    ) -> bool:
        expected = (
            story_arcs.c.current_baseline_id.is_(None)
            if expected_baseline_id is None
            else story_arcs.c.current_baseline_id == expected_baseline_id
        )
        result = await self._connection.execute(
            update(story_arcs)
            .where(
                story_arcs.c.project_id == project_id,
                story_arcs.c.id == arc_id,
                expected,
            )
            .values(
                lifecycle_status=lifecycle_status,
                current_baseline_id=new_baseline_id,
                completed_at_ms=completed_at_ms,
                updated_at_ms=updated_at_ms,
            )
        )
        return result.rowcount == 1

    async def count_committed_chapters(self, *, arc_id: str) -> int:
        value = await self._connection.scalar(
            select(func.count())
            .select_from(chapters)
            .where(chapters.c.arc_id == arc_id, chapters.c.lifecycle_status == "committed")
        )
        return cast(int, value)

    async def insert_book_change_request(
        self, record: ArcBookChangeRequestRecord
    ) -> None:
        await self._connection.execute(
            arc_book_change_requests.insert().values(
                **asdict(record),
                resolved_by_book_baseline_id=None,
                close_reason_code=None,
                closed_at_ms=None,
            )
        )
