from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import pytest
from pydantic import BaseModel
from pydantic_ai import Agent, ModelResponse, NativeOutput, RequestUsage, TextPart, models
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel


class ArcPlanContract(BaseModel):
    title: str
    beats: list[str]


models.ALLOW_MODEL_REQUESTS = False


def test_native_output_is_framework_validated_and_reports_usage() -> None:
    def structured_response(
        _messages: list[object],
        info: AgentInfo,
    ) -> ModelResponse:
        assert info.model_request_parameters.output_mode == "native"
        return ModelResponse(
            parts=[TextPart(json.dumps({"title": "First arc", "beats": ["arrival", "choice"]}))],
            usage=RequestUsage(input_tokens=11, output_tokens=7),
        )

    agent = Agent(
        FunctionModel(structured_response, model_name="contract-native"),
        output_type=NativeOutput(ArcPlanContract, strict=True),
    )

    result = asyncio.run(agent.run("Plan the first arc."))

    assert result.output == ArcPlanContract(
        title="First arc",
        beats=["arrival", "choice"],
    )
    assert result.usage.requests == 1
    assert result.usage.input_tokens == 11
    assert result.usage.output_tokens == 7


def test_native_output_capability_failure_does_not_fallback_to_prompt_json() -> None:
    agent = Agent(
        TestModel(custom_output_text='{"title":"fallback","beats":[]}'),
        output_type=NativeOutput(ArcPlanContract),
    )

    with pytest.raises(Exception, match="Native structured output is not supported"):
        asyncio.run(agent.run("Plan without the required capability."))


def test_plain_text_stream_yields_provider_deltas_and_complete_output() -> None:
    async def streamed_response(
        _messages: list[object],
        _info: AgentInfo,
    ) -> AsyncIterator[str]:
        yield "第一段"
        yield "，冲突建立。"

    async def exercise() -> tuple[list[str], str, int]:
        agent = Agent(
            FunctionModel(stream_function=streamed_response, model_name="contract-text-stream"),
            output_type=str,
        )
        async with agent.run_stream("Write chapter prose.") as result:
            deltas = [
                delta
                async for delta in result.stream_text(delta=True, debounce_by=None)
            ]
            output = await result.get_output()
            requests = result.usage.requests
        return deltas, output, requests

    deltas, output, requests = asyncio.run(exercise())

    assert deltas == ["第一段", "，冲突建立。"]
    assert output == "第一段，冲突建立。"
    assert requests == 1


def test_model_exception_preserves_its_type_for_adapter_classification() -> None:
    class ContractTransportError(ConnectionError):
        pass

    def failing_response(
        _messages: list[object],
        _info: AgentInfo,
    ) -> ModelResponse:
        raise ContractTransportError("connection interrupted")

    agent = Agent(FunctionModel(failing_response), output_type=str)

    with pytest.raises(ContractTransportError, match="connection interrupted"):
        asyncio.run(agent.run("This request fails."))


def test_stream_cancel_closes_provider_stream_and_marks_response_interrupted() -> None:
    closed = asyncio.Event()
    continue_streaming = asyncio.Event()

    async def slow_response(
        _messages: list[object],
        _info: AgentInfo,
    ) -> AsyncIterator[str]:
        try:
            yield "partial"
            await continue_streaming.wait()
            yield "unreachable"
        finally:
            closed.set()

    async def exercise() -> tuple[bool, str | None]:
        agent = Agent(FunctionModel(stream_function=slow_response), output_type=str)
        async with agent.run_stream("Start prose.") as result:
            stream = result.stream_text(delta=True, debounce_by=None)
            assert await anext(stream) == "partial"
            await result.cancel()
            cancelled = result.cancelled
            response_state = result.response.state
        await asyncio.wait_for(closed.wait(), timeout=1)
        return cancelled, response_state

    cancelled, response_state = asyncio.run(exercise())

    assert cancelled
    assert response_state == "interrupted"
