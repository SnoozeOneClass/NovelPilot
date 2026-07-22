import json
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any, cast

from app.llm.provider_clients import get_openai_client
from app.llm.provider_errors import (
    ProviderCallError,
    ProviderFailureStage,
    is_retryable_provider_error_message,
    translate_sdk_error,
)
from app.llm.request_options import merge_request_options
from app.schemas.profiles import LlmProfile

if TYPE_CHECKING:
    from openai.types.responses.response_input_param import ResponseInputParam

    from app.llm.gateway import ChatChunk, ChatRequest, ChatResult, ToolCall


def call_openai_compatible(profile: LlmProfile, chat_request: "ChatRequest") -> "ChatResult":
    from app.llm.gateway import ChatResult

    payload = _responses_payload(profile, chat_request, stream=False)
    response = _sdk_response_dict(
        _create_openai_response(profile, payload, stream=False)
    )
    _raise_response_failure(response, stage="response")
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

    payload = _responses_payload(profile, chat_request, stream=True)
    latest_model = profile.model
    latest_usage: dict[str, Any] = {}
    latest_event: dict[str, Any] = {}
    latest_finish_reason = "stop"
    builders: dict[int, dict[str, Any]] = {}
    started: set[int] = set()
    emitted_text = False

    sdk_stream = _create_openai_response(profile, payload, stream=True)
    try:
        for sdk_event in sdk_stream:
            event = _sdk_response_dict(sdk_event)
            _raise_stream_event_error(event)
            latest_event = event
            event_type = _string_value(event.get("type"), "")

            response = event.get("response")
            if isinstance(response, dict):
                latest_model = _string_value(response.get("model"), latest_model)
                usage = response.get("usage")
                if isinstance(usage, dict):
                    latest_usage = usage
                latest_finish_reason = _responses_finish_reason(
                    response,
                    has_tool_calls=bool(builders),
                )
                _merge_response_tool_items(builders, response)

            if event_type == "response.output_text.delta":
                text_delta = _string_value(event.get("delta"), "")
                if text_delta:
                    emitted_text = True
                    yield ChatChunk(
                        text_delta=text_delta,
                        provider_snapshot="openai-compatible",
                        model_snapshot=latest_model,
                        raw_provider_metadata=event,
                    )
                continue

            if event_type in {
                "response.output_item.added",
                "response.output_item.done",
            }:
                index = _event_output_index(event)
                item = event.get("item")
                if isinstance(item, dict) and item.get("type") == "function_call":
                    builder = _tool_builder(builders, index)
                    _merge_function_item(
                        builder,
                        item,
                        index,
                        replace_arguments=event_type == "response.output_item.done",
                    )
                    if index not in started and builder["id"] and builder["name"]:
                        started.add(index)
                        yield _tool_call_start_chunk(
                            builder,
                            latest_model=latest_model,
                            event=event,
                        )
                continue

            if event_type == "response.function_call_arguments.delta":
                index = _event_output_index(event)
                builder = _tool_builder(builders, index)
                arguments_delta = _string_value(event.get("delta"), "")
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
                continue

            if event_type == "response.function_call_arguments.done":
                index = _event_output_index(event)
                builder = _tool_builder(builders, index)
                _merge_tool_identity(builder, "name", event.get("name"), index)
                arguments = event.get("arguments")
                if isinstance(arguments, str):
                    builder["arguments"] = arguments
                continue

            if event_type in {"response.completed", "response.incomplete"}:
                if isinstance(response, dict):
                    latest_finish_reason = _responses_finish_reason(
                        response,
                        has_tool_calls=bool(builders),
                    )
                    if not emitted_text:
                        final_text = _response_text(response)
                        if final_text:
                            emitted_text = True
                            yield ChatChunk(
                                text_delta=final_text,
                                provider_snapshot="openai-compatible",
                                model_snapshot=latest_model,
                                raw_provider_metadata=event,
                            )
    except Exception as exc:
        translated = translate_sdk_error(
            "openai-compatible",
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

    if builders and latest_finish_reason == "stop":
        latest_finish_reason = "tool_call"
    for index in sorted(builders):
        builder = builders[index]
        tool_call = _tool_call_from_builder(builder)
        if index not in started:
            yield _tool_call_start_chunk(
                builder,
                latest_model=latest_model,
                event=latest_event,
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


def _responses_payload(
    profile: LlmProfile,
    chat_request: "ChatRequest",
    *,
    stream: bool,
) -> dict[str, Any]:
    base_payload: dict[str, Any] = {
        "model": profile.model,
        "input": _responses_input(chat_request),
        "stream": stream,
    }
    if chat_request.tools:
        base_payload["tools"] = [
            {
                "type": "function",
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.input_schema,
                "strict": tool.strict,
            }
            for tool in chat_request.tools
        ]
        base_payload["tool_choice"] = _responses_tool_choice(chat_request)
    if chat_request.response_schema is not None:
        schema = chat_request.response_schema
        base_payload["text"] = {
            "format": {
                "type": "json_schema",
                "name": schema.name,
                "description": schema.description,
                "strict": schema.strict,
                "schema": schema.json_schema,
            }
        }
    return merge_request_options(
        base_payload,
        profile.request_options,
        chat_request.request_options,
    )


def _responses_input(chat_request: "ChatRequest") -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for message in chat_request.messages:
        if message.role != "tool" and message.content:
            items.append(
                {
                    "type": "message",
                    "role": message.role,
                    "content": message.content,
                }
            )
        elif message.role != "tool" and not message.tool_calls and not message.tool_results:
            items.append(
                {
                    "type": "message",
                    "role": message.role,
                    "content": "",
                }
            )
        items.extend(
            {
                "type": "function_call",
                "call_id": call.id,
                "name": call.name,
                "arguments": call.raw_arguments
                or json.dumps(call.arguments, ensure_ascii=False),
            }
            for call in message.tool_calls
        )
        items.extend(
            {
                "type": "function_call_output",
                "call_id": result.tool_call_id,
                "output": _result_content(result.content),
            }
            for result in message.tool_results
        )
    return items


def _responses_tool_choice(chat_request: "ChatRequest") -> str | dict[str, Any]:
    choice = chat_request.tool_choice
    if choice is None or choice.mode == "auto":
        return "auto"
    if choice.mode == "required":
        return "required"
    if choice.mode == "none":
        return "none"
    return {"type": "function", "name": choice.name}


def _response_output(response: dict[str, Any]) -> tuple[str, list["ToolCall"], str]:
    text = _response_text(response)
    tool_calls: list[ToolCall] = []
    for index, item in enumerate(_list_value(response.get("output"))):
        if isinstance(item, dict) and item.get("type") == "function_call":
            tool_calls.append(_tool_call_from_response(item, index))
    if not text and not tool_calls:
        raise RuntimeError(
            "OpenAI-compatible Responses output did not contain text or function calls."
        )
    return (
        text,
        tool_calls,
        _responses_finish_reason(response, has_tool_calls=bool(tool_calls)),
    )


def _response_text(response: dict[str, Any]) -> str:
    texts: list[str] = []
    for item in _list_value(response.get("output")):
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        text = _content_text(item.get("content"))
        if text:
            texts.append(text)
    return "".join(texts) or _content_text(response.get("output_text"))


def _tool_call_from_response(value: dict[str, Any], index: int) -> "ToolCall":
    raw_arguments = _string_value(value.get("arguments"), "")
    return _build_tool_call(
        value.get("call_id") or value.get("id"),
        value.get("name"),
        raw_arguments,
        index,
    )


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
        raise RuntimeError("OpenAI-compatible function call is missing a call_id.")
    if not isinstance(name, str) or not name:
        raise RuntimeError("OpenAI-compatible function call is missing a name.")
    arguments, parse_error = parse_tool_arguments(raw_arguments)
    return ToolCall(
        id=call_id,
        name=name,
        arguments=arguments,
        raw_arguments=raw_arguments,
        parse_error=parse_error,
        index=index,
    )


def _tool_builder(
    builders: dict[int, dict[str, Any]],
    index: int,
) -> dict[str, Any]:
    return builders.setdefault(
        index,
        {"id": "", "name": "", "arguments": "", "index": index},
    )


def _merge_response_tool_items(
    builders: dict[int, dict[str, Any]],
    response: dict[str, Any],
) -> None:
    for index, item in enumerate(_list_value(response.get("output"))):
        if not isinstance(item, dict) or item.get("type") != "function_call":
            continue
        _merge_function_item(
            _tool_builder(builders, index),
            item,
            index,
            replace_arguments=True,
        )


def _merge_function_item(
    builder: dict[str, Any],
    item: dict[str, Any],
    index: int,
    *,
    replace_arguments: bool,
) -> None:
    _merge_tool_identity(builder, "id", item.get("call_id") or item.get("id"), index)
    _merge_tool_identity(builder, "name", item.get("name"), index)
    arguments = item.get("arguments")
    if not isinstance(arguments, str):
        return
    if replace_arguments or (arguments and not builder["arguments"]):
        builder["arguments"] = arguments


def _merge_tool_identity(builder: dict[str, Any], field: str, value: Any, index: int) -> None:
    if not isinstance(value, str) or not value:
        return
    current = builder[field]
    if current and current != value:
        raise RuntimeError(
            f"OpenAI-compatible stream changed function call {field} at index {index}."
        )
    builder[field] = value


def _tool_call_start_chunk(
    builder: dict[str, Any],
    *,
    latest_model: str,
    event: dict[str, Any],
) -> "ChatChunk":
    from app.llm.gateway import ChatChunk

    return ChatChunk(
        event_type="tool_call_start",
        tool_call_id=_string_value(builder.get("id"), "") or None,
        tool_name=_string_value(builder.get("name"), "") or None,
        tool_index=cast(int, builder["index"]),
        provider_snapshot="openai-compatible",
        model_snapshot=latest_model,
        raw_provider_metadata=event,
    )


def _create_openai_response(
    profile: LlmProfile,
    payload: dict[str, Any],
    *,
    stream: bool,
) -> Any:
    body = dict(payload)
    model = body.pop("model")
    input_items = body.pop("input")
    body.pop("stream", None)
    if not isinstance(model, str) or not model:
        raise RuntimeError("OpenAI-compatible Responses request is missing a model.")
    if not isinstance(input_items, list):
        raise RuntimeError("OpenAI-compatible Responses request is missing input items.")
    try:
        responses = get_openai_client(profile).responses
        if stream:
            return responses.create(
                model=model,
                input=cast("ResponseInputParam", input_items),
                stream=True,
                extra_body=body or None,
            )
        return responses.create(
            model=model,
            input=cast("ResponseInputParam", input_items),
            stream=False,
            extra_body=body or None,
        )
    except Exception as exc:
        translated = translate_sdk_error(
            "openai-compatible",
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
        raise RuntimeError("OpenAI-compatible SDK returned an unsupported response type.")
    payload = model_dump(mode="json", exclude_none=True)
    if not isinstance(payload, dict):
        raise RuntimeError("OpenAI-compatible SDK returned a non-object response.")
    return cast(dict[str, Any], payload)


def _event_output_index(event: dict[str, Any]) -> int:
    value = event.get("output_index", 0)
    return value if isinstance(value, int) and value >= 0 else 0


def _responses_finish_reason(
    response: dict[str, Any],
    *,
    has_tool_calls: bool,
) -> str:
    status = response.get("status")
    if status == "incomplete":
        details = response.get("incomplete_details")
        if isinstance(details, dict):
            return _normalize_finish_reason(details.get("reason"))
        return "length"
    if status in {"cancelled", "canceled"}:
        return "cancelled"
    if has_tool_calls:
        return "tool_call"
    return "stop"


def _raise_stream_event_error(event: dict[str, Any]) -> None:
    event_type = _string_value(event.get("type"), "")
    if event_type == "error":
        _raise_provider_response_error(event, stage="stream")
    response = event.get("response")
    if not isinstance(response, dict):
        return
    if event_type == "response.failed" or response.get("status") in {
        "failed",
        "cancelled",
        "canceled",
    }:
        _raise_provider_response_error(
            _dict_value(response.get("error")) or response,
            stage="stream",
        )


def _raise_response_failure(
    response: dict[str, Any],
    *,
    stage: ProviderFailureStage,
) -> None:
    if response.get("status") not in {"failed", "cancelled", "canceled"}:
        return
    _raise_provider_response_error(
        _dict_value(response.get("error")) or response,
        stage=stage,
    )


def _raise_provider_response_error(
    error: dict[str, Any],
    *,
    stage: ProviderFailureStage,
) -> None:
    message = error.get("message")
    detail = message if isinstance(message, str) else json.dumps(error, ensure_ascii=False)
    code = _string_value(error.get("code"), "")
    retryable = code in {"server_error", "rate_limit_exceeded"} or (
        is_retryable_provider_error_message(f"{code} {detail}")
    )
    raise ProviderCallError(
        protocol="openai-compatible",
        kind=(
            "rate_limit"
            if code == "rate_limit_exceeded"
            else "connection"
            if retryable
            else "response"
        ),
        stage=stage,
        detail=detail,
        retryable=retryable,
    )


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
    if value in {"length", "max_tokens", "max_output_tokens"}:
        return "length"
    if value in {"cancelled", "canceled"}:
        return "cancelled"
    return value if isinstance(value, str) and value else "stop"
