from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, cast

from sqlalchemy import RowMapping, Table, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from app.db.schema import (
    arc_approval_gates,
    arc_book_change_requests,
    arc_review_submissions,
    arc_workspaces,
    chapter_arc_change_requests,
    chapter_book_change_requests,
    chapter_review_submissions,
    chapter_workspaces,
    story_arcs,
)


@dataclass(frozen=True, slots=True)
class ChapterArcChangeRequestRecord:
    id: str
    project_id: str
    book_id: str
    arc_id: str
    chapter_id: str
    source_submission_id: str
    source_review_id: str
    target_arc_baseline_id: str
    evidence_ref_id: str
    status: str
    resolved_by_arc_baseline_id: str | None
    close_reason_code: str | None
    created_at_ms: int
    closed_at_ms: int | None


@dataclass(frozen=True, slots=True)
class ChapterBookChangeRequestRecord:
    id: str
    project_id: str
    book_id: str
    arc_id: str
    chapter_id: str
    source_submission_id: str
    source_review_id: str
    target_book_baseline_id: str
    evidence_ref_id: str
    status: str
    resolved_by_book_baseline_id: str | None
    close_reason_code: str | None
    created_at_ms: int
    closed_at_ms: int | None


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
    resolved_by_book_baseline_id: str | None
    close_reason_code: str | None
    created_at_ms: int
    closed_at_ms: int | None


@dataclass(frozen=True, slots=True)
class OpenChangeRequestRecord:
    request_kind: Literal["chapter_to_arc", "chapter_to_book", "arc_to_book"]
    id: str
    project_id: str
    target_id: str
    target_baseline_id: str
    evidence_ref_id: str
    created_at_ms: int


def _chapter_arc_record(row: RowMapping) -> ChapterArcChangeRequestRecord:
    return ChapterArcChangeRequestRecord(
        id=cast(str, row["id"]),
        project_id=cast(str, row["project_id"]),
        book_id=cast(str, row["book_id"]),
        arc_id=cast(str, row["arc_id"]),
        chapter_id=cast(str, row["chapter_id"]),
        source_submission_id=cast(str, row["source_submission_id"]),
        source_review_id=cast(str, row["source_review_id"]),
        target_arc_baseline_id=cast(str, row["target_arc_baseline_id"]),
        evidence_ref_id=cast(str, row["evidence_ref_id"]),
        status=cast(str, row["status"]),
        resolved_by_arc_baseline_id=cast(
            str | None, row["resolved_by_arc_baseline_id"]
        ),
        close_reason_code=cast(str | None, row["close_reason_code"]),
        created_at_ms=cast(int, row["created_at_ms"]),
        closed_at_ms=cast(int | None, row["closed_at_ms"]),
    )


def _chapter_book_record(row: RowMapping) -> ChapterBookChangeRequestRecord:
    return ChapterBookChangeRequestRecord(
        id=cast(str, row["id"]),
        project_id=cast(str, row["project_id"]),
        book_id=cast(str, row["book_id"]),
        arc_id=cast(str, row["arc_id"]),
        chapter_id=cast(str, row["chapter_id"]),
        source_submission_id=cast(str, row["source_submission_id"]),
        source_review_id=cast(str, row["source_review_id"]),
        target_book_baseline_id=cast(str, row["target_book_baseline_id"]),
        evidence_ref_id=cast(str, row["evidence_ref_id"]),
        status=cast(str, row["status"]),
        resolved_by_book_baseline_id=cast(
            str | None, row["resolved_by_book_baseline_id"]
        ),
        close_reason_code=cast(str | None, row["close_reason_code"]),
        created_at_ms=cast(int, row["created_at_ms"]),
        closed_at_ms=cast(int | None, row["closed_at_ms"]),
    )


def _arc_book_record(row: RowMapping) -> ArcBookChangeRequestRecord:
    return ArcBookChangeRequestRecord(
        id=cast(str, row["id"]),
        project_id=cast(str, row["project_id"]),
        book_id=cast(str, row["book_id"]),
        arc_id=cast(str, row["arc_id"]),
        source_submission_id=cast(str, row["source_submission_id"]),
        source_review_id=cast(str, row["source_review_id"]),
        target_book_baseline_id=cast(str, row["target_book_baseline_id"]),
        evidence_ref_id=cast(str, row["evidence_ref_id"]),
        status=cast(str, row["status"]),
        resolved_by_book_baseline_id=cast(
            str | None, row["resolved_by_book_baseline_id"]
        ),
        close_reason_code=cast(str | None, row["close_reason_code"]),
        created_at_ms=cast(int, row["created_at_ms"]),
        closed_at_ms=cast(int | None, row["closed_at_ms"]),
    )


class ChangeRequestRepository:
    def __init__(self, connection: AsyncConnection) -> None:
        self._connection = connection

    async def list_open(self, *, project_id: str) -> list[OpenChangeRequestRecord]:
        records: list[OpenChangeRequestRecord] = []
        chapter_arc_rows = (
            await self._connection.execute(
                select(chapter_arc_change_requests).where(
                    chapter_arc_change_requests.c.project_id == project_id,
                    chapter_arc_change_requests.c.status == "open",
                )
            )
        ).mappings()
        records.extend(
            OpenChangeRequestRecord(
                request_kind="chapter_to_arc",
                id=cast(str, row["id"]),
                project_id=cast(str, row["project_id"]),
                target_id=cast(str, row["arc_id"]),
                target_baseline_id=cast(str, row["target_arc_baseline_id"]),
                evidence_ref_id=cast(str, row["evidence_ref_id"]),
                created_at_ms=cast(int, row["created_at_ms"]),
            )
            for row in chapter_arc_rows
        )
        chapter_book_rows = (
            await self._connection.execute(
                select(chapter_book_change_requests).where(
                    chapter_book_change_requests.c.project_id == project_id,
                    chapter_book_change_requests.c.status == "open",
                )
            )
        ).mappings()
        records.extend(
            OpenChangeRequestRecord(
                request_kind="chapter_to_book",
                id=cast(str, row["id"]),
                project_id=cast(str, row["project_id"]),
                target_id=cast(str, row["book_id"]),
                target_baseline_id=cast(str, row["target_book_baseline_id"]),
                evidence_ref_id=cast(str, row["evidence_ref_id"]),
                created_at_ms=cast(int, row["created_at_ms"]),
            )
            for row in chapter_book_rows
        )
        arc_book_rows = (
            await self._connection.execute(
                select(arc_book_change_requests).where(
                    arc_book_change_requests.c.project_id == project_id,
                    arc_book_change_requests.c.status == "open",
                )
            )
        ).mappings()
        records.extend(
            OpenChangeRequestRecord(
                request_kind="arc_to_book",
                id=cast(str, row["id"]),
                project_id=cast(str, row["project_id"]),
                target_id=cast(str, row["book_id"]),
                target_baseline_id=cast(str, row["target_book_baseline_id"]),
                evidence_ref_id=cast(str, row["evidence_ref_id"]),
                created_at_ms=cast(int, row["created_at_ms"]),
            )
            for row in arc_book_rows
        )
        return sorted(records, key=lambda item: (item.created_at_ms, item.id))

    async def get_chapter_arc(
        self, *, project_id: str, request_id: str
    ) -> ChapterArcChangeRequestRecord | None:
        row = (
            await self._connection.execute(
                select(chapter_arc_change_requests).where(
                    chapter_arc_change_requests.c.project_id == project_id,
                    chapter_arc_change_requests.c.id == request_id,
                )
            )
        ).mappings().one_or_none()
        return None if row is None else _chapter_arc_record(row)

    async def get_chapter_book(
        self, *, project_id: str, request_id: str
    ) -> ChapterBookChangeRequestRecord | None:
        row = (
            await self._connection.execute(
                select(chapter_book_change_requests).where(
                    chapter_book_change_requests.c.project_id == project_id,
                    chapter_book_change_requests.c.id == request_id,
                )
            )
        ).mappings().one_or_none()
        return None if row is None else _chapter_book_record(row)

    async def get_arc_book(
        self, *, project_id: str, request_id: str
    ) -> ArcBookChangeRequestRecord | None:
        row = (
            await self._connection.execute(
                select(arc_book_change_requests).where(
                    arc_book_change_requests.c.project_id == project_id,
                    arc_book_change_requests.c.id == request_id,
                )
            )
        ).mappings().one_or_none()
        return None if row is None else _arc_book_record(row)

    async def reject_chapter_arc(
        self, *, project_id: str, request_id: str, reason: str, now_ms: int
    ) -> bool:
        return await self._reject(
            chapter_arc_change_requests,
            project_id=project_id,
            request_id=request_id,
            reason=reason,
            now_ms=now_ms,
        )

    async def reject_chapter_book(
        self, *, project_id: str, request_id: str, reason: str, now_ms: int
    ) -> bool:
        return await self._reject(
            chapter_book_change_requests,
            project_id=project_id,
            request_id=request_id,
            reason=reason,
            now_ms=now_ms,
        )

    async def reject_arc_book(
        self, *, project_id: str, request_id: str, reason: str, now_ms: int
    ) -> bool:
        return await self._reject(
            arc_book_change_requests,
            project_id=project_id,
            request_id=request_id,
            reason=reason,
            now_ms=now_ms,
        )

    async def _reject(
        self,
        table: Table,
        *,
        project_id: str,
        request_id: str,
        reason: str,
        now_ms: int,
    ) -> bool:
        result = await self._connection.execute(
            update(table)
            .where(
                table.c.project_id == project_id,
                table.c.id == request_id,
                table.c.status == "open",
            )
            .values(
                status="rejected",
                close_reason_code=reason,
                closed_at_ms=now_ms,
            )
        )
        return result.rowcount == 1

    async def resolve_for_arc_baseline(
        self,
        *,
        project_id: str,
        book_id: str,
        arc_id: str,
        previous_baseline_id: str | None,
        new_baseline_id: str,
        now_ms: int,
    ) -> int:
        if previous_baseline_id is None:
            return 0
        result = await self._connection.execute(
            update(chapter_arc_change_requests)
            .where(
                chapter_arc_change_requests.c.project_id == project_id,
                chapter_arc_change_requests.c.book_id == book_id,
                chapter_arc_change_requests.c.arc_id == arc_id,
                chapter_arc_change_requests.c.target_arc_baseline_id
                == previous_baseline_id,
                chapter_arc_change_requests.c.status == "open",
            )
            .values(
                status="resolved",
                resolved_by_arc_baseline_id=new_baseline_id,
                close_reason_code="arc_baseline_committed",
                closed_at_ms=now_ms,
            )
        )
        await self._stale_chapter_work_for_arc(
            project_id=project_id,
            arc_id=arc_id,
            previous_baseline_id=previous_baseline_id,
            now_ms=now_ms,
        )
        return result.rowcount

    async def resolve_for_book_baseline(
        self,
        *,
        project_id: str,
        book_id: str,
        previous_baseline_id: str | None,
        new_baseline_id: str,
        now_ms: int,
    ) -> tuple[int, int]:
        if previous_baseline_id is None:
            return 0, 0
        chapter_result = await self._connection.execute(
            update(chapter_book_change_requests)
            .where(
                chapter_book_change_requests.c.project_id == project_id,
                chapter_book_change_requests.c.book_id == book_id,
                chapter_book_change_requests.c.target_book_baseline_id
                == previous_baseline_id,
                chapter_book_change_requests.c.status == "open",
            )
            .values(
                status="resolved",
                resolved_by_book_baseline_id=new_baseline_id,
                close_reason_code="book_baseline_committed",
                closed_at_ms=now_ms,
            )
        )
        arc_result = await self._connection.execute(
            update(arc_book_change_requests)
            .where(
                arc_book_change_requests.c.project_id == project_id,
                arc_book_change_requests.c.book_id == book_id,
                arc_book_change_requests.c.target_book_baseline_id
                == previous_baseline_id,
                arc_book_change_requests.c.status == "open",
            )
            .values(
                status="resolved",
                resolved_by_book_baseline_id=new_baseline_id,
                close_reason_code="book_baseline_committed",
                closed_at_ms=now_ms,
            )
        )
        await self._stale_arc_work_for_book(
            project_id=project_id,
            book_id=book_id,
            previous_baseline_id=previous_baseline_id,
            now_ms=now_ms,
        )
        await self._stale_chapter_work_for_book(
            project_id=project_id,
            book_id=book_id,
            previous_baseline_id=previous_baseline_id,
            now_ms=now_ms,
        )
        return chapter_result.rowcount, arc_result.rowcount

    async def _stale_chapter_work_for_arc(
        self,
        *,
        project_id: str,
        arc_id: str,
        previous_baseline_id: str,
        now_ms: int,
    ) -> None:
        affected = select(chapter_workspaces.c.chapter_id).where(
            chapter_workspaces.c.project_id == project_id,
            chapter_workspaces.c.arc_id == arc_id,
            chapter_workspaces.c.arc_baseline_id == previous_baseline_id,
            chapter_workspaces.c.state.in_(
                ("active", "blocked_by_user", "blocked_by_upstream")
            ),
        )
        await self._connection.execute(
            update(chapter_review_submissions)
            .where(
                chapter_review_submissions.c.project_id == project_id,
                chapter_review_submissions.c.chapter_id.in_(affected),
                chapter_review_submissions.c.disposition == "pending",
            )
            .values(
                disposition="superseded",
                close_reason_code="upstream_arc_revised",
                closed_at_ms=now_ms,
            )
        )
        await self._connection.execute(
            update(chapter_workspaces)
            .where(
                chapter_workspaces.c.project_id == project_id,
                chapter_workspaces.c.arc_id == arc_id,
                chapter_workspaces.c.arc_baseline_id == previous_baseline_id,
                chapter_workspaces.c.state.in_(
                    ("active", "blocked_by_user", "blocked_by_upstream")
                ),
            )
            .values(
                state="stale",
                lock_version=chapter_workspaces.c.lock_version + 1,
                stale_reason_code="upstream_arc_revised",
                stale_at_ms=now_ms,
                updated_at_ms=now_ms,
            )
        )

    async def _stale_arc_work_for_book(
        self,
        *,
        project_id: str,
        book_id: str,
        previous_baseline_id: str,
        now_ms: int,
    ) -> None:
        affected = select(arc_workspaces.c.arc_id).where(
            arc_workspaces.c.project_id == project_id,
            arc_workspaces.c.book_id == book_id,
            arc_workspaces.c.book_baseline_id == previous_baseline_id,
            arc_workspaces.c.arc_id.in_(
                select(story_arcs.c.id).where(
                    story_arcs.c.project_id == project_id,
                    story_arcs.c.book_id == book_id,
                    story_arcs.c.lifecycle_status.in_(("planning", "active")),
                )
            ),
        )
        await self._connection.execute(
            update(arc_approval_gates)
            .where(
                arc_approval_gates.c.project_id == project_id,
                arc_approval_gates.c.arc_id.in_(affected),
                arc_approval_gates.c.state == "pending",
            )
            .values(state="superseded", closed_at_ms=now_ms)
        )
        await self._connection.execute(
            update(arc_review_submissions)
            .where(
                arc_review_submissions.c.project_id == project_id,
                arc_review_submissions.c.arc_id.in_(affected),
                arc_review_submissions.c.disposition == "pending",
            )
            .values(
                disposition="superseded",
                close_reason_code="upstream_book_revised",
                closed_at_ms=now_ms,
            )
        )
        await self._connection.execute(
            update(arc_workspaces)
            .where(
                arc_workspaces.c.project_id == project_id,
                arc_workspaces.c.book_id == book_id,
                arc_workspaces.c.book_baseline_id == previous_baseline_id,
                arc_workspaces.c.arc_id.in_(
                    select(story_arcs.c.id).where(
                        story_arcs.c.project_id == project_id,
                        story_arcs.c.book_id == book_id,
                        story_arcs.c.lifecycle_status.in_(("planning", "active")),
                    )
                ),
            )
            .values(
                state="stale",
                lock_version=arc_workspaces.c.lock_version + 1,
                stale_reason_code="upstream_book_revised",
                stale_at_ms=now_ms,
                updated_at_ms=now_ms,
            )
        )

    async def _stale_chapter_work_for_book(
        self,
        *,
        project_id: str,
        book_id: str,
        previous_baseline_id: str,
        now_ms: int,
    ) -> None:
        affected = select(chapter_workspaces.c.chapter_id).where(
            chapter_workspaces.c.project_id == project_id,
            chapter_workspaces.c.book_id == book_id,
            chapter_workspaces.c.book_baseline_id == previous_baseline_id,
            chapter_workspaces.c.state.in_(
                ("active", "blocked_by_user", "blocked_by_upstream")
            ),
        )
        await self._connection.execute(
            update(chapter_review_submissions)
            .where(
                chapter_review_submissions.c.project_id == project_id,
                chapter_review_submissions.c.chapter_id.in_(affected),
                chapter_review_submissions.c.disposition == "pending",
            )
            .values(
                disposition="superseded",
                close_reason_code="upstream_book_revised",
                closed_at_ms=now_ms,
            )
        )
        await self._connection.execute(
            update(chapter_workspaces)
            .where(
                chapter_workspaces.c.project_id == project_id,
                chapter_workspaces.c.book_id == book_id,
                chapter_workspaces.c.book_baseline_id == previous_baseline_id,
                chapter_workspaces.c.state.in_(
                    ("active", "blocked_by_user", "blocked_by_upstream")
                ),
            )
            .values(
                state="stale",
                lock_version=chapter_workspaces.c.lock_version + 1,
                stale_reason_code="upstream_book_revised",
                stale_at_ms=now_ms,
                updated_at_ms=now_ms,
            )
        )

    async def has_open(self, *, project_id: str) -> bool:
        for table in (
            chapter_arc_change_requests,
            chapter_book_change_requests,
            arc_book_change_requests,
        ):
            value = await self._connection.scalar(
                select(table.c.id)
                .where(table.c.project_id == project_id, table.c.status == "open")
                .limit(1)
            )
            if value is not None:
                return True
        return False
