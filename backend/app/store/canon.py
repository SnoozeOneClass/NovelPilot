from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import cast

from sqlalchemy import RowMapping, func, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from app.db.schema import canon_baselines, projects


@dataclass(frozen=True, slots=True)
class CanonSeedRecord:
    id: str
    project_id: str
    characters_ref_id: str
    relationships_ref_id: str
    world_facts_ref_id: str
    foreshadowing_ref_id: str
    manifest_fingerprint: str
    created_at_ms: int


@dataclass(frozen=True, slots=True)
class CanonBaselineRecord:
    id: str
    project_id: str
    baseline_version: int
    parent_canon_baseline_id: str | None
    source_book_id: str | None
    source_arc_id: str | None
    source_chapter_id: str | None
    source_chapter_baseline_id: str | None
    accepted_patch_ref_id: str | None
    characters_ref_id: str
    relationships_ref_id: str
    world_facts_ref_id: str
    foreshadowing_ref_id: str
    manifest_fingerprint: str
    created_at_ms: int


def _baseline_record(row: RowMapping) -> CanonBaselineRecord:
    return CanonBaselineRecord(
        id=cast(str, row["id"]),
        project_id=cast(str, row["project_id"]),
        baseline_version=cast(int, row["baseline_version"]),
        parent_canon_baseline_id=cast(str | None, row["parent_canon_baseline_id"]),
        source_book_id=cast(str | None, row["source_book_id"]),
        source_arc_id=cast(str | None, row["source_arc_id"]),
        source_chapter_id=cast(str | None, row["source_chapter_id"]),
        source_chapter_baseline_id=cast(str | None, row["source_chapter_baseline_id"]),
        accepted_patch_ref_id=cast(str | None, row["accepted_patch_ref_id"]),
        characters_ref_id=cast(str, row["characters_ref_id"]),
        relationships_ref_id=cast(str, row["relationships_ref_id"]),
        world_facts_ref_id=cast(str, row["world_facts_ref_id"]),
        foreshadowing_ref_id=cast(str, row["foreshadowing_ref_id"]),
        manifest_fingerprint=cast(str, row["manifest_fingerprint"]),
        created_at_ms=cast(int, row["created_at_ms"]),
    )


class CanonRepository:
    def __init__(self, connection: AsyncConnection) -> None:
        self._connection = connection

    async def insert_seed(self, record: CanonSeedRecord) -> None:
        await self._connection.execute(
            canon_baselines.insert().values(
                id=record.id,
                project_id=record.project_id,
                baseline_version=1,
                characters_ref_id=record.characters_ref_id,
                relationships_ref_id=record.relationships_ref_id,
                world_facts_ref_id=record.world_facts_ref_id,
                foreshadowing_ref_id=record.foreshadowing_ref_id,
                manifest_fingerprint=record.manifest_fingerprint,
                created_at_ms=record.created_at_ms,
            )
        )

    async def get_baseline(
        self, *, project_id: str, baseline_id: str
    ) -> CanonBaselineRecord | None:
        row = (
            await self._connection.execute(
                select(canon_baselines).where(
                    canon_baselines.c.project_id == project_id,
                    canon_baselines.c.id == baseline_id,
                )
            )
        ).mappings().one_or_none()
        return None if row is None else _baseline_record(row)

    async def next_baseline_version(self, *, project_id: str) -> int:
        current = await self._connection.scalar(
            select(func.coalesce(func.max(canon_baselines.c.baseline_version), 0)).where(
                canon_baselines.c.project_id == project_id
            )
        )
        return cast(int, current) + 1

    async def insert_baseline(self, record: CanonBaselineRecord) -> None:
        await self._connection.execute(canon_baselines.insert().values(**asdict(record)))

    async def compare_and_set_current(
        self,
        *,
        project_id: str,
        expected_baseline_id: str,
        new_baseline_id: str,
        updated_at_ms: int,
    ) -> bool:
        result = await self._connection.execute(
            update(projects)
            .where(
                projects.c.id == project_id,
                projects.c.current_canon_baseline_id == expected_baseline_id,
            )
            .values(
                current_canon_baseline_id=new_baseline_id,
                updated_at_ms=updated_at_ms,
            )
        )
        return result.rowcount == 1
