from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

from app.authoring.context import ContextBudgetManager
from app.authoring.domain.models import (
    AuthoringProfileSnapshot,
    Instruction,
    InstructionKind,
    Phase,
    RunStatus,
    TargetLength,
    WorkerRole,
)
from app.authoring.models import EpisodeProfileSelection
from app.authoring.runtime import Engine
from app.authoring.store import AuthoringStore
from app.authoring.tools import EpisodeDeps, ToolGateway
from app.authoring.workers import PydanticWorkerRuntime
from pydantic_ai import ModelResponse, TextPart, ToolCallPart, models
from pydantic_ai.messages import ModelMessage, ModelRequest, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel


def _profile() -> AuthoringProfileSnapshot:
    return AuthoringProfileSnapshot(
        profile_id="tiny-function-model",
        provider_protocol="test",
        model_id="gateway-compaction",
        context_window=20_000,
        max_output_tokens=200,
        capabilities=frozenset({"text_output", "tool_calling", "text_streaming"}),
    )


def _foundation_instruction() -> Instruction:
    return Instruction(
        project_id="p1",
        worker=WorkerRole.ARCHITECT,
        kind=InstructionKind.CREATE_FOUNDATION,
        logical_target="foundation",
        expected_phase=Phase.FOUNDATION,
        terminal_postcondition="foundation_audited",
        reason_code="fixture",
        fact_version="fixture-v1",
    )


def _last_tool_return(messages: list[ModelMessage]) -> str | None:
    for message in reversed(messages):
        if isinstance(message, ModelRequest):
            for part in reversed(message.parts):
                if isinstance(part, ToolReturnPart):
                    return part.tool_name
    return None


def test_real_pydantic_tool_loop_compacts_store_context_then_commits(tmp_path: Path) -> None:
    async def exercise() -> None:
        store = AuthoringStore(tmp_path / "authoring.sqlite3")
        await store.migrate()
        await store.create_project(
            brief="A keeper chooses a costly promise.",
            target=TargetLength.resolve(target_chapters=2),
            project_id="p1",
        )
        gateway = ToolGateway(store)
        foundation = _foundation_instruction()
        deps = EpisodeDeps(store=store, instruction=foundation, profile=_profile())
        await store.set_active_instruction(
            "p1",
            foundation.instruction_key,
            foundation.kind.value,
            foundation.logical_target,
            foundation.fact_version,
        )
        await store.start_episode(
            episode_id=deps.episode_id,
            project_id="p1",
            worker="architect",
            instruction_key=foundation.instruction_key,
            profile_snapshot=_profile().model_dump(mode="json"),
        )
        await gateway.invoke(deps, "save_book", {"title": "The Costly Bell"})
        await gateway.invoke(
            deps,
            "save_foundation",
            {
                "premise": "A keeper chooses a costly promise.",
                "compass": "Every choice has a visible consequence.",
                "characters": [{"name": "Lin", "goal": "keep the promise"}],
                "world": {"large_re-readable_lore": "lore " * 12_000},
                "outline": ["choose", "pay the cost"],
                "planned_through": 2,
            },
        )
        await gateway.invoke(deps, "audit_foundation", {"passed": True, "issues": []})
        await store.finish_episode(deps.episode_id, "p1", succeeded=True)
        await store.set_active_instruction("p1", None, None, None)
        await store.set_status("p1", RunStatus.RUNNING)

        def respond(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
            last = _last_tool_return(messages)
            if last is None:
                return ModelResponse(parts=[ToolCallPart("novel_context", {})])
            if last == "novel_context":
                return ModelResponse(
                    parts=[
                        ToolCallPart(
                            "plan_chapter",
                            {"chapter_number": 1, "plan": "choose and accept the cost"},
                        )
                    ]
                )
            if last == "plan_chapter":
                return ModelResponse(
                    parts=[
                        ToolCallPart(
                            "draft_chapter",
                            {
                                "chapter_number": 1,
                                "content": "Lin rang the bell and accepted its cost.",
                            },
                        )
                    ]
                )
            if last == "draft_chapter":
                return ModelResponse(
                    parts=[
                        ToolCallPart(
                            "check_consistency",
                            {"chapter_number": 1, "passed": True, "issues": []},
                        )
                    ]
                )
            if last == "check_consistency":
                return ModelResponse(
                    parts=[
                        ToolCallPart(
                            "commit_chapter",
                            {
                                "chapter_number": 1,
                                "title": "The First Bell",
                                "content": "Lin rang the bell and accepted its cost.",
                                "facts": {
                                    "summary": "Lin accepts the first cost.",
                                    "character_changes": {"Lin": "committed"},
                                },
                            },
                        )
                    ]
                )
            return ModelResponse(parts=[TextPart("chapter committed")])

        profile = _profile()

        async def stream_respond(
            messages: list[ModelMessage], info: AgentInfo
        ) -> AsyncIterator[str | dict[int, DeltaToolCall]]:
            response = respond(messages, info)
            part = response.parts[0]
            if isinstance(part, ToolCallPart):
                yield {
                    0: DeltaToolCall(
                        name=part.tool_name,
                        json_args=json.dumps(part.args, ensure_ascii=False),
                        tool_call_id=part.tool_call_id,
                    )
                }
            elif isinstance(part, TextPart):
                yield part.content
            else:
                raise TypeError(f"unexpected response part: {type(part).__name__}")

        model = FunctionModel(
            respond,
            stream_function=stream_respond,
            model_name="gateway-compaction",
        )

        def resolve_profile(_project_id: str, _role: WorkerRole) -> EpisodeProfileSelection:
            return EpisodeProfileSelection(snapshot=profile, model=model)

        engine = Engine(
            store,
            PydanticWorkerRuntime(context_manager=ContextBudgetManager(safety_margin=64)),
            resolve_profile,
        )
        with models.override_allow_model_requests(False):
            result = await engine.run("p1", max_instructions=1)

        assert result.instructions_completed == 1
        assert await store.scalar("SELECT count(*) FROM chapters") == 1
        assert (
            await store.scalar("SELECT count(*) FROM checkpoints WHERE step='commit_chapter'") == 1
        )
        events = await store.events("p1")
        compactions = [event for event in events if event["kind"] == "context_compacted"]
        assert compactions
        threshold = ContextBudgetManager(safety_margin=64).input_threshold(profile)
        assert all(event["payload"]["tokens_before"] >= threshold for event in compactions)
        assert all(event["payload"]["tokens_after"] < threshold for event in compactions)
        assert not any(event["kind"] == "context_compaction_failed" for event in events)
        assert (
            await store.scalar("SELECT count(*) FROM model_requests WHERE status='succeeded'") >= 5
        )
        request_evidence = (await store.execution_evidence("p1"))["requests"]
        assert all(row["episode_id"] for row in request_evidence)
        assert [row["request_index"] for row in request_evidence] == list(
            range(1, len(request_evidence) + 1)
        )
        event_kinds = [event["kind"] for event in events]
        assert event_kinds.index("model_request_started") < event_kinds.index(
            "model_output_started"
        )
        assert event_kinds.index("model_output_started") < event_kinds.index("model_tool_streamed")
        assert event_kinds.index("model_tool_streamed") < event_kinds.index(
            "model_request_completed"
        )

    asyncio.run(exercise())
