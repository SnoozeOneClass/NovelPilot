from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import cast

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from app.db.schema import (
    agent_tasks,
    arc_approval_gates,
    arc_review_submissions,
    arc_workspaces,
    book_completions,
    book_review_submissions,
    chapter_baselines,
    chapter_review_submissions,
    chapter_workspaces,
    chapters,
    story_arcs,
)


@dataclass(frozen=True, slots=True)
class TerminalArcRecord:
    arc_id: str
    arc_baseline_id: str
    purpose: str
    lifecycle_status: str


@dataclass(frozen=True, slots=True)
class TerminalChapterRecord:
    chapter_id: str
    chapter_baseline_id: str
    book_ordinal: int
    arc_ordinal: int


@dataclass(frozen=True, slots=True)
class BookCompletionRecord:
    id: str
    project_id: str
    book_id: str
    completion_version: int
    parent_completion_id: str | None
    book_baseline_id: str
    terminal_arc_id: str
    terminal_arc_baseline_id: str
    terminal_chapter_id: str
    terminal_chapter_baseline_id: str
    canon_baseline_id: str
    committed_chapter_count: int
    source_task_id: str
    completion_decision_ref_id: str
    gate_manifest_ref_id: str
    created_at_ms: int


class CompletionRepository:
    def __init__(self, connection: AsyncConnection) -> None:
        self._connection = connection

    async def get_terminal_arc(
        self, *, project_id: str, book_id: str
    ) -> TerminalArcRecord | None:
        row = (
            await self._connection.execute(
                select(
                    story_arcs.c.id,
                    story_arcs.c.current_baseline_id,
                    story_arcs.c.purpose,
                    story_arcs.c.lifecycle_status,
                )
                .where(
                    story_arcs.c.project_id == project_id,
                    story_arcs.c.book_id == book_id,
                )
                .order_by(story_arcs.c.ordinal.desc())
                .limit(1)
            )
        ).mappings().one_or_none()
        if row is None or row["current_baseline_id"] is None:
            return None
        return TerminalArcRecord(
            arc_id=cast(str, row["id"]),
            arc_baseline_id=cast(str, row["current_baseline_id"]),
            purpose=cast(str, row["purpose"]),
            lifecycle_status=cast(str, row["lifecycle_status"]),
        )

    async def get_terminal_chapter(
        self, *, project_id: str, book_id: str, arc_id: str
    ) -> TerminalChapterRecord | None:
        row = (
            await self._connection.execute(
                select(
                    chapters.c.id,
                    chapters.c.current_baseline_id,
                    chapters.c.book_ordinal,
                    chapters.c.arc_ordinal,
                )
                .where(
                    chapters.c.project_id == project_id,
                    chapters.c.book_id == book_id,
                    chapters.c.arc_id == arc_id,
                    chapters.c.lifecycle_status == "committed",
                )
                .order_by(chapters.c.arc_ordinal.desc())
                .limit(1)
            )
        ).mappings().one_or_none()
        if row is None or row["current_baseline_id"] is None:
            return None
        return TerminalChapterRecord(
            chapter_id=cast(str, row["id"]),
            chapter_baseline_id=cast(str, row["current_baseline_id"]),
            book_ordinal=cast(int, row["book_ordinal"]),
            arc_ordinal=cast(int, row["arc_ordinal"]),
        )

    async def count_committed_chapters(self, *, book_id: str) -> int:
        value = await self._connection.scalar(
            select(func.count())
            .select_from(chapters)
            .where(
                chapters.c.book_id == book_id,
                chapters.c.lifecycle_status == "committed",
            )
        )
        return cast(int, value)

    async def has_lifecycle_blocker(
        self,
        *,
        project_id: str,
        book_id: str,
        source_task_id: str,
    ) -> bool:
        checks = (
            select(story_arcs.c.id).where(
                story_arcs.c.project_id == project_id,
                story_arcs.c.book_id == book_id,
                story_arcs.c.lifecycle_status.in_(("planning", "active")),
            ),
            select(chapters.c.id).where(
                chapters.c.project_id == project_id,
                chapters.c.book_id == book_id,
                chapters.c.lifecycle_status == "drafting",
            ),
            select(book_review_submissions.c.id).where(
                book_review_submissions.c.project_id == project_id,
                book_review_submissions.c.book_id == book_id,
                book_review_submissions.c.disposition == "pending",
            ),
            select(arc_review_submissions.c.id).where(
                arc_review_submissions.c.project_id == project_id,
                arc_review_submissions.c.book_id == book_id,
                arc_review_submissions.c.disposition == "pending",
            ),
            select(chapter_review_submissions.c.id).where(
                chapter_review_submissions.c.project_id == project_id,
                chapter_review_submissions.c.book_id == book_id,
                chapter_review_submissions.c.disposition == "pending",
            ),
            select(arc_approval_gates.c.id).where(
                arc_approval_gates.c.project_id == project_id,
                arc_approval_gates.c.book_id == book_id,
                arc_approval_gates.c.state == "pending",
            ),
            select(arc_workspaces.c.id).where(
                arc_workspaces.c.project_id == project_id,
                arc_workspaces.c.book_id == book_id,
                arc_workspaces.c.state != "idle",
            ),
            select(chapter_workspaces.c.id).where(
                chapter_workspaces.c.project_id == project_id,
                chapter_workspaces.c.book_id == book_id,
                chapter_workspaces.c.state != "idle",
            ),
            select(agent_tasks.c.id).where(
                agent_tasks.c.project_id == project_id,
                agent_tasks.c.id != source_task_id,
                (
                    agent_tasks.c.status.in_(("queued", "running"))
                    | (
                        (agent_tasks.c.status == "succeeded")
                        & (agent_tasks.c.delivery_state == "pending")
                    )
                ),
            ),
        )
        for statement in checks:
            if await self._connection.scalar(statement.limit(1)) is not None:
                return True
        return False

    async def next_version(self, *, book_id: str) -> int:
        value = await self._connection.scalar(
            select(func.coalesce(func.max(book_completions.c.completion_version), 0)).where(
                book_completions.c.book_id == book_id
            )
        )
        return cast(int, value) + 1

    async def get_latest_identity(self, *, book_id: str) -> tuple[str, int] | None:
        row = (
            await self._connection.execute(
                select(book_completions.c.id, book_completions.c.completion_version)
                .where(book_completions.c.book_id == book_id)
                .order_by(book_completions.c.completion_version.desc())
                .limit(1)
            )
        ).one_or_none()
        if row is None:
            return None
        return cast(str, row.id), cast(int, row.completion_version)

    async def insert(self, record: BookCompletionRecord) -> None:
        await self._connection.execute(book_completions.insert().values(**asdict(record)))

    async def get_chapter_baseline_identity(
        self,
        *,
        project_id: str,
        chapter_id: str,
        baseline_id: str,
    ) -> tuple[str, str] | None:
        row = (
            await self._connection.execute(
                select(chapter_baselines.c.book_id, chapter_baselines.c.arc_id).where(
                    chapter_baselines.c.project_id == project_id,
                    chapter_baselines.c.chapter_id == chapter_id,
                    chapter_baselines.c.id == baseline_id,
                )
            )
        ).one_or_none()
        if row is None:
            return None
        return cast(str, row.book_id), cast(str, row.arc_id)
