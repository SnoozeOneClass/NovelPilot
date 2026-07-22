import json
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any, cast

from anthropic import Stream
from anthropic.types import Message, RawMessageStreamEvent

from app.llm.provider_clients import get_anthropic_client
from app.llm.provider_errors import (
    ProviderCallError,
    is_retryable_provider_error_message,
    translate_sdk_error,
)
from app.llm.request_options import merge_request_options
from app.schemas.profiles import LlmProfile

if TYPE_CHECKING:
    from app.llm.gateway import ChatChunk, ChatRequest, ChatResult, ToolCall


def call_anthropic_compatible(profile: LlmProfile, chat_request: "ChatRequest") -> "ChatResult":
    from app.llm.gateway import ChatResult

    payload = _anthropic_payload(profile, chat_request, stream=False)
    response = _sdk_response_dict(
        _create_anthropic_response(profile, payload, stream=False)
    )
    content, tool_calls = _response_output(response)
    return ChatResult(
        content=content,
        tool_calls=tool_calls,
        finish_reason=_normalize_finish_reason(response.get("stop_reason")),
        usage=_dict_value(response.get("usage")),
        model_snapshot=_string_value(response.get("model"), profile.model),
        provider_snapshot="anthropic-compatible",
    )


def stream_anthropic_compatible(
    profile: LlmProfile,
    chat_request: "ChatRequest",
) -> Iterator["ChatChunk"]:
    from app.llm.gateway import ChatChunk

    payload = _anthropic_payload(profile, chat_request, stream=True)
    latest_model = profile.model
    latest_usage: dict[str, Any] = {}
    latest_event: dict[str, Any] = {}
    latest_finish_reason = "stop"
    builders: dict[int, dict[str, Any]] = {}
    completed: set[int] = set()

    sdk_stream = _create_anthropic_response(profile, payload, stream=True)
    try:
        for sdk_event in sdk_stream:
            event = _sdk_response_dict(sdk_event)
            _raise_stream_error(event)
            latest_event = event
            latest_model = _string_value(event.get("model"), latest_model)
            event_type = _string_value(event.get("type"), "")
            if event_type == "message_start":
                message = event.get("message")
                if isinstance(message, dict):
                    latest_model = _string_value(message.get("model"), latest_model)
                    latest_usage.update(_dict_value(message.get("usage")))
                continue
            if event_type == "message_delta":
                latest_usage.update(_dict_value(event.get("usage")))
                delta = event.get("delta")
                if isinstance(delta, dict):
                    latest_finish_reason = _normalize_finish_reason(
                        delta.get("stop_reason")
                    )
                continue
            if event_type == "content_block_start":
                index = _event_index(event)
                block = event.get("content_block")
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type")
                if block_type == "text":
                    text_delta = _string_value(block.get("text"), "")
                    if text_delta:
                        yield ChatChunk(
                            text_delta=text_delta,
                            provider_snapshot="anthropic-compatible",
                            model_snapshot=latest_model,
                            raw_provider_metadata=event,
                        )
                    continue
                if block_type != "tool_use":
                    continue
                call_id = block.get("id")
                name = block.get("name")
                if not isinstance(call_id, str) or not call_id:
                    raise RuntimeError("Anthropic-compatible Tool use is missing an ID.")
                if not isinstance(name, str) or not name:
                    raise RuntimeError("Anthropic-compatible Tool use is missing a name.")
                initial_input = block.get("input")
                raw_arguments = ""
                if isinstance(initial_input, dict) and initial_input:
                    raw_arguments = json.dumps(initial_input, ensure_ascii=False)
                builders[index] = {
                    "id": call_id,
                    "name": name,
                    "arguments": raw_arguments,
                    "index": index,
                }
                yield ChatChunk(
                    event_type="tool_call_start",
                    tool_call_id=call_id,
                    tool_name=name,
                    tool_index=index,
                    provider_snapshot="anthropic-compatible",
                    model_snapshot=latest_model,
                    raw_provider_metadata=event,
                )
                continue
            if event_type == "content_block_delta":
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
                arguments_delta = _string_value(delta.get("partial_json"), "")
                if arguments_delta:
                    index = _event_index(event)
                    builder = builders.get(index)
                    if builder is None:
                        raise RuntimeError(
                            "Anthropic-compatible stream sent Tool arguments before Tool start."
                        )
                    builder["arguments"] += arguments_delta
                    yield ChatChunk(
                        event_type="tool_argument_delta",
                        tool_call_id=builder["id"],
                        tool_name=builder["name"],
                        tool_index=index,
                        arguments_delta=arguments_delta,
                        provider_snapshot="anthropic-compatible",
                        model_snapshot=latest_model,
                        raw_provider_metadata=event,
                    )
                continue
            if event_type == "content_block_stop":
                index = _event_index(event)
                builder = builders.get(index)
                if builder is None:
                    continue
                tool_call = _tool_call_from_builder(builder)
                completed.add(index)
                yield ChatChunk(
                    event_type="tool_call_stop",
                    tool_call_id=tool_call.id,
                    tool_name=tool_call.name,
                    tool_index=index,
                    tool_call=tool_call,
                    provider_snapshot="anthropic-compatible",
                    model_snapshot=latest_model,
                    raw_provider_metadata=event,
                )
    except Exception as exc:
        translated = translate_sdk_error(
            "anthropic-compatible",
            exc,
            stage="stream",
        )
        if translated is not None:
            raise translated from exc
        raise
    finally:
        close = getattr(sdk_stream, "close", None)
        if callable(close):
            close()

    for index in sorted(set(builders) - completed):
        tool_call = _tool_call_from_builder(builders[index])
        yield ChatChunk(
            event_type="tool_call_stop",
            tool_call_id=tool_call.id,
            tool_name=tool_call.name,
            tool_index=index,
            tool_call=tool_call,
            provider_snapshot="anthropic-compatible",
            model_snapshot=latest_model,
            raw_provider_metadata=latest_event,
        )

    yield ChatChunk(
        event_type="message_stop",
        finish_reason=latest_finish_reason,
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
    base_payload: dict[str, Any] = {
        "model": profile.model,
        "messages": _anthropic_messages(chat_request),
        "stream": stream,
    }
    if system:
        base_payload["system"] = system
    choice = chat_request.tool_choice
    if chat_request.tools and not (choice is not None and choice.mode == "none"):
        base_payload["tools"] = [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.input_schema,
                "strict": tool.strict,
            }
            for tool in chat_request.tools
        ]
        base_payload["tool_choice"] = _anthropic_tool_choice(chat_request)
    if chat_request.response_schema is not None:
        base_payload["output_config"] = {
            "format": {
                "type": "json_schema",
                "schema": chat_request.response_schema.json_schema,
            }
        }
    return merge_request_options(
        base_payload,
        profile.request_options,
        chat_request.request_options,
    )


def _anthropic_messages(chat_request: "ChatRequest") -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for message in chat_request.messages:
        if message.role == "system":
            continue
        blocks: list[dict[str, Any]] = []
        if message.content:
            blocks.append({"type": "text", "text": message.content})
        blocks.extend(
            {
                "type": "tool_use",
                "id": call.id,
                "name": call.name,
                "input": call.arguments,
            }
            for call in message.tool_calls
        )
        blocks.extend(
            {
                "type": "tool_result",
                "tool_use_id": result.tool_call_id,
                "content": _result_content(result.content),
                "is_error": result.is_error,
            }
            for result in message.tool_results
        )
        role = "assistant" if message.role == "assistant" else "user"
        if len(blocks) == 1 and blocks[0].get("type") == "text":
            messages.append({"role": role, "content": message.content})
        else:
            messages.append({"role": role, "content": blocks})
    return messages


def _anthropic_tool_choice(chat_request: "ChatRequest") -> dict[str, Any]:
    choice = chat_request.tool_choice
    if choice is None or choice.mode == "auto":
        return {"type": "auto"}
    if choice.mode == "required":
        return {"type": "any"}
    if choice.mode == "named":
        return {"type": "tool", "name": choice.name}
    raise RuntimeError("Anthropic Tool choice 'none' must omit the Tool definitions.")


def _response_output(response: dict[str, Any]) -> tuple[str, list["ToolCall"]]:
    texts: list[str] = []
    tool_calls: list[ToolCall] = []
    for index, block in enumerate(_list_value(response.get("content"))):
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str):
                texts.append(text)
        elif block.get("type") == "tool_use":
            tool_calls.append(_tool_call_from_block(block, index))
    if not texts and not tool_calls:
        raise RuntimeError("Anthropic-compatible provider response did not contain text or Tools.")
    return "".join(texts), tool_calls


def _tool_call_from_block(block: dict[str, Any], index: int) -> "ToolCall":
    input_value = block.get("input")
    if not isinstance(input_value, dict):
        raise RuntimeError("Anthropic-compatible Tool input must be a JSON object.")
    raw_arguments = json.dumps(input_value, ensure_ascii=False)
    return _build_tool_call(block.get("id"), block.get("name"), raw_arguments, index)


def _tool_call_from_builder(builder: dict[str, Any]) -> "ToolCall":
    raw_arguments = _string_value(builder.get("arguments"), "") or "{}"
    return _build_tool_call(
        builder.get("id"),
        builder.get("name"),
        raw_arguments,
        cast(int, builder["index"]),
    )


def _build_tool_call(call_id: Any, name: Any, raw_arguments: str, index: int) -> "ToolCall":
    from app.llm.gateway import ToolCall, parse_tool_arguments

    if not isinstance(call_id, str) or not call_id:
        raise RuntimeError("Anthropic-compatible Tool use is missing an ID.")
    if not isinstance(name, str) or not name:
        raise RuntimeError("Anthropic-compatible Tool use is missing a name.")
    arguments, parse_error = parse_tool_arguments(raw_arguments)
    return ToolCall(
        id=call_id,
        name=name,
        arguments=arguments,
        raw_arguments=raw_arguments,
        parse_error=parse_error,
        index=index,
    )


def _create_anthropic_response(
    profile: LlmProfile,
    payload: dict[str, Any],
    *,
    stream: bool,
) -> Any:
    body = dict(payload)
    max_tokens = body.get("max_tokens")
    if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or max_tokens < 1:
        raise RuntimeError(
            "Anthropic-compatible profile request_options.max_tokens must be a positive integer."
        )
    body["stream"] = stream
    try:
        # The existing Profile contract treats ``base_url`` as the versioned API
        # prefix and appends ``/messages``.  Using the SDK's public low-level call
        # preserves that endpoint contract while still delegating HTTP, SSE,
        # connection pooling, error types, and response parsing to the SDK.
        return get_anthropic_client(profile).post(
            "/messages",
            body=body,
            cast_to=Message,
            stream=stream,
            stream_cls=Stream[RawMessageStreamEvent],
        )
    except Exception as exc:
        translated = translate_sdk_error(
            "anthropic-compatible",
            exc,
            stage="request",
        )
        if translated is not None:
            raise translated from exc
        raise


def _sdk_response_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return cast(dict[str, Any], value)
    model_dump = getattr(value, "model_dump", None)
    if not callable(model_dump):
        raise RuntimeError("Anthropic-compatible SDK returned an unsupported response type.")
    payload = model_dump(mode="json", exclude_none=True)
    if not isinstance(payload, dict):
        raise RuntimeError("Anthropic-compatible SDK returned a non-object response.")
    return cast(dict[str, Any], payload)


def _event_index(event: dict[str, Any]) -> int:
    value = event.get("index", 0)
    return value if isinstance(value, int) and value >= 0 else 0


def _string_value(value: Any, fallback: str) -> str:
    return value if isinstance(value, str) else fallback


def _list_value(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _dict_value(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _result_content(value: str | dict[str, Any] | list[Any]) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _normalize_finish_reason(value: Any) -> str:
    if value == "tool_use":
        return "tool_call"
    if value == "max_tokens":
        return "length"
    if value in {"cancelled", "canceled"}:
        return "cancelled"
    return value if isinstance(value, str) and value else "stop"


def _raise_stream_error(event: dict[str, Any]) -> None:
    error = event.get("error")
    if not isinstance(error, dict):
        return
    message = error.get("message")
    detail = message if isinstance(message, str) else json.dumps(error, ensure_ascii=False)
    retryable = is_retryable_provider_error_message(detail)
    raise ProviderCallError(
        protocol="anthropic-compatible",
        kind="connection" if retryable else "response",
        stage="stream",
        detail=detail,
        retryable=retryable,
    )
