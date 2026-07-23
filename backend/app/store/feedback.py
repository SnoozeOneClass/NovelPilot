from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import cast

from sqlalchemy import RowMapping, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from app.db.schema import user_feedback


@dataclass(frozen=True, slots=True)
class FeedbackRecord:
    id: str
    project_id: str
    content_ref_id: str
    status: str
    route_layer: str | None
    book_id: str | None
    arc_id: str | None
    chapter_id: str | None
    applied_command_id: str | None
    created_at_ms: int
    routed_at_ms: int | None
    applied_at_ms: int | None


def _feedback_record(row: RowMapping) -> FeedbackRecord:
    return FeedbackRecord(
        id=cast(str, row["id"]),
        project_id=cast(str, row["project_id"]),
        content_ref_id=cast(str, row["content_ref_id"]),
        status=cast(str, row["status"]),
        route_layer=cast(str | None, row["route_layer"]),
        book_id=cast(str | None, row["book_id"]),
        arc_id=cast(str | None, row["arc_id"]),
        chapter_id=cast(str | None, row["chapter_id"]),
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

    async def mark_applied(
        self,
        *,
        project_id: str,
        feedback_id: str,
        command_id: str,
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
        dismissed_at_ms: int,
    ) -> bool:
        result = await self._connection.execute(
            update(user_feedback)
            .where(
                user_feedback.c.project_id == project_id,
                user_feedback.c.id == feedback_id,
                user_feedback.c.status.in_(("pending", "routed")),
            )
            .values(status="dismissed", applied_at_ms=dismissed_at_ms)
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
