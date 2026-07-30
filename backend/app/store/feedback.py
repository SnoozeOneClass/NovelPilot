from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import cast

from sqlalchemy import RowMapping, exists, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from app.db.schema import agent_tasks, user_feedback


@dataclass(frozen=True, slots=True)
class FeedbackRecord:
    id: str
    project_id: str
    content_ref_id: str
    feedback_kind: str
    status: str
    route_layer: str | None
    captured_run_id: str
    book_id: str | None
    arc_id: str | None
    chapter_id: str | None
    captured_book_baseline_id: str | None
    captured_arc_baseline_id: str | None
    captured_chapter_baseline_id: str | None
    arc_parent_review_id: str | None
    book_parent_review_id: str | None
    arc_closure_review_id: str | None
    book_completion_review_id: str | None
    resulting_correction_lineage_id: str | None
    dismiss_reason_code: str | None
    applied_command_id: str | None
    created_at_ms: int
    routed_at_ms: int | None
    applied_at_ms: int | None


def _feedback_record(row: RowMapping) -> FeedbackRecord:
    return FeedbackRecord(
        id=cast(str, row["id"]),
        project_id=cast(str, row["project_id"]),
        content_ref_id=cast(str, row["content_ref_id"]),
        feedback_kind=cast(str, row["feedback_kind"]),
        status=cast(str, row["status"]),
        route_layer=cast(str | None, row["route_layer"]),
        captured_run_id=cast(str, row["captured_run_id"]),
        book_id=cast(str | None, row["book_id"]),
        arc_id=cast(str | None, row["arc_id"]),
        chapter_id=cast(str | None, row["chapter_id"]),
        captured_book_baseline_id=cast(
            str | None, row["captured_book_baseline_id"]
        ),
        captured_arc_baseline_id=cast(
            str | None, row["captured_arc_baseline_id"]
        ),
        captured_chapter_baseline_id=cast(
            str | None, row["captured_chapter_baseline_id"]
        ),
        arc_parent_review_id=cast(str | None, row["arc_parent_review_id"]),
        book_parent_review_id=cast(str | None, row["book_parent_review_id"]),
        arc_closure_review_id=cast(str | None, row["arc_closure_review_id"]),
        book_completion_review_id=cast(
            str | None, row["book_completion_review_id"]
        ),
        resulting_correction_lineage_id=cast(
            str | None, row["resulting_correction_lineage_id"]
        ),
        dismiss_reason_code=cast(str | None, row["dismiss_reason_code"]),
        applied_command_id=cast(str | None, row["applied_command_id"]),
        created_at_ms=cast(int, row["created_at_ms"]),
        routed_at_ms=cast(int | None, row["routed_at_ms"]),
        applied_at_ms=cast(int | None, row["applied_at_ms"]),
    )


class FeedbackRepository:
    def __init__(self, connection: AsyncConnection) -> None:
        self._connection = connection

    async def insert(self, record: FeedbackRecord) -> None:
        await self._connection.execute(user_feedback.insert().values(**asdict(record)))

    async def get(self, *, project_id: str, feedback_id: str) -> FeedbackRecord | None:
        row = (
            await self._connection.execute(
                select(user_feedback).where(
                    user_feedback.c.project_id == project_id,
                    user_feedback.c.id == feedback_id,
                )
            )
        ).mappings().one_or_none()
        return None if row is None else _feedback_record(row)

    async def list_recent(
        self,
        *,
        project_id: str,
        limit: int = 50,
    ) -> list[FeedbackRecord]:
        rows = (
            await self._connection.execute(
                select(user_feedback)
                .where(user_feedback.c.project_id == project_id)
                .order_by(
                    user_feedback.c.created_at_ms.desc(),
                    user_feedback.c.id.desc(),
                )
                .limit(limit)
            )
        ).mappings()
        return [_feedback_record(row) for row in rows]

    async def route(
        self,
        *,
        project_id: str,
        feedback_id: str,
        route_layer: str,
        book_id: str,
        arc_id: str | None,
        chapter_id: str | None,
        routed_at_ms: int,
    ) -> bool:
        result = await self._connection.execute(
            update(user_feedback)
            .where(
                user_feedback.c.project_id == project_id,
                user_feedback.c.id == feedback_id,
                user_feedback.c.status == "pending",
            )
            .values(
                status="routed",
                route_layer=route_layer,
                book_id=book_id,
                arc_id=arc_id,
                chapter_id=chapter_id,
                routed_at_ms=routed_at_ms,
            )
        )
        return result.rowcount == 1

    async def get_oldest_routed(
        self, *, project_id: str
    ) -> FeedbackRecord | None:
        row = (
            await self._connection.execute(
                select(user_feedback)
                .where(
                    user_feedback.c.project_id == project_id,
                    user_feedback.c.status == "routed",
                )
                .order_by(user_feedback.c.created_at_ms, user_feedback.c.id)
                .limit(1)
            )
        ).mappings().one_or_none()
        return None if row is None else _feedback_record(row)

    async def get_unstarted_correction_lineage(
        self,
        *,
        project_id: str,
        run_id: str,
    ) -> FeedbackRecord | None:
        row = (
            await self._connection.execute(
                select(user_feedback)
                .where(
                    user_feedback.c.project_id == project_id,
                    user_feedback.c.captured_run_id == run_id,
                    user_feedback.c.feedback_kind == "correction_wait_response",
                    user_feedback.c.status == "applied",
                    user_feedback.c.resulting_correction_lineage_id.is_not(None),
                    ~exists(
                        select(agent_tasks.c.id).where(
                            agent_tasks.c.project_id == user_feedback.c.project_id,
                            agent_tasks.c.run_id == user_feedback.c.captured_run_id,
                            agent_tasks.c.source_feedback_id == user_feedback.c.id,
                        )
                    ),
                )
                .order_by(user_feedback.c.applied_at_ms, user_feedback.c.id)
                .limit(1)
            )
        ).mappings().one_or_none()
        return None if row is None else _feedback_record(row)

    async def mark_applied(
        self,
        *,
        project_id: str,
        feedback_id: str,
        command_id: str,
        resulting_correction_lineage_id: str | None,
        applied_at_ms: int,
    ) -> bool:
        result = await self._connection.execute(
            update(user_feedback)
            .where(
                user_feedback.c.project_id == project_id,
                user_feedback.c.id == feedback_id,
                user_feedback.c.status == "routed",
            )
            .values(
                status="applied",
                resulting_correction_lineage_id=resulting_correction_lineage_id,
                dismiss_reason_code=None,
                applied_command_id=command_id,
                applied_at_ms=applied_at_ms,
            )
        )
        return result.rowcount == 1

    async def dismiss(
        self,
        *,
        project_id: str,
        feedback_id: str,
        reason_code: str,
        dismissed_at_ms: int,
    ) -> bool:
        result = await self._connection.execute(
            update(user_feedback)
            .where(
                user_feedback.c.project_id == project_id,
                user_feedback.c.id == feedback_id,
                user_feedback.c.status.in_(("pending", "routed")),
            )
            .values(
                status="dismissed",
                resulting_correction_lineage_id=None,
                dismiss_reason_code=reason_code,
                applied_command_id=None,
                applied_at_ms=dismissed_at_ms,
            )
        )
        return result.rowcount == 1

    async def has_unapplied(self, *, project_id: str) -> bool:
        value = await self._connection.scalar(
            select(user_feedback.c.id)
            .where(
                user_feedback.c.project_id == project_id,
                user_feedback.c.status.in_(("pending", "routed")),
            )
            .limit(1)
        )
        return value is not None
