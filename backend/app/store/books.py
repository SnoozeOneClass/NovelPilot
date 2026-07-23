from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import cast

from sqlalchemy import RowMapping, func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from app.db.schema import (
    book_approvals,
    book_baselines,
    book_completions,
    book_review_submissions,
    book_reviews,
    book_workspaces,
    books,
)


@dataclass(frozen=True, slots=True)
class BookRecord:
    id: str
    project_id: str
    lifecycle_status: str
    current_baseline_id: str | None
    current_completion_id: str | None
    created_at_ms: int
    updated_at_ms: int


@dataclass(frozen=True, slots=True)
class BookWorkspaceRecord:
    id: str
    project_id: str
    book_id: str
    state: str
    lock_version: int
    base_book_baseline_id: str | None
    base_canon_baseline_id: str
    direction_draft_ref_id: str
    discussion_state_ref_id: str
    transcript_ref_id: str
    candidate_constraints_ref_id: str | None
    candidate_titles_ref_id: str | None
    candidate_rolling_plan_ref_id: str | None
    candidate_completion_contract_ref_id: str | None
    readiness_status: str
    repair_policy_id: str
    semantic_repair_count: int
    semantic_repair_limit: int
    stale_reason_code: str | None
    stale_at_ms: int | None
    created_at_ms: int
    updated_at_ms: int
    guidance_ref_id: str | None = None


@dataclass(frozen=True, slots=True)
class BookSubmissionRecord:
    id: str
    project_id: str
    book_id: str
    workspace_id: str
    workspace_lock_version: int
    base_book_baseline_id: str | None
    canon_baseline_id: str
    direction_ref_id: str
    constraints_ref_id: str
    titles_ref_id: str
    rolling_plan_ref_id: str
    completion_contract_ref_id: str
    content_manifest_ref_id: str
    content_fingerprint: str
    disposition: str
    close_reason_code: str | None
    created_at_ms: int
    closed_at_ms: int | None


@dataclass(frozen=True, slots=True)
class BookReviewRecord:
    id: str
    project_id: str
    book_id: str
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
class BookApprovalRecord:
    id: str
    project_id: str
    book_id: str
    submission_id: str
    review_id: str
    decision: str
    selected_title: str | None
    title_source: str | None
    created_at_ms: int


@dataclass(frozen=True, slots=True)
class BookBaselineRecord:
    id: str
    project_id: str
    book_id: str
    baseline_version: int
    parent_baseline_id: str | None
    submission_id: str
    review_id: str
    approval_id: str
    approved_title: str
    title_source: str
    direction_ref_id: str
    constraints_ref_id: str
    rolling_plan_ref_id: str
    completion_contract_ref_id: str
    minimum_chapter_count: int
    maximum_chapter_count: int
    created_at_ms: int


def _book_record(row: RowMapping) -> BookRecord:
    return BookRecord(
        id=cast(str, row["id"]),
        project_id=cast(str, row["project_id"]),
        lifecycle_status=cast(str, row["lifecycle_status"]),
        current_baseline_id=cast(str | None, row["current_baseline_id"]),
        current_completion_id=cast(str | None, row["current_completion_id"]),
        created_at_ms=cast(int, row["created_at_ms"]),
        updated_at_ms=cast(int, row["updated_at_ms"]),
    )


def _workspace_record(row: RowMapping) -> BookWorkspaceRecord:
    return BookWorkspaceRecord(
        id=cast(str, row["id"]),
        project_id=cast(str, row["project_id"]),
        book_id=cast(str, row["book_id"]),
        state=cast(str, row["state"]),
        lock_version=cast(int, row["lock_version"]),
        base_book_baseline_id=cast(str | None, row["base_book_baseline_id"]),
        base_canon_baseline_id=cast(str, row["base_canon_baseline_id"]),
        direction_draft_ref_id=cast(str, row["direction_draft_ref_id"]),
        discussion_state_ref_id=cast(str, row["discussion_state_ref_id"]),
        transcript_ref_id=cast(str, row["transcript_ref_id"]),
        candidate_constraints_ref_id=cast(str | None, row["candidate_constraints_ref_id"]),
        candidate_titles_ref_id=cast(str | None, row["candidate_titles_ref_id"]),
        candidate_rolling_plan_ref_id=cast(str | None, row["candidate_rolling_plan_ref_id"]),
        candidate_completion_contract_ref_id=cast(
            str | None, row["candidate_completion_contract_ref_id"]
        ),
        readiness_status=cast(str, row["readiness_status"]),
        repair_policy_id=cast(str, row["repair_policy_id"]),
        semantic_repair_count=cast(int, row["semantic_repair_count"]),
        semantic_repair_limit=cast(int, row["semantic_repair_limit"]),
        stale_reason_code=cast(str | None, row["stale_reason_code"]),
        stale_at_ms=cast(int | None, row["stale_at_ms"]),
        created_at_ms=cast(int, row["created_at_ms"]),
        updated_at_ms=cast(int, row["updated_at_ms"]),
        guidance_ref_id=cast(str | None, row["guidance_ref_id"]),
    )


def _submission_record(row: RowMapping) -> BookSubmissionRecord:
    return BookSubmissionRecord(
        id=cast(str, row["id"]),
        project_id=cast(str, row["project_id"]),
        book_id=cast(str, row["book_id"]),
        workspace_id=cast(str, row["workspace_id"]),
        workspace_lock_version=cast(int, row["workspace_lock_version"]),
        base_book_baseline_id=cast(str | None, row["base_book_baseline_id"]),
        canon_baseline_id=cast(str, row["canon_baseline_id"]),
        direction_ref_id=cast(str, row["direction_ref_id"]),
        constraints_ref_id=cast(str, row["constraints_ref_id"]),
        titles_ref_id=cast(str, row["titles_ref_id"]),
        rolling_plan_ref_id=cast(str, row["rolling_plan_ref_id"]),
        completion_contract_ref_id=cast(str, row["completion_contract_ref_id"]),
        content_manifest_ref_id=cast(str, row["content_manifest_ref_id"]),
        content_fingerprint=cast(str, row["content_fingerprint"]),
        disposition=cast(str, row["disposition"]),
        close_reason_code=cast(str | None, row["close_reason_code"]),
        created_at_ms=cast(int, row["created_at_ms"]),
        closed_at_ms=cast(int | None, row["closed_at_ms"]),
    )


def _review_record(row: RowMapping) -> BookReviewRecord:
    return BookReviewRecord(
        id=cast(str, row["id"]),
        project_id=cast(str, row["project_id"]),
        book_id=cast(str, row["book_id"]),
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


def _baseline_record(row: RowMapping) -> BookBaselineRecord:
    return BookBaselineRecord(
        id=cast(str, row["id"]),
        project_id=cast(str, row["project_id"]),
        book_id=cast(str, row["book_id"]),
        baseline_version=cast(int, row["baseline_version"]),
        parent_baseline_id=cast(str | None, row["parent_baseline_id"]),
        submission_id=cast(str, row["submission_id"]),
        review_id=cast(str, row["review_id"]),
        approval_id=cast(str, row["approval_id"]),
        approved_title=cast(str, row["approved_title"]),
        title_source=cast(str, row["title_source"]),
        direction_ref_id=cast(str, row["direction_ref_id"]),
        constraints_ref_id=cast(str, row["constraints_ref_id"]),
        rolling_plan_ref_id=cast(str, row["rolling_plan_ref_id"]),
        completion_contract_ref_id=cast(str, row["completion_contract_ref_id"]),
        minimum_chapter_count=cast(int, row["minimum_chapter_count"]),
        maximum_chapter_count=cast(int, row["maximum_chapter_count"]),
        created_at_ms=cast(int, row["created_at_ms"]),
    )


class BookRepository:
    def __init__(self, connection: AsyncConnection) -> None:
        self._connection = connection

    async def insert(self, record: BookRecord) -> None:
        await self._connection.execute(books.insert().values(**asdict(record)))

    async def get_for_project(self, project_id: str) -> BookRecord | None:
        row = (
            await self._connection.execute(
                select(books).where(books.c.project_id == project_id)
            )
        ).mappings().one_or_none()
        return None if row is None else _book_record(row)

    async def insert_workspace(self, record: BookWorkspaceRecord) -> None:
        await self._connection.execute(book_workspaces.insert().values(**asdict(record)))

    async def get_workspace(self, *, project_id: str, book_id: str) -> BookWorkspaceRecord | None:
        row = (
            await self._connection.execute(
                select(book_workspaces).where(
                    book_workspaces.c.project_id == project_id,
                    book_workspaces.c.book_id == book_id,
                )
            )
        ).mappings().one_or_none()
        return None if row is None else _workspace_record(row)

    async def compare_and_set_workspace(
        self,
        *,
        record: BookWorkspaceRecord,
        expected_lock_version: int,
    ) -> bool:
        values = asdict(record)
        values.pop("id")
        values.pop("project_id")
        values.pop("book_id")
        result = await self._connection.execute(
            update(book_workspaces)
            .where(
                book_workspaces.c.id == record.id,
                book_workspaces.c.project_id == record.project_id,
                book_workspaces.c.book_id == record.book_id,
                book_workspaces.c.lock_version == expected_lock_version,
            )
            .values(**values)
        )
        return result.rowcount == 1

    async def find_pending_submission(
        self, *, project_id: str, book_id: str
    ) -> BookSubmissionRecord | None:
        row = (
            await self._connection.execute(
                select(book_review_submissions).where(
                    book_review_submissions.c.project_id == project_id,
                    book_review_submissions.c.book_id == book_id,
                    book_review_submissions.c.disposition == "pending",
                )
            )
        ).mappings().one_or_none()
        return None if row is None else _submission_record(row)

    async def get_submission(
        self, *, project_id: str, submission_id: str
    ) -> BookSubmissionRecord | None:
        row = (
            await self._connection.execute(
                select(book_review_submissions).where(
                    book_review_submissions.c.project_id == project_id,
                    book_review_submissions.c.id == submission_id,
                )
            )
        ).mappings().one_or_none()
        return None if row is None else _submission_record(row)

    async def insert_submission(self, record: BookSubmissionRecord) -> None:
        await self._connection.execute(
            book_review_submissions.insert().values(**asdict(record))
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
            update(book_review_submissions)
            .where(
                book_review_submissions.c.project_id == project_id,
                book_review_submissions.c.id == submission_id,
                book_review_submissions.c.disposition == "pending",
            )
            .values(
                disposition=disposition,
                close_reason_code=reason_code,
                closed_at_ms=closed_at_ms,
            )
        )
        return result.rowcount == 1

    async def insert_review(self, record: BookReviewRecord) -> None:
        await self._connection.execute(book_reviews.insert().values(**asdict(record)))

    async def get_review(self, *, project_id: str, review_id: str) -> BookReviewRecord | None:
        row = (
            await self._connection.execute(
                select(book_reviews).where(
                    book_reviews.c.project_id == project_id,
                    book_reviews.c.id == review_id,
                )
            )
        ).mappings().one_or_none()
        return None if row is None else _review_record(row)

    async def get_latest_review(
        self,
        *,
        project_id: str,
        book_id: str,
    ) -> BookReviewRecord | None:
        row = (
            await self._connection.execute(
                select(book_reviews)
                .where(
                    book_reviews.c.project_id == project_id,
                    book_reviews.c.book_id == book_id,
                )
                .order_by(book_reviews.c.created_at_ms.desc(), book_reviews.c.id.desc())
                .limit(1)
            )
        ).mappings().one_or_none()
        return None if row is None else _review_record(row)

    async def insert_approval(self, record: BookApprovalRecord) -> None:
        await self._connection.execute(book_approvals.insert().values(**asdict(record)))

    async def next_baseline_version(self, *, book_id: str) -> int:
        current = await self._connection.scalar(
            select(func.coalesce(func.max(book_baselines.c.baseline_version), 0)).where(
                book_baselines.c.book_id == book_id
            )
        )
        return cast(int, current) + 1

    async def get_baseline_version(
        self,
        *,
        project_id: str,
        book_id: str,
        baseline_id: str,
    ) -> int | None:
        return cast(
            int | None,
            await self._connection.scalar(
                select(book_baselines.c.baseline_version).where(
                    book_baselines.c.project_id == project_id,
                    book_baselines.c.book_id == book_id,
                    book_baselines.c.id == baseline_id,
                )
            ),
        )

    async def get_baseline(
        self, *, project_id: str, book_id: str, baseline_id: str
    ) -> BookBaselineRecord | None:
        row = (
            await self._connection.execute(
                select(book_baselines).where(
                    book_baselines.c.project_id == project_id,
                    book_baselines.c.book_id == book_id,
                    book_baselines.c.id == baseline_id,
                )
            )
        ).mappings().one_or_none()
        return None if row is None else _baseline_record(row)

    async def insert_baseline(self, record: BookBaselineRecord) -> None:
        await self._connection.execute(book_baselines.insert().values(**asdict(record)))

    async def compare_and_set_current_baseline(
        self,
        *,
        project_id: str,
        book_id: str,
        expected_baseline_id: str | None,
        new_baseline_id: str,
        updated_at_ms: int,
    ) -> bool:
        expected = (
            books.c.current_baseline_id.is_(None)
            if expected_baseline_id is None
            else books.c.current_baseline_id == expected_baseline_id
        )
        result = await self._connection.execute(
            update(books)
            .where(
                books.c.project_id == project_id,
                books.c.id == book_id,
                expected,
                books.c.current_completion_id.is_(None),
            )
            .values(
                lifecycle_status="active",
                current_baseline_id=new_baseline_id,
                updated_at_ms=updated_at_ms,
            )
        )
        return result.rowcount == 1

    async def commit_completion(
        self,
        *,
        project_id: str,
        book_id: str,
        expected_baseline_id: str,
        completion_id: str,
        updated_at_ms: int,
    ) -> bool:
        result = await self._connection.execute(
            update(books)
            .where(
                books.c.project_id == project_id,
                books.c.id == book_id,
                books.c.lifecycle_status == "active",
                books.c.current_baseline_id == expected_baseline_id,
                books.c.current_completion_id.is_(None),
            )
            .values(
                lifecycle_status="completed",
                current_completion_id=completion_id,
                updated_at_ms=updated_at_ms,
            )
        )
        return result.rowcount == 1

    async def reopen(
        self,
        *,
        project_id: str,
        book_id: str,
        expected_completion_id: str,
        updated_at_ms: int,
    ) -> bool:
        completion_exists = await self._connection.scalar(
            select(book_completions.c.id).where(
                book_completions.c.project_id == project_id,
                book_completions.c.book_id == book_id,
                book_completions.c.id == expected_completion_id,
            )
        )
        if completion_exists is None:
            return False
        result = await self._connection.execute(
            update(books)
            .where(
                books.c.project_id == project_id,
                books.c.id == book_id,
                books.c.lifecycle_status == "completed",
                books.c.current_completion_id == expected_completion_id,
            )
            .values(
                lifecycle_status="active",
                current_completion_id=None,
                updated_at_ms=updated_at_ms,
            )
        )
        return result.rowcount == 1
