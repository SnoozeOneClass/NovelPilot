from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
from app.authoring.domain.models import RunStatus, TargetLength, WorkerRole
from app.authoring.errors import LeaseUnavailableError
from app.authoring.models import EpisodeProfileSelection
from app.authoring.runtime.arbiter import FailureContext, FailureDecision
from app.authoring.runtime.engine import Engine
from app.authoring.service import AuthoringService, fake_profile
from app.authoring.store import AuthoringStore
from app.authoring.tools import EpisodeDeps, ToolGateway
from app.authoring.workers import EpisodeResult, ScriptedAutoWorkerRuntime


def test_fake_worker_completes_full_auto_book_with_review_rewrite_and_export(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        store = AuthoringStore(tmp_path / "authoring.sqlite3")
        service = AuthoringService(
            store, worker_runtime=ScriptedAutoWorkerRuntime(ToolGateway(store))
        )
        await service.initialize()
        project_id = await service.create(
            brief="A bell keeper must fulfill a promise before dawn.",
            target_chapters=3,
            project_id="full-auto",
        )

        result = await service.run(project_id)
        view = await service.status(project_id)
        events = await service.events(project_id)
        manuscript, digest = await service.export(project_id)

        assert result.status is RunStatus.COMPLETED
        assert view.chapter_count == 3
        assert any(event["kind"] == "review_saved" for event in events)
        assert any(event["kind"] == "summary_saved" for event in events)
        assert (
            await service.store.scalar(
                "SELECT count(*) FROM rewrite_queue WHERE status='completed'"
            )
            == 1
        )
        assert await service.store.scalar("SELECT count(*) FROM reviews") == 2
        assert "## 第3章" in manuscript
        assert len(digest) == 64
        assert await service.store.scalar("SELECT count(*) FROM export_manifests") == 1
        restore = await store.restore_material(project_id, "chapter:3", "review-contract")
        assert restore["review_evidence"]["dimensions"]["causality"] == 4
        assert restore["review_evidence"]["evidence"]
        with pytest.raises(ValueError, match="completed"):
            await service.pause(project_id)
        with pytest.raises(ValueError, match="completed"):
            await service.cancel(project_id)
        assert (await service.status(project_id)).status is RunStatus.COMPLETED

    asyncio.run(exercise())


def test_rolling_outline_preserves_every_extension_for_later_chapters(tmp_path: Path) -> None:
    async def exercise() -> None:
        store = AuthoringStore(tmp_path / "rolling-outline.sqlite3")
        service = AuthoringService(
            store,
            worker_runtime=ScriptedAutoWorkerRuntime(ToolGateway(store), inject_one_rewrite=False),
        )
        await service.initialize()
        project_id = await service.create(
            brief="keep every rolling outline item",
            target_chapters=7,
            project_id="rolling",
        )
        result = await service.run(project_id)
        assert result.status is RunStatus.COMPLETED
        context = await store.novel_context(project_id)
        foundation = context["foundation"]
        assert isinstance(foundation, dict)
        outline = [*foundation["outline"], *foundation["outline_extension"]]
        assert len(outline) == 7
        assert outline[3].startswith("第4章")
        assert outline[6].startswith("第7章")
        assert [item["number"] for item in context["relevant_chapters"]] == [4, 3, 2]
        assert all(
            item["selection_basis"] == "nearest_prior_outside_recent_window"
            for item in context["relevant_chapters"]
        )

    asyncio.run(exercise())


def test_fake_rewrite_decision_survives_process_restart(tmp_path: Path) -> None:
    async def exercise() -> None:
        database = tmp_path / "fake-restart.sqlite3"
        first_store = AuthoringStore(database)
        first = AuthoringService(
            first_store,
            worker_runtime=ScriptedAutoWorkerRuntime(ToolGateway(first_store)),
        )
        await first.initialize()
        project_id = await first.create(
            brief="restart during rewrite", target_chapters=3, project_id="restart"
        )
        partial = await first.run(project_id, max_instructions=5)
        assert partial.status is RunStatus.RUNNING
        assert (
            await first_store.scalar("SELECT count(*) FROM rewrite_queue WHERE status='pending'")
            == 1
        )

        restarted_store = AuthoringStore(database)
        restarted = AuthoringService(
            restarted_store,
            worker_runtime=ScriptedAutoWorkerRuntime(ToolGateway(restarted_store)),
        )
        await restarted.initialize()
        result = await restarted.run(project_id)
        assert result.status is RunStatus.COMPLETED
        assert (
            await restarted_store.scalar(
                "SELECT count(*) FROM rewrite_queue WHERE status='completed'"
            )
            == 1
        )

    asyncio.run(exercise())


def test_engine_renews_lease_during_a_long_episode(tmp_path: Path) -> None:
    async def exercise() -> None:
        store = AuthoringStore(tmp_path / "lease.sqlite3")
        await store.migrate()
        project_id = await store.create_project(
            brief="long episode",
            target=TargetLength.resolve(target_chapters=1),
            project_id="lease",
        )
        delegate = ScriptedAutoWorkerRuntime(ToolGateway(store), inject_one_rewrite=False)
        started = asyncio.Event()
        release = asyncio.Event()

        class BlockingRuntime:
            async def run(
                self,
                instruction: object,
                deps: EpisodeDeps,
                cancel_event: asyncio.Event | None = None,
            ) -> EpisodeResult:
                from app.authoring.domain.models import Instruction

                assert isinstance(instruction, Instruction)
                started.set()
                await release.wait()
                return await delegate.run(instruction, deps, cancel_event=cancel_event)

        engine = Engine(
            store,
            BlockingRuntime(),
            fake_profile,
            lease_ttl_seconds=0.12,
        )
        task = asyncio.create_task(engine.run(project_id, max_instructions=1))
        await started.wait()
        await asyncio.sleep(0.3)
        with pytest.raises(LeaseUnavailableError):
            await store.acquire_lease(project_id, "intruder", ttl_seconds=0.12)
        release.set()
        result = await task
        assert result.instructions_completed == 1

    asyncio.run(exercise())


def test_retry_budget_is_durable_across_resume(tmp_path: Path) -> None:
    async def exercise() -> None:
        store = AuthoringStore(tmp_path / "retries.sqlite3")
        await store.migrate()
        project_id = await store.create_project(
            brief="persistent failures",
            target=TargetLength.resolve(target_chapters=1),
            project_id="retries",
        )

        class AlwaysFails:
            async def run(
                self,
                instruction: object,
                deps: EpisodeDeps,
                cancel_event: asyncio.Event | None = None,
            ) -> EpisodeResult:
                raise ConnectionError("persistent outage")

        first = Engine(store, AlwaysFails(), fake_profile, max_same_instruction=99)
        result = await first.run(project_id)
        assert result.status is RunStatus.FAILURE_PAUSED
        assert await store.scalar("SELECT count(*) FROM worker_episodes") == 3

        await store.resume(project_id)
        restarted = Engine(store, AlwaysFails(), fake_profile, max_same_instruction=99)
        resumed = await restarted.run(project_id)
        assert resumed.status is RunStatus.FAILURE_PAUSED
        assert await store.scalar("SELECT count(*) FROM worker_episodes") == 3
        assert (
            await store.scalar("SELECT count(*) FROM decisions WHERE kind='failure_arbiter'") == 2
        )

    asyncio.run(exercise())


def test_retryable_provider_failure_waits_and_records_actual_backoff(tmp_path: Path) -> None:
    async def exercise() -> None:
        store = AuthoringStore(tmp_path / "backoff.sqlite3")
        await store.migrate()
        project_id = await store.create_project(
            brief="retry with backoff",
            target=TargetLength.resolve(target_chapters=1),
            project_id="backoff",
        )
        delegate = ScriptedAutoWorkerRuntime(ToolGateway(store), inject_one_rewrite=False)

        class FailOnceWithEvidence:
            failed = False

            async def run(
                self,
                instruction: object,
                deps: EpisodeDeps,
                cancel_event: asyncio.Event | None = None,
            ) -> EpisodeResult:
                from app.authoring.domain.models import Instruction

                assert isinstance(instruction, Instruction)
                if not self.failed:
                    self.failed = True
                    await store.record_model_request(
                        project_id=project_id,
                        episode_id=deps.episode_id,
                        request_index=deps.next_model_request(),
                        profile_fingerprint=deps.profile.fingerprint,
                        purpose="worker",
                        status="failed",
                        retry_reason="provider_http_503",
                        error_type="ModelHTTPError",
                    )
                    raise ConnectionError("retryable provider failure")
                return await delegate.run(instruction, deps, cancel_event=cancel_event)

        started = time.perf_counter()
        result = await Engine(store, FailOnceWithEvidence(), fake_profile).run(
            project_id, max_instructions=1
        )
        elapsed = time.perf_counter() - started

        assert result.instructions_completed == 1
        assert elapsed >= 0.45
        assert (
            await store.scalar("SELECT backoff_ms FROM model_requests WHERE status='failed'") == 500
        )
        assert any(
            event["kind"] == "model_retry_wait" and event["payload"]["delay_ms"] == 500
            for event in await store.events(project_id)
        )

    asyncio.run(exercise())


def test_failure_arbiter_can_grant_only_one_audited_retry(tmp_path: Path) -> None:
    async def exercise() -> None:
        store = AuthoringStore(tmp_path / "arbiter.sqlite3")
        await store.migrate()
        project_id = await store.create_project(
            brief="arbiter retry",
            target=TargetLength.resolve(target_chapters=1),
            project_id="arbiter",
        )

        class AlwaysFails:
            calls = 0

            async def run(
                self,
                instruction: object,
                deps: EpisodeDeps,
                cancel_event: asyncio.Event | None = None,
            ) -> EpisodeResult:
                from app.authoring.domain.models import Instruction

                assert isinstance(instruction, Instruction)
                self.calls += 1
                raise ConnectionError("recoverable failure")

        class RetryOnceArbiter:
            async def decide(self, _context: FailureContext) -> FailureDecision:
                return FailureDecision(action="retry_once", reason_code="single_recovery")

        worker = AlwaysFails()
        result = await Engine(
            store,
            worker,
            fake_profile,
            max_instruction_retries=0,
            failure_arbiter=RetryOnceArbiter(),
        ).run(project_id)

        assert result.status is RunStatus.FAILURE_PAUSED
        assert worker.calls == 2
        assert (
            await store.scalar("SELECT count(*) FROM decisions WHERE kind='failure_arbiter'") == 2
        )
        assert (
            await store.scalar(
                "SELECT count(*) FROM decisions WHERE kind='failure_arbiter' "
                "AND json_extract(decision_json,'$.action')='retry_once'"
            )
            == 1
        )

    asyncio.run(exercise())


def test_provider_failure_persistence_redacts_profile_credentials(tmp_path: Path) -> None:
    async def exercise() -> None:
        store = AuthoringStore(tmp_path / "redaction.sqlite3")
        await store.migrate()
        await store.create_project(
            brief="redact provider errors",
            target=TargetLength.resolve(target_chapters=1),
            project_id="redaction",
        )
        secret = "provider-secret-must-not-persist"
        profile = fake_profile("redaction", WorkerRole.ARCHITECT)

        class LeakingFailure:
            async def run(
                self,
                instruction: object,
                deps: EpisodeDeps,
                cancel_event: asyncio.Event | None = None,
            ) -> EpisodeResult:
                raise ConnectionError(f"transport accidentally included {secret}")

        def resolve(_project_id: str, _role: WorkerRole) -> EpisodeProfileSelection:
            return EpisodeProfileSelection(
                snapshot=profile,
                redaction_secrets=(secret,),
            )

        result = await Engine(
            store,
            LeakingFailure(),
            resolve,
            max_instruction_retries=0,
        ).run("redaction")
        assert result.status is RunStatus.FAILURE_PAUSED
        episode_failure = await store.scalar(
            "SELECT failure FROM worker_episodes WHERE project_id='redaction'"
        )
        failure_reason = (await store.project("redaction")).failure_reason
        assert episode_failure is not None and secret not in episode_failure
        assert failure_reason is not None and secret not in failure_reason
        assert "[REDACTED]" in failure_reason

    asyncio.run(exercise())


def test_failure_after_a_write_tool_replays_checkpoint_without_duplicate_side_effect(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        store = AuthoringStore(tmp_path / "recovery.sqlite3")
        gateway = ToolGateway(store)
        delegate = ScriptedAutoWorkerRuntime(gateway, inject_one_rewrite=False)

        class FailOnceAfterPlan:
            def __init__(self) -> None:
                self.failed = False

            async def run(
                self,
                instruction: object,
                deps: EpisodeDeps,
                cancel_event: asyncio.Event | None = None,
            ) -> EpisodeResult:
                from app.authoring.domain.models import Instruction, InstructionKind

                typed = instruction
                assert isinstance(typed, Instruction)
                if typed.kind is InstructionKind.WRITE_CHAPTER and not self.failed:
                    self.failed = True
                    await gateway.invoke(
                        deps,
                        "plan_chapter",
                        {
                            "chapter_number": 1,
                            "plan": "第1章：承接既有事实，制造选择并留下可追踪后果。",
                        },
                    )
                    raise ConnectionError("injected process failure after durable Tool")
                return await delegate.run(typed, deps, cancel_event=cancel_event)

        service = AuthoringService(store, worker_runtime=FailOnceAfterPlan())
        await service.initialize()
        project_id = await service.create(
            brief="recover the same instruction", target_chapters=1, project_id="recovery"
        )

        result = await service.run(project_id)

        assert result.status is RunStatus.COMPLETED
        assert result.episodes_failed == 1
        assert await store.scalar("SELECT count(*) FROM chapter_versions WHERE kind='plan'") == 1
        assert await store.scalar("SELECT count(*) FROM chapters") == 1

    asyncio.run(exercise())
