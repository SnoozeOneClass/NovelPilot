from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from app.authoring.domain.models import (
    AuthoringProfileSnapshot,
    Instruction,
    InstructionKind,
    Phase,
    TargetLength,
    WorkerRole,
)
from app.authoring.store import AuthoringStore
from app.authoring.tools import EpisodeDeps
from app.authoring.workers import PydanticWorkerRuntime
from pydantic_ai import Agent, RunContext, models
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel


def test_pydantic_adapter_runs_typed_deps_multi_tool_loop_without_leaking_nodes(
    tmp_path: Any,
) -> None:
    async def exercise() -> None:
        instruction = Instruction(
            project_id="p1",
            worker=WorkerRole.ARCHITECT,
            kind=InstructionKind.COMPLETE_BOOK,
            logical_target="book",
            expected_phase=Phase.FINALIZING,
            terminal_postcondition="book_completed",
            reason_code="contract",
            fact_version="v1",
        )
        profile = AuthoringProfileSnapshot(
            profile_id="fake",
            provider_protocol="test-model",
            model_id="test",
            context_window=4_096,
            max_output_tokens=512,
        )
        store = AuthoringStore(tmp_path / "runtime-contract.sqlite3")
        await store.migrate()
        await store.create_project(
            brief="typed dependency and Tool-loop contract",
            target=TargetLength.resolve(target_chapters=1),
            project_id="p1",
        )
        deps = EpisodeDeps(
            store=store,
            instruction=instruction,
            profile=profile,
        )
        agent = Agent(
            TestModel(call_tools="all", custom_output_text="done"),
            deps_type=EpisodeDeps,
            output_type=str,
            max_concurrency=1,
        )

        @agent.tool
        async def inspect_facts(ctx: RunContext[EpisodeDeps]) -> str:
            ctx.deps.successful_tools.append("inspect_facts")
            return ctx.deps.instruction.logical_target

        @agent.tool
        async def complete_book(ctx: RunContext[EpisodeDeps]) -> str:
            ctx.deps.successful_tools.append("complete_book")
            return "terminal"

        runtime = PydanticWorkerRuntime(lambda _role: agent)
        with models.override_allow_model_requests(False):
            result = await runtime.run(instruction, deps)

        assert result.output_text == "done"
        assert result.terminal_tool == "complete_book"
        assert result.successful_tools == ("inspect_facts", "complete_book")
        assert result.request_count == 2

    asyncio.run(exercise())


def test_streaming_profile_persists_text_lifecycle_without_reasoning_content(
    tmp_path: Any,
) -> None:
    async def exercise() -> None:
        store = AuthoringStore(tmp_path / "stream.sqlite3")
        await store.migrate()
        await store.create_project(
            brief="stream",
            target=TargetLength.resolve(target_chapters=1),
            project_id="p1",
        )
        instruction = Instruction(
            project_id="p1",
            worker=WorkerRole.ARCHITECT,
            kind=InstructionKind.COMPLETE_BOOK,
            logical_target="book",
            expected_phase=Phase.FINALIZING,
            terminal_postcondition="book_completed",
            reason_code="contract",
            fact_version="stream-v1",
        )
        profile = AuthoringProfileSnapshot(
            profile_id="stream",
            provider_protocol="function",
            model_id="stream",
            context_window=4_096,
            max_output_tokens=512,
            capabilities=frozenset({"text_output", "tool_calling", "text_streaming"}),
        )
        deps = EpisodeDeps(store=store, instruction=instruction, profile=profile)
        deps.successful_tools.append("complete_book")

        async def stream(_messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[str]:
            yield "safe generated text"

        agent = Agent(FunctionModel(stream_function=stream), deps_type=EpisodeDeps)
        with models.override_allow_model_requests(False):
            result = await PydanticWorkerRuntime(lambda _role: agent).run(instruction, deps)

        assert result.output_text == "safe generated text"
        lifecycle = [
            event for event in await store.events("p1") if event["kind"] == "model_text_streamed"
        ]
        assert len(lifecycle) == 1
        assert "safe generated text" not in str(lifecycle[0])

    asyncio.run(exercise())
