from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import cast

from sqlalchemy import RowMapping, func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from app.db.schema import (
    arc_baselines,
    chapter_baselines,
    chapter_review_submissions,
    chapter_reviews,
    chapter_workspaces,
    chapter_arc_change_requests,
    chapter_book_change_requests,
    chapters,
    projects,
    story_arcs,
)


@dataclass(frozen=True, slots=True)
class ActiveArcContext:
    project_id: str
    book_id: str
    arc_id: str
    arc_baseline_id: str
    book_baseline_id: str
    canon_baseline_id: str
    target_chapter_count: int


@dataclass(frozen=True, slots=True)
class ChapterRecord:
    id: str
    project_id: str
    book_id: str
    arc_id: str
    book_ordinal: int
    arc_ordinal: int
    lifecycle_status: str
    current_baseline_id: str | None
    created_at_ms: int
    updated_at_ms: int
    committed_at_ms: int | None


@dataclass(frozen=True, slots=True)
class ChapterWorkspaceRecord:
    id: str
    project_id: str
    book_id: str
    arc_id: str
    chapter_id: str
    state: str
    lock_version: int
    base_chapter_baseline_id: str | None
    book_baseline_id: str
    arc_baseline_id: str
    canon_baseline_id: str
    plan_ref_id: str | None
    draft_ref_id: str | None
    observations_ref_id: str | None
    candidate_canon_patch_ref_id: str | None
    repair_policy_id: str
    semantic_repair_count: int
    semantic_repair_limit: int
    stale_reason_code: str | None
    stale_at_ms: int | None
    created_at_ms: int
    updated_at_ms: int
    guidance_ref_id: str | None = None


@dataclass(frozen=True, slots=True)
class ChapterSubmissionRecord:
    id: str
    project_id: str
    book_id: str
    arc_id: str
    chapter_id: str
    workspace_id: str
    workspace_lock_version: int
    base_chapter_baseline_id: str | None
    book_baseline_id: str
    arc_baseline_id: str
    canon_before_id: str
    plan_ref_id: str
    draft_ref_id: str
    observations_ref_id: str
    candidate_canon_patch_ref_id: str
    content_manifest_ref_id: str
    content_fingerprint: str
    disposition: str
    close_reason_code: str | None
    created_at_ms: int
    closed_at_ms: int | None


@dataclass(frozen=True, slots=True)
class ChapterReviewRecord:
    id: str
    project_id: str
    book_id: str
    arc_id: str
    chapter_id: str
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
class ChapterBaselineRecord:
    id: str
    project_id: str
    book_id: str
    arc_id: str
    chapter_id: str
    baseline_version: int
    parent_baseline_id: str | None
    submission_id: str
    review_id: str
    book_baseline_id: str
    arc_baseline_id: str
    canon_before_id: str
    canon_after_id: str
    plan_ref_id: str
    prose_ref_id: str
    observations_ref_id: str
    accepted_canon_patch_ref_id: str
    chapter_title: str
    character_count: int
    created_at_ms: int


@dataclass(frozen=True, slots=True)
class ChapterChangeRequestRecord:
    id: str
    project_id: str
    book_id: str
    arc_id: str
    chapter_id: str
    source_submission_id: str
    source_review_id: str
    target_baseline_id: str
    evidence_ref_id: str
    status: str
    created_at_ms: int


def _chapter_record(row: RowMapping) -> ChapterRecord:
    return ChapterRecord(
        id=cast(str, row["id"]),
        project_id=cast(str, row["project_id"]),
        book_id=cast(str, row["book_id"]),
        arc_id=cast(str, row["arc_id"]),
        book_ordinal=cast(int, row["book_ordinal"]),
        arc_ordinal=cast(int, row["arc_ordinal"]),
        lifecycle_status=cast(str, row["lifecycle_status"]),
        current_baseline_id=cast(str | None, row["current_baseline_id"]),
        created_at_ms=cast(int, row["created_at_ms"]),
        updated_at_ms=cast(int, row["updated_at_ms"]),
        committed_at_ms=cast(int | None, row["committed_at_ms"]),
    )


def _workspace_record(row: RowMapping) -> ChapterWorkspaceRecord:
    return ChapterWorkspaceRecord(
        id=cast(str, row["id"]),
        project_id=cast(str, row["project_id"]),
        book_id=cast(str, row["book_id"]),
        arc_id=cast(str, row["arc_id"]),
        chapter_id=cast(str, row["chapter_id"]),
        state=cast(str, row["state"]),
        lock_version=cast(int, row["lock_version"]),
        base_chapter_baseline_id=cast(str | None, row["base_chapter_baseline_id"]),
        book_baseline_id=cast(str, row["book_baseline_id"]),
        arc_baseline_id=cast(str, row["arc_baseline_id"]),
        canon_baseline_id=cast(str, row["canon_baseline_id"]),
        plan_ref_id=cast(str | None, row["plan_ref_id"]),
        draft_ref_id=cast(str | None, row["draft_ref_id"]),
        observations_ref_id=cast(str | None, row["observations_ref_id"]),
        candidate_canon_patch_ref_id=cast(
            str | None, row["candidate_canon_patch_ref_id"]
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


def _submission_record(row: RowMapping) -> ChapterSubmissionRecord:
    return ChapterSubmissionRecord(
        id=cast(str, row["id"]),
        project_id=cast(str, row["project_id"]),
        book_id=cast(str, row["book_id"]),
        arc_id=cast(str, row["arc_id"]),
        chapter_id=cast(str, row["chapter_id"]),
        workspace_id=cast(str, row["workspace_id"]),
        workspace_lock_version=cast(int, row["workspace_lock_version"]),
        base_chapter_baseline_id=cast(str | None, row["base_chapter_baseline_id"]),
        book_baseline_id=cast(str, row["book_baseline_id"]),
        arc_baseline_id=cast(str, row["arc_baseline_id"]),
        canon_before_id=cast(str, row["canon_before_id"]),
        plan_ref_id=cast(str, row["plan_ref_id"]),
        draft_ref_id=cast(str, row["draft_ref_id"]),
        observations_ref_id=cast(str, row["observations_ref_id"]),
        candidate_canon_patch_ref_id=cast(str, row["candidate_canon_patch_ref_id"]),
        content_manifest_ref_id=cast(str, row["content_manifest_ref_id"]),
        content_fingerprint=cast(str, row["content_fingerprint"]),
        disposition=cast(str, row["disposition"]),
        close_reason_code=cast(str | None, row["close_reason_code"]),
        created_at_ms=cast(int, row["created_at_ms"]),
        closed_at_ms=cast(int | None, row["closed_at_ms"]),
    )


def _review_record(row: RowMapping) -> ChapterReviewRecord:
    return ChapterReviewRecord(
        id=cast(str, row["id"]),
        project_id=cast(str, row["project_id"]),
        book_id=cast(str, row["book_id"]),
        arc_id=cast(str, row["arc_id"]),
        chapter_id=cast(str, row["chapter_id"]),
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


def _baseline_record(row: RowMapping) -> ChapterBaselineRecord:
    return ChapterBaselineRecord(
        id=cast(str, row["id"]),
        project_id=cast(str, row["project_id"]),
        book_id=cast(str, row["book_id"]),
        arc_id=cast(str, row["arc_id"]),
        chapter_id=cast(str, row["chapter_id"]),
        baseline_version=cast(int, row["baseline_version"]),
        parent_baseline_id=cast(str | None, row["parent_baseline_id"]),
        submission_id=cast(str, row["submission_id"]),
        review_id=cast(str, row["review_id"]),
        book_baseline_id=cast(str, row["book_baseline_id"]),
        arc_baseline_id=cast(str, row["arc_baseline_id"]),
        canon_before_id=cast(str, row["canon_before_id"]),
        canon_after_id=cast(str, row["canon_after_id"]),
        plan_ref_id=cast(str, row["plan_ref_id"]),
        prose_ref_id=cast(str, row["prose_ref_id"]),
        observations_ref_id=cast(str, row["observations_ref_id"]),
        accepted_canon_patch_ref_id=cast(str, row["accepted_canon_patch_ref_id"]),
        chapter_title=cast(str, row["chapter_title"]),
        character_count=cast(int, row["character_count"]),
        created_at_ms=cast(int, row["created_at_ms"]),
    )


class ChapterRepository:
    def __init__(self, connection: AsyncConnection) -> None:
        self._connection = connection

    async def get_active_arc_context(
        self,
        *,
        project_id: str,
        book_id: str,
        arc_id: str,
        allow_completed: bool = False,
    ) -> ActiveArcContext | None:
        allowed_statuses = ("active", "completed") if allow_completed else ("active",)
        row = (
            await self._connection.execute(
                select(
                    story_arcs.c.project_id,
                    story_arcs.c.book_id,
                    story_arcs.c.id.label("arc_id"),
                    story_arcs.c.current_baseline_id.label("arc_baseline_id"),
                    arc_baselines.c.book_baseline_id,
                    projects.c.current_canon_baseline_id.label("canon_baseline_id"),
                    arc_baselines.c.target_chapter_count,
                )
                .join(
                    arc_baselines,
                    (arc_baselines.c.project_id == story_arcs.c.project_id)
                    & (arc_baselines.c.book_id == story_arcs.c.book_id)
                    & (arc_baselines.c.arc_id == story_arcs.c.id)
                    & (arc_baselines.c.id == story_arcs.c.current_baseline_id),
                )
                .join(projects, projects.c.id == story_arcs.c.project_id)
                .where(
                    story_arcs.c.project_id == project_id,
                    story_arcs.c.book_id == book_id,
                    story_arcs.c.id == arc_id,
                    story_arcs.c.lifecycle_status.in_(allowed_statuses),
                )
            )
        ).mappings().one_or_none()
        if row is None:
            return None
        return ActiveArcContext(
            project_id=cast(str, row["project_id"]),
            book_id=cast(str, row["book_id"]),
            arc_id=cast(str, row["arc_id"]),
            arc_baseline_id=cast(str, row["arc_baseline_id"]),
            book_baseline_id=cast(str, row["book_baseline_id"]),
            canon_baseline_id=cast(str, row["canon_baseline_id"]),
            target_chapter_count=cast(int, row["target_chapter_count"]),
        )

    async def next_ordinals(self, *, book_id: str, arc_id: str) -> tuple[int, int]:
        book_ordinal = await self._connection.scalar(
            select(func.coalesce(func.max(chapters.c.book_ordinal), 0)).where(
                chapters.c.book_id == book_id
            )
        )
        arc_ordinal = await self._connection.scalar(
            select(func.coalesce(func.max(chapters.c.arc_ordinal), 0)).where(
                chapters.c.arc_id == arc_id
            )
        )
        return cast(int, book_ordinal) + 1, cast(int, arc_ordinal) + 1

    async def count_committed(self, *, arc_id: str) -> int:
        value = await self._connection.scalar(
            select(func.count())
            .select_from(chapters)
            .where(chapters.c.arc_id == arc_id, chapters.c.lifecycle_status == "committed")
        )
        return cast(int, value)

    async def count_committed_for_book(self, *, book_id: str) -> int:
        value = await self._connection.scalar(
            select(func.count())
            .select_from(chapters)
            .where(
                chapters.c.book_id == book_id,
                chapters.c.lifecycle_status == "committed",
            )
        )
        return cast(int, value)

    async def insert(self, record: ChapterRecord) -> None:
        await self._connection.execute(chapters.insert().values(**asdict(record)))

    async def get(self, *, project_id: str, chapter_id: str) -> ChapterRecord | None:
        row = (
            await self._connection.execute(
                select(chapters).where(
                    chapters.c.project_id == project_id,
                    chapters.c.id == chapter_id,
                )
            )
        ).mappings().one_or_none()
        return None if row is None else _chapter_record(row)

    async def get_unfinished_for_arc(
        self, *, project_id: str, arc_id: str
    ) -> ChapterRecord | None:
        row = (
            await self._connection.execute(
                select(chapters)
                .where(
                    chapters.c.project_id == project_id,
                    chapters.c.arc_id == arc_id,
                    chapters.c.lifecycle_status == "drafting",
                )
                .order_by(chapters.c.arc_ordinal)
                .limit(1)
            )
        ).mappings().one_or_none()
        return None if row is None else _chapter_record(row)

    async def get_latest_for_arc(
        self, *, project_id: str, arc_id: str
    ) -> ChapterRecord | None:
        row = (
            await self._connection.execute(
                select(chapters)
                .where(
                    chapters.c.project_id == project_id,
                    chapters.c.arc_id == arc_id,
                )
                .order_by(chapters.c.arc_ordinal.desc())
                .limit(1)
            )
        ).mappings().one_or_none()
        return None if row is None else _chapter_record(row)

    async def get_non_idle_workspace_for_arc(
        self, *, project_id: str, arc_id: str
    ) -> tuple[ChapterRecord, ChapterWorkspaceRecord] | None:
        row = (
            await self._connection.execute(
                select(chapters)
                .join(
                    chapter_workspaces,
                    (chapter_workspaces.c.project_id == chapters.c.project_id)
                    & (chapter_workspaces.c.chapter_id == chapters.c.id),
                )
                .where(
                    chapters.c.project_id == project_id,
                    chapters.c.arc_id == arc_id,
                    chapter_workspaces.c.state != "idle",
                )
                .order_by(chapters.c.arc_ordinal.desc())
                .limit(1)
            )
        ).mappings().one_or_none()
        if row is None:
            return None
        chapter = _chapter_record(row)
        workspace = await self.get_workspace(
            project_id=project_id,
            chapter_id=chapter.id,
        )
        if workspace is None:  # pragma: no cover - the join proved it exists.
            return None
        return chapter, workspace

    async def list_committed_baselines(
        self, *, project_id: str, book_id: str
    ) -> list[ChapterBaselineRecord]:
        rows = (
            await self._connection.execute(
                select(chapter_baselines)
                .join(
                    chapters,
                    (chapters.c.project_id == chapter_baselines.c.project_id)
                    & (chapters.c.id == chapter_baselines.c.chapter_id)
                    & (chapters.c.current_baseline_id == chapter_baselines.c.id),
                )
                .where(
                    chapter_baselines.c.project_id == project_id,
                    chapter_baselines.c.book_id == book_id,
                    chapters.c.lifecycle_status == "committed",
                )
                .order_by(chapters.c.book_ordinal)
            )
        ).mappings()
        return [_baseline_record(row) for row in rows]

    async def insert_workspace(self, record: ChapterWorkspaceRecord) -> None:
        await self._connection.execute(chapter_workspaces.insert().values(**asdict(record)))

    async def get_workspace(
        self, *, project_id: str, chapter_id: str
    ) -> ChapterWorkspaceRecord | None:
        row = (
            await self._connection.execute(
                select(chapter_workspaces).where(
                    chapter_workspaces.c.project_id == project_id,
                    chapter_workspaces.c.chapter_id == chapter_id,
                )
            )
        ).mappings().one_or_none()
        return None if row is None else _workspace_record(row)

    async def compare_and_set_workspace(
        self,
        *,
        record: ChapterWorkspaceRecord,
        expected_lock_version: int,
    ) -> bool:
        values = asdict(record)
        for key in ("id", "project_id", "book_id", "arc_id", "chapter_id"):
            values.pop(key)
        result = await self._connection.execute(
            update(chapter_workspaces)
            .where(
                chapter_workspaces.c.id == record.id,
                chapter_workspaces.c.project_id == record.project_id,
                chapter_workspaces.c.chapter_id == record.chapter_id,
                chapter_workspaces.c.lock_version == expected_lock_version,
            )
            .values(**values)
        )
        return result.rowcount == 1

    async def find_pending_submission(
        self, *, project_id: str, chapter_id: str
    ) -> ChapterSubmissionRecord | None:
        row = (
            await self._connection.execute(
                select(chapter_review_submissions).where(
                    chapter_review_submissions.c.project_id == project_id,
                    chapter_review_submissions.c.chapter_id == chapter_id,
                    chapter_review_submissions.c.disposition == "pending",
                )
            )
        ).mappings().one_or_none()
        return None if row is None else _submission_record(row)

    async def get_submission(
        self, *, project_id: str, submission_id: str
    ) -> ChapterSubmissionRecord | None:
        row = (
            await self._connection.execute(
                select(chapter_review_submissions).where(
                    chapter_review_submissions.c.project_id == project_id,
                    chapter_review_submissions.c.id == submission_id,
                )
            )
        ).mappings().one_or_none()
        return None if row is None else _submission_record(row)

    async def insert_submission(self, record: ChapterSubmissionRecord) -> None:
        await self._connection.execute(
            chapter_review_submissions.insert().values(**asdict(record))
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
            update(chapter_review_submissions)
            .where(
                chapter_review_submissions.c.project_id == project_id,
                chapter_review_submissions.c.id == submission_id,
                chapter_review_submissions.c.disposition == "pending",
            )
            .values(
                disposition=disposition,
                close_reason_code=reason_code,
                closed_at_ms=closed_at_ms,
            )
        )
        return result.rowcount == 1

    async def insert_review(self, record: ChapterReviewRecord) -> None:
        await self._connection.execute(chapter_reviews.insert().values(**asdict(record)))

    async def get_review(
        self, *, project_id: str, review_id: str
    ) -> ChapterReviewRecord | None:
        row = (
            await self._connection.execute(
                select(chapter_reviews).where(
                    chapter_reviews.c.project_id == project_id,
                    chapter_reviews.c.id == review_id,
                )
            )
        ).mappings().one_or_none()
        return None if row is None else _review_record(row)

    async def get_latest_review(
        self, *, project_id: str, chapter_id: str
    ) -> ChapterReviewRecord | None:
        row = (
            await self._connection.execute(
                select(chapter_reviews)
                .where(
                    chapter_reviews.c.project_id == project_id,
                    chapter_reviews.c.chapter_id == chapter_id,
                )
                .order_by(chapter_reviews.c.created_at_ms.desc(), chapter_reviews.c.id.desc())
                .limit(1)
            )
        ).mappings().one_or_none()
        return None if row is None else _review_record(row)

    async def next_baseline_version(self, *, chapter_id: str) -> int:
        current = await self._connection.scalar(
            select(func.coalesce(func.max(chapter_baselines.c.baseline_version), 0)).where(
                chapter_baselines.c.chapter_id == chapter_id
            )
        )
        return cast(int, current) + 1

    async def get_baseline_version(
        self, *, project_id: str, chapter_id: str, baseline_id: str
    ) -> int | None:
        return cast(
            int | None,
            await self._connection.scalar(
                select(chapter_baselines.c.baseline_version).where(
                    chapter_baselines.c.project_id == project_id,
                    chapter_baselines.c.chapter_id == chapter_id,
                    chapter_baselines.c.id == baseline_id,
                )
            ),
        )

    async def get_baseline(
        self, *, project_id: str, chapter_id: str, baseline_id: str
    ) -> ChapterBaselineRecord | None:
        row = (
            await self._connection.execute(
                select(chapter_baselines).where(
                    chapter_baselines.c.project_id == project_id,
                    chapter_baselines.c.chapter_id == chapter_id,
                    chapter_baselines.c.id == baseline_id,
                )
            )
        ).mappings().one_or_none()
        return None if row is None else _baseline_record(row)

    async def insert_baseline(self, record: ChapterBaselineRecord) -> None:
        await self._connection.execute(chapter_baselines.insert().values(**asdict(record)))

    async def commit_current_baseline(
        self,
        *,
        project_id: str,
        chapter_id: str,
        expected_baseline_id: str | None,
        new_baseline_id: str,
        committed_at_ms: int,
    ) -> bool:
        expected = (
            chapters.c.current_baseline_id.is_(None)
            if expected_baseline_id is None
            else chapters.c.current_baseline_id == expected_baseline_id
        )
        result = await self._connection.execute(
            update(chapters)
            .where(
                chapters.c.project_id == project_id,
                chapters.c.id == chapter_id,
                expected,
            )
            .values(
                lifecycle_status="committed",
                current_baseline_id=new_baseline_id,
                committed_at_ms=committed_at_ms,
                updated_at_ms=committed_at_ms,
            )
        )
        return result.rowcount == 1

    async def complete_arc_if_target_reached(
        self,
        *,
        project_id: str,
        arc_id: str,
        arc_baseline_id: str,
        committed_count: int,
        target_chapter_count: int,
        now_ms: int,
    ) -> bool:
        if committed_count != target_chapter_count:
            return False
        result = await self._connection.execute(
            update(story_arcs)
            .where(
                story_arcs.c.project_id == project_id,
                story_arcs.c.id == arc_id,
                story_arcs.c.current_baseline_id == arc_baseline_id,
                story_arcs.c.lifecycle_status == "active",
            )
            .values(
                lifecycle_status="completed",
                completed_at_ms=now_ms,
                updated_at_ms=now_ms,
            )
        )
        return result.rowcount == 1

    async def insert_arc_change_request(
        self, record: ChapterChangeRequestRecord
    ) -> None:
        await self._connection.execute(
            chapter_arc_change_requests.insert().values(
                id=record.id,
                project_id=record.project_id,
                book_id=record.book_id,
                arc_id=record.arc_id,
                chapter_id=record.chapter_id,
                source_submission_id=record.source_submission_id,
                source_review_id=record.source_review_id,
                target_arc_baseline_id=record.target_baseline_id,
                evidence_ref_id=record.evidence_ref_id,
                status=record.status,
                created_at_ms=record.created_at_ms,
            )
        )

    async def insert_book_change_request(
        self, record: ChapterChangeRequestRecord
    ) -> None:
        await self._connection.execute(
            chapter_book_change_requests.insert().values(
                id=record.id,
                project_id=record.project_id,
                book_id=record.book_id,
                arc_id=record.arc_id,
                chapter_id=record.chapter_id,
                source_submission_id=record.source_submission_id,
                source_review_id=record.source_review_id,
                target_book_baseline_id=record.target_baseline_id,
                evidence_ref_id=record.evidence_ref_id,
                status=record.status,
                created_at_ms=record.created_at_ms,
            )
        )
