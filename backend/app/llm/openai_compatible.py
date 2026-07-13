import json
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any, cast
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.llm.request_options import merge_request_options
from app.schemas.profiles import LlmProfile

if TYPE_CHECKING:
    from app.llm.gateway import ChatChunk, ChatRequest, ChatResult


def call_openai_compatible(profile: LlmProfile, chat_request: "ChatRequest") -> "ChatResult":
    from app.llm.gateway import ChatResult

    url = str(profile.base_url).rstrip("/") + "/chat/completions"
    payload = _openai_payload(profile, chat_request, stream=False)
    response = _post_json(url, profile.api_key.get_secret_value(), payload)
    content = _response_text(response)
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
        _raise_stream_error(event, "OpenAI-compatible")
        latest_event = event
        latest_model = _string_value(event.get("model"), latest_model)
        usage = event.get("usage")
        if isinstance(usage, dict):
            latest_usage = usage
        for choice in _list_value(event.get("choices")):
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta")
            text_delta = _content_text(delta.get("content")) if isinstance(delta, dict) else ""
            if not text_delta:
                message = choice.get("message")
                if isinstance(message, dict):
                    text_delta = _content_text(message.get("content"))
            if not text_delta:
                text_delta = _content_text(choice.get("text"))
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
    base_payload: dict[str, Any] = {
        "model": profile.model,
        "messages": [message.model_dump() for message in chat_request.messages],
        "stream": stream,
    }
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
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request) as response:
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
        with urlopen(request) as response:
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
        data = line.removeprefix("data:").strip() if line.startswith("data:") else line
        if not data.startswith("{"):
            continue
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


def _response_text(response: dict[str, Any]) -> str:
    direct = _content_text(response.get("output_text"))
    if direct:
        return direct
    for choice in _list_value(response.get("choices")):
        if not isinstance(choice, dict):
            continue
        message = choice.get("message")
        if isinstance(message, dict):
            content = _content_text(message.get("content"))
            if content:
                return content
        content = _content_text(choice.get("text"))
        if content:
            return content
    raise RuntimeError("OpenAI-compatible provider response did not contain text output.")


def _content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return ""
    parts: list[str] = []
    for item in value:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict):
            text = item.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


def _raise_stream_error(event: dict[str, Any], provider_name: str) -> None:
    error = event.get("error")
    if not isinstance(error, dict):
        return
    message = error.get("message")
    detail = message if isinstance(message, str) else json.dumps(error, ensure_ascii=False)
    raise RuntimeError(f"{provider_name} provider stream failed: {detail}")
