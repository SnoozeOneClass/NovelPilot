import json

import pytest
from pydantic import SecretStr

from app.llm import anthropic_compatible, openai_compatible
from app.llm.gateway import ChatMessage, ChatRequest, call_llm
from app.llm.anthropic_compatible import stream_anthropic_compatible
from app.llm.openai_compatible import call_openai_compatible, stream_openai_compatible
from app.schemas.profiles import LlmProfile, LlmProtocol


def test_openai_compatible_adapter_merges_arbitrary_request_options(monkeypatch) -> None:
    captured: dict[str, object] = {}
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
            "stream": True,
        },
    )

    def fake_post_json(url, api_key, payload):
        captured["url"] = url
        captured["api_key"] = api_key
        captured["payload"] = payload
        return {
            "model": "story-model",
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"total_tokens": 3},
        }

    monkeypatch.setattr("app.llm.openai_compatible._post_json", fake_post_json)

    result = call_openai_compatible(
        profile,
        ChatRequest(
            profile_id="main",
            messages=[ChatMessage(role="user", content="Return JSON.")],
            stream=False,
            request_options={
                "temperature": 0.6,
                "response_format": {"type": "json_object"},
            },
        ),
    )

    payload = captured["payload"]
    assert result.content == "ok"
    assert captured["url"] == "https://api.example.com/v1/chat/completions"
    assert captured["api_key"] == "secret"
    assert isinstance(payload, dict)
    assert payload["model"] == "story-model"
    assert payload["messages"] == [{"role": "user", "content": "Return JSON."}]
    assert payload["stream"] is False
    assert payload["temperature"] == 0.6
    assert payload["max_completion_tokens"] == 1200
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["provider_extension"] == {"mode": "novel"}
    assert "max_tokens" not in payload


def test_openai_compatible_streams_text_deltas(monkeypatch) -> None:
    captured: dict[str, object] = {}
    profile = _profile(protocol="openai-compatible", base_url="https://api.example.com/v1")

    def fake_stream_json_events(url, api_key, payload):
        captured["url"] = url
        captured["api_key"] = api_key
        captured["payload"] = payload
        return iter(
            [
                {
                    "model": "story-model",
                    "choices": [{"delta": {"content": "hel"}}],
                },
                {
                    "model": "story-model",
                    "choices": [{"delta": {"content": "lo"}}],
                    "usage": {"total_tokens": 5},
                },
            ]
        )

    monkeypatch.setattr("app.llm.openai_compatible._stream_json_events", fake_stream_json_events)

    chunks = list(
        stream_openai_compatible(
            profile,
            ChatRequest(
                profile_id="main",
                messages=[ChatMessage(role="user", content="Say hello.")],
            ),
        )
    )

    payload = captured["payload"]
    assert captured["url"] == "https://api.example.com/v1/chat/completions"
    assert captured["api_key"] == "secret"
    assert isinstance(payload, dict)
    assert payload["stream"] is True
    assert [chunk.text_delta for chunk in chunks if chunk.text_delta] == ["hel", "lo"]
    assert chunks[-1].event_type == "message_stop"
    assert chunks[-1].usage == {"total_tokens": 5}


def test_openai_stream_accepts_a_non_sse_json_response(monkeypatch) -> None:
    profile = _profile(protocol="openai-compatible", base_url="https://api.example.com/v1")

    def fake_stream_json_events(_url, _api_key, _payload):
        return openai_compatible._iter_sse_json_events(
            iter(
                [
                    b'{"model":"story-model","choices":[{"message":{"content":"ok"}}]}\n'
                ]
            )
        )

    monkeypatch.setattr(openai_compatible, "_stream_json_events", fake_stream_json_events)

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
    monkeypatch.setattr(
        openai_compatible,
        "_stream_json_events",
        lambda *_args: iter([{"error": {"message": "model unavailable"}}]),
    )

    with pytest.raises(RuntimeError, match="model unavailable"):
        list(
            stream_openai_compatible(
                profile,
                ChatRequest(
                    profile_id="main",
                    messages=[ChatMessage(role="user", content="Write.")],
                ),
            )
        )


def test_anthropic_compatible_streams_text_deltas(monkeypatch) -> None:
    captured: dict[str, object] = {}
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

    def fake_stream_json_events(_url, _api_key, payload):
        captured["payload"] = payload
        return iter(
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
            ]
        )

    monkeypatch.setattr(
        "app.llm.anthropic_compatible._stream_json_events",
        fake_stream_json_events,
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
    assert chunks[-1].usage == {"output_tokens": 3}
    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert payload["max_tokens"] == 24000
    assert payload["thinking"] == {"type": "enabled", "budget_tokens": 8000}
    assert payload["system"] == "Visible output only."
    assert payload["messages"] == [{"role": "user", "content": "Say hello."}]


def test_call_llm_stream_collects_result_and_notifies_deltas(monkeypatch) -> None:
    emitted: list[str] = []
    profile = _profile(protocol="openai-compatible", base_url="https://api.example.com/v1")

    def fake_stream_json_events(_url, _api_key, _payload):
        return iter(
            [
                {"model": "story-model", "choices": [{"delta": {"content": "a"}}]},
                {"model": "story-model", "choices": [{"delta": {"content": "b"}}]},
            ]
        )

    monkeypatch.setattr("app.llm.openai_compatible._stream_json_events", fake_stream_json_events)

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
    monkeypatch.setattr(
        openai_compatible,
        "_post_json",
        lambda *_args: {
            "choices": [
                {
                    "message": {
                        "content": [
                            {"type": "text", "text": "first"},
                            {"type": "text", "text": " second"},
                        ]
                    }
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


def test_provider_http_calls_do_not_set_an_application_timeout(monkeypatch) -> None:
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self) -> bytes:
            return json.dumps({"ok": True}).encode("utf-8")

        def __iter__(self):
            return iter([])

    def fake_urlopen(*args, **kwargs):
        calls.append((args, kwargs))
        return FakeResponse()

    monkeypatch.setattr(openai_compatible, "urlopen", fake_urlopen)
    monkeypatch.setattr(anthropic_compatible, "urlopen", fake_urlopen)

    openai_compatible._post_json("https://api.example.com", "secret", {"model": "m"})
    anthropic_compatible._post_json("https://api.example.com", "secret", {"model": "m"})
    list(openai_compatible._stream_json_events("https://api.example.com", "secret", {}))
    list(anthropic_compatible._stream_json_events("https://api.example.com", "secret", {}))

    assert len(calls) == 4
    assert all(len(args) == 1 for args, _kwargs in calls)
    assert all("timeout" not in kwargs for _args, kwargs in calls)


def _profile(*, protocol: LlmProtocol, base_url: str) -> LlmProfile:
    return LlmProfile(
        id="main",
        name="Main",
        protocol=protocol,
        base_url=base_url,
        api_key=SecretStr("secret"),
        model="story-model",
    )
