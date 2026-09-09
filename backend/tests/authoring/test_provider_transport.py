from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import httpx
import pytest
from app.authoring.models.transport import (
    ActivationRequestBudget,
    ActivationRequestBudgetExhausted,
    RequestCountingModel,
    build_observed_transport,
)
from pydantic import BaseModel
from pydantic_ai import Agent, NativeOutput
from pydantic_ai.models.function import AgentInfo, FunctionModel


def test_transport_never_hides_http_retries() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, request=request, json={"error": "temporary"})

    async def exercise() -> tuple[int, int, int]:
        budget = ActivationRequestBudget(model_request_limit=2)
        transport = build_observed_transport(
            budget=budget,
            wrapped=httpx.MockTransport(handler),
        )
        budget.begin_agent_run()
        async with httpx.AsyncClient(transport=transport) as client:
            response = await client.get("https://provider.example/responses")
            assert response.status_code == 503
        budget.assert_terminal_invariants()
        return (
            budget.provider_request_count,
            budget.transport_retry_count,
            budget.model_request_count,
        )

    assert asyncio.run(exercise()) == (1, 0, 1)
    assert calls == 1


def test_six_physical_requests_share_one_replay_budget() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, request=request, json={"ok": True})

    async def exercise() -> tuple[int, int, int]:
        budget = ActivationRequestBudget(model_request_limit=2)
        transport = build_observed_transport(
            budget=budget,
            wrapped=httpx.MockTransport(handler),
        )
        async with httpx.AsyncClient(transport=transport) as client:
            for _ in range(6):
                budget.begin_agent_run()
                response = await client.get("https://provider.example/responses")
                assert response.status_code == 200
            budget.begin_agent_run()
            with pytest.raises(ActivationRequestBudgetExhausted):
                await client.get("https://provider.example/responses")
        budget.assert_terminal_invariants()
        return (
            budget.provider_request_count,
            budget.transport_retry_count,
            budget.model_request_count,
        )

    assert asyncio.run(exercise()) == (6, 5, 1)
    assert calls == 6


class InterruptedStream(httpx.AsyncByteStream):
    def __init__(self, request: httpx.Request) -> None:
        self._request = request

    async def __aiter__(self):
        yield b'data: {"type":"response.output_text.delta"}\n\n'
        raise httpx.ReadTimeout("stream stalled", request=self._request)

    async def aclose(self) -> None:
        return None


def test_stream_error_after_http_200_is_observed_as_same_physical_request() -> None:
    clock = iter((10, 20, 30, 40, 50))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            headers={"x-request-id": "request/one"},
            stream=InterruptedStream(request),
        )

    async def exercise() -> ActivationRequestBudget:
        budget = ActivationRequestBudget(
            model_request_limit=1,
            protocol="openai_responses",
            now_ms=lambda: next(clock),
        )
        transport = build_observed_transport(
            budget=budget,
            wrapped=httpx.MockTransport(handler),
        )
        budget.begin_agent_run()
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(httpx.ReadTimeout):
                async with client.stream(
                    "POST", "https://provider.example/v1/responses"
                ) as response:
                    async for _chunk in response.aiter_bytes():
                        pass
        budget.assert_terminal_invariants()
        return budget

    budget = asyncio.run(exercise())
    assert budget.provider_request_count == 1
    assert budget.model_request_count == 1
    assert budget.transport_retry_count == 0
    assert len(budget.attempts) == 1
    attempt = budget.attempts[0]
    assert attempt.status_code == 200
    assert attempt.provider_request_id == "request/one"
    assert attempt.first_event_at_ms == 30
    assert attempt.last_event_at_ms == 30
    assert attempt.finished_at_ms == 40
    assert attempt.error_type == "ReadTimeout"


class StructuredResult(BaseModel):
    value: str


def test_output_repair_and_full_task_replay_share_semantic_and_retry_budgets() -> None:
    statuses = iter((503, 200, 200))
    model_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        status = next(statuses)
        return httpx.Response(status, request=request, json={"status": status})

    async def exercise() -> tuple[StructuredResult, int, int, int]:
        nonlocal model_calls
        budget = ActivationRequestBudget(model_request_limit=2)
        transport = build_observed_transport(
            budget=budget,
            wrapped=httpx.MockTransport(handler),
        )
        async with httpx.AsyncClient(transport=transport) as client:

            async def model_response(
                _messages: list[object],
                _info: AgentInfo,
            ) -> AsyncIterator[str]:
                nonlocal model_calls
                model_calls += 1
                response = await client.post("https://provider.example/responses", json={})
                response.raise_for_status()
                payload = {"wrong": "shape"} if model_calls == 2 else {"value": "valid"}
                yield json.dumps(payload)

            model = RequestCountingModel(
                FunctionModel(
                    stream_function=model_response,
                    model_name="combined-budget",
                ),
                budget=budget,
            )
            agent = Agent(
                model,
                output_type=NativeOutput(StructuredResult, strict=True),
                retries={"tools": 0, "output": 1},
            )
            while True:
                budget.begin_agent_run()
                try:
                    output = (await agent.run("Return a structured result.")).output
                except httpx.HTTPStatusError:
                    budget.record_retry_decision(
                        retry=True,
                        reason="provider_http_503",
                        delay_seconds=0,
                    )
                    continue
                break
        budget.assert_terminal_invariants()
        return (
            output,
            budget.provider_request_count,
            budget.transport_retry_count,
            budget.model_request_count,
        )

    assert asyncio.run(exercise()) == (StructuredResult(value="valid"), 3, 1, 2)
    assert model_calls == 3
