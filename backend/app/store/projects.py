from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from sqlalchemy import RowMapping, delete, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from app.db.schema import agent_task_attempts, engine_slot, generation_runs, projects


@dataclass(frozen=True, slots=True)
class ProjectRecord:
    id: str
    operation_mode: str
    lifecycle_status: str
    settings_lock_version: int
    default_profile_id: str | None
    book_profile_id: str | None
    arc_profile_id: str | None
    chapter_profile_id: str | None
    evaluator_profile_id: str | None
    current_canon_baseline_id: str
    created_at_ms: int
    updated_at_ms: int


def _project_record(row: RowMapping) -> ProjectRecord:
    return ProjectRecord(
        id=cast(str, row["id"]),
        operation_mode=cast(str, row["operation_mode"]),
        lifecycle_status=cast(str, row["lifecycle_status"]),
        settings_lock_version=cast(int, row["settings_lock_version"]),
        default_profile_id=cast(str | None, row["default_profile_id"]),
        book_profile_id=cast(str | None, row["book_profile_id"]),
        arc_profile_id=cast(str | None, row["arc_profile_id"]),
        chapter_profile_id=cast(str | None, row["chapter_profile_id"]),
        evaluator_profile_id=cast(str | None, row["evaluator_profile_id"]),
        current_canon_baseline_id=cast(str, row["current_canon_baseline_id"]),
        created_at_ms=cast(int, row["created_at_ms"]),
        updated_at_ms=cast(int, row["updated_at_ms"]),
    )


class ProjectRepository:
    def __init__(self, connection: AsyncConnection) -> None:
        self._connection = connection

    async def get(self, project_id: str) -> ProjectRecord | None:
        row = (
            await self._connection.execute(select(projects).where(projects.c.id == project_id))
        ).mappings().one_or_none()
        return None if row is None else _project_record(row)

    async def list_all(self) -> list[ProjectRecord]:
        rows = (
            await self._connection.execute(
                select(projects).order_by(projects.c.created_at_ms, projects.c.id)
            )
        ).mappings()
        return [_project_record(row) for row in rows]

    async def insert(self, record: ProjectRecord) -> None:
        await self._connection.execute(
            projects.insert().values(
                id=record.id,
                operation_mode=record.operation_mode,
                lifecycle_status=record.lifecycle_status,
                settings_lock_version=record.settings_lock_version,
                default_profile_id=record.default_profile_id,
                book_profile_id=record.book_profile_id,
                arc_profile_id=record.arc_profile_id,
                chapter_profile_id=record.chapter_profile_id,
                evaluator_profile_id=record.evaluator_profile_id,
                current_canon_baseline_id=record.current_canon_baseline_id,
                created_at_ms=record.created_at_ms,
                updated_at_ms=record.updated_at_ms,
            )
        )

    async def compare_and_set_settings(
        self,
        *,
        project_id: str,
        expected_lock_version: int,
        operation_mode: str,
        default_profile_id: str | None,
        book_profile_id: str | None,
        arc_profile_id: str | None,
        chapter_profile_id: str | None,
        evaluator_profile_id: str | None,
        updated_at_ms: int,
    ) -> bool:
        result = await self._connection.execute(
            update(projects)
            .where(
                projects.c.id == project_id,
                projects.c.settings_lock_version == expected_lock_version,
            )
            .values(
                operation_mode=operation_mode,
                default_profile_id=default_profile_id,
                book_profile_id=book_profile_id,
                arc_profile_id=arc_profile_id,
                chapter_profile_id=chapter_profile_id,
                evaluator_profile_id=evaluator_profile_id,
                settings_lock_version=expected_lock_version + 1,
                updated_at_ms=updated_at_ms,
            )
        )
        return result.rowcount == 1

    async def has_active_execution(self, project_id: str) -> bool:
        running_attempt = await self._connection.scalar(
            select(agent_task_attempts.c.id)
            .where(
                agent_task_attempts.c.project_id == project_id,
                agent_task_attempts.c.status == "running",
            )
            .limit(1)
        )
        if running_attempt is not None:
            return True
        claimed_slot = await self._connection.scalar(
            select(engine_slot.c.slot_id)
            .join(generation_runs, generation_runs.c.id == engine_slot.c.active_run_id)
            .where(generation_runs.c.project_id == project_id)
            .limit(1)
        )
        return claimed_slot is not None

    async def delete_root(self, project_id: str) -> bool:
        result = await self._connection.execute(delete(projects).where(projects.c.id == project_id))
        return result.rowcount == 1

    async def set_lifecycle_status(
        self,
        *,
        project_id: str,
        expected_status: str,
        new_status: str,
        updated_at_ms: int,
    ) -> bool:
        result = await self._connection.execute(
            update(projects)
            .where(
                projects.c.id == project_id,
                projects.c.lifecycle_status == expected_status,
            )
            .values(lifecycle_status=new_status, updated_at_ms=updated_at_ms)
        )
        return result.rowcount == 1
