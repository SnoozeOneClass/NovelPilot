from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine

from app.db.uow import UnitOfWork
from app.domain.chapter.commands import ChapterNotFoundError
from app.domain.chapter.contracts import ChapterTextView


class ChapterQueryService:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def get_current_text(
        self, *, project_id: str, chapter_id: str
    ) -> ChapterTextView:
        async with UnitOfWork(self._engine, begin_mode="DEFERRED") as session:
            chapter = await session.chapters.get(
                project_id=project_id,
                chapter_id=chapter_id,
            )
            if chapter is None or chapter.current_baseline_id is None:
                raise ChapterNotFoundError(chapter_id)
            baseline = await session.chapters.get_baseline(
                project_id=project_id,
                chapter_id=chapter_id,
                baseline_id=chapter.current_baseline_id,
            )
            if baseline is None:  # pragma: no cover - constrained current pointer.
                raise ChapterNotFoundError(chapter_id)
            prose = (
                await session.content.get_packed(
                    project_id=project_id,
                    ref_id=baseline.prose_ref_id,
                )
            ).unpack_and_verify().decode("utf-8")
            return ChapterTextView(
                project_id=project_id,
                chapter_id=chapter_id,
                chapter_title=baseline.chapter_title,
                prose=prose,
            )
