from __future__ import annotations

import asyncio
import json

import httpx
import httpx2
import pytest
from app.authoring.models.binding import (
    AnthropicMessagesAdapter,
    OpenAIResponsesAdapter,
    ProfileCredential,
    ResolvedModelBinding,
)
from app.authoring.models.contracts import ApiFamily, ProfileCapabilities, ProfileSnapshot
from app.authoring.models.retry import retryable_provider_failure
from app.authoring.models.transport import (
    ActivationRequestBudget,
    ActivationRequestBudgetExhausted,
    ModelRequestBudgetExhausted,
    TransportRetryBudgetExhausted,
    build_observed_transport,
    request_budget_exhaustion,
)
from openai import APIConnectionError
from pydantic_ai import Agent, models
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError
from pydantic_ai.messages import ModelRequest, ModelResponse, UserPromptPart
from pydantic_ai.models import ModelRequestParameters
from test_usage_extraction_compatibility import RESPONSE_USAGE, _text_sse

pytestmark = pytest.mark.synthetic_integration


def _anthropic_text_sse() -> bytes:
    events = [
        {
            "type": "message_start",
            "message": {
                "id": "msg-budget",
                "type": "message",
                "role": "assistant",
                "model": "opaque-model",
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 10, "output_tokens": 0},
            },
        },
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": ""},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "done"},
        },
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": 1},
        },
        {"type": "message_stop"},
    ]
    return "".join(
        f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events
    ).encode()


def _binding(
    api_family: ApiFamily, handler, *, limit: int, retries: int = 5
) -> ResolvedModelBinding:
    profile = ProfileSnapshot.create(
        profile_id="offline-budget",
        display_name="Offline request budget",
        api_family=api_family,
        base_url=(
            "https://provider.example/v1"
            if api_family == "openai_responses"
            else "https://provider.example"
        ),
        model_id="opaque-model",
        capabilities=ProfileCapabilities(text_streaming=True, tool_calling=True),
        request_options={"max_tokens": 2048},
    )
    adapter = (
        OpenAIResponsesAdapter(transport_factory=lambda: httpx.MockTransport(handler))
        if api_family == "openai_responses"
        else AnthropicMessagesAdapter(transport_factory=lambda: httpx2.MockTransport(handler))
    )
    return adapter.build(
        profile=profile,
        credential=ProfileCredential.from_plaintext("synthetic-test-credential"),
        model_request_limit=limit,
        transport_retry_limit=retries,
    )


async def _request(binding: ResolvedModelBinding, *, streaming: bool) -> ModelResponse:
    messages = [ModelRequest(parts=[UserPromptPart("Return done.")])]
    parameters = ModelRequestParameters()
    if streaming:
        async with binding.model.request_stream(messages, None, parameters) as response:
            async for _event in response:
                pass
            return response.get()
    return await binding.model.request(messages, None, parameters)


@pytest.mark.parametrize("api_family", ["openai_responses", "anthropic_messages"])
@pytest.mark.parametrize("streaming", [False, True], ids=["request", "request_stream"])
@pytest.mark.parametrize("limit", [2, 24], ids=["probe", "worker"])
def test_production_adapter_honors_all_semantic_calls_before_typed_exhaustion(
    api_family: ApiFamily, streaming: bool, limit: int
) -> None:
    requests = []

    def handler(request):
        requests.append(request)
        response_type = httpx.Response if api_family == "openai_responses" else httpx2.Response
        return response_type(
            200,
            headers={"content-type": "text/event-stream"},
            content=(
                _text_sse("done", usage=RESPONSE_USAGE)
                if api_family == "openai_responses"
                else _anthropic_text_sse()
            ),
        )

    binding = _binding(api_family, handler, limit=limit)

    async def exercise() -> None:
        async with binding:
            binding.budget.begin_agent_run()
            for _ in range(limit):
                response = await _request(binding, streaming=streaming)
                assert response.text == "done"
            with pytest.raises(ActivationRequestBudgetExhausted) as error:
                await _request(binding, streaming=streaming)
            assert isinstance(error.value, ModelRequestBudgetExhausted)
            assert not binding.budget.can_replay
            binding.budget.begin_agent_run()
            with pytest.raises(ModelRequestBudgetExhausted) as repeated:
                await _request(binding, streaming=streaming)
            assert repeated.value is error.value

    with models.override_allow_model_requests(True):
        asyncio.run(exercise())
    assert len(requests) == limit
    assert binding.budget.model_request_count == limit
    assert binding.budget.transport_retry_count == 0
    assert binding.budget.provider_request_count == limit
    assert binding.budget.provider_request_limit == limit + 5
    binding.budget.assert_terminal_invariants()


@pytest.mark.parametrize(
    "limits",
    [
        {"model_request_limit": 0},
        {"model_request_limit": -1},
        {"model_request_limit": True},
        {"model_request_limit": 2.5},
        {"model_request_limit": float("inf")},
        {"model_request_limit": 2, "transport_retry_limit": -1},
        {"model_request_limit": 2, "transport_retry_limit": True},
        {"model_request_limit": 2, "transport_retry_limit": float("inf")},
        {"model_request_limit": 24, "provider_request_limit": 6},
        {"model_request_limit": 2, "provider_request_limit": 6},
        {"model_request_limit": 2, "provider_request_limit": float("inf")},
    ],
)
def test_request_limits_reject_invalid_or_incompatible_policies(limits) -> None:
    with pytest.raises(ValueError):
        ActivationRequestBudget(**limits)


@pytest.mark.parametrize("api_family", ["openai_responses", "anthropic_messages"])
def test_binding_rejects_invalid_budget_before_any_provider_send(api_family: ApiFamily) -> None:
    with pytest.raises(ValueError, match="model_request_limit"):
        _binding(api_family, lambda _request: pytest.fail("No HTTP allowed"), limit=0)


@pytest.mark.parametrize("api_family", ["openai_responses", "anthropic_messages"])
def test_binding_accepts_an_explicit_probe_budget_without_retries(api_family: ApiFamily) -> None:
    binding = _binding(
        api_family, lambda _request: pytest.fail("No HTTP expected"), limit=2, retries=0
    )
    try:
        assert binding.budget.provider_request_limit == 2
        assert binding.budget.transport_retry_limit == 0
    finally:
        asyncio.run(binding.aclose())


def test_physical_budget_counts_all_replay_sends_and_stops_before_extra_http() -> None:
    requests = []
    budget = ActivationRequestBudget(
        model_request_limit=3, transport_retry_limit=2, provider_request_limit=5
    )

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    async def exercise() -> None:
        transport = build_observed_transport(budget=budget, wrapped=httpx.MockTransport(handler))
        async with httpx.AsyncClient(transport=transport) as client:
            for run_length in (3, 2):
                budget.begin_agent_run()
                for _ in range(run_length):
                    budget.begin_model_call()
                    try:
                        await client.post("https://provider.example/responses")
                    finally:
                        budget.end_model_call()
            assert not budget.can_replay
            with pytest.raises(ActivationRequestBudgetExhausted) as error:
                # Bypass semantic preflight to exercise the final transport guard.
                await client.post("https://provider.example/responses")
            assert type(error.value) is ActivationRequestBudgetExhausted
            budget.begin_agent_run()
            with pytest.raises(ActivationRequestBudgetExhausted) as repeated:
                budget.begin_model_call()
            assert repeated.value is error.value

    asyncio.run(exercise())
    assert len(requests) == budget.provider_request_count == 5
    assert budget.model_request_count == 3
    assert budget.transport_retry_count == 2
    budget.assert_terminal_invariants()


def test_replays_cannot_spend_unused_semantic_capacity_as_extra_retries() -> None:
    request = httpx.Request("POST", "https://provider.example/responses")
    budget = ActivationRequestBudget(model_request_limit=3, transport_retry_limit=1)
    for run_length in (1, 2):
        budget.begin_agent_run()
        for _ in range(run_length):
            budget.begin_model_call()
            budget.begin_provider_request(request)
            budget.end_model_call()
    # Replay ordinal 1 consumed the retry, while new ordinal 2 remained semantic.
    assert (budget.model_request_count, budget.transport_retry_count) == (2, 1)
    assert budget.provider_request_count == 3
    assert budget.provider_request_limit == 4
    assert not budget.can_replay
    budget.begin_agent_run()
    with pytest.raises(TransportRetryBudgetExhausted):
        budget.begin_model_call()
    assert budget.provider_request_count == 3
    budget.assert_terminal_invariants()


def test_failed_local_model_call_does_not_fabricate_a_provider_send_or_retry() -> None:
    request = httpx.Request("POST", "https://provider.example/responses")
    budget = ActivationRequestBudget(model_request_limit=3, transport_retry_limit=1)
    budget.begin_agent_run()
    budget.begin_model_call()
    budget.end_model_call()  # Local validation can fail before transport is entered.
    budget.begin_model_call()
    budget.begin_provider_request(request)
    budget.end_model_call()
    assert (
        budget.provider_request_count,
        budget.model_request_count,
        budget.transport_retry_count,
    ) == (
        1,
        1,
        0,
    )
    budget.assert_terminal_invariants()
    budget.begin_agent_run()
    for _ in range(2):
        budget.begin_model_call()
        budget.begin_provider_request(request)
        budget.end_model_call()
    assert (
        budget.provider_request_count,
        budget.model_request_count,
        budget.transport_retry_count,
    ) == (
        3,
        2,
        1,
    )
    budget.assert_terminal_invariants()


def test_multiple_sends_within_one_model_call_use_the_retry_allowance() -> None:
    request = httpx.Request("POST", "https://provider.example/responses")
    budget = ActivationRequestBudget(model_request_limit=3, transport_retry_limit=1)
    budget.begin_model_call()
    budget.begin_provider_request(request)
    budget.begin_provider_request(request)
    with pytest.raises(TransportRetryBudgetExhausted):
        budget.begin_provider_request(request)
    budget.end_model_call()
    assert budget.provider_request_count == 2
    assert budget.model_request_count == 1
    assert budget.transport_retry_count == 1
    budget.assert_terminal_invariants()


@pytest.mark.parametrize("api_family", ["openai_responses", "anthropic_messages"])
@pytest.mark.parametrize("streaming", [False, True], ids=["request", "request_stream"])
def test_production_sdk_wrappers_preserve_original_local_exhaustion(
    api_family: ApiFamily, streaming: bool
) -> None:
    local_error = ActivationRequestBudgetExhausted("No physical capacity remains.")
    requests = []

    def handler(request):
        requests.append(request)
        raise local_error

    binding = _binding(api_family, handler, limit=24)

    async def exercise() -> None:
        async with binding:
            with pytest.raises(ActivationRequestBudgetExhausted) as error:
                await _request(binding, streaming=streaming)
            assert error.value is local_error
            assert error.value.__cause__ is None
            assert retryable_provider_failure(error.value) is None

    with models.override_allow_model_requests(True):
        asyncio.run(exercise())
    assert len(requests) == binding.budget.provider_request_count == 1
    assert binding.budget.attempts[0].error_type == "ActivationRequestBudgetExhausted"
    binding.budget.assert_terminal_invariants()


@pytest.mark.parametrize("api_family", ["openai_responses", "anthropic_messages"])
@pytest.mark.parametrize("streaming", [False, True], ids=["request", "request_stream"])
def test_real_connection_errors_keep_the_sdk_cause_and_are_not_retried_invisibly(
    api_family: ApiFamily, streaming: bool
) -> None:
    requests = []

    def handler(request):
        requests.append(request)
        error_type = httpx.ConnectError if api_family == "openai_responses" else httpx2.ConnectError
        error = error_type("The network is unavailable.", request=request)
        error.__context__ = ActivationRequestBudgetExhausted("Unrelated earlier local error.")
        raise error

    binding = _binding(api_family, handler, limit=24)

    async def exercise() -> None:
        async with binding:
            with pytest.raises(ModelAPIError) as error:
                await _request(binding, streaming=streaming)
            assert request_budget_exhaustion(error.value) is None
            assert error.value.__cause__ is not None
            assert isinstance(
                error.value.__cause__.__cause__, (httpx.ConnectError, httpx2.ConnectError)
            )

    with models.override_allow_model_requests(True):
        asyncio.run(exercise())
    assert len(requests) == binding.budget.provider_request_count == 1
    assert binding.budget.transport_retry_count == 0
    binding.budget.assert_terminal_invariants()


def test_exhaustion_helper_ignores_unrelated_context_and_cyclic_wrappers() -> None:
    local_error = ModelRequestBudgetExhausted("Semantic turns exhausted.")
    sdk_error = APIConnectionError(request=httpx.Request("POST", "https://provider.example"))
    wrapped = ModelAPIError(model_name="opaque-model", message="Connection error.")
    wrapped.__cause__ = sdk_error
    sdk_error.__cause__ = local_error
    assert request_budget_exhaustion(wrapped) is local_error
    assert retryable_provider_failure(wrapped) is None
    sdk_error.__cause__ = wrapped
    assert request_budget_exhaustion(wrapped) is None
    wrapped.__cause__ = None
    wrapped.__context__ = local_error
    assert request_budget_exhaustion(wrapped) is None
    network_error = httpx.ConnectError("Connection error.")
    network_error.__context__ = local_error
    failure = retryable_provider_failure(network_error)
    assert failure is not None
    assert failure.reason == "provider_connection_failure"


@pytest.mark.parametrize("api_family", ["openai_responses", "anthropic_messages"])
def test_retryable_http_response_keeps_one_visible_sdk_attempt(api_family: ApiFamily) -> None:
    requests = []

    def handler(request):
        requests.append(request)
        response_type = httpx.Response if api_family == "openai_responses" else httpx2.Response
        return response_type(
            503,
            json={"error": {"type": "overloaded_error", "message": "Temporary failure."}},
        )

    binding = _binding(api_family, handler, limit=24)

    async def exercise() -> None:
        async with binding:
            with pytest.raises(ModelHTTPError) as error:
                await _request(binding, streaming=False)
            failure = retryable_provider_failure(error.value)
            assert failure is not None
            assert failure.reason == "provider_http_503"
            assert request_budget_exhaustion(error.value) is None

    with models.override_allow_model_requests(True):
        asyncio.run(exercise())
    assert len(requests) == binding.budget.provider_request_count == 1
    assert binding.budget.transport_retry_count == 0
    assert binding.budget.attempts[0].status_code == 503
    binding.budget.assert_terminal_invariants()


def _tool_sse(ordinal: int) -> bytes:
    response = {
        "id": f"resp-budget-{ordinal}",
        "object": "response",
        "created_at": 1_783_468_800,
        "model": "opaque-model",
        "status": "in_progress",
        "output": [],
    }
    call = {
        "id": f"fc-budget-{ordinal}",
        "call_id": f"call-budget-{ordinal}",
        "type": "function_call",
        "name": "step",
        "arguments": "",
        "status": "in_progress",
    }
    arguments = json.dumps({"ordinal": ordinal})
    complete_call = {**call, "arguments": arguments, "status": "completed"}
    events = [
        {"type": "response.created", "response": response},
        {"type": "response.output_item.added", "output_index": 0, "item": call},
        {
            "type": "response.function_call_arguments.delta",
            "output_index": 0,
            "item_id": call["id"],
            "delta": arguments,
        },
        {"type": "response.output_item.done", "output_index": 0, "item": complete_call},
        {
            "type": "response.completed",
            "response": {
                **response,
                "status": "completed",
                "output": [complete_call],
                "usage": RESPONSE_USAGE,
            },
        },
    ]
    return "".join(
        f"event: {event['type']}\ndata: {json.dumps({**event, 'sequence_number': index})}\n\n"
        for index, event in enumerate(events)
    ).encode()


@pytest.mark.parametrize("streaming", [False, True], ids=["agent-run", "agent-stream"])
def test_production_responses_tool_loop_completes_seven_tools_with_no_transport_retries(
    streaming: bool,
) -> None:
    requests = []
    completed_steps = []

    def handler(request):
        requests.append(json.loads(request.content))
        ordinal = len(requests)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=(
                _tool_sse(ordinal) if ordinal <= 7 else _text_sse("done", usage=RESPONSE_USAGE)
            ),
        )

    binding = _binding("openai_responses", handler, limit=24)

    async def exercise() -> None:
        async with binding:
            agent = Agent(binding.model)

            @agent.tool_plain
            def step(ordinal: int) -> str:
                completed_steps.append(ordinal)
                return f"Completed step {ordinal}."

            if streaming:
                async with agent.run_stream("Complete seven steps, then return done.") as result:
                    assert await result.get_output() == "done"
            else:
                assert (await agent.run("Complete seven steps, then return done.")).output == "done"

    with models.override_allow_model_requests(True):
        asyncio.run(exercise())
    assert completed_steps == list(range(1, 8))
    assert len(requests) == binding.budget.provider_request_count == 8
    assert binding.budget.model_request_count == 8
    assert binding.budget.transport_retry_count == 0
    binding.budget.assert_terminal_invariants()
