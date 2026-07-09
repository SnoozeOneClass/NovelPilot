from pydantic import SecretStr

from app.llm.gateway import ChatMessage, ChatRequest, call_llm
from app.llm.openai_compatible import call_openai_compatible, stream_openai_compatible
from app.llm.anthropic_compatible import stream_anthropic_compatible
from app.schemas.profiles import LlmProfile, LlmProtocol


def test_openai_compatible_adapter_honors_request_controls(monkeypatch) -> None:
    captured: dict[str, object] = {}
    profile = LlmProfile(
        id="main",
        name="Main",
        protocol="openai-compatible",
        base_url="https://api.example.com/v1",
        api_key=SecretStr("secret"),
        model="story-model",
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
            response_format={"type": "json_object"},
            metadata={"max_tokens": 1200},
        ),
    )

    payload = captured["payload"]
    assert result.content == "ok"
    assert captured["url"] == "https://api.example.com/v1/chat/completions"
    assert captured["api_key"] == "secret"
    assert isinstance(payload, dict)
    assert payload["max_tokens"] == 1200
    assert payload["response_format"] == {"type": "json_object"}


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
                metadata={"max_tokens": 50},
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


def test_anthropic_compatible_streams_text_deltas(monkeypatch) -> None:
    profile = _profile(protocol="anthropic-compatible", base_url="https://api.example.com")

    def fake_stream_json_events(_url, _api_key, _payload):
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
            stream=True,
            metadata={"on_text_delta": lambda chunk: emitted.append(chunk.text_delta)},
        ),
    )

    assert emitted == ["a", "b"]
    assert result.content == "ab"
    assert result.model_snapshot == "story-model"
    assert result.provider_snapshot == "openai-compatible"


def _profile(*, protocol: LlmProtocol, base_url: str) -> LlmProfile:
    return LlmProfile(
        id="main",
        name="Main",
        protocol=protocol,
        base_url=base_url,
        api_key=SecretStr("secret"),
        model="story-model",
    )
