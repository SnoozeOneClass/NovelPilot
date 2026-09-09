from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

from app.authoring.domain.models import (
    AuthoringProfileSnapshot,
    Instruction,
    Phase,
    ProjectView,
    RunStatus,
    StateSnapshot,
    TargetLength,
    content_hash,
)
from app.authoring.errors import (
    LeaseUnavailableError,
    StateCorruptionError,
    TerminalPostconditionError,
)

CHAPTER_COMMIT_PAYLOAD_VERSION = 1

TERMINAL_TOOLS = {
    "create_foundation": "audit_foundation",
    "write_chapter": "commit_chapter",
    "rewrite_chapter": "commit_chapter",
    "review_boundary": "save_review",
    "save_summary": "save_arc_summary",
    "extend_outline": "revise_outline",
    "complete_book": "complete_book",
}

AUTHORING_TABLES = frozenset(
    {
        "authoring_schema_migrations",
        "projects",
        "run_state",
        "planning_revisions",
        "content_blobs",
        "chapters",
        "chapter_versions",
        "chapter_facts",
        "canon_snapshots",
        "summaries",
        "reviews",
        "rewrite_queue",
        "checkpoints",
        "worker_episodes",
        "tool_invocations",
        "decisions",
        "model_usage",
        "model_requests",
        "export_manifests",
        "domain_events",
    }
)


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def encode_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def decode_json(value: str) -> Any:
    return json.loads(value)


def decode_text_blob(value: str | bytes) -> str:
    return value if isinstance(value, str) else value.decode("utf-8")


class AuthoringStore:
    """Facts and evidence for the isolated authoring runtime.

    Connections are deliberately short lived.  No transaction survives a model
    wait, while every Tool mutation owns one ``BEGIN IMMEDIATE`` transaction.
    """

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path.resolve()

    async def _connect(self) -> aiosqlite.Connection:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = await aiosqlite.connect(self.database_path)
        connection.row_factory = aiosqlite.Row
        await connection.execute("PRAGMA foreign_keys = ON")
        await connection.execute("PRAGMA busy_timeout = 5000")
        await connection.execute("PRAGMA journal_mode = WAL")
        return connection

    async def migrate(self) -> None:
        migration_directory = Path(__file__).with_name("migrations")
        connection = await self._connect()
        try:
            existing = {
                row[0]
                for row in await (
                    await connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' "
                        "AND name NOT LIKE 'sqlite_%'"
                    )
                ).fetchall()
            }
            unexpected = existing.difference(AUTHORING_TABLES)
            if unexpected:
                names = ", ".join(sorted(unexpected))
                raise StateCorruptionError(
                    f"authoring database path contains non-authoring tables: {names}"
                )
            for migration in sorted(migration_directory.glob("[0-9][0-9][0-9][0-9]_*.sql")):
                history_exists = await (
                    await connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' "
                        "AND name='authoring_schema_migrations'"
                    )
                ).fetchone()
                applied = None
                if history_exists is not None:
                    applied = await (
                        await connection.execute(
                            "SELECT 1 FROM authoring_schema_migrations WHERE version=?",
                            (migration.stem,),
                        )
                    ).fetchone()
                if applied is None:
                    await connection.executescript(migration.read_text(encoding="utf-8"))
        finally:
            await connection.close()

    @asynccontextmanager
    async def write_transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        connection = await self._connect()
        try:
            await connection.execute("BEGIN IMMEDIATE")
            yield connection
            await connection.commit()
        except BaseException:
            await connection.rollback()
            raise
        finally:
            await connection.close()

    async def create_project(
        self,
        brief: str,
        target: TargetLength,
        project_id: str | None = None,
        profile_bindings: Mapping[str, str] | None = None,
    ) -> str:
        if not brief.strip():
            raise ValueError("brief must be non-empty")
        identifier = project_id or str(uuid.uuid4())
        async with self.write_transaction() as connection:
            await connection.execute(
                "INSERT INTO projects(id, brief, target_json, profile_bindings_json, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    identifier,
                    brief.strip(),
                    encode_json(target.model_dump(mode="json")),
                    encode_json(dict(profile_bindings or {})),
                    utc_now(),
                ),
            )
            await connection.execute(
                "INSERT INTO run_state(project_id, status, phase) VALUES (?, 'ready', 'foundation')",
                (identifier,),
            )
            await self.append_event(
                connection,
                identifier,
                "project_created",
                {"target": target.model_dump(mode="json")},
            )
        return identifier

    async def project(self, project_id: str) -> ProjectView:
        connection = await self._connect()
        try:
            cursor = await connection.execute(
                "SELECT p.id, p.brief, p.title, p.target_json, r.status, r.phase, "
                "r.failure_reason, (SELECT count(*) FROM chapters c WHERE c.project_id=p.id) AS chapter_count "
                "FROM projects p JOIN run_state r ON r.project_id=p.id WHERE p.id=?",
                (project_id,),
            )
            row = await cursor.fetchone()
        finally:
            await connection.close()
        if row is None:
            raise KeyError(project_id)
        return ProjectView(
            id=row["id"],
            brief=row["brief"],
            title=row["title"],
            status=RunStatus(row["status"]),
            phase=Phase(row["phase"]),
            target=TargetLength.model_validate(decode_json(row["target_json"])),
            chapter_count=row["chapter_count"],
            failure_reason=row["failure_reason"],
        )

    async def list_projects(self) -> list[ProjectView]:
        connection = await self._connect()
        try:
            cursor = await connection.execute("SELECT id FROM projects ORDER BY created_at")
            ids = [row["id"] for row in await cursor.fetchall()]
        finally:
            await connection.close()
        return [await self.project(project_id) for project_id in ids]

    async def load_state(self, project_id: str) -> StateSnapshot:
        connection = await self._connect()
        try:
            await connection.execute("BEGIN")
            project = await (
                await connection.execute(
                    "SELECT p.target_json, r.* FROM projects p JOIN run_state r "
                    "ON r.project_id=p.id WHERE p.id=?",
                    (project_id,),
                )
            ).fetchone()
            if project is None:
                raise KeyError(project_id)
            plan = await (
                await connection.execute(
                    "SELECT revision, audited, planned_through FROM planning_revisions "
                    "WHERE project_id=? ORDER BY revision DESC LIMIT 1",
                    (project_id,),
                )
            ).fetchone()
            chapter_count_row = await (
                await connection.execute(
                    "SELECT count(*) AS n FROM chapters WHERE project_id=?", (project_id,)
                )
            ).fetchone()
            chapter_max_row = await (
                await connection.execute(
                    "SELECT coalesce(max(chapter_number),0) AS n FROM chapters WHERE project_id=?",
                    (project_id,),
                )
            ).fetchone()
            assert chapter_count_row is not None and chapter_max_row is not None
            chapter_count = chapter_count_row["n"]
            chapter_max = chapter_max_row["n"]
            if chapter_count != chapter_max:
                corrupted = "committed chapter sequence contains a gap"
            else:
                corrupted = None
            rewrites = await (
                await connection.execute(
                    "SELECT chapter_number FROM rewrite_queue WHERE project_id=? AND status='pending' "
                    "ORDER BY review_boundary, chapter_number",
                    (project_id,),
                )
            ).fetchall()
            reviewed_row = await (
                await connection.execute(
                    "SELECT coalesce(max(boundary),0) AS n FROM reviews "
                    "WHERE project_id=? AND verdict='accept'",
                    (project_id,),
                )
            ).fetchone()
            summarized_row = await (
                await connection.execute(
                    "SELECT coalesce(max(boundary),0) AS n FROM summaries "
                    "WHERE project_id=? AND kind='arc'",
                    (project_id,),
                )
            ).fetchone()
            assert reviewed_row is not None and summarized_row is not None
            reviewed = reviewed_row["n"]
            summarized = summarized_row["n"]
            await connection.commit()
        except BaseException:
            await connection.rollback()
            raise
        finally:
            await connection.close()

        version_material = {
            "lock": project["lock_version"],
            "plan": None if plan is None else plan["revision"],
            "chapters": chapter_count,
            "reviewed": reviewed,
            "summarized": summarized,
            "rewrites": [row["chapter_number"] for row in rewrites],
        }
        return StateSnapshot(
            project_id=project_id,
            status=RunStatus(project["status"]),
            phase=Phase(project["phase"]),
            target=TargetLength.model_validate(decode_json(project["target_json"])),
            fact_version=content_hash(version_material),
            foundation_present=plan is not None,
            foundation_audited=bool(plan and plan["audited"]),
            chapter_count=chapter_count,
            planned_through=0 if plan is None else plan["planned_through"],
            active_instruction_key=project["active_instruction_key"],
            active_instruction_kind=project["active_instruction_kind"],
            active_logical_target=project["active_logical_target"],
            active_fact_version=project["active_fact_version"],
            pending_rewrites=tuple(row["chapter_number"] for row in rewrites),
            reviewed_through=reviewed,
            summarized_through=summarized,
            final_audit_complete=RunStatus(project["status"]) is RunStatus.COMPLETED,
            corrupted_reason=corrupted,
        )

    async def set_active_instruction(
        self,
        project_id: str,
        key: str | None,
        kind: str | None,
        target: str | None,
        fact_version: str | None = None,
    ) -> None:
        async with self.write_transaction() as connection:
            cursor = await connection.execute(
                "UPDATE run_state SET active_instruction_key=?, active_instruction_kind=?, "
                "active_logical_target=?, active_fact_version=?, lock_version=lock_version+1 "
                "WHERE project_id=?",
                (key, kind, target, fact_version, project_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(project_id)

    async def set_status(
        self,
        project_id: str,
        status: RunStatus,
        failure_reason: str | None = None,
    ) -> None:
        phase = Phase.COMPLETE.value if status is RunStatus.COMPLETED else None
        async with self.write_transaction() as connection:
            cursor = await connection.execute(
                "UPDATE run_state SET status=?, phase=coalesce(?,phase), failure_reason=?, "
                "lock_version=lock_version+1 WHERE project_id=?",
                (status.value, phase, failure_reason, project_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(project_id)
            await self.append_event(
                connection,
                project_id,
                f"run_{status.value}",
                {} if failure_reason is None else {"reason": failure_reason},
            )

    async def pause(self, project_id: str) -> None:
        await self._set_control_status(
            project_id,
            RunStatus.PAUSED,
            allowed={RunStatus.READY, RunStatus.RUNNING},
        )

    async def resume(self, project_id: str) -> None:
        await self._set_control_status(
            project_id,
            RunStatus.READY,
            allowed={RunStatus.PAUSED, RunStatus.FAILURE_PAUSED},
            idempotent={RunStatus.READY},
        )

    async def cancel(self, project_id: str) -> None:
        await self._set_control_status(
            project_id,
            RunStatus.CANCELLED,
            allowed={
                RunStatus.READY,
                RunStatus.RUNNING,
                RunStatus.PAUSED,
                RunStatus.FAILURE_PAUSED,
            },
            idempotent={RunStatus.CANCELLED},
        )

    async def _set_control_status(
        self,
        project_id: str,
        target: RunStatus,
        *,
        allowed: set[RunStatus],
        idempotent: set[RunStatus] | None = None,
    ) -> None:
        async with self.write_transaction() as connection:
            row = await (
                await connection.execute(
                    "SELECT status FROM run_state WHERE project_id=?", (project_id,)
                )
            ).fetchone()
            if row is None:
                raise KeyError(project_id)
            current = RunStatus(row["status"])
            if current in (idempotent or set()):
                return
            if current not in allowed:
                raise ValueError(f"cannot {target.value} project in {current.value}")
            await connection.execute(
                "UPDATE run_state SET status=?,failure_reason=NULL,lock_version=lock_version+1 "
                "WHERE project_id=?",
                (target.value, project_id),
            )
            await self.append_event(connection, project_id, f"run_{target.value}", {})

    async def acquire_lease(
        self,
        project_id: str,
        owner: str,
        ttl_seconds: float = 30.0,
    ) -> None:
        now = time.time()
        async with self.write_transaction() as connection:
            cursor = await connection.execute(
                "UPDATE run_state SET lease_owner=?, lease_expires_at=?, lock_version=lock_version+1 "
                "WHERE project_id=? AND (lease_owner IS NULL OR lease_owner=? OR lease_expires_at<?)",
                (owner, now + ttl_seconds, project_id, owner, now),
            )
            if cursor.rowcount != 1:
                raise LeaseUnavailableError(f"project {project_id} already has an active writer")

    async def renew_lease(self, project_id: str, owner: str, ttl_seconds: float = 30.0) -> None:
        async with self.write_transaction() as connection:
            cursor = await connection.execute(
                "UPDATE run_state SET lease_expires_at=? WHERE project_id=? AND lease_owner=?",
                (time.time() + ttl_seconds, project_id, owner),
            )
            if cursor.rowcount != 1:
                raise LeaseUnavailableError("engine lease was lost")

    async def release_lease(self, project_id: str, owner: str) -> None:
        async with self.write_transaction() as connection:
            await connection.execute(
                "UPDATE run_state SET lease_owner=NULL, lease_expires_at=NULL "
                "WHERE project_id=? AND lease_owner=?",
                (project_id, owner),
            )

    async def reconcile(self, project_id: str) -> int:
        """Reconcile orphan execution and audit all persisted route dependencies."""
        state = await self.load_state(project_id)
        if state.corrupted_reason:
            await self.set_status(
                project_id, RunStatus.FAILURE_PAUSED, failure_reason=state.corrupted_reason
            )
            raise StateCorruptionError(state.corrupted_reason)
        recovered = 0
        corruption: list[str] = []
        active_values = (
            state.active_instruction_key,
            state.active_instruction_kind,
            state.active_logical_target,
            state.active_fact_version,
        )
        if any(value is not None for value in active_values) and not all(
            value is not None for value in active_values
        ):
            corruption.append("active instruction identity is incomplete")
        elif state.active_instruction_key is not None:
            expected_key = content_hash(
                {
                    "project_id": project_id,
                    "kind": state.active_instruction_kind,
                    "target": state.active_logical_target,
                    "fact_version": state.active_fact_version,
                }
            )
            if expected_key != state.active_instruction_key:
                corruption.append("active instruction key does not match its durable identity")
        if state.active_instruction_kind is not None and state.active_logical_target is not None:
            prefix_by_kind = {
                "create_foundation": "foundation",
                "write_chapter": "chapter:",
                "rewrite_chapter": "chapter:",
                "review_boundary": "boundary:",
                "save_summary": "boundary:",
                "extend_outline": "chapter:",
                "complete_book": "book",
            }
            prefix = prefix_by_kind.get(state.active_instruction_kind.value)
            if prefix is None or not state.active_logical_target.startswith(prefix):
                corruption.append("active instruction kind and logical target conflict")
        async with self.write_transaction() as connection:
            running = await (
                await connection.execute(
                    "SELECT id,instruction_key,instruction_kind,logical_target FROM worker_episodes "
                    "WHERE project_id=? AND status='running'",
                    (project_id,),
                )
            ).fetchall()
            project_row = await (
                await connection.execute(
                    "SELECT target_json FROM projects WHERE id=?", (project_id,)
                )
            ).fetchone()
            plan = await (
                await connection.execute(
                    "SELECT planned_through FROM planning_revisions WHERE project_id=? "
                    "ORDER BY revision DESC LIMIT 1",
                    (project_id,),
                )
            ).fetchone()
            chapter_rows = list(
                await (
                    await connection.execute(
                        "SELECT chapter_number,revision FROM chapters WHERE project_id=? "
                        "ORDER BY chapter_number",
                        (project_id,),
                    )
                ).fetchall()
            )
            target = (
                0
                if project_row is None
                else int(decode_json(project_row["target_json"])["target_chapters"])
            )
            if chapter_rows and plan is None:
                corruption.append("committed chapters exist without an active planning revision")
            if plan is not None and (
                plan["planned_through"] < len(chapter_rows) or plan["planned_through"] > target
            ):
                corruption.append(
                    "active planning coverage conflicts with committed chapters or target"
                )
            for chapter in chapter_rows:
                facts = await (
                    await connection.execute(
                        "SELECT 1 FROM chapter_facts WHERE project_id=? AND chapter_number=? "
                        "AND revision=?",
                        (project_id, chapter["chapter_number"], chapter["revision"]),
                    )
                ).fetchone()
                if facts is None:
                    corruption.append(
                        f"chapter {chapter['chapter_number']} current revision has no ChapterFacts"
                    )
            if chapter_rows:
                canon = await (
                    await connection.execute(
                        "SELECT through_chapter,payload_json FROM canon_snapshots WHERE project_id=? "
                        "ORDER BY version DESC LIMIT 1",
                        (project_id,),
                    )
                ).fetchone()
                if canon is None or canon["through_chapter"] != len(chapter_rows):
                    corruption.append("latest Canon does not cover all committed chapters")
                elif not all(
                    f"chapter:{chapter['chapter_number']}" in decode_json(canon["payload_json"])
                    for chapter in chapter_rows
                ):
                    corruption.append("latest Canon is missing committed chapter facts")

            episode_profiles = await (
                await connection.execute(
                    "SELECT id,profile_snapshot_json,fallback_profile_snapshot_json "
                    "FROM worker_episodes WHERE project_id=?",
                    (project_id,),
                )
            ).fetchall()
            for episode in episode_profiles:
                try:
                    AuthoringProfileSnapshot.model_validate_json(episode["profile_snapshot_json"])
                except ValueError:
                    corruption.append(f"episode {episode['id']} has invalid Profile metadata")
                fallback_profile = episode["fallback_profile_snapshot_json"]
                if fallback_profile is not None:
                    try:
                        AuthoringProfileSnapshot.model_validate_json(fallback_profile)
                    except ValueError:
                        corruption.append(
                            f"episode {episode['id']} has invalid fallback Profile metadata"
                        )

            missing_evidence = await (
                await connection.execute(
                    "SELECT count(*) AS n FROM checkpoints c LEFT JOIN tool_invocations t "
                    "ON t.project_id=c.project_id AND t.instruction_key=c.instruction_key "
                    "AND t.tool_name=c.step AND t.logical_target=c.logical_target "
                    "WHERE c.project_id=? AND t.id IS NULL",
                    (project_id,),
                )
            ).fetchone()
            assert missing_evidence is not None
            if missing_evidence["n"]:
                corruption.append("checkpoint exists without Tool invocation evidence")
            orphan_invocations = await (
                await connection.execute(
                    "SELECT count(*) AS n FROM tool_invocations t LEFT JOIN checkpoints c "
                    "ON c.project_id=t.project_id AND c.instruction_key=t.instruction_key "
                    "AND c.step=t.tool_name AND c.logical_target=t.logical_target "
                    "WHERE t.project_id=? AND c.instruction_key IS NULL",
                    (project_id,),
                )
            ).fetchone()
            assert orphan_invocations is not None
            if orphan_invocations["n"]:
                corruption.append("Tool invocation exists without a checkpoint")
            mismatched_evidence = await (
                await connection.execute(
                    "SELECT count(*) AS n FROM checkpoints c JOIN tool_invocations t "
                    "ON t.project_id=c.project_id AND t.instruction_key=c.instruction_key "
                    "AND t.tool_name=c.step AND t.logical_target=c.logical_target "
                    "WHERE c.project_id=? AND (c.payload_hash<>t.payload_hash "
                    "OR c.result_json<>t.result_json)",
                    (project_id,),
                )
            ).fetchone()
            assert mismatched_evidence is not None
            if mismatched_evidence["n"]:
                corruption.append("checkpoint and Tool invocation payload/result evidence disagree")

            completed = await (
                await connection.execute(
                    "SELECT instruction_key,instruction_kind,logical_target FROM worker_episodes "
                    "WHERE project_id=? AND status='completed' AND instruction_kind<>''",
                    (project_id,),
                )
            ).fetchall()
            for episode in completed:
                terminal = TERMINAL_TOOLS.get(episode["instruction_kind"])
                if terminal is None:
                    corruption.append("completed episode has an unknown Instruction kind")
                    continue
                checkpoint = await (
                    await connection.execute(
                        "SELECT 1 FROM checkpoints WHERE project_id=? AND instruction_key=? "
                        "AND step=? AND logical_target=?",
                        (
                            project_id,
                            episode["instruction_key"],
                            terminal,
                            episode["logical_target"],
                        ),
                    )
                ).fetchone()
                if checkpoint is None:
                    corruption.append("completed episode lacks its terminal checkpoint")

            # Decide all orphan outcomes only after auditing evidence. A checkpoint alone
            # cannot make an interrupted Agent successful, or conceal state corruption.
            terminal_episodes: set[str] = set()
            for episode in running:
                try:
                    if await self._terminal_complete(
                        connection,
                        project_id,
                        episode["instruction_key"],
                        episode["instruction_kind"],
                        episode["logical_target"],
                    ):
                        terminal_episodes.add(episode["id"])
                except StateCorruptionError as error:
                    corruption.append(str(error))

            if corruption:
                reason = "; ".join(dict.fromkeys(corruption))
                await connection.execute(
                    "UPDATE run_state SET status='failure_paused',failure_reason=?,"
                    "lock_version=lock_version+1 WHERE project_id=?",
                    (reason, project_id),
                )
                await self.append_event(
                    connection, project_id, "run_failed", {"reason": reason, "source": "reconcile"}
                )
            else:
                for episode in running:
                    eligible = state.status in {
                        RunStatus.READY,
                        RunStatus.RUNNING,
                        RunStatus.COMPLETED,
                    }
                    if eligible and episode["id"] in terminal_episodes:
                        await connection.execute(
                            "UPDATE worker_episodes SET status='completed',ended_at=? WHERE id=?",
                            (utc_now(), episode["id"]),
                        )
                        await self.append_event(
                            connection,
                            project_id,
                            "episode_reconciled_completed",
                            {
                                "episode_id": episode["id"],
                                "terminal_tool": TERMINAL_TOOLS[episode["instruction_kind"]],
                            },
                        )
                        if state.active_instruction_key == episode["instruction_key"]:
                            await self._clear_active_instruction(connection, project_id)
                            recovered = 1
                    else:
                        await connection.execute(
                            "UPDATE worker_episodes SET status='interrupted', ended_at=?, "
                            "failure='process restarted before episode terminalization' WHERE id=?",
                            (utc_now(), episode["id"]),
                        )
        if corruption:
            raise StateCorruptionError("; ".join(dict.fromkeys(corruption)))
        return recovered

    @staticmethod
    async def _clear_active_instruction(connection: aiosqlite.Connection, project_id: str) -> None:
        await connection.execute(
            "UPDATE run_state SET active_instruction_key=NULL,active_instruction_kind=NULL,"
            "active_logical_target=NULL,active_fact_version=NULL,lock_version=lock_version+1 "
            "WHERE project_id=?",
            (project_id,),
        )

    @staticmethod
    def _require_lease(row: aiosqlite.Row, owner: str) -> None:
        if row["lease_owner"] != owner or (row["lease_expires_at"] or 0) <= time.time():
            raise LeaseUnavailableError("engine lease was lost before terminal acknowledgement")

    async def recover_active_instruction(
        self,
        project_id: str,
        owner: str,
        *,
        source_episode_id: str | None = None,
    ) -> bool:
        """Acknowledge durable work without rewriting failed execution evidence."""
        async with self.write_transaction() as connection:
            state = await (
                await connection.execute(
                    "SELECT * FROM run_state WHERE project_id=?",
                    (project_id,),
                )
            ).fetchone()
            if state is None:
                raise KeyError(project_id)
            self._require_lease(state, owner)
            if state["status"] not in {"ready", "running", "completed"}:
                return False
            key = state["active_instruction_key"]
            if key is None:
                return False
            if not await self._terminal_complete(
                connection,
                project_id,
                key,
                state["active_instruction_kind"],
                state["active_logical_target"],
            ):
                return False
            await self._clear_active_instruction(connection, project_id)
            await self.append_event(
                connection,
                project_id,
                "instruction_recovered",
                {
                    "instruction_key": key,
                    "terminal_tool": TERMINAL_TOOLS[state["active_instruction_kind"]],
                    "source_episode_id": source_episode_id,
                },
            )
            return True

    async def _terminal_complete(
        self,
        connection: aiosqlite.Connection,
        project_id: str,
        instruction_key: str,
        kind: str,
        logical_target: str,
    ) -> bool:
        terminal = TERMINAL_TOOLS.get(kind)
        if terminal is None:
            return False
        rows = await (
            await connection.execute(
                "SELECT c.*,t.id AS invocation_id,t.payload_hash AS invocation_payload,"
                "t.result_json AS invocation_result,t.idempotency_key FROM checkpoints c "
                "LEFT JOIN tool_invocations t ON t.project_id=c.project_id "
                "AND t.instruction_key=c.instruction_key AND t.tool_name=c.step "
                "AND t.logical_target=c.logical_target WHERE c.project_id=? "
                "AND c.instruction_key=? AND c.logical_target=?",
                (project_id, instruction_key, logical_target),
            )
        ).fetchall()
        checkpoints: dict[str, dict[str, Any]] = {}
        payload_hashes: dict[str, str] = {}
        for row in rows:
            identity = content_hash(
                {
                    "project": project_id,
                    "instruction": instruction_key,
                    "step": row["step"],
                    "target": logical_target,
                    "payload": row["payload_hash"],
                }
            )
            if (
                row["invocation_id"] is None
                or row["payload_hash"] != row["invocation_payload"]
                or row["result_json"] != row["invocation_result"]
                or row["idempotency_key"] != identity
            ):
                raise StateCorruptionError(
                    "terminal recovery checkpoint and Tool invocation evidence disagree"
                )
            try:
                checkpoints[row["step"]] = dict(decode_json(row["result_json"]))
                payload_hashes[row["step"]] = row["payload_hash"]
            except (ValueError, TypeError) as error:
                raise StateCorruptionError(
                    "terminal recovery result evidence is malformed"
                ) from error
        if terminal not in checkpoints:
            return False
        result = checkpoints[terminal]
        try:
            valid = await self._terminal_facts_match(
                connection,
                project_id,
                instruction_key,
                kind,
                logical_target,
                result,
                checkpoints,
                payload_hashes,
            )
        except (KeyError, TypeError, ValueError) as error:
            raise StateCorruptionError(f"{terminal} terminal facts are malformed") from error
        if not valid:
            raise StateCorruptionError(
                f"{terminal} terminal checkpoint has no matching domain facts"
            )
        return True

    async def _terminal_facts_match(
        self,
        connection: aiosqlite.Connection,
        project_id: str,
        instruction_key: str,
        kind: str,
        logical_target: str,
        result: dict[str, Any],
        checkpoints: dict[str, dict[str, Any]],
        payload_hashes: dict[str, str],
    ) -> bool:
        if kind in {"create_foundation", "extend_outline"}:
            plan = await (
                await connection.execute(
                    "SELECT * FROM planning_revisions WHERE project_id=? ORDER BY revision DESC LIMIT 1",
                    (project_id,),
                )
            ).fetchone()
            if plan is None or not plan["audited"]:
                return False
            saved = checkpoints.get("save_foundation", result)
            if "revision" in saved and (
                plan["revision"] != saved["revision"]
                or plan["planned_through"] != saved["planned_through"]
            ):
                return False
            payload = decode_json(plan["payload_json"])
            outline = [*payload.get("outline", []), *payload.get("outline_extension", [])]
            if kind == "create_foundation":
                if (
                    "save_foundation" in payload_hashes
                    and content_hash(payload) != payload_hashes["save_foundation"]
                ):
                    return False
                if "save_book" in checkpoints:
                    project = await (
                        await connection.execute(
                            "SELECT title FROM projects WHERE id=?",
                            (project_id,),
                        )
                    ).fetchone()
                    if project is None or project["title"] != checkpoints["save_book"].get("title"):
                        return False
            else:
                prior = await (
                    await connection.execute(
                        "SELECT planned_through FROM planning_revisions WHERE project_id=? AND revision=?",
                        (project_id, plan["revision"] - 1),
                    )
                ).fetchone()
                if (
                    prior is None
                    or content_hash(
                        {
                            "planned_through": plan["planned_through"],
                            "outline_extension": outline[prior["planned_through"] :],
                        }
                    )
                    != payload_hashes["revise_outline"]
                ):
                    return False
            return len(outline) == plan["planned_through"] and (
                result.get("audited") is True
                if kind == "create_foundation"
                else plan["planned_through"] >= int(logical_target.split(":")[1])
            )
        if kind in {"write_chapter", "rewrite_chapter"}:
            number = int(logical_target.split(":")[1])
            row = await (
                await connection.execute(
                    "SELECT c.*,b.content,f.facts_json FROM chapters c "
                    "JOIN content_blobs b ON b.sha256=c.content_sha256 "
                    "JOIN chapter_facts f ON f.project_id=c.project_id AND f.chapter_number=c.chapter_number "
                    "AND f.revision=c.revision WHERE c.project_id=? AND c.chapter_number=?",
                    (project_id, number),
                )
            ).fetchone()
            canon = await (
                await connection.execute(
                    "SELECT payload_json,through_chapter FROM canon_snapshots WHERE project_id=? "
                    "ORDER BY version DESC LIMIT 1",
                    (project_id,),
                )
            ).fetchone()
            count = await (
                await connection.execute(
                    "SELECT count(*) AS n,max(chapter_number) AS last FROM chapters WHERE project_id=?",
                    (project_id,),
                )
            ).fetchone()
            check = checkpoints.get("check_consistency", {})
            if (
                row is None
                or canon is None
                or count is None
                or row["commit_instruction_key"] != instruction_key
                or row["revision"] != result.get("revision")
                or row["content_sha256"] != result.get("content_sha256")
                or number != result.get("chapter_number")
                or content_hash(decode_text_blob(row["content"])) != row["content_sha256"]
                or count["n"] != count["last"]
                or canon["through_chapter"] != count["n"]
                or check.get("passed") is not True
                or check.get("chapter_number") != number
                or decode_json(canon["payload_json"]).get(logical_target)
                != decode_json(row["facts_json"])
            ):
                return False
            committed_payload_hash = content_hash(
                {
                    "chapter_number": number,
                    "title": row["title"],
                    "content": decode_text_blob(row["content"]),
                    "facts": decode_json(row["facts_json"]),
                }
            )
            if (
                "committed_payload_version" not in result
                and "committed_payload_sha256" not in result
            ):
                if committed_payload_hash != payload_hashes["commit_chapter"]:
                    raise StateCorruptionError(
                        "unverifiable legacy committed-payload evidence: stored chapter fields "
                        "do not reconstruct the original Tool payload hash"
                    )
            elif (
                type(result.get("committed_payload_version")) is not int
                or result["committed_payload_version"] != CHAPTER_COMMIT_PAYLOAD_VERSION
                or committed_payload_hash != result.get("committed_payload_sha256")
            ):
                return False
            if kind == "rewrite_chapter":
                pending = await (
                    await connection.execute(
                        "SELECT 1 FROM rewrite_queue WHERE project_id=? AND chapter_number=? AND status='pending'",
                        (project_id, number),
                    )
                ).fetchone()
                return pending is None
            return True
        if kind == "review_boundary":
            boundary = int(logical_target.split(":")[1])
            review = await (
                await connection.execute(
                    "SELECT * FROM reviews WHERE project_id=? AND boundary=? ORDER BY revision DESC LIMIT 1",
                    (project_id, boundary),
                )
            ).fetchone()
            if (
                review is None
                or result.get("boundary") != boundary
                or review["verdict"] != result.get("verdict")
            ):
                return False
            event = await (
                await connection.execute(
                    "SELECT payload_json FROM domain_events WHERE project_id=? AND kind='review_saved' "
                    "AND json_extract(payload_json,'$.boundary')=? AND json_extract(payload_json,'$.revision')=? "
                    "ORDER BY seq DESC LIMIT 1",
                    (project_id, boundary, review["revision"]),
                )
            ).fetchone()
            if event is None:
                return False
            targets = decode_json(event["payload_json"])["chapters"]
            if len(targets) != result.get("rewrite_count"):
                return False
            for chapter in targets:
                queued = await (
                    await connection.execute(
                        "SELECT 1 FROM rewrite_queue WHERE project_id=? AND review_boundary=? AND chapter_number=?",
                        (project_id, boundary, chapter),
                    )
                ).fetchone()
                if queued is None:
                    return False
            return (
                content_hash(
                    {
                        "boundary": boundary,
                        "verdict": review["verdict"],
                        "dimensions": decode_json(review["dimensions_json"]),
                        "evidence": decode_json(review["evidence_json"]),
                        "chapters": targets,
                    }
                )
                == payload_hashes["save_review"]
            )
        if kind == "save_summary":
            boundary = int(logical_target.split(":")[1])
            summary = await (
                await connection.execute(
                    "SELECT payload_json FROM summaries WHERE project_id=? AND kind='arc' AND boundary=?",
                    (project_id, boundary),
                )
            ).fetchone()
            return (
                summary is not None
                and result.get("kind") == "arc"
                and result.get("boundary") == boundary
                and bool(decode_json(summary["payload_json"]).get("summary"))
                and content_hash(decode_json(summary["payload_json"]))
                == payload_hashes["save_arc_summary"]
            )
        if kind == "complete_book":
            state = await (
                await connection.execute(
                    "SELECT r.status,r.phase,json_extract(p.target_json,'$.target_chapters') AS target,"
                    "(SELECT count(*) FROM chapters WHERE project_id=p.id) AS chapters "
                    "FROM projects p JOIN run_state r ON r.project_id=p.id WHERE p.id=?",
                    (project_id,),
                )
            ).fetchone()
            return (
                state is not None
                and state["status"] == "completed"
                and state["phase"] == "complete"
                and state["chapters"] >= state["target"]
                and result.get("completed") is True
                and state["chapters"] == result.get("chapters")
            )
        return False

    async def append_event(
        self,
        connection: aiosqlite.Connection,
        project_id: str,
        kind: str,
        payload: Mapping[str, Any],
    ) -> int:
        cursor = await connection.execute(
            "INSERT INTO domain_events(project_id, kind, payload_json, created_at) VALUES (?,?,?,?)",
            (project_id, kind, encode_json(dict(payload)), utc_now()),
        )
        return int(cursor.lastrowid or 0)

    async def events(self, project_id: str, after_seq: int = 0) -> list[dict[str, Any]]:
        connection = await self._connect()
        try:
            rows = await (
                await connection.execute(
                    "SELECT seq, kind, payload_json, created_at FROM domain_events "
                    "WHERE project_id=? AND seq>? ORDER BY seq",
                    (project_id, after_seq),
                )
            ).fetchall()
        finally:
            await connection.close()
        return [
            {
                "seq": row["seq"],
                "kind": row["kind"],
                "payload": decode_json(row["payload_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    async def chapter(self, project_id: str, chapter_number: int) -> dict[str, Any]:
        connection = await self._connect()
        try:
            row = await (
                await connection.execute(
                    "SELECT c.chapter_number, c.title, c.content_sha256, c.revision, b.content "
                    "FROM chapters c JOIN content_blobs b ON b.sha256=c.content_sha256 "
                    "WHERE c.project_id=? AND c.chapter_number=?",
                    (project_id, chapter_number),
                )
            ).fetchone()
        finally:
            await connection.close()
        if row is None:
            raise KeyError((project_id, chapter_number))
        return {
            "chapter_number": row["chapter_number"],
            "title": row["title"],
            "content": decode_text_blob(row["content"]),
            "content_sha256": row["content_sha256"],
            "revision": row["revision"],
        }

    async def novel_context(self, project_id: str, recent_limit: int = 3) -> dict[str, Any]:
        connection = await self._connect()
        try:
            project = await (
                await connection.execute(
                    "SELECT brief, title, target_json FROM projects WHERE id=?", (project_id,)
                )
            ).fetchone()
            if project is None:
                raise KeyError(project_id)
            plan = await (
                await connection.execute(
                    "SELECT payload_json, planned_through FROM planning_revisions "
                    "WHERE project_id=? ORDER BY revision DESC LIMIT 1",
                    (project_id,),
                )
            ).fetchone()
            chapters = list(
                await (
                    await connection.execute(
                        "SELECT c.chapter_number, c.title, f.facts_json FROM chapters c "
                        "LEFT JOIN chapter_facts f ON f.project_id=c.project_id "
                        "AND f.chapter_number=c.chapter_number AND f.revision=c.revision "
                        "WHERE c.project_id=? ORDER BY c.chapter_number DESC LIMIT ?",
                        (project_id, recent_limit),
                    )
                ).fetchall()
            )
            canon = await (
                await connection.execute(
                    "SELECT payload_json FROM canon_snapshots WHERE project_id=? "
                    "ORDER BY version DESC LIMIT 1",
                    (project_id,),
                )
            ).fetchone()
            pending = await (
                await connection.execute(
                    "SELECT chapter_number FROM rewrite_queue WHERE project_id=? AND status='pending'",
                    (project_id,),
                )
            ).fetchall()
            latest_review = await (
                await connection.execute(
                    "SELECT boundary,verdict,dimensions_json,evidence_json FROM reviews "
                    "WHERE project_id=? ORDER BY boundary DESC,revision DESC LIMIT 1",
                    (project_id,),
                )
            ).fetchone()
            all_chapters = list(
                await (
                    await connection.execute(
                        "SELECT c.chapter_number,c.title,f.facts_json FROM chapters c "
                        "JOIN chapter_facts f ON f.project_id=c.project_id "
                        "AND f.chapter_number=c.chapter_number AND f.revision=c.revision "
                        "WHERE c.project_id=? ORDER BY c.chapter_number",
                        (project_id,),
                    )
                ).fetchall()
            )
        finally:
            await connection.close()
        recent_numbers = {int(row["chapter_number"]) for row in chapters}
        relevant = [
            {
                "number": int(row["chapter_number"]),
                "title": str(row["title"]),
                "facts": decode_json(row["facts_json"]),
                "selection_basis": "nearest_prior_outside_recent_window",
            }
            for row in reversed(all_chapters)
            if int(row["chapter_number"]) not in recent_numbers
        ][:3]
        return {
            "brief": project["brief"],
            "title": project["title"],
            "target": decode_json(project["target_json"]),
            "foundation": None if plan is None else decode_json(plan["payload_json"]),
            "planned_through": 0 if plan is None else plan["planned_through"],
            "recent_chapters": [
                {
                    "number": row["chapter_number"],
                    "title": row["title"],
                    "facts": None if row["facts_json"] is None else decode_json(row["facts_json"]),
                }
                for row in reversed(chapters)
            ],
            "canon": {} if canon is None else decode_json(canon["payload_json"]),
            "pending_rewrites": [row["chapter_number"] for row in pending],
            "relevant_chapters": relevant,
            "latest_review": {}
            if latest_review is None
            else {
                "boundary": latest_review["boundary"],
                "verdict": latest_review["verdict"],
                "dimensions": decode_json(latest_review["dimensions_json"]),
                "evidence": decode_json(latest_review["evidence_json"]),
            },
        }

    async def chapter_for_instruction(
        self,
        project_id: str,
        instruction_key: str,
        chapter_number: int,
        *,
        allow_unstarted: bool = False,
    ) -> dict[str, Any]:
        """Read the authorized in-progress version; formal chapter history remains immutable."""
        connection = await self._connect()
        unstarted = False
        try:
            versions = await (
                await connection.execute(
                    "SELECT v.kind,v.content_sha256,b.content FROM chapter_versions v "
                    "JOIN content_blobs b ON b.sha256=v.content_sha256 WHERE v.project_id=? "
                    "AND v.instruction_key=? AND v.chapter_number=? ORDER BY v.id",
                    (project_id, instruction_key, chapter_number),
                )
            ).fetchall()
            if allow_unstarted and not versions:
                # Only the actual active new-chapter target can be absent by design.
                # A broken blob or another instruction's saved work is not an empty chapter.
                row = await (
                    await connection.execute(
                        "SELECT 1 FROM run_state r WHERE r.project_id=? AND r.active_instruction_key=? "
                        "AND r.active_instruction_kind='write_chapter' AND r.active_logical_target=? "
                        "AND NOT EXISTS(SELECT 1 FROM chapters c WHERE c.project_id=r.project_id AND c.chapter_number=?) "
                        "AND NOT EXISTS(SELECT 1 FROM chapter_versions v WHERE v.project_id=r.project_id AND v.chapter_number=?) "
                        "AND NOT EXISTS(SELECT 1 FROM checkpoints c WHERE c.project_id=r.project_id "
                        "AND c.logical_target=r.active_logical_target "
                        "AND c.step IN ('plan_chapter','draft_chapter','edit_chapter',"
                        "'check_consistency','commit_chapter'))",
                        (
                            project_id,
                            instruction_key,
                            f"chapter:{chapter_number}",
                            chapter_number,
                            chapter_number,
                        ),
                    )
                ).fetchone()
                unstarted = row is not None
        finally:
            await connection.close()
        if not versions:
            if unstarted:
                return {
                    "chapter_number": chapter_number,
                    "status": "not_started",
                    "plan": None,
                    "content": None,
                }
            return await self.chapter(project_id, chapter_number)
        result: dict[str, Any] = {"chapter_number": chapter_number, "content": None}
        for version in versions:
            body = decode_text_blob(version["content"])
            if content_hash(body) != version["content_sha256"]:
                raise StateCorruptionError("saved chapter version does not match its content hash")
            if version["kind"] == "plan":
                result["plan"] = body
            else:
                result.update(
                    {
                        "content": body,
                        "content_sha256": version["content_sha256"],
                        "kind": version["kind"],
                        "instruction_key": instruction_key,
                    }
                )
        try:
            committed = await self.chapter(project_id, chapter_number)
        except KeyError:
            if allow_unstarted:
                return result
            raise
        if result["content"] is None:
            return {**committed, "plan": result.get("plan")}
        return {**committed, **result}

    async def export_snapshot(self, project_id: str, format: str) -> tuple[str, str]:
        if format not in {"markdown", "txt"}:
            raise ValueError("format must be markdown or txt")
        async with self.write_transaction() as connection:
            project = await (
                await connection.execute("SELECT title FROM projects WHERE id=?", (project_id,))
            ).fetchone()
            if project is None:
                raise KeyError(project_id)
            rows = await (
                await connection.execute(
                    "SELECT c.chapter_number,c.title,c.revision,c.content_sha256,b.content "
                    "FROM chapters c JOIN content_blobs b ON b.sha256=c.content_sha256 "
                    "WHERE c.project_id=? ORDER BY c.chapter_number",
                    (project_id,),
                )
            ).fetchall()
            title = str(project["title"] or "Untitled Novel")
            parts = [f"# {title}", ""]
            for row in rows:
                parts.extend(
                    [
                        f"## 第{row['chapter_number']}章 {row['title']}",
                        "",
                        decode_text_blob(row["content"]).strip(),
                        "",
                    ]
                )
            markdown = "\n".join(parts).rstrip() + "\n"
            if format == "markdown":
                document = markdown
                media_type = "text/markdown; charset=utf-8"
            else:
                document = (
                    "\n".join(
                        line.removeprefix("# ").removeprefix("## ")
                        for line in markdown.splitlines()
                    ).rstrip()
                    + "\n"
                )
                media_type = "text/plain; charset=utf-8"
            digest = content_hash(document)
            source_fingerprint = content_hash(
                {
                    "title": title,
                    "chapters": [
                        {
                            "number": row["chapter_number"],
                            "revision": row["revision"],
                            "content_sha256": row["content_sha256"],
                        }
                        for row in rows
                    ],
                }
            )
            encoded = document.encode("utf-8")
            await connection.execute(
                "INSERT OR IGNORE INTO content_blobs(sha256,media_type,byte_length,content,created_at) "
                "VALUES (?,?,?,?,?)",
                (digest, media_type, len(encoded), encoded, utc_now()),
            )
            manifest = await connection.execute(
                "INSERT OR IGNORE INTO export_manifests(project_id,format,source_fingerprint,"
                "content_sha256,byte_length,created_at) VALUES (?,?,?,?,?,?)",
                (project_id, format, source_fingerprint, digest, len(encoded), utc_now()),
            )
            if manifest.rowcount:
                await self.append_event(
                    connection,
                    project_id,
                    "export_snapshot_created",
                    {"format": format, "sha256": digest, "source": source_fingerprint},
                )
            return document, digest

    async def scalar(self, query: str, parameters: tuple[Any, ...] = ()) -> Any:
        """Small read-only seam for diagnostics and contract tests."""
        connection = await self._connect()
        try:
            row = await (await connection.execute(query, parameters)).fetchone()
        finally:
            await connection.close()
        return None if row is None else row[0]

    async def start_episode(
        self,
        *,
        episode_id: str,
        project_id: str,
        worker: str,
        instruction_key: str,
        profile_snapshot: Mapping[str, Any],
        instruction_kind: str = "",
        logical_target: str = "",
        fallback_profile_snapshot: Mapping[str, Any] | None = None,
    ) -> None:
        async with self.write_transaction() as connection:
            await connection.execute(
                "INSERT INTO worker_episodes(id,project_id,worker,instruction_key,instruction_kind,"
                "logical_target,profile_snapshot_json,fallback_profile_snapshot_json,status,"
                "started_at) VALUES (?,?,?,?,?,?,?,?,'running',?)",
                (
                    episode_id,
                    project_id,
                    worker,
                    instruction_key,
                    instruction_kind,
                    logical_target,
                    encode_json(dict(profile_snapshot)),
                    None
                    if fallback_profile_snapshot is None
                    else encode_json(dict(fallback_profile_snapshot)),
                    utc_now(),
                ),
            )
            await self.append_event(
                connection,
                project_id,
                "episode_started",
                {"episode_id": episode_id, "worker": worker, "instruction_key": instruction_key},
            )

    async def finish_episode(
        self,
        episode_id: str,
        project_id: str,
        *,
        succeeded: bool,
        failure: str | None = None,
        usage: Mapping[str, Any] | None = None,
        instruction: Instruction | None = None,
        lease_owner: str | None = None,
    ) -> None:
        status = "completed" if succeeded else "failed"
        async with self.write_transaction() as connection:
            if succeeded and instruction is not None:
                state = await (
                    await connection.execute(
                        "SELECT * FROM run_state WHERE project_id=?",
                        (project_id,),
                    )
                ).fetchone()
                if state is None:
                    raise KeyError(project_id)
                if lease_owner is not None:
                    self._require_lease(state, lease_owner)
                if state["status"] == "cancelled":
                    raise TerminalPostconditionError(
                        "cancelled work cannot be acknowledged as success"
                    )
                if state["active_instruction_key"] != instruction.instruction_key:
                    raise StateCorruptionError(
                        "terminal acknowledgement belongs to an inactive instruction"
                    )
                if not await self._terminal_complete(
                    connection,
                    project_id,
                    instruction.instruction_key,
                    instruction.kind.value,
                    instruction.logical_target,
                ):
                    raise TerminalPostconditionError(
                        "Worker ended without authoritative terminal facts"
                    )
            cursor = await connection.execute(
                "UPDATE worker_episodes SET status=?,ended_at=?,failure=? WHERE id=? AND status='running'",
                (status, utc_now(), failure, episode_id),
            )
            if cursor.rowcount != 1:
                raise StateCorruptionError("episode cannot be terminalized exactly once")
            if usage is not None:
                await connection.execute(
                    "INSERT INTO model_usage(project_id,episode_id,request_index,profile_fingerprint,"
                    "input_tokens,output_tokens,cache_tokens,latency_ms,cost_microunits,"
                    "metadata_json,created_at) VALUES (?,?,1,?,?,?,?,?,?,?,?)",
                    (
                        project_id,
                        episode_id,
                        usage["profile_fingerprint"],
                        usage["input_tokens"],
                        usage["output_tokens"],
                        usage["cache_tokens"],
                        usage["latency_ms"],
                        usage["cost_microunits"],
                        encode_json(dict(usage["metadata"])),
                        utc_now(),
                    ),
                )
            await self.append_event(
                connection,
                project_id,
                f"episode_{status}",
                {"episode_id": episode_id, **({} if failure is None else {"failure": failure})},
            )
            if succeeded and instruction is not None:
                await self._clear_active_instruction(connection, project_id)

    async def instruction_episode_count(self, project_id: str, instruction_key: str) -> int:
        value = await self.scalar(
            "SELECT count(*) FROM worker_episodes WHERE project_id=? AND instruction_key=?",
            (project_id, instruction_key),
        )
        return int(value or 0)

    async def instruction_failure_count(self, project_id: str, instruction_key: str) -> int:
        value = await self.scalar(
            "SELECT count(*) FROM worker_episodes WHERE project_id=? AND instruction_key=? "
            "AND status IN ('failed','interrupted')",
            (project_id, instruction_key),
        )
        return int(value or 0)

    async def has_checkpoint(
        self, project_id: str, instruction_key: str, step: str, logical_target: str
    ) -> bool:
        value = await self.scalar(
            "SELECT 1 FROM checkpoints WHERE project_id=? AND instruction_key=? "
            "AND step=? AND logical_target=?",
            (project_id, instruction_key, step, logical_target),
        )
        return value is not None

    async def profile_bindings(self, project_id: str) -> dict[str, str]:
        connection = await self._connect()
        try:
            row = await (
                await connection.execute(
                    "SELECT profile_bindings_json FROM projects WHERE id=?", (project_id,)
                )
            ).fetchone()
        finally:
            await connection.close()
        if row is None:
            raise KeyError(project_id)
        value = decode_json(row["profile_bindings_json"])
        if not isinstance(value, dict) or any(
            not isinstance(key, str) or not isinstance(item, str) for key, item in value.items()
        ):
            raise StateCorruptionError("project Profile bindings are malformed")
        return dict(value)

    async def update_profile_bindings(self, project_id: str, bindings: Mapping[str, str]) -> None:
        async with self.write_transaction() as connection:
            state = await (
                await connection.execute(
                    "SELECT status,active_instruction_key FROM run_state WHERE project_id=?",
                    (project_id,),
                )
            ).fetchone()
            if state is None:
                raise KeyError(project_id)
            if state["active_instruction_key"] is not None and state["status"] not in {
                RunStatus.PAUSED.value,
                RunStatus.FAILURE_PAUSED.value,
            }:
                raise ValueError("Profile bindings may change only at an Episode boundary or pause")
            await connection.execute(
                "UPDATE projects SET profile_bindings_json=? WHERE id=?",
                (encode_json(dict(bindings)), project_id),
            )
            await self.append_event(
                connection,
                project_id,
                "profile_bindings_updated",
                {"roles": sorted(bindings)},
            )

    async def restore_material(
        self, project_id: str, logical_target: str, instruction_key: str
    ) -> dict[str, Any]:
        context = await self.novel_context(project_id)
        connection = await self._connect()
        try:
            checkpoints = await (
                await connection.execute(
                    "SELECT step FROM checkpoints WHERE project_id=? AND instruction_key=? AND logical_target=? "
                    "ORDER BY created_at",
                    (project_id, instruction_key, logical_target),
                )
            ).fetchall()
            summaries = await (
                await connection.execute(
                    "SELECT kind,boundary,payload_json FROM summaries WHERE project_id=? "
                    "ORDER BY boundary DESC LIMIT 3",
                    (project_id,),
                )
            ).fetchall()
            chapter_plan: str | None = None
            current_work: dict[str, object] = {}
            saved_steps = {row["step"] for row in checkpoints}
            if logical_target.startswith("chapter:"):
                number = int(logical_target.removeprefix("chapter:"))
                current_work = {"plan_status": "not_started", "content_status": "not_started"}
                plan_row = await (
                    await connection.execute(
                        "SELECT b.content,v.content_sha256 FROM chapter_versions v JOIN content_blobs b "
                        "ON b.sha256=v.content_sha256 WHERE v.project_id=? "
                        "AND v.chapter_number=? AND v.instruction_key=? AND v.kind='plan' "
                        "ORDER BY v.version DESC LIMIT 1",
                        (project_id, number, instruction_key),
                    )
                ).fetchone()
                if plan_row is None and "plan_chapter" in saved_steps:
                    raise StateCorruptionError(
                        "saved chapter plan checkpoint has no readable version"
                    )
                if plan_row is not None:
                    chapter_plan = decode_text_blob(plan_row["content"])
                    if content_hash(chapter_plan) != plan_row["content_sha256"]:
                        raise StateCorruptionError(
                            "saved chapter plan does not match its content hash"
                        )
                    current_work["plan_sha256"] = plan_row["content_sha256"]
                    current_work["plan_status"] = "saved"
                    if len(chapter_plan) > 4000:
                        current_work["plan_status"] = "omitted"
                        current_work["plan_reread"] = {
                            "tool": "read_chapter",
                            "chapter_number": number,
                        }
                        chapter_plan = (
                            chapter_plan[:4000]
                            + " [truncated; read_chapter returns the full saved plan]"
                        )
                draft = await (
                    await connection.execute(
                        "SELECT v.kind,v.content_sha256,b.content FROM chapter_versions v "
                        "JOIN content_blobs b ON b.sha256=v.content_sha256 WHERE v.project_id=? "
                        "AND v.chapter_number=? AND v.instruction_key=? AND v.kind IN ('draft','edit') "
                        "ORDER BY v.id DESC LIMIT 1",
                        (project_id, number, instruction_key),
                    )
                ).fetchone()
                if draft is None and saved_steps.intersection({"draft_chapter", "edit_chapter"}):
                    raise StateCorruptionError(
                        "saved chapter content checkpoint has no readable version"
                    )
                if draft is not None:
                    body = decode_text_blob(draft["content"])
                    if content_hash(body) != draft["content_sha256"]:
                        raise StateCorruptionError(
                            "saved chapter draft does not match its content hash"
                        )
                    current_work.update(
                        {
                            "kind": draft["kind"],
                            "content_sha256": draft["content_sha256"],
                            "content_characters": len(body),
                            "content": body if len(body) <= 8000 else None,
                            "content_status": "saved" if len(body) <= 8000 else "omitted",
                        }
                    )
                    if len(body) > 8000:
                        current_work["reread"] = {"tool": "read_chapter", "chapter_number": number}
                committed = await (
                    await connection.execute(
                        "SELECT content_sha256 FROM chapters WHERE project_id=? AND chapter_number=?",
                        (project_id, number),
                    )
                ).fetchone()
                if committed is not None:
                    current_work["committed_content_sha256"] = committed["content_sha256"]
                    current_work["committed_reread"] = {
                        "tool": "read_chapter",
                        "chapter_number": number,
                    }
        finally:
            await connection.close()
        foundation = context.get("foundation")
        outline: list[str] = []
        if isinstance(foundation, dict):
            for name in ("outline", "outline_extension"):
                value = foundation.get(name, [])
                if isinstance(value, list):
                    outline.extend(str(item) for item in value)
        target_number = 1
        if logical_target.startswith("chapter:"):
            target_number = int(logical_target.removeprefix("chapter:"))
        current_outline = (
            outline[target_number - 1]
            if 0 < target_number <= len(outline)
            else f"Authorized target: {logical_target}"
        )
        next_outline = outline[target_number] if target_number < len(outline) else None
        canon = context.get("canon")
        if not isinstance(canon, dict) or not canon:
            canon = {"project_brief": context["brief"]}
        pending = context.get("pending_rewrites", [])
        review_evidence = context["latest_review"]
        return {
            "chapter_plan": chapter_plan or current_outline[:2000],
            "current_outline": current_outline[:2000],
            "next_outline": None if next_outline is None else next_outline[:2000],
            "canon": self._bounded_restore_fact(canon, 6000),
            "review_tasks": tuple(f"rewrite chapter {item}" for item in pending[:10]),
            "review_evidence": self._bounded_restore_fact(review_evidence, 3000),
            "relevant_history": tuple(
                self._bounded_restore_fact(item, 1500)
                for item in context.get("relevant_chapters", [])[:3]
            ),
            "successful_checkpoints": tuple(row["step"] for row in checkpoints),
            "authorization_boundary": f"Only {logical_target} may be mutated.",
            "current_work": current_work,
            "project_context": {
                "brief": str(context["brief"])[:2000],
                "title": context["title"],
                "target": context["target"],
                "foundation": self._bounded_restore_fact(foundation or {}, 6000),
            },
            "stored_summary": "\n".join(
                f"{row['kind']}@{row['boundary']}: {decode_json(row['payload_json']).get('summary', '')[:1500]}"
                for row in summaries
            ),
        }

    @staticmethod
    def _bounded_restore_fact(value: dict[str, Any], limit: int) -> dict[str, Any]:
        encoded = encode_json(value)
        if len(encoded) <= limit:
            return value
        return {"excerpt": encoded[:limit], "truncated": True, "reread": "novel_context"}

    async def record_context_compaction(
        self,
        project_id: str,
        episode_id: str,
        tokens_before: int,
        tokens_after: int,
        strategies: tuple[str, ...],
    ) -> None:
        async with self.write_transaction() as connection:
            await self.append_event(
                connection,
                project_id,
                "context_compacted",
                {
                    "episode_id": episode_id,
                    "tokens_before": tokens_before,
                    "tokens_after": tokens_after,
                    "strategies": list(strategies),
                },
            )

    async def record_context_compaction_failure(
        self, project_id: str, episode_id: str, error: str
    ) -> int:
        async with self.write_transaction() as connection:
            await connection.execute(
                "UPDATE worker_episodes SET compaction_failure_count=compaction_failure_count+1 "
                "WHERE id=? AND project_id=?",
                (episode_id, project_id),
            )
            row = await (
                await connection.execute(
                    "SELECT sum(compaction_failure_count) AS failures FROM worker_episodes "
                    "WHERE project_id=? AND instruction_key=(SELECT instruction_key "
                    "FROM worker_episodes WHERE id=?)",
                    (project_id, episode_id),
                )
            ).fetchone()
            if row is None:
                raise KeyError(episode_id)
            count = int(row["failures"] or 0)
            await self.append_event(
                connection,
                project_id,
                "context_compaction_failed",
                {"episode_id": episode_id, "attempt": count, "error": error},
            )
            return count

    async def record_runtime_event(
        self, project_id: str, kind: str, payload: Mapping[str, Any]
    ) -> None:
        async with self.write_transaction() as connection:
            await self.append_event(connection, project_id, kind, payload)

    async def execution_evidence(self, project_id: str) -> dict[str, Any]:
        connection = await self._connect()
        try:
            episodes = await (
                await connection.execute(
                    "SELECT id,worker,instruction_key,instruction_kind,logical_target,"
                    "profile_snapshot_json,fallback_profile_snapshot_json,status,"
                    "compaction_failure_count FROM worker_episodes "
                    "WHERE project_id=? ORDER BY started_at",
                    (project_id,),
                )
            ).fetchall()
            usage = await (
                await connection.execute(
                    "SELECT episode_id,profile_fingerprint,input_tokens,output_tokens,cache_tokens,"
                    "latency_ms,cost_microunits,metadata_json FROM model_usage WHERE project_id=? "
                    "ORDER BY id",
                    (project_id,),
                )
            ).fetchall()
            requests = await (
                await connection.execute(
                    "SELECT episode_id,request_index,profile_fingerprint,purpose,status,input_tokens,"
                    "output_tokens,cache_read_tokens,cache_write_tokens,latency_ms,"
                    "cost_microunits,retry_reason,retry_after_ms,backoff_ms,error_type,metadata_json "
                    "FROM model_requests WHERE project_id=? ORDER BY id",
                    (project_id,),
                )
            ).fetchall()
        finally:
            await connection.close()
        return {
            "episodes": [
                {
                    "id": row["id"],
                    "worker": row["worker"],
                    "instruction_key": row["instruction_key"],
                    "instruction_kind": row["instruction_kind"],
                    "logical_target": row["logical_target"],
                    "profile_snapshot": decode_json(row["profile_snapshot_json"]),
                    "fallback_profile_snapshot": (
                        None
                        if row["fallback_profile_snapshot_json"] is None
                        else decode_json(row["fallback_profile_snapshot_json"])
                    ),
                    "status": row["status"],
                    "compaction_failure_count": row["compaction_failure_count"],
                }
                for row in episodes
            ],
            "usage": [
                {
                    "episode_id": row["episode_id"],
                    "profile_fingerprint": row["profile_fingerprint"],
                    "input_tokens": row["input_tokens"],
                    "output_tokens": row["output_tokens"],
                    "cache_tokens": row["cache_tokens"],
                    "latency_ms": row["latency_ms"],
                    "cost_microunits": row["cost_microunits"],
                    "metadata": decode_json(row["metadata_json"]),
                }
                for row in usage
            ],
            "requests": [
                {
                    "episode_id": row["episode_id"],
                    "request_index": row["request_index"],
                    "profile_fingerprint": row["profile_fingerprint"],
                    "purpose": row["purpose"],
                    "status": row["status"],
                    "input_tokens": row["input_tokens"],
                    "output_tokens": row["output_tokens"],
                    "cache_read_tokens": row["cache_read_tokens"],
                    "cache_write_tokens": row["cache_write_tokens"],
                    "latency_ms": row["latency_ms"],
                    "cost_microunits": row["cost_microunits"],
                    "retry_reason": row["retry_reason"],
                    "retry_after_ms": row["retry_after_ms"],
                    "backoff_ms": row["backoff_ms"],
                    "error_type": row["error_type"],
                    "metadata": decode_json(row["metadata_json"]),
                }
                for row in requests
            ],
        }

    async def retry_delay_for_episode(self, episode_id: str, failure_count: int) -> int | None:
        connection = await self._connect()
        try:
            row = await (
                await connection.execute(
                    "SELECT id,retry_after_ms FROM model_requests WHERE episode_id=? "
                    "AND status='failed' AND retry_reason IS NOT NULL ORDER BY id DESC LIMIT 1",
                    (episode_id,),
                )
            ).fetchone()
        finally:
            await connection.close()
        if row is None:
            return None
        requested = row["retry_after_ms"]
        delay_ms = (
            int(requested)
            if requested is not None
            else min(500 * 2 ** max(0, failure_count - 1), 8_000)
        )
        async with self.write_transaction() as connection:
            await connection.execute(
                "UPDATE model_requests SET backoff_ms=? WHERE id=?",
                (delay_ms, row["id"]),
            )
        return delay_ms

    async def record_model_request(
        self,
        *,
        project_id: str,
        episode_id: str,
        request_index: int,
        profile_fingerprint: str,
        purpose: str,
        status: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        latency_ms: int = 0,
        cost_microunits: int = 0,
        retry_reason: str | None = None,
        retry_after_ms: int | None = None,
        backoff_ms: int | None = None,
        error_type: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        async with self.write_transaction() as connection:
            await connection.execute(
                "INSERT INTO model_requests(project_id,episode_id,request_index,"
                "profile_fingerprint,purpose,status,input_tokens,output_tokens,cache_read_tokens,"
                "cache_write_tokens,latency_ms,cost_microunits,retry_reason,retry_after_ms,"
                "backoff_ms,error_type,metadata_json,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    project_id,
                    episode_id,
                    request_index,
                    profile_fingerprint,
                    purpose,
                    status,
                    input_tokens,
                    output_tokens,
                    cache_read_tokens,
                    cache_write_tokens,
                    latency_ms,
                    cost_microunits,
                    retry_reason,
                    retry_after_ms,
                    backoff_ms,
                    error_type,
                    encode_json(dict(metadata or {})),
                    utc_now(),
                ),
            )

    async def record_failure_decision(
        self,
        project_id: str,
        instruction_key: str,
        input_value: Mapping[str, Any],
        decision: Mapping[str, Any],
    ) -> None:
        async with self.write_transaction() as connection:
            await connection.execute(
                "INSERT INTO decisions(project_id,instruction_key,kind,input_json,decision_json,"
                "created_at) VALUES (?,?,'failure_arbiter',?,?,?)",
                (
                    project_id,
                    instruction_key,
                    encode_json(dict(input_value)),
                    encode_json(dict(decision)),
                    utc_now(),
                ),
            )
            await self.append_event(
                connection,
                project_id,
                "failure_decided",
                {"instruction_key": instruction_key, **dict(decision)},
            )

    async def arbiter_retry_count(self, project_id: str, instruction_key: str) -> int:
        value = await self.scalar(
            "SELECT count(*) FROM decisions WHERE project_id=? AND instruction_key=? "
            "AND kind='failure_arbiter' AND json_extract(decision_json,'$.action')='retry_once'",
            (project_id, instruction_key),
        )
        return int(value or 0)
