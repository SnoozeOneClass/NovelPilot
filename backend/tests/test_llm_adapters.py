import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from anthropic import Anthropic
from openai import OpenAI
from pydantic import SecretStr

from app.llm import anthropic_compatible, openai_compatible
from app.llm.gateway import (
    ChatMessage,
    ChatRequest,
    ResponseSchema,
    ToolCall,
    ToolChoice,
    ToolDefinition,
    ToolResult,
    call_llm,
)
from app.llm.anthropic_compatible import stream_anthropic_compatible
from app.llm.openai_compatible import call_openai_compatible, stream_openai_compatible
from app.llm.provider_clients import (
    close_provider_clients,
    get_anthropic_client,
    get_openai_client,
)
from app.llm.provider_errors import ProviderCallError
from app.schemas.profiles import LlmProfile, LlmProtocol


class _CreateEndpoint:
    def __init__(self, response: Any, captured: dict[str, Any]) -> None:
        self._response = response
        self._captured = captured

    def create(self, **kwargs: Any) -> Any:
        self._captured.update(kwargs)
        return self._response


def test_openai_compatible_adapter_merges_arbitrary_request_options(monkeypatch) -> None:
    captured = _install_openai_fake(
        monkeypatch,
        {
            "model": "story-model",
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "ok"}],
                }
            ],
            "usage": {"total_tokens": 3},
        },
    )
    profile = LlmProfile(
        id="main",
        name="Main",
        protocol="openai-compatible",
        base_url="https://api.example.com/v1",
        api_key=SecretStr("secret"),
        model="story-model",
        request_options={
            "temperature": 0.25,
            "max_completion_tokens": 1200,
            "provider_extension": {"mode": "novel"},
            "model": "must-not-replace-profile-model",
            "messages": [{"role": "user", "content": "must not replace context"}],
            "input": [{"role": "user", "content": "must not replace context"}],
            "stream": True,
        },
    )

    result = call_openai_compatible(
        profile,
        ChatRequest(
            profile_id="main",
            messages=[ChatMessage(role="user", content="Return JSON.")],
            stream=False,
            request_options={
                "temperature": 0.6,
                "response_format": {"type": "json_object"},
                "text": {"format": {"type": "text"}},
                "tools": [{"type": "function", "function": {"name": "unsafe"}}],
            },
        ),
    )

    payload = _captured_payload(captured)
    assert result.content == "ok"
    assert isinstance(payload, dict)
    assert payload["model"] == "story-model"
    assert payload["input"] == [
        {"type": "message", "role": "user", "content": "Return JSON."}
    ]
    assert payload["stream"] is False
    assert payload["temperature"] == 0.6
    assert payload["max_completion_tokens"] == 1200
    assert "response_format" not in payload
    assert "text" not in payload
    assert "tools" not in payload
    assert "messages" not in payload
    assert payload["provider_extension"] == {"mode": "novel"}
    assert "max_tokens" not in payload


def test_openai_compatible_streams_text_deltas(monkeypatch) -> None:
    captured = _install_openai_fake(
        monkeypatch,
        [
            {
                "type": "response.output_text.delta",
                "delta": "hel",
            },
            {
                "type": "response.output_text.delta",
                "delta": "lo",
            },
            {
                "type": "response.completed",
                "response": {
                    "model": "story-model",
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "content": [
                                {"type": "output_text", "text": "hello"}
                            ],
                        }
                    ],
                    "usage": {"total_tokens": 5},
                },
            },
        ],
    )
    profile = _profile(protocol="openai-compatible", base_url="https://api.example.com/v1")

    chunks = list(
        stream_openai_compatible(
            profile,
            ChatRequest(
                profile_id="main",
                messages=[ChatMessage(role="user", content="Say hello.")],
            ),
        )
    )

    payload = _captured_payload(captured)
    assert isinstance(payload, dict)
    assert payload["stream"] is True
    assert [chunk.text_delta for chunk in chunks if chunk.text_delta] == ["hel", "lo"]
    assert chunks[-1].event_type == "message_stop"
    assert chunks[-1].usage == {"total_tokens": 5}


def test_openai_stream_accepts_sdk_normalized_message_event(monkeypatch) -> None:
    profile = _profile(protocol="openai-compatible", base_url="https://api.example.com/v1")
    _install_openai_fake(
        monkeypatch,
        [
            {
                "type": "response.completed",
                "response": {
                    "model": "story-model",
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "content": [{"type": "output_text", "text": "ok"}],
                        }
                    ],
                },
            }
        ],
    )

    chunks = list(
        stream_openai_compatible(
            profile,
            ChatRequest(
                profile_id="main",
                messages=[ChatMessage(role="user", content="Write.")],
            ),
        )
    )

    assert [chunk.text_delta for chunk in chunks if chunk.text_delta] == ["ok"]


def test_openai_stream_surfaces_provider_error_events(monkeypatch) -> None:
    profile = _profile(protocol="openai-compatible", base_url="https://api.example.com/v1")
    _install_openai_fake(
        monkeypatch,
        [
            {
                "type": "response.failed",
                "response": {
                    "status": "failed",
                    "error": {
                        "code": "server_error",
                        "message": "local error: tls: bad record MAC",
                    },
                },
            }
        ],
    )

    with pytest.raises(ProviderCallError, match="bad record MAC") as raised:
        list(
            stream_openai_compatible(
                profile,
                ChatRequest(
                    profile_id="main",
                    messages=[ChatMessage(role="user", content="Write.")],
                ),
            )
        )
    assert raised.value.retryable is True
    assert raised.value.stage == "stream"


def test_anthropic_compatible_streams_text_deltas(monkeypatch) -> None:
    captured = _install_anthropic_fake(
        monkeypatch,
        [
            {
                "type": "message_start",
                "message": {
                    "model": "story-model",
                    "usage": {"input_tokens": 2},
                },
            },
            {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "hel"},
            },
            {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "lo"},
            },
            {"type": "message_delta", "usage": {"output_tokens": 3}},
        ],
    )
    profile = _profile(
        protocol="anthropic-compatible",
        base_url="https://api.example.com",
    ).model_copy(
        update={
            "request_options": {
                "max_tokens": 24000,
                "thinking": {"type": "enabled", "budget_tokens": 8000},
                "system": "must not replace assembled instructions",
            }
        }
    )

    chunks = list(
        stream_anthropic_compatible(
            profile,
            ChatRequest(
                profile_id="main",
                messages=[
                    ChatMessage(role="system", content="Visible output only."),
                    ChatMessage(role="user", content="Say hello."),
                ],
            ),
        )
    )

    assert [chunk.text_delta for chunk in chunks if chunk.text_delta] == ["hel", "lo"]
    assert chunks[-1].event_type == "message_stop"
    assert chunks[-1].usage == {"input_tokens": 2, "output_tokens": 3}
    payload = _captured_payload(captured)
    assert isinstance(payload, dict)
    assert payload["max_tokens"] == 24000
    assert payload["thinking"] == {"type": "enabled", "budget_tokens": 8000}
    assert payload["system"] == "Visible output only."
    assert payload["messages"] == [{"role": "user", "content": "Say hello."}]


def test_call_llm_stream_collects_result_and_notifies_deltas(monkeypatch) -> None:
    emitted: list[str] = []
    profile = _profile(protocol="openai-compatible", base_url="https://api.example.com/v1")
    _install_openai_fake(
        monkeypatch,
        [
            {"type": "response.output_text.delta", "delta": "a"},
            {"type": "response.output_text.delta", "delta": "b"},
            {
                "type": "response.completed",
                "response": {
                    "model": "story-model",
                    "status": "completed",
                    "output": [],
                },
            },
        ],
    )

    result = call_llm(
        profile,
        ChatRequest(
            profile_id="main",
            messages=[ChatMessage(role="user", content="Write.")],
            metadata={"on_text_delta": lambda chunk: emitted.append(chunk.text_delta)},
        ),
    )

    assert emitted == ["a", "b"]
    assert result.content == "ab"
    assert result.model_snapshot == "story-model"
    assert result.provider_snapshot == "openai-compatible"


def test_openai_compatible_accepts_block_content_in_non_stream_response(monkeypatch) -> None:
    profile = _profile(protocol="openai-compatible", base_url="https://api.example.com/v1")
    _install_openai_fake(
        monkeypatch,
        {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [
                        {"type": "output_text", "text": "first"},
                        {"type": "output_text", "text": " second"},
                    ],
                }
            ]
        },
    )

    result = call_openai_compatible(
        profile,
        ChatRequest(
            profile_id="main",
            messages=[ChatMessage(role="user", content="Write.")],
            stream=False,
        ),
    )

    assert result.content == "first second"


def test_provider_sdk_clients_are_reused_with_hidden_retries_disabled() -> None:
    openai_profile = _profile(
        protocol="openai-compatible",
        base_url="https://api.example.com/v1",
    )
    anthropic_profile = _profile(
        protocol="anthropic-compatible",
        base_url="https://api.example.com",
    )

    close_provider_clients()
    try:
        openai_client = get_openai_client(openai_profile)
        anthropic_client = get_anthropic_client(anthropic_profile)

        assert get_openai_client(openai_profile) is openai_client
        assert get_anthropic_client(anthropic_profile) is anthropic_client
        assert openai_client.max_retries == 0
        assert anthropic_client.max_retries == 0
    finally:
        close_provider_clients()

    assert openai_client.is_closed
    assert anthropic_client.is_closed


def test_openai_sdk_transport_preserves_endpoint_and_streaming(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers.get("authorization")
        captured["payload"] = json.loads(request.content)
        events = [
            {
                "type": "response.output_text.delta",
                "sequence_number": 1,
                "item_id": "msg-1",
                "output_index": 0,
                "content_index": 0,
                "delta": "hello",
                "logprobs": [],
            },
            {
                "type": "response.completed",
                "sequence_number": 2,
                "response": {
                    "id": "resp-1",
                    "object": "response",
                    "created_at": 1,
                    "model": "story-model",
                    "status": "completed",
                    "output": [
                        {
                            "id": "msg-1",
                            "type": "message",
                            "role": "assistant",
                            "status": "completed",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": "hello",
                                    "annotations": [],
                                }
                            ],
                        }
                    ],
                    "parallel_tool_calls": True,
                    "tool_choice": "auto",
                    "tools": [],
                    "usage": {
                        "input_tokens": 1,
                        "input_tokens_details": {"cached_tokens": 0},
                        "output_tokens": 1,
                        "output_tokens_details": {"reasoning_tokens": 0},
                        "total_tokens": 2,
                    },
                },
            },
        ]
        body = "".join(f"data: {json.dumps(event)}\n\n" for event in events)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text=body,
        )

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    sdk_client = OpenAI(
        api_key="secret",
        base_url="https://api.example.com/v1",
        max_retries=0,
        http_client=http_client,
    )
    monkeypatch.setattr(
        openai_compatible,
        "get_openai_client",
        lambda _profile: sdk_client,
    )
    try:
        chunks = list(
            stream_openai_compatible(
                _profile(
                    protocol="openai-compatible",
                    base_url="https://api.example.com/v1",
                ),
                ChatRequest(
                    profile_id="main",
                    messages=[ChatMessage(role="user", content="Write.")],
                ),
            )
        )
    finally:
        sdk_client.close()

    assert captured["url"] == "https://api.example.com/v1/responses"
    assert captured["authorization"] == "Bearer secret"
    assert captured["payload"]["stream"] is True
    assert captured["payload"]["input"] == [
        {"type": "message", "role": "user", "content": "Write."}
    ]
    assert [chunk.text_delta for chunk in chunks if chunk.text_delta] == ["hello"]
    assert chunks[-1].usage == {
        "input_tokens": 1,
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens": 1,
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": 2,
    }


def test_anthropic_sdk_transport_parses_sse(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["payload"] = json.loads(request.content)
        events = [
            (
                "message_start",
                {
                    "type": "message_start",
                    "message": {
                        "id": "msg-1",
                        "type": "message",
                        "role": "assistant",
                        "model": "story-model",
                        "content": [],
                        "stop_reason": None,
                        "stop_sequence": None,
                        "usage": {"input_tokens": 1, "output_tokens": 0},
                    },
                },
            ),
            (
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                },
            ),
            (
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "hello"},
                },
            ),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            (
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                    "usage": {"output_tokens": 1},
                },
            ),
            ("message_stop", {"type": "message_stop"}),
        ]
        body = "".join(
            f"event: {event_name}\ndata: {json.dumps(event)}\n\n"
            for event_name, event in events
        )
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text=body,
        )

    http_client = httpx.Client(transport=httpx.MockTransport(handler))
    sdk_client = Anthropic(
        api_key="secret",
        base_url="https://api.example.com",
        max_retries=0,
        http_client=http_client,
    )
    monkeypatch.setattr(
        anthropic_compatible,
        "get_anthropic_client",
        lambda _profile: sdk_client,
    )
    profile = _profile(
        protocol="anthropic-compatible",
        base_url="https://api.example.com",
    ).model_copy(update={"request_options": {"max_tokens": 4096}})
    try:
        chunks = list(
            stream_anthropic_compatible(
                profile,
                ChatRequest(
                    profile_id="main",
                    messages=[ChatMessage(role="user", content="Write.")],
                ),
            )
        )
    finally:
        sdk_client.close()

    assert captured["url"] == "https://api.example.com/messages"
    assert captured["payload"]["stream"] is True
    assert captured["payload"]["max_tokens"] == 4096
    assert [chunk.text_delta for chunk in chunks if chunk.text_delta] == ["hello"]
    assert chunks[-1].usage == {"input_tokens": 1, "output_tokens": 1}


def test_openai_sdk_status_error_retains_retry_after(monkeypatch) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            502,
            headers={"retry-after": "2"},
            json={"error": {"message": "upstream unavailable", "type": "server_error"}},
        )

    sdk_client = OpenAI(
        api_key="secret",
        base_url="https://api.example.com/v1",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    monkeypatch.setattr(
        openai_compatible,
        "get_openai_client",
        lambda _profile: sdk_client,
    )
    try:
        with pytest.raises(ProviderCallError) as raised:
            call_openai_compatible(
                _profile(
                    protocol="openai-compatible",
                    base_url="https://api.example.com/v1",
                ),
                ChatRequest(
                    profile_id="main",
                    stream=False,
                    messages=[ChatMessage(role="user", content="Write.")],
                ),
            )
    finally:
        sdk_client.close()

    assert raised.value.status_code == 502
    assert raised.value.retryable is True
    assert raised.value.retry_after_seconds == 2


def test_openai_sdk_connection_error_is_normalized(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(
            "local error: tls: bad record MAC",
            request=request,
        )

    sdk_client = OpenAI(
        api_key="secret",
        base_url="https://api.example.com/v1",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    monkeypatch.setattr(
        openai_compatible,
        "get_openai_client",
        lambda _profile: sdk_client,
    )
    try:
        with pytest.raises(ProviderCallError, match="bad record MAC") as raised:
            call_openai_compatible(
                _profile(
                    protocol="openai-compatible",
                    base_url="https://api.example.com/v1",
                ),
                ChatRequest(
                    profile_id="main",
                    stream=False,
                    messages=[ChatMessage(role="user", content="Write.")],
                ),
            )
    finally:
        sdk_client.close()

    assert raised.value.kind == "connection"
    assert raised.value.stage == "request"
    assert raised.value.retryable is True


def test_openai_compatible_maps_tools_results_and_non_stream_call(monkeypatch) -> None:
    captured = _install_openai_fake(
        monkeypatch,
        {
            "model": "story-model",
            "status": "completed",
            "output": [
                {
                    "type": "function_call",
                    "call_id": "call-next",
                    "name": "submit_candidate",
                    "arguments": '{"revision":2}',
                }
            ],
        },
    )
    profile = _profile(protocol="openai-compatible", base_url="https://api.example.com/v1")
    prior_call = ToolCall(
        id="call-prior",
        name="lookup_context",
        arguments={"pack": "book"},
        raw_arguments='{"pack":"book"}',
    )

    result = call_llm(
        profile,
        ChatRequest(
            profile_id="main",
            stream=False,
            messages=[
                ChatMessage(role="user", content="Continue."),
                ChatMessage(role="assistant", tool_calls=[prior_call]),
                ChatMessage(
                    role="tool",
                    tool_results=[
                        ToolResult(
                            tool_call_id="call-prior",
                            name="lookup_context",
                            content={"status": "ok"},
                        )
                    ],
                ),
            ],
            tools=[_tool("lookup_context"), _tool("submit_candidate")],
            tool_choice=ToolChoice(mode="required"),
        ),
    )

    payload = _captured_payload(captured)
    assert isinstance(payload, dict)
    assert payload["tool_choice"] == "required"
    assert [item["name"] for item in payload["tools"]] == [
        "lookup_context",
        "submit_candidate",
    ]
    assert payload["input"][1] == {
        "type": "function_call",
        "call_id": "call-prior",
        "name": "lookup_context",
        "arguments": '{"pack":"book"}',
    }
    assert payload["input"][2] == {
        "type": "function_call_output",
        "call_id": "call-prior",
        "output": '{"status": "ok"}',
    }
    assert result.finish_reason == "tool_call"
    assert result.tool_calls[0].arguments == {"revision": 2}


def test_openai_compatible_streams_fragmented_tool_arguments(monkeypatch) -> None:
    profile = _profile(protocol="openai-compatible", base_url="https://api.example.com/v1")
    deltas: list[str] = []

    _install_openai_fake(
        monkeypatch,
        [
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {
                    "type": "function_call",
                    "id": "fc-1",
                    "call_id": "call-1",
                    "name": "submit_candidate",
                    "arguments": "",
                },
            },
            {
                "type": "response.function_call_arguments.delta",
                "output_index": 0,
                "item_id": "fc-1",
                "delta": '{"revision":',
            },
            {
                "type": "response.function_call_arguments.delta",
                "output_index": 0,
                "item_id": "fc-1",
                "delta": "2}",
            },
            {
                "type": "response.function_call_arguments.done",
                "output_index": 0,
                "item_id": "fc-1",
                "name": "submit_candidate",
                "arguments": '{"revision":2}',
            },
            {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": {
                    "type": "function_call",
                    "id": "fc-1",
                    "call_id": "call-1",
                    "name": "submit_candidate",
                    "arguments": '{"revision":2}',
                },
            },
            {
                "type": "response.completed",
                "response": {
                    "model": "story-model",
                    "status": "completed",
                    "output": [
                        {
                            "type": "function_call",
                            "id": "fc-1",
                            "call_id": "call-1",
                            "name": "submit_candidate",
                            "arguments": '{"revision":2}',
                        }
                    ],
                },
            },
        ],
    )

    result = call_llm(
        profile,
        ChatRequest(
            profile_id="main",
            messages=[ChatMessage(role="user", content="Submit.")],
            tools=[_tool("submit_candidate")],
            metadata={
                "on_tool_event": lambda chunk: deltas.append(chunk.arguments_delta)
                if chunk.arguments_delta
                else None
            },
        ),
    )

    assert deltas == ['{"revision":', "2}"]
    assert result.finish_reason == "tool_call"
    assert result.tool_calls[0].arguments == {"revision": 2}


def test_anthropic_compatible_maps_tools_results_and_streamed_call(monkeypatch) -> None:
    captured = _install_anthropic_fake(
        monkeypatch,
        [
            {
                "type": "message_start",
                "message": {"model": "story-model", "usage": {"input_tokens": 4}},
            },
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {
                    "type": "tool_use",
                    "id": "toolu-next",
                    "name": "submit_candidate",
                    "input": {},
                },
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {
                    "type": "input_json_delta",
                    "partial_json": '{"revision":2}',
                },
            },
            {"type": "content_block_stop", "index": 0},
            {
                "type": "message_delta",
                "delta": {"stop_reason": "tool_use"},
                "usage": {"output_tokens": 3},
            },
        ],
    )
    profile = _profile(
        protocol="anthropic-compatible",
        base_url="https://api.example.com",
    ).model_copy(update={"request_options": {"max_tokens": 4096}})
    prior_call = ToolCall(
        id="toolu-prior",
        name="lookup_context",
        arguments={"pack": "arc"},
        raw_arguments='{"pack":"arc"}',
    )

    result = call_llm(
        profile,
        ChatRequest(
            profile_id="main",
            messages=[
                ChatMessage(role="assistant", tool_calls=[prior_call]),
                ChatMessage(
                    role="tool",
                    tool_results=[
                        ToolResult(
                            tool_call_id="toolu-prior",
                            name="lookup_context",
                            content={"status": "ok"},
                        )
                    ],
                ),
            ],
            tools=[_tool("lookup_context"), _tool("submit_candidate")],
            tool_choice=ToolChoice(mode="named", name="submit_candidate"),
        ),
    )

    payload = _captured_payload(captured)
    assert isinstance(payload, dict)
    assert payload["tool_choice"] == {"type": "tool", "name": "submit_candidate"}
    assert payload["messages"][0]["content"][0]["type"] == "tool_use"
    assert payload["messages"][1]["content"][0] == {
        "type": "tool_result",
        "tool_use_id": "toolu-prior",
        "content": '{"status": "ok"}',
        "is_error": False,
    }
    assert result.finish_reason == "tool_call"
    assert result.tool_calls[0].arguments == {"revision": 2}
    assert result.usage == {"input_tokens": 4, "output_tokens": 3}


def test_structured_output_maps_natively_for_both_protocols(monkeypatch) -> None:
    schema = ResponseSchema(
        name="evaluation_result",
        description="One strict evaluation.",
        json_schema={
            "type": "object",
            "properties": {"outcome": {"type": "string"}},
            "required": ["outcome"],
            "additionalProperties": False,
        },
    )
    openai_captured = _install_openai_fake(
        monkeypatch,
        {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [
                        {"type": "output_text", "text": '{"outcome":"pass"}'}
                    ],
                }
            ],
        },
    )
    anthropic_captured = _install_anthropic_fake(
        monkeypatch,
        {"content": [{"type": "text", "text": '{"outcome":"pass"}'}]},
    )
    request = ChatRequest(
        profile_id="main",
        stream=False,
        messages=[ChatMessage(role="user", content="Evaluate.")],
        response_schema=schema,
    )

    openai_result = call_llm(
        _profile(protocol="openai-compatible", base_url="https://api.example.com/v1"),
        request,
    )
    anthropic_result = call_llm(
        _profile(
            protocol="anthropic-compatible",
            base_url="https://api.example.com",
        ).model_copy(update={"request_options": {"max_tokens": 4096}}),
        request,
    )

    openai_payload = _captured_payload(openai_captured)
    anthropic_payload = _captured_payload(anthropic_captured)
    assert isinstance(openai_payload, dict)
    assert isinstance(anthropic_payload, dict)
    assert openai_payload["text"]["format"]["type"] == "json_schema"
    assert openai_payload["text"]["format"]["strict"] is True
    assert anthropic_payload["output_config"] == {
        "format": {"type": "json_schema", "schema": schema.json_schema}
    }
    assert openai_result.structured_output == {"outcome": "pass"}
    assert anthropic_result.structured_output == {"outcome": "pass"}


def test_chat_request_rejects_tools_with_structured_output() -> None:
    with pytest.raises(ValueError, match="cannot share one request"):
        ChatRequest(
            profile_id="main",
            messages=[ChatMessage(role="user", content="Invalid.")],
            tools=[_tool("submit_candidate")],
            response_schema=ResponseSchema(
                name="result",
                json_schema={"type": "object"},
            ),
        )


def _profile(*, protocol: LlmProtocol, base_url: str) -> LlmProfile:
    return LlmProfile(
        id="main",
        name="Main",
        protocol=protocol,
        base_url=base_url,
        api_key=SecretStr("secret"),
        model="story-model",
    )


def _tool(name: str) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=f"Run {name}.",
        input_schema={
            "type": "object",
            "properties": {"revision": {"type": "integer"}},
            "additionalProperties": False,
        },
    )


def _install_openai_fake(monkeypatch, response: Any) -> dict[str, Any]:
    captured: dict[str, Any] = {}
    client = SimpleNamespace(responses=_CreateEndpoint(response, captured))
    monkeypatch.setattr(openai_compatible, "get_openai_client", lambda _profile: client)
    return captured


def _install_anthropic_fake(monkeypatch, response: Any) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    def post(path: str, **kwargs: Any) -> Any:
        captured["path"] = path
        captured.update(kwargs)
        return response

    client = SimpleNamespace(post=post)
    monkeypatch.setattr(
        anthropic_compatible,
        "get_anthropic_client",
        lambda _profile: client,
    )
    return captured


def _captured_payload(captured: dict[str, Any]) -> dict[str, Any]:
    if isinstance(captured.get("body"), dict):
        return dict(captured["body"])
    payload = dict(captured.get("extra_body") or {})
    for key in ("model", "input", "messages", "max_tokens", "stream"):
        if key in captured:
            payload[key] = captured[key]
    return payload
