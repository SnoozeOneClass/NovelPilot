from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from app.authoring.domain.models import (
    AuthoringProfileSnapshot,
    Instruction,
    InstructionKind,
    Phase,
    RunStatus,
    TargetLength,
    WorkerRole,
    content_hash,
)
from app.authoring.errors import (
    LeaseUnavailableError,
    StateCorruptionError,
    ToolAuthorizationError,
    ToolConflictError,
)
from app.authoring.models import EpisodeProfileSelection
from app.authoring.models.transport import ActivationRequestBudgetExhausted
from app.authoring.runtime import Engine, route
from app.authoring.store import AuthoringStore
from app.authoring.tools import EpisodeDeps, ToolGateway
from app.authoring.tools.contracts import CommitChapterInput
from app.authoring.workers import PydanticWorkerRuntime
from pydantic_ai import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.messages import ModelMessage, ModelRequest, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import RequestUsage


def _profile() -> AuthoringProfileSnapshot:
    return AuthoringProfileSnapshot(
        profile_id="recovery",
        provider_protocol="function",
        model_id="recovery",
        context_window=32_768,
        max_output_tokens=1024,
    )


async def _store(path: Path) -> AuthoringStore:
    store = AuthoringStore(path)
    await store.migrate()
    await store.create_project(
        brief="A keeper makes a costly choice.",
        target=TargetLength.resolve(target_chapters=1),
        project_id="p1",
    )
    return store


def _last_tool(messages: list[ModelMessage]) -> str | None:
    return next(
        (
            part.tool_name
            for message in reversed(messages)
            if isinstance(message, ModelRequest)
            for part in reversed(message.parts)
            if isinstance(part, ToolReturnPart)
        ),
        None,
    )


def _restore(messages: list[ModelMessage]) -> dict[str, Any]:
    for message in messages:
        if isinstance(message, ModelRequest):
            for part in message.parts:
                if isinstance(part, UserPromptPart) and isinstance(part.content, str):
                    marker = "Mandatory restore pack:\n"
                    if marker in part.content:
                        return dict(json.loads(part.content.split(marker, 1)[1]))
    return {}


def _call(tool: str, arguments: dict[str, Any]) -> ModelResponse:
    return ModelResponse(
        parts=[ToolCallPart(tool, arguments)],
        usage=RequestUsage(input_tokens=17, output_tokens=11),
    )


def _foundation(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
    last = _last_tool(messages)
    if last is None:
        return _call("save_book", {"title": "The Costly Bell"})
    if last == "save_book":
        return _call(
            "save_foundation",
            {
                "premise": "A keeper makes a costly choice.",
                "compass": "Choices have costs.",
                "characters": [{"name": "Lin", "goal": "keep a promise"}],
                "world": {"rule": "The bell demands a price."},
                "outline": ["Make the choice"],
                "planned_through": 1,
            },
        )
    if last == "save_foundation":
        return _call("audit_foundation", {"passed": True, "issues": []})
    return ModelResponse(parts=[TextPart("Foundation complete.")])


def _writer(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
    last = _last_tool(messages)
    if last is None:
        return _call("plan_chapter", {"chapter_number": 1, "plan": "Keep the promise."})
    if last == "plan_chapter":
        return _call("draft_chapter", {"chapter_number": 1, "content": "Lin rang the bell."})
    if last == "draft_chapter":
        return _call("check_consistency", {"chapter_number": 1, "passed": True, "issues": []})
    if last == "check_consistency":
        return _call(
            "commit_chapter",
            {
                "chapter_number": 1,
                "title": "The Bell",
                "content": "Lin rang the bell.",
                "facts": {"summary": "Lin made the promised choice."},
            },
        )
    return ModelResponse(parts=[TextPart("Chapter committed.")])


def _engine(store: AuthoringStore, respond: Any) -> Engine:
    model = FunctionModel(respond, model_name="recovery")
    return Engine(
        store,
        PydanticWorkerRuntime(),
        lambda _project, _role: EpisodeProfileSelection(snapshot=_profile(), model=model),
    )


def test_terminal_tool_then_model_failure_recovers_without_second_episode(tmp_path: Path) -> None:
    async def exercise() -> None:
        store = await _store(tmp_path / "postamble.sqlite3")

        def fail_postamble(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            if _last_tool(messages) == "audit_foundation":
                raise RuntimeError("injected final model response failure")
            return _foundation(messages, info)

        result = await _engine(store, fail_postamble).run("p1", max_instructions=1)
        assert result.instructions_completed == 1
        assert result.episodes_failed == 1
        assert "injected final model response failure" in (result.last_error or "")
        assert result.status is RunStatus.RUNNING
        assert await store.scalar("SELECT count(*) FROM worker_episodes") == 1
        assert await store.scalar("SELECT status FROM worker_episodes") == "failed"
        assert await store.scalar("SELECT count(*) FROM planning_revisions") == 1
        assert (await store.load_state("p1")).active_instruction_key is None
        evidence = await store.execution_evidence("p1")
        assert [request["status"] for request in evidence["requests"]] == [
            "succeeded",
            "succeeded",
            "succeeded",
            "failed",
        ]
        assert sum(r["output_tokens"] for r in evidence["requests"]) == 33
        assert any(e["kind"] == "instruction_recovered" for e in await store.events("p1"))

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "interrupted_after,large_draft",
    [
        ("plan_chapter", False),
        ("draft_chapter", False),
        ("draft_chapter", True),
    ],
)
def test_fresh_worker_uses_saved_plan_and_draft_instead_of_regenerating(
    tmp_path: Path,
    interrupted_after: str,
    large_draft: bool,
) -> None:
    async def exercise() -> None:
        store = await _store(tmp_path / "resume.sqlite3")
        await _engine(store, _foundation).run("p1", max_instructions=1)
        interrupted = False
        initial_packs: list[dict[str, Any]] = []
        body = "Lin rang the bell." * (700 if large_draft else 1)

        def writer(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
            nonlocal interrupted
            last = _last_tool(messages)
            if last == interrupted_after and not interrupted:
                interrupted = True
                raise RuntimeError("injected interruption after saved work")
            if last is None:
                pack = _restore(messages)
                initial_packs.append(pack)
                saved = pack.get("successful_checkpoints", [])
                if "draft_chapter" in saved:
                    if large_draft:
                        assert pack["current_work"]["content"] is None
                        return _call("read_chapter", {"chapter_number": 1})
                    assert "Lin rang the bell." in json.dumps(pack)
                    return _call(
                        "check_consistency", {"chapter_number": 1, "passed": True, "issues": []}
                    )
                if "plan_chapter" in saved:
                    assert pack["chapter_plan"] == "Keep the original promise."
                    return _call("draft_chapter", {"chapter_number": 1, "content": body})
                # A newly generated plan differs on retry unless persisted facts were supplied.
                plan = (
                    "A different regenerated plan." if interrupted else "Keep the original promise."
                )
                return _call("plan_chapter", {"chapter_number": 1, "plan": plan})
            if last == "plan_chapter":
                return _call("draft_chapter", {"chapter_number": 1, "content": body})
            if last in {"draft_chapter", "read_chapter"}:
                if last == "read_chapter":
                    reread = next(
                        part.content
                        for message in reversed(messages)
                        if isinstance(message, ModelRequest)
                        for part in message.parts
                        if isinstance(part, ToolReturnPart) and part.tool_name == "read_chapter"
                    )
                    assert isinstance(reread, dict) and reread["content"] == body
                return _call(
                    "check_consistency", {"chapter_number": 1, "passed": True, "issues": []}
                )
            if last == "check_consistency":
                return _call(
                    "commit_chapter",
                    {
                        "chapter_number": 1,
                        "title": "The Bell",
                        "content": body,
                        "facts": {"summary": "Lin made the promised choice."},
                    },
                )
            return ModelResponse(parts=[TextPart("Chapter committed.")])

        result = await _engine(store, writer).run("p1", max_instructions=1)
        assert result.instructions_completed == 1
        assert result.episodes_failed == 1
        assert len(initial_packs) == 2
        assert interrupted_after in initial_packs[1]["successful_checkpoints"]
        assert initial_packs[1]["next_action"] in {"draft_chapter", "check_consistency"}
        assert await store.scalar("SELECT count(*) FROM chapter_versions WHERE kind='plan'") == 1
        assert await store.scalar("SELECT count(*) FROM chapters") == 1
        assert (
            await store.scalar("SELECT count(*) FROM checkpoints WHERE step='commit_chapter'") == 1
        )

    asyncio.run(exercise())


@pytest.mark.parametrize("episode_status", ["running", "failed"])
def test_restart_recovers_terminal_instruction_before_resolving_model(
    tmp_path: Path,
    episode_status: str,
) -> None:
    async def exercise() -> None:
        store = await _store(tmp_path / "restart.sqlite3")
        deps = await _terminal_episode(store)
        if episode_status == "failed":
            await store.finish_episode(
                deps.episode_id, "p1", succeeded=False, failure="original request failed"
            )

        def forbidden_resolution(_project: str, _role: Any) -> EpisodeProfileSelection:
            raise AssertionError("completed instruction must not resolve a model")

        restarted = AuthoringStore(store.database_path)
        result = await Engine(restarted, PydanticWorkerRuntime(), forbidden_resolution).run(
            "p1", max_instructions=1
        )
        assert result.instructions_completed == 1
        assert result.status is RunStatus.RUNNING
        assert (await store.load_state("p1")).active_instruction_key is None
        assert await store.scalar("SELECT count(*) FROM worker_episodes") == 1
        assert await store.scalar("SELECT status FROM worker_episodes") == (
            "completed" if episode_status == "running" else "failed"
        )
        assert (
            await store.scalar("SELECT count(*) FROM checkpoints WHERE step='audit_foundation'")
            == 1
        )

    asyncio.run(exercise())


async def _terminal_episode(store: AuthoringStore) -> EpisodeDeps:
    await store.set_status("p1", RunStatus.RUNNING)
    instruction = route(await store.load_state("p1"))
    assert instruction is not None
    deps = EpisodeDeps(
        store=store,
        instruction=instruction,
        profile=_profile(),
        model=FunctionModel(_foundation, model_name="recovery"),
    )
    await store.set_active_instruction(
        "p1",
        instruction.instruction_key,
        instruction.kind.value,
        instruction.logical_target,
        instruction.fact_version,
    )
    await store.start_episode(
        episode_id=deps.episode_id,
        project_id="p1",
        worker=instruction.worker.value,
        instruction_key=instruction.instruction_key,
        instruction_kind=instruction.kind.value,
        logical_target=instruction.logical_target,
        profile_snapshot=_profile().model_dump(mode="json"),
    )
    await PydanticWorkerRuntime().run(instruction, deps)
    return deps


@pytest.mark.parametrize("corruption", ["invocation", "fact", "planning_payload"])
def test_reconcile_validates_terminal_evidence_before_marking_orphan_completed(
    tmp_path: Path,
    corruption: str,
) -> None:
    async def exercise() -> None:
        store = await _store(tmp_path / "corruption.sqlite3")
        await _terminal_episode(store)
        with sqlite3.connect(store.database_path) as connection:
            if corruption == "invocation":
                connection.execute(
                    "UPDATE tool_invocations SET payload_hash='corrupt' WHERE tool_name='audit_foundation'"
                )
            elif corruption == "fact":
                connection.execute("UPDATE planning_revisions SET audited=0")
            else:
                connection.execute(
                    "UPDATE planning_revisions SET payload_json=json_set(payload_json,'$.premise','changed')"
                )
        with pytest.raises(StateCorruptionError):
            await store.reconcile("p1")
        assert await store.scalar("SELECT status FROM worker_episodes") != "completed"
        assert (await store.project("p1")).status is RunStatus.FAILURE_PAUSED
        assert not any(
            e["kind"] == "episode_reconciled_completed" for e in await store.events("p1")
        )

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "fault", ["postamble", "cancel", "pause", "lease", "corruption", "conflict"]
)
def test_chapter_terminal_recovery_respects_control_and_integrity_boundaries(
    tmp_path: Path,
    fault: str,
) -> None:
    async def exercise() -> None:
        store = await _store(tmp_path / "chapter-terminal.sqlite3")
        await _engine(store, _foundation).run("p1", max_instructions=1)

        async def failing_writer(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            if _last_tool(messages) != "commit_chapter":
                return _writer(messages, info)
            if fault == "cancel":
                await store.cancel("p1")
                raise asyncio.CancelledError
            if fault == "pause":
                await store.pause("p1")
            if fault == "lease":
                async with store.write_transaction() as connection:
                    await connection.execute("UPDATE run_state SET lease_owner='another-engine'")
            if fault == "corruption":
                async with store.write_transaction() as connection:
                    await connection.execute("UPDATE canon_snapshots SET payload_json='{}'")
            if fault == "conflict":
                return _call(
                    "commit_chapter",
                    {
                        "chapter_number": 1,
                        "title": "Changed title",
                        "content": "Changed committed content.",
                        "facts": {"summary": "Must be rejected."},
                    },
                )
            raise RuntimeError("injected post-commit model failure")

        engine = _engine(store, failing_writer)
        if fault == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await engine.run("p1", max_instructions=1)
        elif fault == "lease":
            with pytest.raises(LeaseUnavailableError):
                await engine.run("p1", max_instructions=1)
        else:
            result = await engine.run("p1", max_instructions=1)
            assert result.instructions_completed == (1 if fault == "postamble" else 0)
            assert result.episodes_failed == 1
            expected = {
                "postamble": RunStatus.RUNNING,
                "pause": RunStatus.PAUSED,
                "corruption": RunStatus.FAILURE_PAUSED,
                "conflict": RunStatus.FAILURE_PAUSED,
            }
            assert result.status is expected[fault]
        assert await store.scalar("SELECT count(*) FROM chapters") == 1
        assert await store.scalar("SELECT count(*) FROM worker_episodes WHERE worker='writer'") == 1
        assert (
            await store.scalar("SELECT count(*) FROM checkpoints WHERE step='commit_chapter'") == 1
        )
        assert (await store.chapter("p1", 1))["content"] == "Lin rang the bell."
        recovered = [e for e in await store.events("p1") if e["kind"] == "instruction_recovered"]
        assert bool(recovered) is (fault == "postamble")
        if fault == "conflict":
            assert "ToolConflictError" in (result.last_error or "")
        if fault == "cancel":
            assert (await store.project("p1")).status is RunStatus.CANCELLED

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "status", [RunStatus.PAUSED, RunStatus.FAILURE_PAUSED, RunStatus.CANCELLED]
)
def test_restart_leaves_stopped_control_state_and_terminal_work_pending_until_resume(
    tmp_path: Path,
    status: RunStatus,
) -> None:
    async def exercise() -> None:
        store = await _store(tmp_path / "stopped.sqlite3")
        deps = await _terminal_episode(store)
        await store.set_status("p1", status)

        def forbidden(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
            raise AssertionError("stopped work cannot enter the model")

        result = await _engine(store, forbidden).run("p1", max_instructions=1)
        assert result.status is status
        assert result.instructions_completed == 0
        assert await store.scalar("SELECT status FROM worker_episodes") == "interrupted"
        assert (
            await store.load_state("p1")
        ).active_instruction_key == deps.instruction.instruction_key
        if status is not RunStatus.CANCELLED:
            await store.resume("p1")
            resumed = await _engine(store, forbidden).run("p1", max_instructions=1)
            assert resumed.instructions_completed == 1
            assert await store.scalar("SELECT count(*) FROM worker_episodes") == 1

    asyncio.run(exercise())


def test_nonterminal_local_budget_exhaustion_pauses_once_without_full_episode_retry(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        store = await _store(tmp_path / "budget.sqlite3")

        def exhausted(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            if _last_tool(messages) == "save_book":
                raise ActivationRequestBudgetExhausted("injected local request cap")
            return _foundation(messages, info)

        result = await _engine(store, exhausted).run("p1", max_instructions=1)
        assert result.status is RunStatus.FAILURE_PAUSED
        assert result.instructions_completed == 0 and result.episodes_failed == 1
        assert result.last_error == "ActivationRequestBudgetExhausted: injected local request cap"
        assert await store.scalar("SELECT count(*) FROM worker_episodes") == 1
        assert await store.scalar("SELECT count(*) FROM decisions") == 0
        assert [r["status"] for r in (await store.execution_evidence("p1"))["requests"]] == [
            "succeeded",
            "failed",
        ]

    asyncio.run(exercise())


def test_large_saved_draft_is_bounded_in_restore_and_rereadable_through_writer_tool(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        store = await _store(tmp_path / "large-draft.sqlite3")
        await _engine(store, _foundation).run("p1", max_instructions=1)
        instruction = route(await store.load_state("p1"))
        assert instruction is not None
        deps = EpisodeDeps(store=store, instruction=instruction, profile=_profile())
        await store.set_active_instruction(
            "p1",
            instruction.instruction_key,
            instruction.kind.value,
            instruction.logical_target,
            instruction.fact_version,
        )
        await store.start_episode(
            episode_id=deps.episode_id,
            project_id="p1",
            worker="writer",
            instruction_key=instruction.instruction_key,
            profile_snapshot=_profile().model_dump(mode="json"),
        )
        body = "The bell rang again. " * 1000
        gateway = ToolGateway(store)
        await gateway.invoke(
            deps, "plan_chapter", {"chapter_number": 1, "plan": "Keep this exact plan."}
        )
        await gateway.invoke(deps, "draft_chapter", {"chapter_number": 1, "content": body})
        material = await store.restore_material("p1", "chapter:1", instruction.instruction_key)
        assert material["current_work"]["content"] is None
        assert material["current_work"]["reread"] == {"tool": "read_chapter", "chapter_number": 1}
        assert len(json.dumps(material)) < 8000
        reloaded = await gateway.invoke(deps, "read_chapter", {"chapter_number": 1})
        assert reloaded["content"] == body.strip()
        assert reloaded["plan"] == "Keep this exact plan."
        with pytest.raises(ToolConflictError):
            await gateway.invoke(
                deps, "plan_chapter", {"chapter_number": 1, "plan": "Changed payload."}
            )

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "case",
    [
        "title_corruption",
        "normalized_new",
        "normal_legacy",
        "padded_legacy",
        "facts_corruption",
        "body_corruption",
        "revision_corruption",
        "equivalent_normalization_conflict",
    ],
)
def test_committed_payload_evidence_binds_normalized_domain_facts_and_legacy_limits(
    tmp_path: Path,
    case: str,
) -> None:
    async def exercise() -> None:
        store = await _store(tmp_path / "committed-payload.sqlite3")
        await _engine(store, _foundation).run("p1", max_instructions=1)
        padded = case in {"normalized_new", "padded_legacy", "equivalent_normalization_conflict"}
        arguments = {
            "chapter_number": 1,
            "title": "  The Bell\r\n" if padded else "The Bell",
            "content": "\nLin rang the bell. \t" if padded else "Lin rang the bell.",
            "facts": {"summary": "Lin made the promised choice."},
        }
        raw_payload = CommitChapterInput.model_validate(arguments).model_dump(
            mode="python", exclude_none=True
        )

        async def commit_then_fail(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            last = _last_tool(messages)
            if last == "check_consistency":
                return _call("commit_chapter", arguments)
            if last != "commit_chapter":
                return _writer(messages, info)
            if case == "equivalent_normalization_conflict":
                # Same normalized facts are still a different raw Tool write and must conflict.
                return _call(
                    "commit_chapter",
                    {**arguments, "title": "The Bell", "content": "Lin rang the bell."},
                )
            async with store.write_transaction() as connection:
                if case.endswith("legacy"):
                    for table in ("checkpoints", "tool_invocations"):
                        tool_column = "step" if table == "checkpoints" else "tool_name"
                        await connection.execute(
                            f"UPDATE {table} SET result_json=json_remove(result_json,"
                            "'$.committed_payload_version','$.committed_payload_sha256') "
                            f"WHERE {tool_column}='commit_chapter'",
                        )
                elif case == "title_corruption":
                    await connection.execute("UPDATE chapters SET title='Corrupted title'")
                elif case == "facts_corruption":
                    await connection.execute(
                        "UPDATE chapter_facts SET facts_json=json_set(facts_json,'$.summary','Corrupted summary')"
                    )
                    await connection.execute(
                        "UPDATE canon_snapshots SET payload_json=json_set(payload_json,'$.\"chapter:1\".summary','Corrupted summary')"
                    )
                elif case == "body_corruption":
                    await connection.execute(
                        "UPDATE content_blobs SET content='Corrupted body' WHERE sha256=(SELECT content_sha256 FROM chapters)"
                    )
                elif case == "revision_corruption":
                    await connection.execute("UPDATE chapters SET revision=revision+1")
            raise RuntimeError("injected postamble failure after domain evidence fixture")

        result = await _engine(store, commit_then_fail).run("p1", max_instructions=1)
        accepted = case in {"normalized_new", "normal_legacy"}
        assert result.instructions_completed == int(accepted)
        assert result.status is (RunStatus.RUNNING if accepted else RunStatus.FAILURE_PAUSED)
        assert result.episodes_failed == 1
        assert await store.scalar("SELECT count(*) FROM worker_episodes WHERE worker='writer'") == 1
        assert (
            await store.scalar("SELECT status FROM worker_episodes WHERE worker='writer'")
            == "failed"
        )
        assert (
            await store.scalar("SELECT count(*) FROM checkpoints WHERE step='commit_chapter'") == 1
        )
        assert await store.scalar(
            "SELECT payload_hash FROM checkpoints WHERE step='commit_chapter'"
        ) == content_hash(raw_payload)
        assert await store.scalar(
            "SELECT payload_hash FROM tool_invocations WHERE tool_name='commit_chapter'"
        ) == content_hash(raw_payload)
        checkpoint = json.loads(
            await store.scalar("SELECT result_json FROM checkpoints WHERE step='commit_chapter'")
        )
        if not case.endswith("legacy"):
            assert checkpoint["committed_payload_version"] == 1
            assert checkpoint["committed_payload_sha256"] == content_hash(
                {
                    **raw_payload,
                    "title": "The Bell",
                    "content": "Lin rang the bell.",
                }
            )
        if padded:
            assert (await store.chapter("p1", 1))["title"] == "The Bell"
            assert (await store.chapter("p1", 1))["content"] == "Lin rang the bell."
        if case == "padded_legacy":
            assert "unverifiable legacy committed-payload evidence" in (result.last_error or "")
        if case == "equivalent_normalization_conflict":
            assert "ToolConflictError" in (result.last_error or "")
        recoveries = [e for e in await store.events("p1") if e["kind"] == "instruction_recovered"]
        assert bool(recoveries) is accepted

    asyncio.run(exercise())


def test_new_writer_can_read_empty_current_target_then_commit_in_same_episode(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        store = await _store(tmp_path / "new-chapter-reader.sqlite3")
        await _engine(store, _foundation).run("p1", max_instructions=1)
        initial_pack: dict[str, Any] = {}

        def read_first(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            nonlocal initial_pack
            last = _last_tool(messages)
            if last is None:
                initial_pack = _restore(messages)
                return _call("read_chapter", {"chapter_number": 1})
            if last == "read_chapter":
                returned = next(
                    part.content
                    for message in reversed(messages)
                    if isinstance(message, ModelRequest)
                    for part in message.parts
                    if isinstance(part, ToolReturnPart) and part.tool_name == "read_chapter"
                )
                assert returned == {
                    "chapter_number": 1,
                    "status": "not_started",
                    "plan": None,
                    "content": None,
                }
                return _call("plan_chapter", {"chapter_number": 1, "plan": "Keep the promise."})
            return _writer(messages, info)

        result = await _engine(store, read_first).run("p1", max_instructions=1)
        assert result.instructions_completed == 1
        assert result.episodes_failed == 0
        assert await store.scalar("SELECT count(*) FROM worker_episodes WHERE worker='writer'") == 1
        assert await store.scalar("SELECT count(*) FROM chapter_versions WHERE kind='plan'") == 1
        assert await store.scalar("SELECT count(*) FROM chapters") == 1
        assert initial_pack["next_action"] == "plan_chapter"
        assert initial_pack["current_work"] == {
            "plan_status": "not_started",
            "content_status": "not_started",
        }
        assert "read_chapter" not in initial_pack["resume_rule"]
        assert not any(event["kind"] == "tool_failed" for event in await store.events("p1"))

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "case",
    [
        "other_writer_target",
        "missing_rewrite",
        "missing_rewrite_with_draft",
        "missing_editor_target",
        "lost_saved_plan",
        "lost_saved_draft",
        "orphan_consistency",
    ],
)
def test_empty_chapter_read_does_not_soften_other_target_or_rewrite_authorization(
    tmp_path: Path, case: str
) -> None:
    async def exercise() -> None:
        store = await _store(tmp_path / "strict-reader.sqlite3")
        editor = case == "missing_editor_target"
        rewrite = case.startswith("missing_rewrite")
        instruction = Instruction(
            project_id="p1",
            worker=WorkerRole.EDITOR if editor else WorkerRole.WRITER,
            kind=InstructionKind.REVIEW_BOUNDARY
            if editor
            else (InstructionKind.REWRITE_CHAPTER if rewrite else InstructionKind.WRITE_CHAPTER),
            logical_target="boundary:1" if editor else "chapter:1",
            expected_phase=Phase.WRITING,
            terminal_postcondition="reader-fixture",
            reason_code="reader-fixture",
            fact_version="reader-fixture",
        )
        deps = EpisodeDeps(store=store, instruction=instruction, profile=_profile())
        await store.set_active_instruction(
            "p1",
            instruction.instruction_key,
            instruction.kind.value,
            instruction.logical_target,
            instruction.fact_version,
        )
        await store.start_episode(
            episode_id=deps.episode_id,
            project_id="p1",
            worker=instruction.worker.value,
            instruction_key=instruction.instruction_key,
            profile_snapshot=_profile().model_dump(mode="json"),
        )
        gateway = ToolGateway(store)
        if case == "missing_rewrite_with_draft":
            await gateway.invoke(
                deps,
                "edit_chapter",
                {
                    "chapter_number": 1,
                    "content": "A draft cannot replace a missing formal rewrite target.",
                },
            )
        if case.startswith("lost_saved_"):
            plan = case == "lost_saved_plan"
            await gateway.invoke(
                deps,
                "plan_chapter" if plan else "draft_chapter",
                {"chapter_number": 1, "plan" if plan else "content": "Durable saved work."},
            )
            async with store.write_transaction() as connection:
                await connection.execute("DELETE FROM chapter_versions WHERE project_id='p1'")
            with pytest.raises(StateCorruptionError):
                await store.restore_material("p1", "chapter:1", instruction.instruction_key)
        if case == "orphan_consistency":
            await gateway.invoke(
                deps,
                "check_consistency",
                {"chapter_number": 1, "passed": True, "issues": []},
            )
        if case == "other_writer_target":
            with pytest.raises(ToolAuthorizationError):
                await gateway.invoke(deps, "read_chapter", {"chapter_number": 2})
        else:
            with pytest.raises(KeyError):
                await gateway.invoke(deps, "read_chapter", {"chapter_number": 1})
        assert await store.scalar("SELECT count(*) FROM chapters") == 0

    asyncio.run(exercise())
