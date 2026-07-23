from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from pydantic import BaseModel
from pydantic_ai import Agent, ModelResponse, NativeOutput, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from app.agents.transport import (
    ActivationRequestBudget,
    RequestCountingModel,
    build_retrying_transport,
)


async def _no_sleep(_seconds: float) -> None:
    return None


def test_transport_retries_share_one_six_request_budget() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 6:
            return httpx.Response(503, request=request, json={"error": "temporary"})
        return httpx.Response(200, request=request, json={"ok": True})

    async def exercise() -> tuple[int, int, int]:
        budget = ActivationRequestBudget(model_request_limit=2)
        transport = build_retrying_transport(
            budget=budget,
            wrapped=httpx.MockTransport(handler),
            sleep=_no_sleep,
        )
        async with httpx.AsyncClient(transport=transport) as client:
            response = await client.get("https://provider.example/responses")
            assert response.status_code == 200
        budget.assert_terminal_invariants()
        return (
            budget.provider_request_count,
            budget.transport_retry_count,
            budget.model_request_count,
        )

    assert asyncio.run(exercise()) == (6, 5, 1)


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (401, {"error": {"code": "invalid_api_key"}}),
        (429, {"error": {"code": "insufficient_quota", "message": "credits exhausted"}}),
    ],
)
def test_auth_and_explicit_quota_fail_fast(status: int, body: dict[str, object]) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status, request=request, json=body)

    async def exercise() -> tuple[int, int]:
        budget = ActivationRequestBudget(model_request_limit=2)
        transport = build_retrying_transport(
            budget=budget,
            wrapped=httpx.MockTransport(handler),
            sleep=_no_sleep,
        )
        async with httpx.AsyncClient(transport=transport) as client:
            response = await client.post("https://provider.example/responses", json={})
            assert response.status_code == status
        return budget.provider_request_count, budget.transport_retry_count

    assert asyncio.run(exercise()) == (1, 0)
    assert calls == 1


class StructuredResult(BaseModel):
    value: str


def test_native_output_repair_and_transport_retries_cannot_multiply_budget() -> None:
    statuses = iter((503, 200, 503, 503, 503, 200))
    physical_calls = 0
    model_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal physical_calls
        physical_calls += 1
        status = next(statuses)
        return httpx.Response(status, request=request, json={"status": status})

    async def exercise() -> tuple[StructuredResult, int, int, int]:
        nonlocal model_calls
        budget = ActivationRequestBudget(model_request_limit=2)
        transport = build_retrying_transport(
            budget=budget,
            wrapped=httpx.MockTransport(handler),
            sleep=_no_sleep,
        )
        async with httpx.AsyncClient(transport=transport) as client:

            async def model_response(
                _messages: list[object],
                _info: AgentInfo,
            ) -> ModelResponse:
                nonlocal model_calls
                model_calls += 1
                response = await client.post("https://provider.example/responses", json={})
                assert response.status_code == 200
                payload = {"wrong": "shape"} if model_calls == 1 else {"value": "valid"}
                return ModelResponse(parts=[TextPart(json.dumps(payload))])

            model = RequestCountingModel(
                FunctionModel(model_response, model_name="combined-budget"),
                budget=budget,
            )
            agent = Agent(
                model,
                output_type=NativeOutput(StructuredResult, strict=True),
                retries={"tools": 0, "output": 1},
            )
            result = await agent.run("Return a structured result.")
        budget.assert_terminal_invariants()
        return (
            result.output,
            budget.provider_request_count,
            budget.transport_retry_count,
            budget.model_request_count,
        )

    assert asyncio.run(exercise()) == (StructuredResult(value="valid"), 6, 4, 2)
    assert physical_calls == 6
    assert model_calls == 2
