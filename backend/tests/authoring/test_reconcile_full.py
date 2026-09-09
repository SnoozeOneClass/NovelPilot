from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest
from app.authoring.domain.models import AuthoringProfileSnapshot, RunStatus, TargetLength
from app.authoring.errors import StateCorruptionError
from app.authoring.runtime import route
from app.authoring.store import AuthoringStore
from app.authoring.tools import EpisodeDeps, ToolGateway


def _profile() -> AuthoringProfileSnapshot:
    return AuthoringProfileSnapshot(
        profile_id="reconcile",
        provider_protocol="fake",
        model_id="fake",
        context_window=4_096,
        max_output_tokens=512,
    )


def test_reconcile_terminal_checkpoint_completes_orphan_and_audits_canon(tmp_path: Path) -> None:
    async def exercise() -> None:
        store = AuthoringStore(tmp_path / "authoring.sqlite3")
        await store.migrate()
        await store.create_project(
            brief="reconcile",
            target=TargetLength.resolve(target_chapters=1),
            project_id="p1",
        )
        # Seed the prerequisite planning fact directly; this test owns the
        # interrupted Writer boundary rather than Architect behavior.
        async with store.write_transaction() as connection:
            await connection.execute(
                "INSERT INTO planning_revisions(project_id,revision,payload_json,audited,"
                "planned_through,created_at) VALUES ('p1',1,'{\"outline\":[\"one\"]}',1,1,'now')"
            )
            await connection.execute(
                "UPDATE run_state SET status='running',phase='writing' WHERE project_id='p1'"
            )
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
            worker=instruction.worker.value,
            instruction_key=instruction.instruction_key,
            instruction_kind=instruction.kind.value,
            logical_target=instruction.logical_target,
            profile_snapshot=_profile().model_dump(mode="json"),
        )
        gateway = ToolGateway(store)
        await gateway.invoke(deps, "plan_chapter", {"chapter_number": 1, "plan": "choose"})
        await gateway.invoke(deps, "draft_chapter", {"chapter_number": 1, "content": "A choice."})
        await gateway.invoke(
            deps,
            "check_consistency",
            {"chapter_number": 1, "passed": True, "issues": []},
        )
        await gateway.invoke(
            deps,
            "commit_chapter",
            {
                "chapter_number": 1,
                "title": "Choice",
                "content": "A choice.",
                "facts": {"summary": "The choice is made."},
            },
        )

        await store.reconcile("p1")

        assert (
            await store.scalar("SELECT status FROM worker_episodes WHERE id=?", (deps.episode_id,))
            == "completed"
        )
        assert (await store.load_state("p1")).active_instruction_key is None
        assert any(
            event["kind"] == "episode_reconciled_completed" for event in await store.events("p1")
        )

        with sqlite3.connect(store.database_path) as connection:
            connection.execute("UPDATE canon_snapshots SET payload_json='{}' WHERE project_id='p1'")
        with pytest.raises(StateCorruptionError, match="Canon"):
            await store.reconcile("p1")
        assert (await store.project("p1")).status is RunStatus.FAILURE_PAUSED

    asyncio.run(exercise())
