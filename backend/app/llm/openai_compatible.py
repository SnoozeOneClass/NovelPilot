import json
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any, cast
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.llm.request_options import merge_request_options
from app.schemas.profiles import LlmProfile

if TYPE_CHECKING:
    from app.llm.gateway import ChatChunk, ChatRequest, ChatResult, ToolCall


def call_openai_compatible(profile: LlmProfile, chat_request: "ChatRequest") -> "ChatResult":
    from app.llm.gateway import ChatResult

    url = str(profile.base_url).rstrip("/") + "/chat/completions"
    payload = _openai_payload(profile, chat_request, stream=False)
    response = _post_json(url, profile.api_key.get_secret_value(), payload)
    content, tool_calls, finish_reason = _response_output(response)
    return ChatResult(
        content=content,
        tool_calls=tool_calls,
        finish_reason=finish_reason,
        usage=_dict_value(response.get("usage")),
        model_snapshot=_string_value(response.get("model"), profile.model),
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
    latest_finish_reason = "stop"
    builders: dict[int, dict[str, Any]] = {}
    started: set[int] = set()

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
            finish_reason = choice.get("finish_reason")
            if isinstance(finish_reason, str):
                latest_finish_reason = _normalize_finish_reason(finish_reason)
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

            tool_deltas: list[Any] = []
            if isinstance(delta, dict):
                tool_deltas = _list_value(delta.get("tool_calls"))
            if not tool_deltas:
                message = choice.get("message")
                if isinstance(message, dict):
                    tool_deltas = _list_value(message.get("tool_calls"))
            for fallback_index, tool_delta in enumerate(tool_deltas):
                if not isinstance(tool_delta, dict):
                    continue
                index_value = tool_delta.get("index", fallback_index)
                index = index_value if isinstance(index_value, int) and index_value >= 0 else fallback_index
                builder = builders.setdefault(
                    index,
                    {"id": "", "name": "", "arguments": "", "index": index},
                )
                _merge_tool_identity(builder, "id", tool_delta.get("id"), index)
                function = tool_delta.get("function")
                if isinstance(function, dict):
                    _merge_tool_identity(builder, "name", function.get("name"), index)
                if index not in started and (builder["id"] or builder["name"]):
                    started.add(index)
                    yield ChatChunk(
                        event_type="tool_call_start",
                        tool_call_id=builder["id"] or None,
                        tool_name=builder["name"] or None,
                        tool_index=index,
                        provider_snapshot="openai-compatible",
                        model_snapshot=latest_model,
                        raw_provider_metadata=event,
                    )
                arguments_delta = _tool_arguments_delta(function)
                if arguments_delta:
                    builder["arguments"] += arguments_delta
                    yield ChatChunk(
                        event_type="tool_argument_delta",
                        tool_call_id=builder["id"] or None,
                        tool_name=builder["name"] or None,
                        tool_index=index,
                        arguments_delta=arguments_delta,
                        provider_snapshot="openai-compatible",
                        model_snapshot=latest_model,
                        raw_provider_metadata=event,
                    )

    for index in sorted(builders):
        tool_call = _tool_call_from_builder(builders[index])
        if index not in started:
            yield ChatChunk(
                event_type="tool_call_start",
                tool_call_id=tool_call.id,
                tool_name=tool_call.name,
                tool_index=index,
                provider_snapshot="openai-compatible",
                model_snapshot=latest_model,
                raw_provider_metadata=latest_event,
            )
        yield ChatChunk(
            event_type="tool_call_stop",
            tool_call_id=tool_call.id,
            tool_name=tool_call.name,
            tool_index=index,
            tool_call=tool_call,
            provider_snapshot="openai-compatible",
            model_snapshot=latest_model,
            raw_provider_metadata=latest_event,
        )

    yield ChatChunk(
        event_type="message_stop",
        finish_reason=latest_finish_reason,
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
        "messages": _openai_messages(chat_request),
        "stream": stream,
    }
    if chat_request.tools:
        base_payload["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.input_schema,
                    "strict": tool.strict,
                },
            }
            for tool in chat_request.tools
        ]
        base_payload["tool_choice"] = _openai_tool_choice(chat_request)
    if chat_request.response_schema is not None:
        schema = chat_request.response_schema
        base_payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": schema.name,
                "description": schema.description,
                "strict": schema.strict,
                "schema": schema.json_schema,
            },
        }
    return merge_request_options(
        base_payload,
        profile.request_options,
        chat_request.request_options,
    )


def _openai_messages(chat_request: "ChatRequest") -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for message in chat_request.messages:
        if message.role == "tool":
            result = message.tool_results[0]
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": result.tool_call_id,
                    "content": _result_content(result.content),
                }
            )
            continue
        payload: dict[str, Any] = {"role": message.role, "content": message.content}
        if message.tool_calls:
            payload["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": call.raw_arguments
                        or json.dumps(call.arguments, ensure_ascii=False),
                    },
                }
                for call in message.tool_calls
            ]
        messages.append(payload)
    return messages


def _openai_tool_choice(chat_request: "ChatRequest") -> str | dict[str, Any]:
    choice = chat_request.tool_choice
    if choice is None or choice.mode == "auto":
        return "auto"
    if choice.mode == "required":
        return "required"
    if choice.mode == "none":
        return "none"
    return {"type": "function", "function": {"name": choice.name}}


def _response_output(response: dict[str, Any]) -> tuple[str, list["ToolCall"], str]:
    direct = _content_text(response.get("output_text"))
    for choice in _list_value(response.get("choices")):
        if not isinstance(choice, dict):
            continue
        message = choice.get("message")
        text = direct
        tool_calls: list[ToolCall] = []
        if isinstance(message, dict):
            text = _content_text(message.get("content")) or text
            tool_calls = [
                _tool_call_from_response(item, index)
                for index, item in enumerate(_list_value(message.get("tool_calls")))
                if isinstance(item, dict)
            ]
        text = text or _content_text(choice.get("text"))
        if text or tool_calls:
            return text, tool_calls, _normalize_finish_reason(choice.get("finish_reason"))
    if direct:
        return direct, [], "stop"
    raise RuntimeError("OpenAI-compatible provider response did not contain text or Tool calls.")


def _tool_call_from_response(value: dict[str, Any], index: int) -> "ToolCall":
    function = value.get("function")
    if not isinstance(function, dict):
        raise RuntimeError("OpenAI-compatible Tool call did not contain a function object.")
    raw_arguments = _tool_arguments_delta(function)
    return _build_tool_call(value.get("id"), function.get("name"), raw_arguments, index)


def _tool_call_from_builder(builder: dict[str, Any]) -> "ToolCall":
    return _build_tool_call(
        builder.get("id"),
        builder.get("name"),
        _string_value(builder.get("arguments"), ""),
        cast(int, builder["index"]),
    )


def _build_tool_call(call_id: Any, name: Any, raw_arguments: str, index: int) -> "ToolCall":
    from app.llm.gateway import ToolCall, parse_tool_arguments

    if not isinstance(call_id, str) or not call_id:
        raise RuntimeError("OpenAI-compatible Tool call is missing an ID.")
    if not isinstance(name, str) or not name:
        raise RuntimeError("OpenAI-compatible Tool call is missing a function name.")
    arguments, parse_error = parse_tool_arguments(raw_arguments)
    return ToolCall(
        id=call_id,
        name=name,
        arguments=arguments,
        raw_arguments=raw_arguments,
        parse_error=parse_error,
        index=index,
    )


def _merge_tool_identity(builder: dict[str, Any], field: str, value: Any, index: int) -> None:
    if not isinstance(value, str) or not value:
        return
    current = builder[field]
    if current and current != value:
        raise RuntimeError(
            f"OpenAI-compatible stream changed Tool call {field} at index {index}."
        )
    builder[field] = value


def _tool_arguments_delta(function: Any) -> str:
    if not isinstance(function, dict):
        return ""
    arguments = function.get("arguments")
    if isinstance(arguments, str):
        return arguments
    if isinstance(arguments, dict):
        return json.dumps(arguments, ensure_ascii=False)
    return ""


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


def _dict_value(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


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


def _result_content(value: str | dict[str, Any] | list[Any]) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _normalize_finish_reason(value: Any) -> str:
    if value in {"tool_calls", "function_call"}:
        return "tool_call"
    if value in {"length", "max_tokens"}:
        return "length"
    if value in {"cancelled", "canceled"}:
        return "cancelled"
    return value if isinstance(value, str) and value else "stop"


def _raise_stream_error(event: dict[str, Any], provider_name: str) -> None:
    error = event.get("error")
    if not isinstance(error, dict):
        return
    message = error.get("message")
    detail = message if isinstance(message, str) else json.dumps(error, ensure_ascii=False)
    raise RuntimeError(f"{provider_name} provider stream failed: {detail}")
