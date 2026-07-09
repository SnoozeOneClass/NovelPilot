import json
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any, cast
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.schemas.profiles import LlmProfile

if TYPE_CHECKING:
    from app.llm.gateway import ChatChunk, ChatRequest, ChatResult


def call_openai_compatible(profile: LlmProfile, chat_request: "ChatRequest") -> "ChatResult":
    from app.llm.gateway import ChatResult

    url = str(profile.base_url).rstrip("/") + "/chat/completions"
    payload = _openai_payload(profile, chat_request, stream=False)
    response = _post_json(url, profile.api_key.get_secret_value(), payload)
    content = response["choices"][0]["message"]["content"]
    return ChatResult(
        content=content,
        usage=response.get("usage", {}),
        model_snapshot=response.get("model", profile.model),
        provider_snapshot="openai-compatible",
    )


def stream_openai_compatible(
    profile: LlmProfile,
    chat_request: "ChatRequest",
) -> Iterator["ChatChunk"]:
    from app.llm.gateway import ChatChunk

    url = str(profile.base_url).rstrip("/") + "/chat/completions"
    payload = _openai_payload(profile, chat_request, stream=True)
    latest_model = profile.model
    latest_usage: dict[str, Any] = {}
    latest_event: dict[str, Any] = {}

    for event in _stream_json_events(url, profile.api_key.get_secret_value(), payload):
        latest_event = event
        latest_model = _string_value(event.get("model"), latest_model)
        usage = event.get("usage")
        if isinstance(usage, dict):
            latest_usage = usage
        for choice in _list_value(event.get("choices")):
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta")
            text_delta = ""
            if isinstance(delta, dict):
                text_delta = _string_value(delta.get("content"), "")
            if text_delta:
                yield ChatChunk(
                    text_delta=text_delta,
                    provider_snapshot="openai-compatible",
                    model_snapshot=latest_model,
                    raw_provider_metadata=event,
                )

    yield ChatChunk(
        event_type="message_stop",
        usage=latest_usage,
        model_snapshot=latest_model,
        provider_snapshot="openai-compatible",
        raw_provider_metadata=latest_event,
    )


def _openai_payload(
    profile: LlmProfile,
    chat_request: "ChatRequest",
    *,
    stream: bool,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": profile.model,
        "messages": [message.model_dump() for message in chat_request.messages],
        "temperature": chat_request.temperature,
        "max_tokens": chat_request.metadata.get("max_tokens", 4096),
        "stream": stream,
    }
    if chat_request.response_format is not None:
        payload["response_format"] = chat_request.response_format
    return payload


def _post_json(url: str, api_key: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=120) as response:
            parsed = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenAI-compatible provider returned {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"OpenAI-compatible provider request failed: {exc}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("OpenAI-compatible provider returned a non-object response.")
    return cast(dict[str, Any], parsed)


def _stream_json_events(
    url: str,
    api_key: str,
    payload: dict[str, Any],
) -> Iterator[dict[str, Any]]:
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=120) as response:
            for event in _iter_sse_json_events(response):
                yield event
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenAI-compatible provider returned {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"OpenAI-compatible provider request failed: {exc}") from exc


def _iter_sse_json_events(lines: Iterator[bytes]) -> Iterator[dict[str, Any]]:
    for raw_line in lines:
        line = raw_line.decode("utf-8", errors="replace").strip()
        if not line.startswith("data:"):
            continue
        data = line.removeprefix("data:").strip()
        if not data or data == "[DONE]":
            continue
        try:
            event = json.loads(data)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            yield cast(dict[str, Any], event)


def _string_value(value: Any, fallback: str) -> str:
    return value if isinstance(value, str) else fallback


def _list_value(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []
