from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from app.authoring.domain.models import (
    AuthoringProfileSnapshot,
    RunStatus,
    TargetLength,
    WorkerRole,
)
from app.authoring.models import EpisodeProfileSelection
from app.authoring.runtime import Engine
from app.authoring.store import AuthoringStore
from app.authoring.workers import PydanticWorkerRuntime
from pydantic_ai import ModelResponse, TextPart, ToolCallPart, models
from pydantic_ai.messages import ModelMessage, ModelRequest, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel


def _last_tool(messages: list[ModelMessage]) -> str | None:
    for message in reversed(messages):
        if isinstance(message, ModelRequest):
            for part in reversed(message.parts):
                if isinstance(part, ToolReturnPart):
                    return part.tool_name
    return None


def _profile() -> AuthoringProfileSnapshot:
    return AuthoringProfileSnapshot(
        profile_id="control",
        provider_protocol="function",
        model_id="control",
        context_window=16_384,
        max_output_tokens=1_024,
    )


def test_cancel_interrupts_active_pydantic_request_before_any_tool_side_effect(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        store = AuthoringStore(tmp_path / "cancel.sqlite3")
        await store.migrate()
        await store.create_project(
            brief="cancel safely",
            target=TargetLength.resolve(target_chapters=1),
            project_id="p1",
        )
        started = asyncio.Event()
        never = asyncio.Event()

        async def blocked(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
            started.set()
            await never.wait()
            return ModelResponse(parts=[TextPart("unreachable")])

        profile = _profile()

        def resolve(_project_id: str, _role: WorkerRole) -> EpisodeProfileSelection:
            return EpisodeProfileSelection(
                snapshot=profile,
                model=FunctionModel(blocked, model_name="blocked"),
            )

        cancel_event = asyncio.Event()
        engine_task = asyncio.create_task(
            Engine(store, PydanticWorkerRuntime(), resolve).run("p1", cancel_event=cancel_event)
        )
        await asyncio.wait_for(started.wait(), timeout=2)
        cancel_event.set()
        with pytest.raises(asyncio.CancelledError):
            await engine_task

        assert (await store.project("p1")).status is RunStatus.CANCELLED
        assert await store.scalar("SELECT count(*) FROM tool_invocations") == 0

    with models.override_allow_model_requests(False):
        asyncio.run(exercise())


def test_pause_takes_effect_after_active_pydantic_episode_terminal_tool(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        store = AuthoringStore(tmp_path / "pause.sqlite3")
        await store.migrate()
        await store.create_project(
            brief="pause at a safe boundary",
            target=TargetLength.resolve(target_chapters=1),
            project_id="p1",
        )
        started = asyncio.Event()
        release = asyncio.Event()

        async def foundation(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
            last = _last_tool(messages)
            if last is None:
                started.set()
                await release.wait()
                return ModelResponse(parts=[ToolCallPart("save_book", {"title": "Paused"})])
            if last == "save_book":
                return ModelResponse(
                    parts=[
                        ToolCallPart(
                            "save_foundation",
                            {
                                "premise": "pause at a safe boundary",
                                "compass": "choices have consequences",
                                "characters": [{"name": "Lin", "goal": "finish"}],
                                "world": {"rule": "time advances"},
                                "outline": ["choose"],
                                "planned_through": 1,
                            },
                        )
                    ]
                )
            if last == "save_foundation":
                return ModelResponse(
                    parts=[ToolCallPart("audit_foundation", {"passed": True, "issues": []})]
                )
            return ModelResponse(parts=[TextPart("foundation complete")])

        profile = _profile()
        model = FunctionModel(foundation, model_name="pause-boundary")

        def resolve(_project_id: str, _role: WorkerRole) -> EpisodeProfileSelection:
            return EpisodeProfileSelection(snapshot=profile, model=model)

        engine_task = asyncio.create_task(Engine(store, PydanticWorkerRuntime(), resolve).run("p1"))
        await asyncio.wait_for(started.wait(), timeout=2)
        await store.pause("p1")
        release.set()
        result = await engine_task

        assert result.status is RunStatus.PAUSED
        assert await store.scalar("SELECT count(*) FROM planning_revisions") == 1
        assert (
            await store.scalar("SELECT count(*) FROM checkpoints WHERE step='audit_foundation'")
            == 1
        )

    with models.override_allow_model_requests(False):
        asyncio.run(exercise())
