from __future__ import annotations

import asyncio
from pathlib import Path

from app.authoring.context import ContextBudgetManager
from app.authoring.domain.models import (
    AuthoringProfileSnapshot,
    RunStatus,
    TargetLength,
    WorkerRole,
)
from app.authoring.models import EpisodeProfileSelection
from app.authoring.models.transport import ModelRequestBudgetExhausted
from app.authoring.runtime import Engine
from app.authoring.store import AuthoringStore
from app.authoring.workers import PydanticWorkerRuntime
from pydantic_ai import ModelResponse, TextPart, models
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.function import AgentInfo, FunctionModel


def test_compaction_failures_persist_across_episode_retries_and_pause(tmp_path: Path) -> None:
    async def exercise() -> None:
        store = AuthoringStore(tmp_path / "authoring.sqlite3")
        await store.migrate()
        await store.create_project(
            brief="budget cannot fit",
            target=TargetLength.resolve(target_chapters=1),
            project_id="p1",
        )
        provider_calls = 0

        def must_not_reach_provider(
            _messages: list[ModelMessage], _info: AgentInfo
        ) -> ModelResponse:
            nonlocal provider_calls
            provider_calls += 1
            return ModelResponse(parts=[TextPart("unreachable")])

        profile = AuthoringProfileSnapshot(
            profile_id="too-small",
            provider_protocol="function",
            model_id="too-small",
            context_window=101,
            max_output_tokens=100,
        )
        model = FunctionModel(must_not_reach_provider, model_name="too-small")

        def resolve(_project_id: str, _role: WorkerRole) -> EpisodeProfileSelection:
            return EpisodeProfileSelection(snapshot=profile, model=model)

        manager = ContextBudgetManager(safety_margin=20, max_failures=2)
        with models.override_allow_model_requests(False):
            result = await Engine(
                store,
                PydanticWorkerRuntime(context_manager=manager),
                resolve,
                max_instruction_retries=1,
            ).run("p1")

        assert result.status is RunStatus.FAILURE_PAUSED
        assert provider_calls == 0
        failures = [
            event
            for event in await store.events("p1")
            if event["kind"] == "context_compaction_failed"
        ]
        assert [event["payload"]["attempt"] for event in failures] == [1, 2]
        assert (
            await store.scalar(
                "SELECT sum(compaction_failure_count) FROM worker_episodes WHERE project_id='p1'"
            )
            == 2
        )

    asyncio.run(exercise())


def test_summary_request_budget_exhaustion_pauses_without_refilling_episode(tmp_path: Path) -> None:
    async def exercise() -> None:
        store = AuthoringStore(tmp_path / "summary-budget.sqlite3")
        await store.migrate()
        await store.create_project(
            brief="A story needing bounded context.",
            target=TargetLength.resolve(target_chapters=1),
            project_id="p1",
        )
        summary_requests = 0
        resolutions = 0

        def exhausted_summary(_messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            nonlocal summary_requests
            assert not info.function_tools
            summary_requests += 1
            raise ModelRequestBudgetExhausted("context summary reached the local request limit")

        profile = AuthoringProfileSnapshot(
            profile_id="summary-budget",
            provider_protocol="function",
            model_id="summary-budget",
            context_window=8_100,
            max_output_tokens=100,
        )
        model = FunctionModel(exhausted_summary, model_name="summary-budget")

        def resolve(_project_id: str, _role: WorkerRole) -> EpisodeProfileSelection:
            nonlocal resolutions
            resolutions += 1
            return EpisodeProfileSelection(snapshot=profile, model=model)

        with models.override_allow_model_requests(False):
            result = await Engine(store, PydanticWorkerRuntime(), resolve).run("p1")

        assert result.status is RunStatus.FAILURE_PAUSED
        assert result.instructions_completed == 0
        assert result.episodes_failed == summary_requests == resolutions == 1
        assert result.last_error == (
            "ModelRequestBudgetExhausted: context summary reached the local request limit"
        )
        assert await store.scalar("SELECT count(*) FROM decisions") == 0
        requests = (await store.execution_evidence("p1"))["requests"]
        assert len(requests) == 1
        assert requests[0]["purpose"] == "context_summary"
        assert requests[0]["status"] == "failed"
        assert requests[0]["error_type"] == "ModelRequestBudgetExhausted"

    asyncio.run(exercise())
