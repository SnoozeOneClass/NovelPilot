import json
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any, cast
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.llm.request_options import merge_request_options
from app.schemas.profiles import LlmProfile

if TYPE_CHECKING:
    from app.llm.gateway import ChatChunk, ChatRequest, ChatResult


def call_anthropic_compatible(profile: LlmProfile, chat_request: "ChatRequest") -> "ChatResult":
    from app.llm.gateway import ChatResult

    url = str(profile.base_url).rstrip("/") + "/messages"
    payload = _anthropic_payload(profile, chat_request, stream=False)
    response = _post_json(url, profile.api_key.get_secret_value(), payload)
    content_blocks = response.get("content", [])
    text = "".join(block.get("text", "") for block in content_blocks if block.get("type") == "text")
    return ChatResult(
        content=text,
        usage=response.get("usage", {}),
        model_snapshot=response.get("model", profile.model),
        provider_snapshot="anthropic-compatible",
    )


def stream_anthropic_compatible(
    profile: LlmProfile,
    chat_request: "ChatRequest",
) -> Iterator["ChatChunk"]:
    from app.llm.gateway import ChatChunk

    url = str(profile.base_url).rstrip("/") + "/messages"
    payload = _anthropic_payload(profile, chat_request, stream=True)
    latest_model = profile.model
    latest_usage: dict[str, Any] = {}
    latest_event: dict[str, Any] = {}

    for event in _stream_json_events(url, profile.api_key.get_secret_value(), payload):
        _raise_stream_error(event)
        latest_event = event
        latest_model = _string_value(event.get("model"), latest_model)
        event_type = _string_value(event.get("type"), "")
        if event_type == "message_start":
            message = event.get("message")
            if isinstance(message, dict):
                latest_model = _string_value(message.get("model"), latest_model)
                usage = message.get("usage")
                if isinstance(usage, dict):
                    latest_usage = usage
            continue
        if event_type == "message_delta":
            usage = event.get("usage")
            if isinstance(usage, dict):
                latest_usage = usage
            continue
        if event_type != "content_block_delta":
            continue

        delta = event.get("delta")
        if not isinstance(delta, dict):
            continue
        text_delta = _string_value(delta.get("text"), "")
        if text_delta:
            yield ChatChunk(
                text_delta=text_delta,
                provider_snapshot="anthropic-compatible",
                model_snapshot=latest_model,
                raw_provider_metadata=event,
            )

    yield ChatChunk(
        event_type="message_stop",
        usage=latest_usage,
        model_snapshot=latest_model,
        provider_snapshot="anthropic-compatible",
        raw_provider_metadata=latest_event,
    )


def _anthropic_payload(
    profile: LlmProfile,
    chat_request: "ChatRequest",
    *,
    stream: bool,
) -> dict[str, Any]:
    system = "\n\n".join(
        message.content for message in chat_request.messages if message.role == "system"
    )
    messages = [
        message.model_dump()
        for message in chat_request.messages
        if message.role in {"user", "assistant"}
    ]
    base_payload: dict[str, Any] = {
        "model": profile.model,
        "messages": messages,
        "stream": stream,
    }
    if system:
        base_payload["system"] = system
    return merge_request_options(
        base_payload,
        profile.request_options,
        chat_request.request_options,
    )


def _post_json(url: str, api_key: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request) as response:
            parsed = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Anthropic-compatible provider returned {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"Anthropic-compatible provider request failed: {exc}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("Anthropic-compatible provider returned a non-object response.")
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
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        },
        method="POST",
    )
    try:
        with urlopen(request) as response:
            for event in _iter_sse_json_events(response):
                yield event
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Anthropic-compatible provider returned {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"Anthropic-compatible provider request failed: {exc}") from exc


def _iter_sse_json_events(lines: Iterator[bytes]) -> Iterator[dict[str, Any]]:
    for raw_line in lines:
        line = raw_line.decode("utf-8", errors="replace").strip()
        data = line.removeprefix("data:").strip() if line.startswith("data:") else line
        if not data.startswith("{"):
            continue
        if not data:
            continue
        try:
            event = json.loads(data)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            yield cast(dict[str, Any], event)


def _string_value(value: Any, fallback: str) -> str:
    return value if isinstance(value, str) else fallback


def _raise_stream_error(event: dict[str, Any]) -> None:
    error = event.get("error")
    if not isinstance(error, dict):
        return
    message = error.get("message")
    detail = message if isinstance(message, str) else json.dumps(error, ensure_ascii=False)
    raise RuntimeError(f"Anthropic-compatible provider stream failed: {detail}")
