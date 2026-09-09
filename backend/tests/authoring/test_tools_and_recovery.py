from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from app.authoring.domain.models import (
    AuthoringProfileSnapshot,
    Instruction,
    InstructionKind,
    Phase,
    TargetLength,
    WorkerRole,
)
from app.authoring.errors import ToolAuthorizationError, ToolConflictError
from app.authoring.store import AuthoringStore
from app.authoring.tools import EpisodeDeps, ToolGateway


def _profile() -> AuthoringProfileSnapshot:
    return AuthoringProfileSnapshot(
        profile_id="fake",
        provider_protocol="fake",
        model_id="fake",
        context_window=4_096,
        max_output_tokens=512,
    )


def _instruction(kind: InstructionKind, worker: WorkerRole, target: str) -> Instruction:
    return Instruction(
        project_id="p1",
        worker=worker,
        kind=kind,
        logical_target=target,
        expected_phase=Phase.WRITING,
        terminal_postcondition="terminal",
        reason_code="test",
        fact_version="facts",
    )


def test_tool_permissions_and_checkpoint_replay_are_durable(tmp_path: Path) -> None:
    async def exercise() -> None:
        store = AuthoringStore(tmp_path / "authoring.sqlite3")
        await store.migrate()
        await store.create_project(
            brief="idea", target=TargetLength.resolve(target_chapters=1), project_id="p1"
        )
        instruction = _instruction(InstructionKind.WRITE_CHAPTER, WorkerRole.WRITER, "chapter:1")
        deps = EpisodeDeps(store=store, instruction=instruction, profile=_profile())
        await store.start_episode(
            episode_id=deps.episode_id,
            project_id="p1",
            worker="writer",
            instruction_key=instruction.instruction_key,
            profile_snapshot=_profile().model_dump(mode="json"),
        )
        await store.set_active_instruction(
            "p1",
            instruction.instruction_key,
            instruction.kind.value,
            instruction.logical_target,
            instruction.fact_version,
        )
        gateway = ToolGateway(store)

        first = await gateway.invoke(
            deps, "plan_chapter", {"chapter_number": 1, "plan": "make a choice"}
        )
        replay = await gateway.invoke(
            deps, "plan_chapter", {"chapter_number": 1, "plan": "make a choice"}
        )
        assert first == replay
        assert await store.scalar("SELECT count(*) FROM chapter_versions") == 1

        with pytest.raises(ToolConflictError):
            await gateway.invoke(
                deps, "plan_chapter", {"chapter_number": 1, "plan": "different payload"}
            )
        stale_lease = EpisodeDeps(
            store=store,
            instruction=instruction,
            profile=_profile(),
            lease_owner="expired-owner",
        )
        with pytest.raises(ToolAuthorizationError, match="lost its lease"):
            await gateway.invoke(
                stale_lease,
                "draft_chapter",
                {"chapter_number": 1, "content": "must not commit"},
            )
        with pytest.raises(ToolAuthorizationError):
            await gateway.invoke(deps, "save_book", {"title": "forbidden"})
        with pytest.raises(ToolAuthorizationError):
            await gateway.invoke(
                deps, "draft_chapter", {"chapter_number": 2, "content": "wrong target"}
            )
        await store.cancel("p1")
        with pytest.raises(ToolAuthorizationError, match="cancelled"):
            await gateway.invoke(
                deps, "draft_chapter", {"chapter_number": 1, "content": "too late"}
            )

    asyncio.run(exercise())


def test_episode_usage_and_completion_commit_atomically(tmp_path: Path) -> None:
    async def exercise() -> None:
        store = AuthoringStore(tmp_path / "usage.sqlite3")
        await store.migrate()
        await store.create_project(
            brief="idea", target=TargetLength.resolve(target_chapters=1), project_id="p1"
        )
        instruction = _instruction(InstructionKind.WRITE_CHAPTER, WorkerRole.WRITER, "chapter:1")
        deps = EpisodeDeps(store=store, instruction=instruction, profile=_profile())
        await store.start_episode(
            episode_id=deps.episode_id,
            project_id="p1",
            worker="writer",
            instruction_key=instruction.instruction_key,
            profile_snapshot=_profile().model_dump(mode="json"),
        )

        with pytest.raises(KeyError):
            await store.finish_episode(
                deps.episode_id,
                "p1",
                succeeded=True,
                usage={"profile_fingerprint": "incomplete"},
            )
        assert (
            await store.scalar("SELECT status FROM worker_episodes WHERE id=?", (deps.episode_id,))
            == "running"
        )
        assert await store.scalar("SELECT count(*) FROM model_usage") == 0

    asyncio.run(exercise())


def test_instruction_scope_and_restarted_episode_replay_are_enforced(tmp_path: Path) -> None:
    async def exercise() -> None:
        database = tmp_path / "restart.sqlite3"
        store = AuthoringStore(database)
        await store.migrate()
        await store.create_project(
            brief="idea", target=TargetLength.resolve(target_chapters=1), project_id="p1"
        )
        instruction = _instruction(InstructionKind.WRITE_CHAPTER, WorkerRole.WRITER, "chapter:1")
        first = EpisodeDeps(store=store, instruction=instruction, profile=_profile())
        await store.start_episode(
            episode_id=first.episode_id,
            project_id="p1",
            worker="writer",
            instruction_key=instruction.instruction_key,
            profile_snapshot=_profile().model_dump(mode="json"),
        )
        await store.set_active_instruction(
            "p1",
            instruction.instruction_key,
            instruction.kind.value,
            instruction.logical_target,
            instruction.fact_version,
        )
        await ToolGateway(store).invoke(
            first, "plan_chapter", {"chapter_number": 1, "plan": "durable plan"}
        )

        restarted_store = AuthoringStore(database)
        await restarted_store.migrate()
        await restarted_store.reconcile("p1")
        replay = EpisodeDeps(store=restarted_store, instruction=instruction, profile=_profile())
        await restarted_store.start_episode(
            episode_id=replay.episode_id,
            project_id="p1",
            worker="writer",
            instruction_key=instruction.instruction_key,
            profile_snapshot=_profile().model_dump(mode="json"),
        )
        await ToolGateway(restarted_store).invoke(
            replay, "plan_chapter", {"chapter_number": 1, "plan": "durable plan"}
        )
        assert await restarted_store.scalar("SELECT count(*) FROM chapter_versions") == 1
        replay_events = [
            event
            for event in await restarted_store.events("p1")
            if event["kind"] == "tool_replayed"
        ]
        assert replay_events[-1]["payload"]["episode_id"] == replay.episode_id

        architect = _instruction(
            InstructionKind.CREATE_FOUNDATION, WorkerRole.ARCHITECT, "foundation"
        )
        with pytest.raises(ToolAuthorizationError, match="instruction scope"):
            await ToolGateway(restarted_store).invoke(
                EpisodeDeps(store=restarted_store, instruction=architect, profile=_profile()),
                "complete_book",
                {"audit_passed": True},
            )

    asyncio.run(exercise())


def test_commit_requires_consistency_and_leaves_no_partial_formal_chapter(tmp_path: Path) -> None:
    async def exercise() -> None:
        store = AuthoringStore(tmp_path / "authoring.sqlite3")
        await store.migrate()
        await store.create_project(
            brief="idea", target=TargetLength.resolve(target_chapters=1), project_id="p1"
        )
        instruction = _instruction(InstructionKind.WRITE_CHAPTER, WorkerRole.WRITER, "chapter:1")
        deps = EpisodeDeps(store=store, instruction=instruction, profile=_profile())
        await store.start_episode(
            episode_id=deps.episode_id,
            project_id="p1",
            worker="writer",
            instruction_key=instruction.instruction_key,
            profile_snapshot=_profile().model_dump(mode="json"),
        )
        await store.set_active_instruction(
            "p1",
            instruction.instruction_key,
            instruction.kind.value,
            instruction.logical_target,
            instruction.fact_version,
        )
        gateway = ToolGateway(store)

        with pytest.raises(ValueError, match="consistency checkpoint"):
            await gateway.invoke(
                deps,
                "commit_chapter",
                {
                    "chapter_number": 1,
                    "title": "one",
                    "content": "body",
                    "facts": {"summary": "valid facts before transactional precondition"},
                },
            )

        assert await store.scalar("SELECT count(*) FROM chapters") == 0
        assert await store.scalar("SELECT count(*) FROM content_blobs") == 0
        assert await store.scalar("SELECT count(*) FROM checkpoints") == 0

    asyncio.run(exercise())
