from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from app.db.schema import (
    agent_tasks,
    chapter_baselines,
    chapters,
    domain_events,
    story_arcs,
)


@dataclass(frozen=True, slots=True)
class ArcSnapshotRecord:
    arc_id: str
    ordinal: int
    purpose: str
    lifecycle_status: str
    arc_baseline_id: str


@dataclass(frozen=True, slots=True)
class ChapterSnapshotRecord:
    chapter_id: str
    arc_id: str
    book_ordinal: int
    arc_ordinal: int
    chapter_baseline_id: str
    chapter_title: str
    prose_ref_id: str


@dataclass(frozen=True, slots=True)
class TaskSnapshotRecord:
    task_id: str
    status: str
    successful_attempt_id: str | None
    delivery_state: str


class SnapshotRepository:
    def __init__(self, connection: AsyncConnection) -> None:
        self._connection = connection

    async def list_arcs(
        self, *, project_id: str, book_id: str
    ) -> list[ArcSnapshotRecord]:
        rows = (
            await self._connection.execute(
                select(
                    story_arcs.c.id,
                    story_arcs.c.ordinal,
                    story_arcs.c.purpose,
                    story_arcs.c.lifecycle_status,
                    story_arcs.c.current_baseline_id,
                )
                .where(
                    story_arcs.c.project_id == project_id,
                    story_arcs.c.book_id == book_id,
                    story_arcs.c.current_baseline_id.is_not(None),
                )
                .order_by(story_arcs.c.ordinal)
            )
        ).mappings().all()
        return [
            ArcSnapshotRecord(
                arc_id=cast(str, row["id"]),
                ordinal=cast(int, row["ordinal"]),
                purpose=cast(str, row["purpose"]),
                lifecycle_status=cast(str, row["lifecycle_status"]),
                arc_baseline_id=cast(str, row["current_baseline_id"]),
            )
            for row in rows
        ]

    async def list_chapters(
        self, *, project_id: str, book_id: str
    ) -> list[ChapterSnapshotRecord]:
        rows = (
            await self._connection.execute(
                select(
                    chapters.c.id,
                    chapters.c.arc_id,
                    chapters.c.book_ordinal,
                    chapters.c.arc_ordinal,
                    chapters.c.current_baseline_id,
                    chapter_baselines.c.chapter_title,
                    chapter_baselines.c.prose_ref_id,
                )
                .join(
                    chapter_baselines,
                    (chapter_baselines.c.project_id == chapters.c.project_id)
                    & (chapter_baselines.c.chapter_id == chapters.c.id)
                    & (chapter_baselines.c.id == chapters.c.current_baseline_id),
                )
                .where(
                    chapters.c.project_id == project_id,
                    chapters.c.book_id == book_id,
                    chapters.c.lifecycle_status == "committed",
                )
                .order_by(chapters.c.book_ordinal)
            )
        ).mappings().all()
        return [
            ChapterSnapshotRecord(
                chapter_id=cast(str, row["id"]),
                arc_id=cast(str, row["arc_id"]),
                book_ordinal=cast(int, row["book_ordinal"]),
                arc_ordinal=cast(int, row["arc_ordinal"]),
                chapter_baseline_id=cast(str, row["current_baseline_id"]),
                chapter_title=cast(str, row["chapter_title"]),
                prose_ref_id=cast(str, row["prose_ref_id"]),
            )
            for row in rows
        ]

    async def list_tasks(self, *, project_id: str) -> list[TaskSnapshotRecord]:
        rows = (
            await self._connection.execute(
                select(
                    agent_tasks.c.id,
                    agent_tasks.c.status,
                    agent_tasks.c.successful_attempt_id,
                    agent_tasks.c.delivery_state,
                )
                .where(agent_tasks.c.project_id == project_id)
                .order_by(agent_tasks.c.created_at_ms, agent_tasks.c.id)
            )
        ).mappings().all()
        return [
            TaskSnapshotRecord(
                task_id=cast(str, row["id"]),
                status=cast(str, row["status"]),
                successful_attempt_id=cast(str | None, row["successful_attempt_id"]),
                delivery_state=cast(str, row["delivery_state"]),
            )
            for row in rows
        ]

    async def last_event_sequence(self, *, project_id: str) -> int:
        value = await self._connection.scalar(
            select(func.coalesce(func.max(domain_events.c.sequence), 0)).where(
                domain_events.c.project_id == project_id
            )
        )
        return cast(int, value)
