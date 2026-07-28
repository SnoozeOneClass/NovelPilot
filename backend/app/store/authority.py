from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, TypeVar, cast

from sqlalchemy import RowMapping, func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from app.db.schema import (
    arc_book_change_requests,
    arc_closure_reviews,
    arc_closures,
    arc_parent_reviews,
    book_boundary_reviews,
    book_parent_reviews,
    book_progress_handoffs,
    books,
    chapter_arc_change_requests,
    story_arcs,
)

RecordT = TypeVar("RecordT")


def _record(record_type: type[RecordT], row: RowMapping) -> RecordT:
    return record_type(**cast(Any, dict(row)))


@dataclass(frozen=True, slots=True)
class ArcParentReviewRecord:
    id: str
    project_id: str
    book_id: str
    arc_id: str
    request_id: str
    target_arc_baseline_id: str
    source_task_id: str
    source_attempt_id: str
    strategy_id: str
    strategy_version: int
    rubric_id: str
    rubric_version: int
    arc_contract_judgment: str
    parent_review_judgment: str
    disposition: str
    resolution_owner: str
    detail_ref_id: str
    precheck_ref_id: str
    user_question_ref_id: str | None
    exact_input_fingerprint: str
    correction_lineage_id: str
    correction_lineage_origin: str
    automatic_correction_round: int
    review_ordinal: int
    predecessor_review_id: str | None
    source_feedback_id: str | None
    source_exhausted_review_id: str | None
    opened_arc_workspace_id: str | None
    created_at_ms: int


@dataclass(frozen=True, slots=True)
class BookParentReviewRecord:
    id: str
    project_id: str
    book_id: str
    arc_id: str
    request_id: str
    target_book_baseline_id: str
    source_task_id: str
    source_attempt_id: str
    strategy_id: str
    strategy_version: int
    rubric_id: str
    rubric_version: int
    book_contract_judgment: str
    disposition: str
    resolution_owner: str
    detail_ref_id: str
    precheck_ref_id: str
    user_question_ref_id: str | None
    exact_input_fingerprint: str
    correction_lineage_id: str
    correction_lineage_origin: str
    automatic_correction_round: int
    review_ordinal: int
    predecessor_review_id: str | None
    source_feedback_id: str | None
    source_exhausted_review_id: str | None
    opened_book_workspace_id: str | None
    created_at_ms: int


@dataclass(frozen=True, slots=True)
class ArcClosureReviewRecord:
    id: str
    project_id: str
    book_id: str
    arc_id: str
    book_baseline_id: str
    arc_baseline_id: str
    canon_baseline_id: str
    terminal_chapter_id: str
    terminal_chapter_baseline_id: str
    cumulative_committed_chapter_count: int
    closure_cumulative_chapter_count: int
    chapter_set_fingerprint: str
    chapter_set_manifest_ref_id: str
    source_task_id: str
    source_attempt_id: str
    strategy_id: str
    strategy_version: int
    rubric_id: str
    rubric_version: int
    arc_contract_judgment: str
    parent_review_judgment: str
    disposition: str
    resolution_owner: str
    detail_ref_id: str
    precheck_ref_id: str
    user_question_ref_id: str | None
    exact_input_fingerprint: str
    correction_lineage_id: str
    correction_lineage_origin: str
    automatic_correction_round: int
    review_ordinal: int
    predecessor_review_id: str | None
    source_feedback_id: str | None
    source_exhausted_review_id: str | None
    opened_arc_workspace_id: str | None
    created_at_ms: int


@dataclass(frozen=True, slots=True)
class ArcClosureRecord:
    id: str
    project_id: str
    book_id: str
    arc_id: str
    closure_version: int
    parent_closure_id: str | None
    closure_review_id: str
    book_baseline_id: str
    arc_baseline_id: str
    canon_baseline_id: str
    terminal_chapter_id: str
    terminal_chapter_baseline_id: str
    cumulative_committed_chapter_count: int
    chapter_set_fingerprint: str
    chapter_set_manifest_ref_id: str
    normalized_result_ref_id: str
    created_at_ms: int


@dataclass(frozen=True, slots=True)
class BookBoundaryReviewRecord:
    id: str
    project_id: str
    book_id: str
    book_baseline_id: str
    arc_closure_id: str
    canon_baseline_id: str
    committed_chapter_count: int
    chapter_set_fingerprint: str
    source_task_id: str
    source_attempt_id: str
    strategy_id: str
    strategy_version: int
    rubric_id: str
    rubric_version: int
    requirement_statuses_ref_id: str
    ending_trajectory_judgment: str
    book_contract_judgment: str
    disposition: str
    resolution_owner: str
    detail_ref_id: str
    precheck_ref_id: str
    user_question_ref_id: str | None
    exact_input_fingerprint: str
    correction_lineage_id: str
    correction_lineage_origin: str
    automatic_correction_round: int
    review_ordinal: int
    predecessor_review_id: str | None
    source_feedback_id: str | None
    source_exhausted_review_id: str | None
    opened_book_workspace_id: str | None
    created_at_ms: int


@dataclass(frozen=True, slots=True)
class BookProgressHandoffRecord:
    id: str
    project_id: str
    book_id: str
    handoff_version: int
    parent_handoff_id: str | None
    source_boundary_review_id: str
    arc_closure_id: str
    book_baseline_id: str
    canon_baseline_id: str
    next_arc_purpose: str
    remaining_requirements_ref_id: str
    guidance_ref_id: str
    created_at_ms: int


class ArcParentReviewRepository:
    def __init__(self, connection: AsyncConnection) -> None:
        self._connection = connection

    async def insert(self, record: ArcParentReviewRecord) -> None:
        await self._connection.execute(
            arc_parent_reviews.insert().values(**asdict(record))
        )

    async def get(
        self, *, project_id: str, review_id: str
    ) -> ArcParentReviewRecord | None:
        row = (
            await self._connection.execute(
                select(arc_parent_reviews).where(
                    arc_parent_reviews.c.project_id == project_id,
                    arc_parent_reviews.c.id == review_id,
                )
            )
        ).mappings().one_or_none()
        return None if row is None else _record(ArcParentReviewRecord, row)

    async def get_latest_for_request(
        self, *, project_id: str, request_id: str
    ) -> ArcParentReviewRecord | None:
        latest_id = (
            select(chapter_arc_change_requests.c.latest_parent_review_id)
            .where(
                chapter_arc_change_requests.c.project_id == project_id,
                chapter_arc_change_requests.c.id == request_id,
            )
            .scalar_subquery()
        )
        row = (
            await self._connection.execute(
                select(arc_parent_reviews).where(
                    arc_parent_reviews.c.project_id == project_id,
                    arc_parent_reviews.c.request_id == request_id,
                    arc_parent_reviews.c.id == latest_id,
                )
            )
        ).mappings().one_or_none()
        return None if row is None else _record(ArcParentReviewRecord, row)

    async def get_for_exact_input(
        self, *, project_id: str, request_id: str, exact_input_fingerprint: str
    ) -> ArcParentReviewRecord | None:
        row = (
            await self._connection.execute(
                select(arc_parent_reviews).where(
                    arc_parent_reviews.c.project_id == project_id,
                    arc_parent_reviews.c.request_id == request_id,
                    arc_parent_reviews.c.exact_input_fingerprint
                    == exact_input_fingerprint,
                )
            )
        ).mappings().one_or_none()
        return None if row is None else _record(ArcParentReviewRecord, row)

    async def mark_workspace_opened(
        self,
        *,
        project_id: str,
        review_id: str,
        workspace_id: str,
    ) -> bool:
        result = await self._connection.execute(
            update(arc_parent_reviews)
            .where(
                arc_parent_reviews.c.project_id == project_id,
                arc_parent_reviews.c.id == review_id,
                arc_parent_reviews.c.disposition == "arc_revision_warranted",
                arc_parent_reviews.c.opened_arc_workspace_id.is_(None),
            )
            .values(opened_arc_workspace_id=workspace_id)
        )
        return result.rowcount == 1

    async def has_round_one(
        self, *, project_id: str, correction_lineage_id: str
    ) -> bool:
        value = await self._connection.scalar(
            select(arc_parent_reviews.c.id)
            .where(
                arc_parent_reviews.c.project_id == project_id,
                arc_parent_reviews.c.correction_lineage_id
                == correction_lineage_id,
                arc_parent_reviews.c.automatic_correction_round == 1,
            )
            .limit(1)
        )
        return value is not None


class BookParentReviewRepository:
    def __init__(self, connection: AsyncConnection) -> None:
        self._connection = connection

    async def insert(self, record: BookParentReviewRecord) -> None:
        await self._connection.execute(
            book_parent_reviews.insert().values(**asdict(record))
        )

    async def get(
        self, *, project_id: str, review_id: str
    ) -> BookParentReviewRecord | None:
        row = (
            await self._connection.execute(
                select(book_parent_reviews).where(
                    book_parent_reviews.c.project_id == project_id,
                    book_parent_reviews.c.id == review_id,
                )
            )
        ).mappings().one_or_none()
        return None if row is None else _record(BookParentReviewRecord, row)

    async def get_latest_for_request(
        self, *, project_id: str, request_id: str
    ) -> BookParentReviewRecord | None:
        latest_id = (
            select(arc_book_change_requests.c.latest_parent_review_id)
            .where(
                arc_book_change_requests.c.project_id == project_id,
                arc_book_change_requests.c.id == request_id,
            )
            .scalar_subquery()
        )
        row = (
            await self._connection.execute(
                select(book_parent_reviews).where(
                    book_parent_reviews.c.project_id == project_id,
                    book_parent_reviews.c.request_id == request_id,
                    book_parent_reviews.c.id == latest_id,
                )
            )
        ).mappings().one_or_none()
        return None if row is None else _record(BookParentReviewRecord, row)

    async def get_for_exact_input(
        self, *, project_id: str, request_id: str, exact_input_fingerprint: str
    ) -> BookParentReviewRecord | None:
        row = (
            await self._connection.execute(
                select(book_parent_reviews).where(
                    book_parent_reviews.c.project_id == project_id,
                    book_parent_reviews.c.request_id == request_id,
                    book_parent_reviews.c.exact_input_fingerprint
                    == exact_input_fingerprint,
                )
            )
        ).mappings().one_or_none()
        return None if row is None else _record(BookParentReviewRecord, row)

    async def mark_workspace_opened(
        self,
        *,
        project_id: str,
        review_id: str,
        workspace_id: str,
    ) -> bool:
        result = await self._connection.execute(
            update(book_parent_reviews)
            .where(
                book_parent_reviews.c.project_id == project_id,
                book_parent_reviews.c.id == review_id,
                book_parent_reviews.c.disposition == "book_revision_warranted",
                book_parent_reviews.c.opened_book_workspace_id.is_(None),
            )
            .values(opened_book_workspace_id=workspace_id)
        )
        return result.rowcount == 1

    async def has_round_one(
        self, *, project_id: str, correction_lineage_id: str
    ) -> bool:
        value = await self._connection.scalar(
            select(book_parent_reviews.c.id)
            .where(
                book_parent_reviews.c.project_id == project_id,
                book_parent_reviews.c.correction_lineage_id
                == correction_lineage_id,
                book_parent_reviews.c.automatic_correction_round == 1,
            )
            .limit(1)
        )
        return value is not None


class ArcClosureReviewRepository:
    def __init__(self, connection: AsyncConnection) -> None:
        self._connection = connection

    async def insert(self, record: ArcClosureReviewRecord) -> None:
        await self._connection.execute(
            arc_closure_reviews.insert().values(**asdict(record))
        )

    async def get(
        self, *, project_id: str, review_id: str
    ) -> ArcClosureReviewRecord | None:
        row = (
            await self._connection.execute(
                select(arc_closure_reviews).where(
                    arc_closure_reviews.c.project_id == project_id,
                    arc_closure_reviews.c.id == review_id,
                )
            )
        ).mappings().one_or_none()
        return None if row is None else _record(ArcClosureReviewRecord, row)

    async def get_latest_for_arc(
        self, *, project_id: str, arc_id: str
    ) -> ArcClosureReviewRecord | None:
        latest_id = (
            select(story_arcs.c.latest_closure_review_id)
            .where(
                story_arcs.c.project_id == project_id,
                story_arcs.c.id == arc_id,
            )
            .scalar_subquery()
        )
        row = (
            await self._connection.execute(
                select(arc_closure_reviews).where(
                    arc_closure_reviews.c.project_id == project_id,
                    arc_closure_reviews.c.arc_id == arc_id,
                    arc_closure_reviews.c.id == latest_id,
                )
            )
        ).mappings().one_or_none()
        return None if row is None else _record(ArcClosureReviewRecord, row)

    async def get_for_exact_input(
        self, *, project_id: str, arc_id: str, exact_input_fingerprint: str
    ) -> ArcClosureReviewRecord | None:
        row = (
            await self._connection.execute(
                select(arc_closure_reviews).where(
                    arc_closure_reviews.c.project_id == project_id,
                    arc_closure_reviews.c.arc_id == arc_id,
                    arc_closure_reviews.c.exact_input_fingerprint
                    == exact_input_fingerprint,
                )
            )
        ).mappings().one_or_none()
        return None if row is None else _record(ArcClosureReviewRecord, row)

    async def mark_workspace_opened(
        self,
        *,
        project_id: str,
        review_id: str,
        workspace_id: str,
    ) -> bool:
        result = await self._connection.execute(
            update(arc_closure_reviews)
            .where(
                arc_closure_reviews.c.project_id == project_id,
                arc_closure_reviews.c.id == review_id,
                arc_closure_reviews.c.disposition == "arc_revision_warranted",
                arc_closure_reviews.c.opened_arc_workspace_id.is_(None),
            )
            .values(opened_arc_workspace_id=workspace_id)
        )
        return result.rowcount == 1

    async def compare_and_set_latest(
        self,
        *,
        project_id: str,
        arc_id: str,
        arc_baseline_id: str,
        expected_review_id: str | None,
        new_review_id: str,
        updated_at_ms: int,
    ) -> bool:
        expected = (
            story_arcs.c.latest_closure_review_id.is_(None)
            if expected_review_id is None
            else story_arcs.c.latest_closure_review_id == expected_review_id
        )
        result = await self._connection.execute(
            update(story_arcs)
            .where(
                story_arcs.c.project_id == project_id,
                story_arcs.c.id == arc_id,
                story_arcs.c.lifecycle_status == "closing",
                story_arcs.c.current_baseline_id == arc_baseline_id,
                story_arcs.c.current_closure_id.is_(None),
                expected,
            )
            .values(
                latest_closure_review_id=new_review_id,
                updated_at_ms=updated_at_ms,
            )
        )
        return result.rowcount == 1

    async def has_round_one(
        self, *, project_id: str, correction_lineage_id: str
    ) -> bool:
        value = await self._connection.scalar(
            select(arc_closure_reviews.c.id)
            .where(
                arc_closure_reviews.c.project_id == project_id,
                arc_closure_reviews.c.correction_lineage_id
                == correction_lineage_id,
                arc_closure_reviews.c.automatic_correction_round == 1,
            )
            .limit(1)
        )
        return value is not None


class ArcClosureRepository:
    def __init__(self, connection: AsyncConnection) -> None:
        self._connection = connection

    async def insert(self, record: ArcClosureRecord) -> None:
        await self._connection.execute(arc_closures.insert().values(**asdict(record)))

    async def get(
        self, *, project_id: str, closure_id: str
    ) -> ArcClosureRecord | None:
        row = (
            await self._connection.execute(
                select(arc_closures).where(
                    arc_closures.c.project_id == project_id,
                    arc_closures.c.id == closure_id,
                )
            )
        ).mappings().one_or_none()
        return None if row is None else _record(ArcClosureRecord, row)

    async def next_version(self, *, arc_id: str) -> int:
        value = await self._connection.scalar(
            select(func.coalesce(func.max(arc_closures.c.closure_version), 0)).where(
                arc_closures.c.arc_id == arc_id
            )
        )
        return cast(int, value) + 1

    async def get_latest_for_arc(
        self, *, project_id: str, arc_id: str
    ) -> ArcClosureRecord | None:
        row = (
            await self._connection.execute(
                select(arc_closures)
                .where(
                    arc_closures.c.project_id == project_id,
                    arc_closures.c.arc_id == arc_id,
                )
                .order_by(
                    arc_closures.c.closure_version.desc(),
                    arc_closures.c.id.desc(),
                )
                .limit(1)
            )
        ).mappings().one_or_none()
        return None if row is None else _record(ArcClosureRecord, row)

    async def compare_and_set_current(
        self,
        *,
        project_id: str,
        arc_id: str,
        arc_baseline_id: str,
        closure_review_id: str,
        closure_id: str,
        completed_at_ms: int,
    ) -> bool:
        result = await self._connection.execute(
            update(story_arcs)
            .where(
                story_arcs.c.project_id == project_id,
                story_arcs.c.id == arc_id,
                story_arcs.c.lifecycle_status == "closing",
                story_arcs.c.current_baseline_id == arc_baseline_id,
                story_arcs.c.latest_closure_review_id == closure_review_id,
                story_arcs.c.current_closure_id.is_(None),
            )
            .values(
                lifecycle_status="completed",
                current_closure_id=closure_id,
                completed_at_ms=completed_at_ms,
                updated_at_ms=completed_at_ms,
            )
        )
        return result.rowcount == 1


class BookBoundaryReviewRepository:
    def __init__(self, connection: AsyncConnection) -> None:
        self._connection = connection

    async def insert(self, record: BookBoundaryReviewRecord) -> None:
        await self._connection.execute(
            book_boundary_reviews.insert().values(**asdict(record))
        )

    async def get(
        self, *, project_id: str, review_id: str
    ) -> BookBoundaryReviewRecord | None:
        row = (
            await self._connection.execute(
                select(book_boundary_reviews).where(
                    book_boundary_reviews.c.project_id == project_id,
                    book_boundary_reviews.c.id == review_id,
                )
            )
        ).mappings().one_or_none()
        return None if row is None else _record(BookBoundaryReviewRecord, row)

    async def get_latest_for_book(
        self, *, project_id: str, book_id: str
    ) -> BookBoundaryReviewRecord | None:
        latest_id = (
            select(books.c.latest_boundary_review_id)
            .where(
                books.c.project_id == project_id,
                books.c.id == book_id,
            )
            .scalar_subquery()
        )
        row = (
            await self._connection.execute(
                select(book_boundary_reviews).where(
                    book_boundary_reviews.c.project_id == project_id,
                    book_boundary_reviews.c.book_id == book_id,
                    book_boundary_reviews.c.id == latest_id,
                )
            )
        ).mappings().one_or_none()
        return None if row is None else _record(BookBoundaryReviewRecord, row)

    async def get_latest_opened_revision_for_closure(
        self,
        *,
        project_id: str,
        book_id: str,
        arc_closure_id: str,
        workspace_id: str,
    ) -> BookBoundaryReviewRecord | None:
        row = (
            await self._connection.execute(
                select(book_boundary_reviews)
                .where(
                    book_boundary_reviews.c.project_id == project_id,
                    book_boundary_reviews.c.book_id == book_id,
                    book_boundary_reviews.c.arc_closure_id == arc_closure_id,
                    book_boundary_reviews.c.disposition
                    == "book_revision_warranted",
                    book_boundary_reviews.c.opened_book_workspace_id
                    == workspace_id,
                )
                .order_by(
                    book_boundary_reviews.c.created_at_ms.desc(),
                    book_boundary_reviews.c.id.desc(),
                )
                .limit(1)
            )
        ).mappings().one_or_none()
        return None if row is None else _record(BookBoundaryReviewRecord, row)

    async def get_for_exact_input(
        self, *, project_id: str, book_id: str, exact_input_fingerprint: str
    ) -> BookBoundaryReviewRecord | None:
        row = (
            await self._connection.execute(
                select(book_boundary_reviews).where(
                    book_boundary_reviews.c.project_id == project_id,
                    book_boundary_reviews.c.book_id == book_id,
                    book_boundary_reviews.c.exact_input_fingerprint
                    == exact_input_fingerprint,
                )
            )
        ).mappings().one_or_none()
        return None if row is None else _record(BookBoundaryReviewRecord, row)

    async def mark_workspace_opened(
        self,
        *,
        project_id: str,
        review_id: str,
        workspace_id: str,
    ) -> bool:
        result = await self._connection.execute(
            update(book_boundary_reviews)
            .where(
                book_boundary_reviews.c.project_id == project_id,
                book_boundary_reviews.c.id == review_id,
                book_boundary_reviews.c.disposition == "book_revision_warranted",
                book_boundary_reviews.c.opened_book_workspace_id.is_(None),
            )
            .values(opened_book_workspace_id=workspace_id)
        )
        return result.rowcount == 1

    async def compare_and_set_latest(
        self,
        *,
        project_id: str,
        book_id: str,
        book_baseline_id: str,
        expected_review_id: str | None,
        new_review_id: str,
        updated_at_ms: int,
    ) -> bool:
        expected = (
            books.c.latest_boundary_review_id.is_(None)
            if expected_review_id is None
            else books.c.latest_boundary_review_id == expected_review_id
        )
        result = await self._connection.execute(
            update(books)
            .where(
                books.c.project_id == project_id,
                books.c.id == book_id,
                books.c.lifecycle_status == "active",
                books.c.current_baseline_id == book_baseline_id,
                books.c.current_completion_id.is_(None),
                expected,
            )
            .values(
                latest_boundary_review_id=new_review_id,
                updated_at_ms=updated_at_ms,
            )
        )
        return result.rowcount == 1

    async def has_round_one(
        self, *, project_id: str, correction_lineage_id: str
    ) -> bool:
        value = await self._connection.scalar(
            select(book_boundary_reviews.c.id)
            .where(
                book_boundary_reviews.c.project_id == project_id,
                book_boundary_reviews.c.correction_lineage_id
                == correction_lineage_id,
                book_boundary_reviews.c.automatic_correction_round == 1,
            )
            .limit(1)
        )
        return value is not None


class BookProgressHandoffRepository:
    def __init__(self, connection: AsyncConnection) -> None:
        self._connection = connection

    async def insert(self, record: BookProgressHandoffRecord) -> None:
        await self._connection.execute(
            book_progress_handoffs.insert().values(**asdict(record))
        )

    async def get(
        self, *, project_id: str, handoff_id: str
    ) -> BookProgressHandoffRecord | None:
        row = (
            await self._connection.execute(
                select(book_progress_handoffs).where(
                    book_progress_handoffs.c.project_id == project_id,
                    book_progress_handoffs.c.id == handoff_id,
                )
            )
        ).mappings().one_or_none()
        return None if row is None else _record(BookProgressHandoffRecord, row)

    async def get_for_boundary_review(
        self, *, project_id: str, boundary_review_id: str
    ) -> BookProgressHandoffRecord | None:
        row = (
            await self._connection.execute(
                select(book_progress_handoffs).where(
                    book_progress_handoffs.c.project_id == project_id,
                    book_progress_handoffs.c.source_boundary_review_id
                    == boundary_review_id,
                )
            )
        ).mappings().one_or_none()
        return None if row is None else _record(BookProgressHandoffRecord, row)

    async def get_latest_for_book(
        self, *, project_id: str, book_id: str
    ) -> BookProgressHandoffRecord | None:
        row = (
            await self._connection.execute(
                select(book_progress_handoffs)
                .where(
                    book_progress_handoffs.c.project_id == project_id,
                    book_progress_handoffs.c.book_id == book_id,
                )
                .order_by(
                    book_progress_handoffs.c.handoff_version.desc(),
                    book_progress_handoffs.c.id.desc(),
                )
                .limit(1)
            )
        ).mappings().one_or_none()
        return None if row is None else _record(BookProgressHandoffRecord, row)

    async def next_version(self, *, book_id: str) -> int:
        value = await self._connection.scalar(
            select(
                func.coalesce(func.max(book_progress_handoffs.c.handoff_version), 0)
            ).where(book_progress_handoffs.c.book_id == book_id)
        )
        return cast(int, value) + 1

    async def compare_and_set_current(
        self,
        *,
        project_id: str,
        book_id: str,
        book_baseline_id: str,
        boundary_review_id: str,
        expected_handoff_id: str | None,
        new_handoff_id: str,
        updated_at_ms: int,
    ) -> bool:
        expected = (
            books.c.current_progress_handoff_id.is_(None)
            if expected_handoff_id is None
            else books.c.current_progress_handoff_id == expected_handoff_id
        )
        result = await self._connection.execute(
            update(books)
            .where(
                books.c.project_id == project_id,
                books.c.id == book_id,
                books.c.lifecycle_status == "active",
                books.c.current_baseline_id == book_baseline_id,
                books.c.latest_boundary_review_id == boundary_review_id,
                books.c.current_completion_id.is_(None),
                expected,
            )
            .values(
                current_progress_handoff_id=new_handoff_id,
                updated_at_ms=updated_at_ms,
            )
        )
        return result.rowcount == 1
