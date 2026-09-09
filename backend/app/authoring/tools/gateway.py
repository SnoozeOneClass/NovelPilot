from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import aiosqlite
from pydantic_ai.models import Model

from app.authoring.domain.models import (
    AuthoringProfileSnapshot,
    Instruction,
    InstructionKind,
    RunStatus,
    WorkerRole,
    content_hash,
)
from app.authoring.errors import ToolAuthorizationError, ToolConflictError
from app.authoring.store.store import (
    CHAPTER_COMMIT_PAYLOAD_VERSION,
    AuthoringStore,
    decode_json,
    encode_json,
    utc_now,
)
from app.authoring.tools.contracts import ToolInput, validate_tool_input

ToolMutation = Callable[[aiosqlite.Connection, Mapping[str, Any]], Awaitable[dict[str, Any]]]

ROLE_TOOLS: dict[WorkerRole, frozenset[str]] = {
    WorkerRole.ARCHITECT: frozenset(
        {
            "novel_context",
            "save_book",
            "save_foundation",
            "audit_foundation",
            "revise_outline",
            "resolve_outline_feedback",
            "complete_book",
        }
    ),
    WorkerRole.WRITER: frozenset(
        {
            "novel_context",
            "read_chapter",
            "plan_chapter",
            "draft_chapter",
            "edit_chapter",
            "check_consistency",
            "commit_chapter",
        }
    ),
    WorkerRole.EDITOR: frozenset(
        {
            "novel_context",
            "read_chapter",
            "save_review",
            "save_arc_summary",
            "save_volume_summary",
        }
    ),
    WorkerRole.ARBITER: frozenset({"novel_context"}),
}

INSTRUCTION_TOOLS: dict[InstructionKind, frozenset[str]] = {
    InstructionKind.CREATE_FOUNDATION: frozenset(
        {"novel_context", "save_book", "save_foundation", "audit_foundation"}
    ),
    InstructionKind.WRITE_CHAPTER: frozenset(
        {
            "novel_context",
            "read_chapter",
            "plan_chapter",
            "draft_chapter",
            "edit_chapter",
            "check_consistency",
            "commit_chapter",
        }
    ),
    InstructionKind.REWRITE_CHAPTER: frozenset(
        {
            "novel_context",
            "read_chapter",
            "plan_chapter",
            "draft_chapter",
            "edit_chapter",
            "check_consistency",
            "commit_chapter",
        }
    ),
    InstructionKind.REVIEW_BOUNDARY: frozenset({"novel_context", "read_chapter", "save_review"}),
    InstructionKind.SAVE_SUMMARY: frozenset(
        {"novel_context", "read_chapter", "save_arc_summary", "save_volume_summary"}
    ),
    InstructionKind.EXTEND_OUTLINE: frozenset(
        {"novel_context", "revise_outline", "resolve_outline_feedback"}
    ),
    InstructionKind.COMPLETE_BOOK: frozenset({"novel_context", "complete_book"}),
}

TERMINAL_TOOL: dict[InstructionKind, str] = {
    InstructionKind.CREATE_FOUNDATION: "audit_foundation",
    InstructionKind.WRITE_CHAPTER: "commit_chapter",
    InstructionKind.REWRITE_CHAPTER: "commit_chapter",
    InstructionKind.REVIEW_BOUNDARY: "save_review",
    InstructionKind.SAVE_SUMMARY: "save_arc_summary",
    InstructionKind.EXTEND_OUTLINE: "revise_outline",
    InstructionKind.COMPLETE_BOOK: "complete_book",
}


@dataclass(slots=True)
class EpisodeDeps:
    store: AuthoringStore
    instruction: Instruction
    profile: AuthoringProfileSnapshot
    lease_owner: str | None = None
    model: Model | None = None
    fallback_model: Model | None = None
    fallback_profile: AuthoringProfileSnapshot | None = None
    episode_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    successful_tools: list[str] = field(default_factory=list)
    tool_side_effects: bool = False
    model_output_started: bool = False
    model_request_count: int = 0
    tool_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def project_id(self) -> str:
        return self.instruction.project_id

    @property
    def worker(self) -> WorkerRole:
        return self.instruction.worker

    @property
    def terminal_tool(self) -> str:
        return TERMINAL_TOOL[self.instruction.kind]

    def next_model_request(self) -> int:
        self.model_request_count += 1
        return self.model_request_count


class ToolGateway:
    """Role-scoped Tool API with transactionally durable idempotency evidence."""

    def __init__(self, store: AuthoringStore) -> None:
        self.store = store

    async def invoke(
        self,
        deps: EpisodeDeps,
        tool_name: str,
        payload: ToolInput | dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            validated = validate_tool_input(tool_name, payload)
            arguments = validated.model_dump(mode="python", exclude_none=True)
            self._authorize(deps, tool_name, arguments)
            if tool_name == "novel_context":
                return await self.store.novel_context(deps.project_id)
            if tool_name == "read_chapter":
                number = self._positive_int(arguments, "chapter_number")
                if deps.worker is WorkerRole.WRITER:
                    return await self.store.chapter_for_instruction(
                        deps.project_id,
                        deps.instruction.instruction_key,
                        number,
                        allow_unstarted=deps.instruction.kind is InstructionKind.WRITE_CHAPTER,
                    )
                return await self.store.chapter(deps.project_id, number)

            mutation = self._mutation(deps, tool_name)
            async with deps.tool_lock:
                result = await self._write(deps, tool_name, arguments, mutation)
            if tool_name not in deps.successful_tools:
                deps.successful_tools.append(tool_name)
            deps.tool_side_effects = True
            return result
        except Exception as error:
            await self.store.record_runtime_event(
                deps.project_id,
                "tool_failed",
                {
                    "episode_id": deps.episode_id,
                    "tool": tool_name,
                    "error_type": type(error).__name__,
                },
            )
            raise

    def _authorize(
        self,
        deps: EpisodeDeps,
        tool_name: str,
        payload: Mapping[str, Any],
    ) -> None:
        if tool_name not in ROLE_TOOLS[deps.worker]:
            raise ToolAuthorizationError(f"{deps.worker.value} is not allowed to call {tool_name}")
        if tool_name not in INSTRUCTION_TOOLS[deps.instruction.kind]:
            raise ToolAuthorizationError(
                f"{tool_name} is outside {deps.instruction.kind.value} instruction scope"
            )
        if "project_id" in payload:
            raise ToolAuthorizationError("project identity comes from EpisodeDeps")
        if "chapter_number" in payload and deps.worker is WorkerRole.WRITER:
            number = self._positive_int(payload, "chapter_number")
            expected = self._target_number(deps.instruction.logical_target, "chapter")
            if number != expected:
                raise ToolAuthorizationError(
                    f"chapter target {payload['chapter_number']} is outside {deps.instruction.logical_target}"
                )
        if "chapter_number" in payload and deps.worker is WorkerRole.EDITOR:
            number = self._positive_int(payload, "chapter_number")
            boundary = self._target_number(deps.instruction.logical_target, "boundary")
            if number > boundary:
                raise ToolAuthorizationError(
                    f"chapter target {number} is outside reviewed boundary {boundary}"
                )
        if "boundary" in payload and deps.worker is WorkerRole.EDITOR:
            boundary = self._positive_int(payload, "boundary")
            expected = self._target_number(deps.instruction.logical_target, "boundary")
            if boundary != expected:
                raise ToolAuthorizationError("review/summary boundary is outside instruction")

    @staticmethod
    def _target_number(logical_target: str, prefix: str) -> int:
        expected_prefix = f"{prefix}:"
        if not logical_target.startswith(expected_prefix):
            raise ToolAuthorizationError(f"expected a {prefix} logical target")
        try:
            return int(logical_target.removeprefix(expected_prefix))
        except ValueError as error:
            raise ToolAuthorizationError("logical target is malformed") from error

    @staticmethod
    def _positive_int(payload: Mapping[str, Any], name: str) -> int:
        value = payload.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{name} must be a positive integer")
        return value

    async def _write(
        self,
        deps: EpisodeDeps,
        tool_name: str,
        payload: Mapping[str, Any],
        mutation: ToolMutation,
    ) -> dict[str, Any]:
        payload_digest = content_hash(payload)
        identity = content_hash(
            {
                "project": deps.project_id,
                "instruction": deps.instruction.instruction_key,
                "step": tool_name,
                "target": deps.instruction.logical_target,
                "payload": payload_digest,
            }
        )
        started = time.perf_counter()
        async with self.store.write_transaction() as connection:
            run_state = await (
                await connection.execute(
                    "SELECT status,active_instruction_key,lease_owner FROM run_state "
                    "WHERE project_id=?",
                    (deps.project_id,),
                )
            ).fetchone()
            if run_state is None:
                raise KeyError(deps.project_id)
            status = RunStatus(run_state["status"])
            if status in {RunStatus.CANCELLED, RunStatus.COMPLETED}:
                raise ToolAuthorizationError(f"project is {status.value}; writes are closed")
            if run_state["active_instruction_key"] != deps.instruction.instruction_key:
                raise ToolAuthorizationError("Tool call belongs to a stale or inactive instruction")
            if deps.lease_owner is not None and run_state["lease_owner"] != deps.lease_owner:
                raise ToolAuthorizationError("Tool call belongs to an Engine that lost its lease")
            replay = await (
                await connection.execute(
                    "SELECT result_json FROM tool_invocations WHERE idempotency_key=?",
                    (identity,),
                )
            ).fetchone()
            if replay is not None:
                await self.store.append_event(
                    connection,
                    deps.project_id,
                    "tool_replayed",
                    {"episode_id": deps.episode_id, "tool": tool_name},
                )
                return dict(decode_json(replay["result_json"]))
            prior = await (
                await connection.execute(
                    "SELECT payload_hash, result_json FROM checkpoints WHERE project_id=? "
                    "AND instruction_key=? AND step=? AND logical_target=?",
                    (
                        deps.project_id,
                        deps.instruction.instruction_key,
                        tool_name,
                        deps.instruction.logical_target,
                    ),
                )
            ).fetchone()
            if prior is not None:
                if prior["payload_hash"] == payload_digest:
                    await self.store.append_event(
                        connection,
                        deps.project_id,
                        "tool_replayed",
                        {"episode_id": deps.episode_id, "tool": tool_name},
                    )
                    return dict(decode_json(prior["result_json"]))
                raise ToolConflictError(
                    f"{tool_name} already committed different payload for this instruction"
                )

            await self.store.append_event(
                connection,
                deps.project_id,
                "tool_started",
                {"episode_id": deps.episode_id, "tool": tool_name},
            )
            result = await mutation(connection, payload)
            encoded = encode_json(result)
            await connection.execute(
                "INSERT INTO checkpoints(project_id,instruction_key,step,logical_target,"
                "payload_hash,result_json,created_at) VALUES (?,?,?,?,?,?,?)",
                (
                    deps.project_id,
                    deps.instruction.instruction_key,
                    tool_name,
                    deps.instruction.logical_target,
                    payload_digest,
                    encoded,
                    utc_now(),
                ),
            )
            await connection.execute(
                "INSERT INTO tool_invocations(project_id,episode_id,instruction_key,tool_name,"
                "logical_target,idempotency_key,payload_hash,result_json,duration_ms,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    deps.project_id,
                    deps.episode_id,
                    deps.instruction.instruction_key,
                    tool_name,
                    deps.instruction.logical_target,
                    identity,
                    payload_digest,
                    encoded,
                    int((time.perf_counter() - started) * 1000),
                    utc_now(),
                ),
            )
            await self.store.append_event(
                connection,
                deps.project_id,
                "tool_completed",
                {"episode_id": deps.episode_id, "tool": tool_name},
            )
            return result

    def _mutation(self, deps: EpisodeDeps, tool_name: str) -> ToolMutation:
        handlers: dict[str, ToolMutation] = {
            "save_book": lambda connection, payload: self._save_book(connection, deps, payload),
            "save_foundation": lambda connection, payload: self._save_foundation(
                connection, deps, payload
            ),
            "audit_foundation": lambda connection, payload: self._audit_foundation(
                connection, deps, payload
            ),
            "revise_outline": lambda connection, payload: self._revise_outline(
                connection, deps, payload
            ),
            "resolve_outline_feedback": lambda connection, payload: self._checkpoint_only(payload),
            "complete_book": lambda connection, payload: self._complete_book(
                connection, deps, payload
            ),
            "plan_chapter": lambda connection, payload: self._save_chapter_version(
                connection, deps, payload, "plan"
            ),
            "draft_chapter": lambda connection, payload: self._save_chapter_version(
                connection, deps, payload, "draft"
            ),
            "edit_chapter": lambda connection, payload: self._save_chapter_version(
                connection, deps, payload, "edit"
            ),
            "check_consistency": lambda connection, payload: self._check_consistency(
                connection, deps, payload
            ),
            "commit_chapter": lambda connection, payload: self._commit_chapter(
                connection, deps, payload
            ),
            "save_review": lambda connection, payload: self._save_review(connection, deps, payload),
            "save_arc_summary": lambda connection, payload: self._save_summary(
                connection, deps, payload, "arc"
            ),
            "save_volume_summary": lambda connection, payload: self._save_summary(
                connection, deps, payload, "volume"
            ),
        }
        try:
            return handlers[tool_name]
        except KeyError as error:
            raise ToolAuthorizationError(f"unknown Tool {tool_name}") from error

    async def _save_book(
        self, connection: aiosqlite.Connection, deps: EpisodeDeps, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        title = str(payload.get("title", "")).strip()
        if not title:
            raise ValueError("title must be non-empty")
        await connection.execute("UPDATE projects SET title=? WHERE id=?", (title, deps.project_id))
        return {"title": title}

    async def _save_foundation(
        self, connection: aiosqlite.Connection, deps: EpisodeDeps, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        planned = self._positive_int(payload, "planned_through")
        target_row = await (
            await connection.execute(
                "SELECT target_json FROM projects WHERE id=?", (deps.project_id,)
            )
        ).fetchone()
        if target_row is None:
            raise KeyError(deps.project_id)
        target = decode_json(target_row["target_json"])["target_chapters"]
        if planned > target:
            raise ValueError("outline exceeds the frozen target")
        outline = payload.get("outline")
        if not isinstance(outline, list) or len(outline) != planned:
            raise ValueError("foundation outline must cover every planned chapter")
        existing = await (
            await connection.execute(
                "SELECT count(*) AS n FROM planning_revisions WHERE project_id=?",
                (deps.project_id,),
            )
        ).fetchone()
        assert existing is not None
        revision = int(existing["n"]) + 1
        await connection.execute(
            "INSERT INTO planning_revisions(project_id,revision,payload_json,audited,"
            "planned_through,created_at) VALUES (?,?,?,0,?,?)",
            (deps.project_id, revision, encode_json(dict(payload)), planned, utc_now()),
        )
        return {"revision": revision, "planned_through": planned}

    async def _audit_foundation(
        self, connection: aiosqlite.Connection, deps: EpisodeDeps, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        passed = payload.get("passed")
        if passed is not True:
            raise ValueError("foundation audit must pass before writing")
        cursor = await connection.execute(
            "UPDATE planning_revisions SET audited=1 WHERE id=(SELECT id FROM planning_revisions "
            "WHERE project_id=? ORDER BY revision DESC LIMIT 1)",
            (deps.project_id,),
        )
        if cursor.rowcount != 1:
            raise ValueError("foundation must be saved before it can be audited")
        await connection.execute(
            "UPDATE run_state SET phase='writing', lock_version=lock_version+1 WHERE project_id=?",
            (deps.project_id,),
        )
        return {"audited": True}

    async def _revise_outline(
        self, connection: aiosqlite.Connection, deps: EpisodeDeps, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        planned = self._positive_int(payload, "planned_through")
        prior = await (
            await connection.execute(
                "SELECT revision,planned_through,payload_json FROM planning_revisions "
                "WHERE project_id=? ORDER BY revision DESC LIMIT 1",
                (deps.project_id,),
            )
        ).fetchone()
        if prior is None or planned <= prior["planned_through"]:
            raise ValueError("outline extension must advance planned_through")
        target_row = await (
            await connection.execute(
                "SELECT target_json FROM projects WHERE id=?", (deps.project_id,)
            )
        ).fetchone()
        assert target_row is not None
        target = decode_json(target_row["target_json"])["target_chapters"]
        if planned > target:
            raise ValueError("outline exceeds the frozen target")
        extension = payload.get("outline_extension")
        expected_items = planned - int(prior["planned_through"])
        if not isinstance(extension, list) or len(extension) != expected_items:
            raise ValueError("outline extension must cover every newly planned chapter")
        await connection.execute(
            "UPDATE planning_revisions SET audited=0 WHERE project_id=? AND audited=1",
            (deps.project_id,),
        )
        revision = prior["revision"] + 1
        prior_payload = decode_json(prior["payload_json"])
        previous_extension = prior_payload.get("outline_extension", [])
        if not isinstance(previous_extension, list):
            raise TypeError("stored outline extension is malformed")
        merged = {
            **prior_payload,
            **dict(payload),
            "outline_extension": [*previous_extension, *extension],
        }
        await connection.execute(
            "INSERT INTO planning_revisions(project_id,revision,payload_json,audited,"
            "planned_through,created_at) VALUES (?,?,?,1,?,?)",
            (deps.project_id, revision, encode_json(merged), planned, utc_now()),
        )
        await self.store.append_event(
            connection, deps.project_id, "outline_extended", {"planned_through": planned}
        )
        return {"revision": revision, "planned_through": planned}

    async def _complete_book(
        self, connection: aiosqlite.Connection, deps: EpisodeDeps, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        if payload.get("audit_passed") is not True:
            raise ValueError("completion audit must pass before the book can complete")
        counts = await (
            await connection.execute(
                "SELECT (SELECT count(*) FROM chapters WHERE project_id=?) AS chapters, "
                "json_extract((SELECT target_json FROM projects WHERE id=?),'$.target_chapters') AS target",
                (deps.project_id, deps.project_id),
            )
        ).fetchone()
        assert counts is not None
        if counts["chapters"] < counts["target"]:
            raise ValueError("cannot complete before the frozen target")
        await connection.execute(
            "UPDATE run_state SET status='completed', phase='complete', lock_version=lock_version+1 "
            "WHERE project_id=?",
            (deps.project_id,),
        )
        await self.store.append_event(connection, deps.project_id, "run_completed", {})
        return {"completed": True, "chapters": counts["chapters"]}

    async def _save_chapter_version(
        self,
        connection: aiosqlite.Connection,
        deps: EpisodeDeps,
        payload: Mapping[str, Any],
        kind: str,
    ) -> dict[str, Any]:
        number = self._positive_int(payload, "chapter_number")
        body_key = "plan" if kind == "plan" else "content"
        content = str(payload.get(body_key, "")).strip()
        if not content:
            raise ValueError(f"{body_key} must be non-empty")
        digest = content_hash(content)
        await connection.execute(
            "INSERT OR IGNORE INTO content_blobs(sha256,media_type,byte_length,content,created_at) "
            "VALUES (?,?,?,?,?)",
            (digest, "text/plain; charset=utf-8", len(content.encode("utf-8")), content, utc_now()),
        )
        row = await (
            await connection.execute(
                "SELECT coalesce(max(version),0)+1 AS v FROM chapter_versions "
                "WHERE project_id=? AND chapter_number=? AND kind=?",
                (deps.project_id, number, kind),
            )
        ).fetchone()
        assert row is not None
        await connection.execute(
            "INSERT INTO chapter_versions(project_id,chapter_number,kind,content_sha256,"
            "instruction_key,version,created_at) VALUES (?,?,?,?,?,?,?)",
            (
                deps.project_id,
                number,
                kind,
                digest,
                deps.instruction.instruction_key,
                row["v"],
                utc_now(),
            ),
        )
        return {"chapter_number": number, "kind": kind, "content_sha256": digest}

    async def _check_consistency(
        self, _connection: aiosqlite.Connection, deps: EpisodeDeps, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        number = self._positive_int(payload, "chapter_number")
        if payload.get("passed") is not True:
            raise ValueError("consistency check must pass before commit")
        return {"chapter_number": number, "passed": True, "issues": payload.get("issues", [])}

    async def _commit_chapter(
        self, connection: aiosqlite.Connection, deps: EpisodeDeps, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        number = self._positive_int(payload, "chapter_number")
        title = str(payload.get("title", "")).strip()
        content = str(payload.get("content", "")).strip()
        facts = payload.get("facts")
        if not title or not content or not isinstance(facts, dict):
            raise ValueError("commit_chapter requires title, content, and object facts")
        check = await (
            await connection.execute(
                "SELECT 1 FROM checkpoints WHERE project_id=? AND instruction_key=? "
                "AND step='check_consistency'",
                (deps.project_id, deps.instruction.instruction_key),
            )
        ).fetchone()
        if check is None:
            raise ValueError("consistency checkpoint is required before commit")
        digest = content_hash(content)
        await connection.execute(
            "INSERT OR IGNORE INTO content_blobs(sha256,media_type,byte_length,content,created_at) "
            "VALUES (?,?,?,?,?)",
            (digest, "text/plain; charset=utf-8", len(content.encode("utf-8")), content, utc_now()),
        )
        existing = await (
            await connection.execute(
                "SELECT content_sha256,revision FROM chapters WHERE project_id=? AND chapter_number=?",
                (deps.project_id, number),
            )
        ).fetchone()
        if deps.instruction.kind is InstructionKind.WRITE_CHAPTER:
            expected = await (
                await connection.execute(
                    "SELECT count(*)+1 AS n FROM chapters WHERE project_id=?", (deps.project_id,)
                )
            ).fetchone()
            assert expected is not None
            if number != expected["n"]:
                raise ToolConflictError("new chapters must commit contiguously")
            if existing is not None:
                raise ToolConflictError("formal chapter already exists")
            revision = 1
            await connection.execute(
                "INSERT INTO chapters(project_id,chapter_number,title,content_sha256,"
                "commit_instruction_key,revision,created_at) VALUES (?,?,?,?,?,?,?)",
                (
                    deps.project_id,
                    number,
                    title,
                    digest,
                    deps.instruction.instruction_key,
                    revision,
                    utc_now(),
                ),
            )
        else:
            if existing is None:
                raise ToolConflictError("rewrite target is not a committed chapter")
            queued = await (
                await connection.execute(
                    "SELECT 1 FROM rewrite_queue WHERE project_id=? AND chapter_number=? "
                    "AND status='pending'",
                    (deps.project_id, number),
                )
            ).fetchone()
            if queued is None:
                raise ToolAuthorizationError("chapter has no pending rewrite")
            revision = existing["revision"] + 1
            await connection.execute(
                "UPDATE chapters SET title=?,content_sha256=?,revision=?,commit_instruction_key=? "
                "WHERE project_id=? AND chapter_number=?",
                (
                    title,
                    digest,
                    revision,
                    deps.instruction.instruction_key,
                    deps.project_id,
                    number,
                ),
            )
            await connection.execute(
                "UPDATE rewrite_queue SET status='completed',attempts=attempts+1 "
                "WHERE project_id=? AND chapter_number=? AND status='pending'",
                (deps.project_id, number),
            )
        await connection.execute(
            "INSERT INTO chapter_facts(project_id,chapter_number,revision,facts_json,created_at) "
            "VALUES (?,?,?,?,?)",
            (deps.project_id, number, revision, encode_json(facts), utc_now()),
        )
        prior = await (
            await connection.execute(
                "SELECT payload_json FROM canon_snapshots WHERE project_id=? ORDER BY version DESC LIMIT 1",
                (deps.project_id,),
            )
        ).fetchone()
        canon = {} if prior is None else decode_json(prior["payload_json"])
        canon[f"chapter:{number}"] = facts
        canon_version = await (
            await connection.execute(
                "SELECT coalesce(max(version),0)+1 AS v FROM canon_snapshots WHERE project_id=?",
                (deps.project_id,),
            )
        ).fetchone()
        assert canon_version is not None
        chapter_count = await (
            await connection.execute(
                "SELECT count(*) AS n FROM chapters WHERE project_id=?", (deps.project_id,)
            )
        ).fetchone()
        assert chapter_count is not None
        await connection.execute(
            "INSERT INTO canon_snapshots(project_id,version,through_chapter,payload_json,created_at) "
            "VALUES (?,?,?,?,?)",
            (
                deps.project_id,
                canon_version["v"],
                chapter_count["n"],
                encode_json(canon),
                utc_now(),
            ),
        )
        await self.store.append_event(
            connection,
            deps.project_id,
            "chapter_committed",
            {"chapter_number": number, "revision": revision, "content_sha256": digest},
        )
        return {
            "chapter_number": number,
            "revision": revision,
            "content_sha256": digest,
            # Bind the values actually persisted, while _write retains the original
            # validated input hash for strict changed-payload conflict protection.
            "committed_payload_version": CHAPTER_COMMIT_PAYLOAD_VERSION,
            "committed_payload_sha256": content_hash(
                {
                    "chapter_number": number,
                    "title": title,
                    "content": content,
                    "facts": facts,
                }
            ),
        }

    async def _save_review(
        self, connection: aiosqlite.Connection, deps: EpisodeDeps, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        boundary = self._positive_int(payload, "boundary")
        verdict = payload.get("verdict")
        dimensions = payload.get("dimensions")
        evidence = payload.get("evidence")
        chapters = payload.get("chapters", [])
        if verdict not in {"accept", "polish", "rewrite"}:
            raise ValueError("invalid review verdict")
        if not isinstance(dimensions, dict) or len(dimensions) != 7:
            raise ValueError("review requires exactly seven dimensions")
        if not isinstance(evidence, list) or any(len(str(item)) > 240 for item in evidence):
            raise ValueError("review evidence must be a list of short excerpts")
        if verdict != "accept" and not chapters:
            raise ValueError("polish/rewrite verdict requires chapter targets")
        if verdict == "accept" and chapters:
            raise ValueError("accept verdict cannot enqueue chapter rewrites")
        revision_row = await (
            await connection.execute(
                "SELECT coalesce(max(revision),0)+1 AS revision FROM reviews "
                "WHERE project_id=? AND boundary=?",
                (deps.project_id, boundary),
            )
        ).fetchone()
        assert revision_row is not None
        await connection.execute(
            "INSERT INTO reviews(project_id,boundary,revision,verdict,dimensions_json,"
            "evidence_json,created_at) VALUES (?,?,?,?,?,?,?)",
            (
                deps.project_id,
                boundary,
                revision_row["revision"],
                verdict,
                encode_json(dimensions),
                encode_json(evidence),
                utc_now(),
            ),
        )
        for chapter in chapters:
            number = int(chapter)
            if number < 1 or number > boundary:
                raise ValueError("review chapter target is outside the reviewed boundary")
            await connection.execute(
                "INSERT INTO rewrite_queue(project_id,chapter_number,review_boundary,status) "
                "VALUES (?,?,?,'pending')",
                (deps.project_id, number, boundary),
            )
        await self.store.append_event(
            connection,
            deps.project_id,
            "review_saved",
            {
                "boundary": boundary,
                "revision": revision_row["revision"],
                "verdict": verdict,
                "chapters": chapters,
            },
        )
        return {"boundary": boundary, "verdict": verdict, "rewrite_count": len(chapters)}

    async def _save_summary(
        self,
        connection: aiosqlite.Connection,
        deps: EpisodeDeps,
        payload: Mapping[str, Any],
        kind: str,
    ) -> dict[str, Any]:
        boundary = self._positive_int(payload, "boundary")
        summary = str(payload.get("summary", "")).strip()
        if not summary:
            raise ValueError("summary must be non-empty")
        await connection.execute(
            "INSERT INTO summaries(project_id,kind,boundary,payload_json,created_at) VALUES (?,?,?,?,?)",
            (deps.project_id, kind, boundary, encode_json(dict(payload)), utc_now()),
        )
        await self.store.append_event(
            connection,
            deps.project_id,
            "summary_saved",
            {"kind": kind, "boundary": boundary},
        )
        return {"kind": kind, "boundary": boundary}

    async def _checkpoint_only(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return {"saved": True, "payload": dict(payload)}
