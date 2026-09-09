from __future__ import annotations

import asyncio
from pathlib import Path

from app.authoring.domain.models import AuthoringProfileSnapshot, TargetLength, WorkerRole
from app.authoring.models import EpisodeProfileSelection
from app.authoring.runtime import Engine
from app.authoring.store import AuthoringStore
from app.authoring.workers import PydanticWorkerRuntime
from pydantic_ai import ModelResponse, TextPart, ToolCallPart, models
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import ModelMessage, ModelRequest, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel


def _profile(identifier: str) -> AuthoringProfileSnapshot:
    return AuthoringProfileSnapshot(
        profile_id=identifier,
        provider_protocol="function",
        model_id=identifier,
        context_window=16_384,
        max_output_tokens=1_024,
        input_price_per_million=1,
        output_price_per_million=2,
    )


def _last_tool(messages: list[ModelMessage]) -> str | None:
    for message in reversed(messages):
        if isinstance(message, ModelRequest):
            for part in reversed(message.parts):
                if isinstance(part, ToolReturnPart):
                    return part.tool_name
    return None


def test_pre_output_failure_uses_explicit_fallback_and_records_actual_profile(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        store = AuthoringStore(tmp_path / "authoring.sqlite3")
        await store.migrate()
        await store.create_project(
            brief="A promise before dawn.",
            target=TargetLength.resolve(target_chapters=1),
            project_id="p1",
        )

        def fail_before_output(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
            raise ModelHTTPError(503, "primary", {"error": "temporarily unavailable"})

        def fallback_response(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
            last = _last_tool(messages)
            if last is None:
                return ModelResponse(parts=[ToolCallPart("save_book", {"title": "Fallback"})])
            if last == "save_book":
                return ModelResponse(
                    parts=[
                        ToolCallPart(
                            "save_foundation",
                            {
                                "premise": "A promise before dawn.",
                                "compass": "Every choice has a cost.",
                                "characters": [{"name": "Lin", "goal": "finish"}],
                                "world": {"rule": "bells remember"},
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

        primary = _profile("primary")
        fallback = _profile("fallback")
        primary_model = FunctionModel(fail_before_output, model_name="primary")
        fallback_model = FunctionModel(fallback_response, model_name="fallback")

        def resolve(_project_id: str, _role: WorkerRole) -> EpisodeProfileSelection:
            return EpisodeProfileSelection(
                snapshot=primary,
                model=primary_model,
                fallback_snapshot=fallback,
                fallback_model=fallback_model,
            )

        with models.override_allow_model_requests(False):
            result = await Engine(store, PydanticWorkerRuntime(), resolve).run(
                "p1", max_instructions=1
            )

        assert result.instructions_completed == 1
        events = await store.events("p1")
        fallback_events = [event for event in events if event["kind"] == "model_fallback"]
        assert len(fallback_events) == 1
        assert fallback_events[0]["payload"]["to_profile"] == "fallback"
        usage_metadata = await store.scalar("SELECT metadata_json FROM model_usage")
        assert usage_metadata is not None
        assert '"profile_id":"fallback"' in usage_metadata
        assert (
            await store.scalar("SELECT profile_fingerprint FROM model_usage")
            == fallback.fingerprint
        )

    asyncio.run(exercise())


def test_fallback_is_blocked_after_primary_tool_response_and_side_effect(tmp_path: Path) -> None:
    async def exercise() -> None:
        store = AuthoringStore(tmp_path / "blocked.sqlite3")
        await store.migrate()
        await store.create_project(
            brief="Do not splice providers.",
            target=TargetLength.resolve(target_chapters=1),
            project_id="p1",
        )
        fallback_calls = 0

        def primary_response(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
            if _last_tool(messages) is None:
                return ModelResponse(parts=[ToolCallPart("save_book", {"title": "Primary"})])
            raise ModelHTTPError(503, "primary", {"error": "after Tool response"})

        def fallback_response(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
            nonlocal fallback_calls
            fallback_calls += 1
            return ModelResponse(parts=[TextPart("must not run")])

        primary = _profile("primary")
        fallback = _profile("fallback")

        def resolve(_project_id: str, _role: WorkerRole) -> EpisodeProfileSelection:
            return EpisodeProfileSelection(
                snapshot=primary,
                model=FunctionModel(primary_response, model_name="primary-after-tool"),
                fallback_snapshot=fallback,
                fallback_model=FunctionModel(fallback_response, model_name="forbidden-fallback"),
            )

        with models.override_allow_model_requests(False):
            result = await Engine(
                store,
                PydanticWorkerRuntime(),
                resolve,
                max_instruction_retries=0,
            ).run("p1")

        assert result.status.value == "failure_paused"
        assert fallback_calls == 0
        events = await store.events("p1")
        assert not any(event["kind"] == "model_fallback" for event in events)
        assert any(
            event["kind"] == "model_request_failed"
            and event["payload"]["fallback_allowed"] is False
            for event in events
        )
        assert await store.scalar("SELECT title FROM projects WHERE id='p1'") == "Primary"

    asyncio.run(exercise())


def test_fallback_is_blocked_after_text_output_without_tool_side_effect(tmp_path: Path) -> None:
    async def exercise() -> None:
        store = AuthoringStore(tmp_path / "blocked-after-text.sqlite3")
        await store.migrate()
        await store.create_project(
            brief="Do not splice a partial answer.",
            target=TargetLength.resolve(target_chapters=1),
            project_id="p1",
        )
        primary_calls = 0
        fallback_calls = 0

        def primary_response(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
            nonlocal primary_calls
            primary_calls += 1
            if primary_calls == 1:
                return ModelResponse(parts=[TextPart("premature final answer")])
            raise ModelHTTPError(503, "primary", {"error": "after output"})

        def fallback_response(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
            nonlocal fallback_calls
            fallback_calls += 1
            return ModelResponse(parts=[TextPart("must not run")])

        primary = _profile("primary")
        fallback = _profile("fallback")

        def resolve(_project_id: str, _role: WorkerRole) -> EpisodeProfileSelection:
            return EpisodeProfileSelection(
                snapshot=primary,
                model=FunctionModel(primary_response, model_name="primary-after-text"),
                fallback_snapshot=fallback,
                fallback_model=FunctionModel(fallback_response, model_name="forbidden-fallback"),
            )

        with models.override_allow_model_requests(False):
            result = await Engine(
                store,
                PydanticWorkerRuntime(),
                resolve,
                max_instruction_retries=0,
            ).run("p1")

        assert result.status.value == "failure_paused"
        assert primary_calls == 2
        assert fallback_calls == 0
        assert await store.scalar("SELECT count(*) FROM tool_invocations") == 0
        events = await store.events("p1")
        assert any(
            event["kind"] == "model_request_failed"
            and event["payload"]["fallback_allowed"] is False
            for event in events
        )

    asyncio.run(exercise())
