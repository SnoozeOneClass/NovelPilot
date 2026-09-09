from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from app.authoring.models.binding import (
    ModelBindingResolver,
    OpenAIResponsesAdapter,
    ProfileCredential,
)
from app.authoring.models.catalog import StoredProfile
from app.authoring.models.contracts import ProfileCapabilities, ProfileSnapshot
from app.authoring.models.probe import (
    PROBE_MARKER,
    ProfileCapabilityProbeError,
    probe_stored_profile,
)
from pydantic import SecretStr
from pydantic_ai import Agent, models

pytestmark = pytest.mark.synthetic_integration

MODEL_ID = "deployment/opaque-usage-fixture"
RESPONSE_USAGE: dict[str, object] = {
    "input_tokens": 354,
    "input_tokens_details": {"cached_tokens": 128, "cache_write_tokens": 16},
    "output_tokens": 21,
    "output_tokens_details": {"reasoning_tokens": 13},
    "total_tokens": 375,
}


def _text_sse(text: str, *, usage: dict[str, object] | None) -> bytes:
    response: dict[str, object] = {
        "id": "resp-usage-fixture",
        "object": "response",
        "created_at": 1_783_468_800,
        "model": MODEL_ID,
        "status": "in_progress",
        "output": [],
        "parallel_tool_calls": False,
        "tool_choice": "auto",
        "tools": [],
    }
    message: dict[str, object] = {
        "id": "msg-usage-fixture",
        "type": "message",
        "role": "assistant",
        "status": "in_progress",
        "content": [],
    }
    part = {"type": "output_text", "text": text, "annotations": [], "logprobs": []}
    completed_message = {**message, "status": "completed", "content": [part]}
    completed_response = {**response, "status": "completed", "output": [completed_message]}
    if usage is not None:
        completed_response["usage"] = usage
    events = [
        {"type": "response.created", "response": response},
        {"type": "response.output_item.added", "output_index": 0, "item": message},
        {
            "type": "response.content_part.added",
            "item_id": message["id"],
            "output_index": 0,
            "content_index": 0,
            "part": {**part, "text": ""},
        },
        {
            "type": "response.output_text.delta",
            "item_id": message["id"],
            "output_index": 0,
            "content_index": 0,
            "delta": text,
            "logprobs": [],
        },
        {
            "type": "response.output_text.done",
            "item_id": message["id"],
            "output_index": 0,
            "content_index": 0,
            "text": text,
            "logprobs": [],
        },
        {
            "type": "response.content_part.done",
            "item_id": message["id"],
            "output_index": 0,
            "content_index": 0,
            "part": part,
        },
        {"type": "response.output_item.done", "output_index": 0, "item": completed_message},
        {"type": "response.completed", "response": completed_response},
    ]
    return "".join(
        f"event: {event['type']}\ndata: {json.dumps({**event, 'sequence_number': index})}\n\n"
        for index, event in enumerate(events)
    ).encode("utf-8")


@pytest.mark.parametrize("stream", [False, True], ids=["agent-run", "agent-stream"])
def test_responses_adapter_preserves_completed_usage_with_opaque_model(stream: bool) -> None:
    captured: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/responses"
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_text_sse(PROBE_MARKER, usage=RESPONSE_USAGE),
        )

    profile = ProfileSnapshot.create(
        profile_id="usage-compatibility",
        display_name="Usage compatibility",
        api_family="openai_responses",
        base_url="https://provider.example/v1",
        model_id=MODEL_ID,
        capabilities=ProfileCapabilities(text_streaming=True, usage_reporting=True),
    )
    binding = OpenAIResponsesAdapter(transport_factory=lambda: httpx.MockTransport(handler)).build(
        profile=profile,
        credential=ProfileCredential.from_plaintext("test-secret"),
        model_request_limit=1,
    )

    async def exercise() -> None:
        binding.budget.begin_agent_run()
        async with binding:
            agent = Agent(binding.model, output_type=str)
            if stream:
                async with agent.run_stream("Return the capability marker.") as streamed:
                    chunks = [
                        delta async for delta in streamed.stream_text(delta=True, debounce_by=None)
                    ]
                    output = await streamed.get_output()
                    assert "".join(chunks) == output
                    response, usage = streamed.response, streamed.usage
            else:
                result = await agent.run("Return the capability marker.")
                output = result.output
                response, usage = result.response, result.usage
        assert output == PROBE_MARKER
        assert response.state == "complete"
        assert response.model_name == MODEL_ID
        assert usage.requests == 1
        # Assert both the SDK-to-model extraction and the Agent's accumulated usage.
        for measured in (response.usage, usage):
            assert measured.input_tokens == 354
            assert measured.output_tokens == 21
            assert measured.total_tokens == 375
            assert measured.cache_read_tokens == 128
            assert measured.cache_write_tokens == 16
            assert measured.details["reasoning_tokens"] == 13

    with models.override_allow_model_requests(True):
        asyncio.run(exercise())

    binding.budget.assert_terminal_invariants()
    assert binding.budget.provider_request_count == 1
    assert len(captured) == 1
    assert captured[0]["model"] == MODEL_ID
    assert captured[0]["stream"] is True


def test_real_capability_probe_rejects_complete_response_without_usage() -> None:
    captured: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_text_sse(json.dumps({"marker": PROBE_MARKER}), usage=None),
        )

    profile = StoredProfile(
        id="usage-compatibility",
        display_name="Usage compatibility",
        api_family="openai_responses",
        base_url="https://provider.example/v1",
        api_key=SecretStr("test-secret"),
        model_id=MODEL_ID,
    )
    resolver = ModelBindingResolver(
        adapters=[OpenAIResponsesAdapter(transport_factory=lambda: httpx.MockTransport(handler))]
    )
    with (
        models.override_allow_model_requests(True),
        pytest.raises(ProfileCapabilityProbeError, match="usable request/token usage"),
    ):
        asyncio.run(probe_stored_profile(profile, resolver=resolver))

    # A valid structured response with missing usage must fail before further probe requests.
    assert len(captured) == 1
    assert captured[0]["model"] == MODEL_ID
    assert captured[0]["stream"] is True
