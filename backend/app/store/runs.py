from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import cast

from sqlalchemy import RowMapping, func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from app.db.schema import engine_slot, generation_runs


@dataclass(frozen=True, slots=True)
class GenerationRunRecord:
    id: str
    project_id: str
    run_number: int
    status: str
    desired_state: str
    lock_version: int
    wait_reason_code: str | None
    created_at_ms: int
    updated_at_ms: int
    blocking_task_id: str | None = None
    failure_code: str | None = None
    failure_ref_id: str | None = None
    started_at_ms: int | None = None
    finished_at_ms: int | None = None


@dataclass(frozen=True, slots=True)
class EngineSlotRecord:
    slot_id: int
    active_run_id: str | None
    owner_instance_id: str | None
    lease_token: str | None
    lease_expires_at_ms: int | None
    heartbeat_at_ms: int | None
    lock_version: int


def _run_record(row: RowMapping) -> GenerationRunRecord:
    return GenerationRunRecord(
        id=cast(str, row["id"]),
        project_id=cast(str, row["project_id"]),
        run_number=cast(int, row["run_number"]),
        status=cast(str, row["status"]),
        desired_state=cast(str, row["desired_state"]),
        lock_version=cast(int, row["lock_version"]),
        wait_reason_code=cast(str | None, row["wait_reason_code"]),
        blocking_task_id=cast(str | None, row["blocking_task_id"]),
        failure_code=cast(str | None, row["failure_code"]),
        failure_ref_id=cast(str | None, row["failure_ref_id"]),
        created_at_ms=cast(int, row["created_at_ms"]),
        started_at_ms=cast(int | None, row["started_at_ms"]),
        updated_at_ms=cast(int, row["updated_at_ms"]),
        finished_at_ms=cast(int | None, row["finished_at_ms"]),
    )


def _slot_record(row: RowMapping) -> EngineSlotRecord:
    return EngineSlotRecord(
        slot_id=cast(int, row["slot_id"]),
        active_run_id=cast(str | None, row["active_run_id"]),
        owner_instance_id=cast(str | None, row["owner_instance_id"]),
        lease_token=cast(str | None, row["lease_token"]),
        lease_expires_at_ms=cast(int | None, row["lease_expires_at_ms"]),
        heartbeat_at_ms=cast(int | None, row["heartbeat_at_ms"]),
        lock_version=cast(int, row["lock_version"]),
    )


class RunRepository:
    def __init__(self, connection: AsyncConnection) -> None:
        self._connection = connection

    async def insert(self, record: GenerationRunRecord) -> None:
        await self._connection.execute(generation_runs.insert().values(**asdict(record)))

    async def get(self, *, project_id: str, run_id: str) -> GenerationRunRecord | None:
        row = (
            await self._connection.execute(
                select(generation_runs).where(
                    generation_runs.c.project_id == project_id,
                    generation_runs.c.id == run_id,
                )
            )
        ).mappings().one_or_none()
        return None if row is None else _run_record(row)

    async def get_open_for_project(self, project_id: str) -> GenerationRunRecord | None:
        row = (
            await self._connection.execute(
                select(generation_runs).where(
                    generation_runs.c.project_id == project_id,
                    generation_runs.c.finished_at_ms.is_(None),
                )
            )
        ).mappings().one_or_none()
        return None if row is None else _run_record(row)

    async def get_latest_for_project(self, project_id: str) -> GenerationRunRecord | None:
        row = (
            await self._connection.execute(
                select(generation_runs)
                .where(generation_runs.c.project_id == project_id)
                .order_by(generation_runs.c.run_number.desc())
                .limit(1)
            )
        ).mappings().one_or_none()
        return None if row is None else _run_record(row)

    async def next_run_number(self, *, project_id: str) -> int:
        value = await self._connection.scalar(
            select(func.coalesce(func.max(generation_runs.c.run_number), 0)).where(
                generation_runs.c.project_id == project_id
            )
        )
        return cast(int, value) + 1

    async def find_next_runnable(self) -> GenerationRunRecord | None:
        row = (
            await self._connection.execute(
                select(generation_runs)
                .where(
                    generation_runs.c.status == "running",
                    generation_runs.c.desired_state == "running",
                    generation_runs.c.finished_at_ms.is_(None),
                )
                .order_by(generation_runs.c.updated_at_ms, generation_runs.c.id)
                .limit(1)
            )
        ).mappings().one_or_none()
        return None if row is None else _run_record(row)

    async def ensure_engine_slot(self) -> None:
        statement = sqlite_insert(engine_slot).values(slot_id=1, lock_version=1)
        await self._connection.execute(
            statement.on_conflict_do_nothing(index_elements=[engine_slot.c.slot_id])
        )

    async def get_engine_slot(self) -> EngineSlotRecord:
        await self.ensure_engine_slot()
        row = (
            await self._connection.execute(select(engine_slot).where(engine_slot.c.slot_id == 1))
        ).mappings().one()
        return _slot_record(row)

    async def claim_engine_slot(
        self,
        *,
        run_id: str,
        owner_instance_id: str,
        lease_token: str,
        lease_expires_at_ms: int,
        now_ms: int,
    ) -> bool:
        await self.ensure_engine_slot()
        result = await self._connection.execute(
            update(engine_slot)
            .where(
                engine_slot.c.slot_id == 1,
                engine_slot.c.active_run_id.is_(None),
            )
            .values(
                active_run_id=run_id,
                owner_instance_id=owner_instance_id,
                lease_token=lease_token,
                lease_expires_at_ms=lease_expires_at_ms,
                heartbeat_at_ms=now_ms,
                lock_version=engine_slot.c.lock_version + 1,
            )
        )
        return result.rowcount == 1

    async def heartbeat_engine_slot(
        self,
        *,
        run_id: str,
        owner_instance_id: str,
        lease_token: str,
        lease_expires_at_ms: int,
        now_ms: int,
    ) -> bool:
        result = await self._connection.execute(
            update(engine_slot)
            .where(
                engine_slot.c.slot_id == 1,
                engine_slot.c.active_run_id == run_id,
                engine_slot.c.owner_instance_id == owner_instance_id,
                engine_slot.c.lease_token == lease_token,
            )
            .values(
                lease_expires_at_ms=lease_expires_at_ms,
                heartbeat_at_ms=now_ms,
                lock_version=engine_slot.c.lock_version + 1,
            )
        )
        return result.rowcount == 1

    async def release_engine_slot(
        self,
        *,
        run_id: str,
        owner_instance_id: str,
        lease_token: str,
    ) -> bool:
        result = await self._connection.execute(
            update(engine_slot)
            .where(
                engine_slot.c.slot_id == 1,
                engine_slot.c.active_run_id == run_id,
                engine_slot.c.owner_instance_id == owner_instance_id,
                engine_slot.c.lease_token == lease_token,
            )
            .values(
                active_run_id=None,
                owner_instance_id=None,
                lease_token=None,
                lease_expires_at_ms=None,
                heartbeat_at_ms=None,
                lock_version=engine_slot.c.lock_version + 1,
            )
        )
        return result.rowcount == 1

    async def release_expired_engine_slot(self, *, now_ms: int) -> str | None:
        slot = await self.get_engine_slot()
        if (
            slot.active_run_id is None
            or slot.lease_expires_at_ms is None
            or slot.lease_expires_at_ms > now_ms
        ):
            return None
        result = await self._connection.execute(
            update(engine_slot)
            .where(
                engine_slot.c.slot_id == 1,
                engine_slot.c.active_run_id == slot.active_run_id,
                engine_slot.c.lease_token == slot.lease_token,
                engine_slot.c.lease_expires_at_ms <= now_ms,
            )
            .values(
                active_run_id=None,
                owner_instance_id=None,
                lease_token=None,
                lease_expires_at_ms=None,
                heartbeat_at_ms=None,
                lock_version=engine_slot.c.lock_version + 1,
            )
        )
        return slot.active_run_id if result.rowcount == 1 else None

    async def start_waiting_run(
        self, *, project_id: str, run_id: str, expected_lock_version: int, now_ms: int
    ) -> bool:
        result = await self._connection.execute(
            update(generation_runs)
            .where(
                generation_runs.c.project_id == project_id,
                generation_runs.c.id == run_id,
                generation_runs.c.status == "waiting_for_user",
                generation_runs.c.desired_state == "running",
                generation_runs.c.lock_version == expected_lock_version,
            )
            .values(
                status="running",
                wait_reason_code=None,
                started_at_ms=func.coalesce(generation_runs.c.started_at_ms, now_ms),
                updated_at_ms=now_ms,
                lock_version=expected_lock_version + 1,
            )
        )
        return result.rowcount == 1

    async def request_pause(
        self, *, run: GenerationRunRecord, now_ms: int
    ) -> GenerationRunRecord | None:
        if run.status == "running":
            target_status = "pause_requested"
        elif run.status == "waiting_for_user":
            target_status = "paused"
        elif run.status in {"paused", "pause_requested"}:
            return run
        else:
            return None
        result = await self._connection.execute(
            update(generation_runs)
            .where(
                generation_runs.c.project_id == run.project_id,
                generation_runs.c.id == run.id,
                generation_runs.c.status == run.status,
                generation_runs.c.lock_version == run.lock_version,
            )
            .values(
                status=target_status,
                desired_state="paused",
                updated_at_ms=now_ms,
                lock_version=run.lock_version + 1,
            )
        )
        if result.rowcount != 1:
            return None
        return await self.get(project_id=run.project_id, run_id=run.id)

    async def settle_requested_pause(self, *, run_id: str, now_ms: int) -> bool:
        result = await self._connection.execute(
            update(generation_runs)
            .where(
                generation_runs.c.id == run_id,
                generation_runs.c.status == "pause_requested",
                generation_runs.c.desired_state == "paused",
            )
            .values(
                status="paused",
                updated_at_ms=now_ms,
                lock_version=generation_runs.c.lock_version + 1,
            )
        )
        return result.rowcount == 1

    async def resume_paused_run(
        self, *, run: GenerationRunRecord, now_ms: int
    ) -> GenerationRunRecord | None:
        if run.status != "paused" or run.desired_state != "paused":
            return None
        result = await self._connection.execute(
            update(generation_runs)
            .where(
                generation_runs.c.project_id == run.project_id,
                generation_runs.c.id == run.id,
                generation_runs.c.status == "paused",
                generation_runs.c.lock_version == run.lock_version,
            )
            .values(
                status="running",
                desired_state="running",
                wait_reason_code=None,
                started_at_ms=func.coalesce(generation_runs.c.started_at_ms, now_ms),
                updated_at_ms=now_ms,
                lock_version=run.lock_version + 1,
            )
        )
        if result.rowcount != 1:
            return None
        return await self.get(project_id=run.project_id, run_id=run.id)

    async def wait_for_user(self, *, run_id: str, reason_code: str, now_ms: int) -> bool:
        result = await self._connection.execute(
            update(generation_runs)
            .where(
                generation_runs.c.id == run_id,
                generation_runs.c.status == "running",
                generation_runs.c.desired_state == "running",
            )
            .values(
                status="waiting_for_user",
                wait_reason_code=reason_code,
                updated_at_ms=now_ms,
                lock_version=generation_runs.c.lock_version + 1,
            )
        )
        return result.rowcount == 1

    async def ensure_wait_for_user(
        self, *, run_id: str, reason_code: str, now_ms: int
    ) -> bool:
        """Enter or refine a user gate without requiring a prior scheduler start."""
        result = await self._connection.execute(
            update(generation_runs)
            .where(
                generation_runs.c.id == run_id,
                generation_runs.c.status.in_(("running", "waiting_for_user")),
                generation_runs.c.desired_state == "running",
                generation_runs.c.finished_at_ms.is_(None),
            )
            .values(
                status="waiting_for_user",
                wait_reason_code=reason_code,
                updated_at_ms=now_ms,
                lock_version=generation_runs.c.lock_version + 1,
            )
        )
        return result.rowcount == 1

    async def failure_pause(
        self,
        *,
        run_id: str,
        task_id: str,
        failure_code: str,
        failure_ref_id: str,
        now_ms: int,
    ) -> bool:
        result = await self._connection.execute(
            update(generation_runs)
            .where(
                generation_runs.c.id == run_id,
                generation_runs.c.status.in_(("running", "pause_requested", "paused")),
                generation_runs.c.finished_at_ms.is_(None),
            )
            .values(
                status="failure_paused",
                desired_state="paused",
                wait_reason_code=None,
                blocking_task_id=task_id,
                failure_code=failure_code,
                failure_ref_id=failure_ref_id,
                updated_at_ms=now_ms,
                lock_version=generation_runs.c.lock_version + 1,
            )
        )
        return result.rowcount == 1

    async def retry_failure(self, *, run: GenerationRunRecord, now_ms: int) -> bool:
        result = await self._connection.execute(
            update(generation_runs)
            .where(
                generation_runs.c.project_id == run.project_id,
                generation_runs.c.id == run.id,
                generation_runs.c.status == "failure_paused",
                generation_runs.c.lock_version == run.lock_version,
            )
            .values(
                status="running",
                desired_state="running",
                blocking_task_id=None,
                failure_code=None,
                failure_ref_id=None,
                updated_at_ms=now_ms,
                lock_version=run.lock_version + 1,
            )
        )
        return result.rowcount == 1

    async def complete(self, *, run_id: str, now_ms: int) -> bool:
        result = await self._connection.execute(
            update(generation_runs)
            .where(
                generation_runs.c.id == run_id,
                generation_runs.c.status == "running",
                generation_runs.c.desired_state == "running",
            )
            .values(
                status="completed",
                desired_state="paused",
                wait_reason_code=None,
                finished_at_ms=now_ms,
                updated_at_ms=now_ms,
                lock_version=generation_runs.c.lock_version + 1,
            )
        )
        return result.rowcount == 1
