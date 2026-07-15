import json
from collections.abc import Callable, Iterator
from copy import deepcopy
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from app.llm.anthropic_compatible import call_anthropic_compatible, stream_anthropic_compatible
from app.llm.openai_compatible import call_openai_compatible, stream_openai_compatible
from app.schemas.profiles import LlmProfile


def strict_model_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Return a provider-strict schema without weakening local Pydantic validation.

    OpenAI strict Tool/Structured Output schemas require every declared object
    property to appear in ``required``. Pydantic omits fields with local defaults
    from that list and emits the unsupported ``default`` annotation, so its raw
    schema cannot be sent as a strict provider contract.
    """

    return normalize_strict_json_schema(model.model_json_schema())


def normalize_strict_json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    normalized = deepcopy(schema)
    _normalize_strict_schema_node(normalized)
    return normalized


def _normalize_strict_schema_node(node: Any) -> None:
    if isinstance(node, list):
        for item in node:
            _normalize_strict_schema_node(item)
        return
    if not isinstance(node, dict):
        return

    node.pop("default", None)
    properties = node.get("properties")
    if isinstance(properties, dict):
        node["required"] = list(properties)
        node["additionalProperties"] = False
    for value in node.values():
        _normalize_strict_schema_node(value)


class ToolDefinition(BaseModel):
    name: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    description: str = Field(min_length=1, max_length=4_000)
    input_schema: dict[str, Any]
    strict: bool = True


class ToolChoice(BaseModel):
    mode: Literal["auto", "required", "none", "named"] = "auto"
    name: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_named_choice(self) -> "ToolChoice":
        if self.mode == "named" and self.name is None:
            raise ValueError("Named tool choice requires a tool name.")
        if self.mode != "named" and self.name is not None:
            raise ValueError("A tool name is only valid for named tool choice.")
        return self


class ResponseSchema(BaseModel):
    name: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    description: str = Field(default="", max_length=4_000)
    json_schema: dict[str, Any]
    strict: bool = True


class ToolCall(BaseModel):
    id: str = Field(min_length=1, max_length=512)
    name: str = Field(min_length=1, max_length=128)
    arguments: dict[str, Any] = Field(default_factory=dict)
    raw_arguments: str = ""
    parse_error: str | None = None
    index: int = Field(default=0, ge=0)


class ToolResult(BaseModel):
    tool_call_id: str = Field(min_length=1, max_length=512)
    name: str | None = Field(default=None, min_length=1, max_length=128)
    content: str | dict[str, Any] | list[Any]
    is_error: bool = False


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_results: list[ToolResult] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_role_content(self) -> "ChatMessage":
        if self.tool_calls and self.role != "assistant":
            raise ValueError("Tool calls are only valid on assistant messages.")
        if self.tool_results and self.role not in {"user", "tool"}:
            raise ValueError("Tool results are only valid on user or tool messages.")
        if self.role == "tool" and len(self.tool_results) != 1:
            raise ValueError("A tool message must contain exactly one tool result.")
        if self.role == "system" and (self.tool_calls or self.tool_results):
            raise ValueError("System messages cannot contain Tool calls or results.")
        return self


class ChatRequest(BaseModel):
    profile_id: str
    messages: list[ChatMessage]
    stream: bool = True
    tools: list[ToolDefinition] = Field(default_factory=list)
    tool_choice: ToolChoice | None = None
    response_schema: ResponseSchema | None = None
    request_options: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_execution_shape(self) -> "ChatRequest":
        if self.tools and self.response_schema is not None:
            raise ValueError("Tool execution and Structured Output cannot share one request.")
        if self.tool_choice is not None and not self.tools:
            raise ValueError("Tool choice requires at least one Tool definition.")
        names = [tool.name for tool in self.tools]
        if len(names) != len(set(names)):
            raise ValueError("Tool names must be unique within a request.")
        if self.tool_choice is not None and self.tool_choice.mode == "named":
            if self.tool_choice.name not in set(names):
                raise ValueError("Named tool choice must reference an exposed Tool.")
        return self

    @property
    def execution_mode(self) -> Literal["text", "tools", "structured_result"]:
        if self.tools:
            return "tools"
        if self.response_schema is not None:
            return "structured_result"
        return "text"


class ChatResult(BaseModel):
    content: str
    tool_calls: list[ToolCall] = Field(default_factory=list)
    structured_output: dict[str, Any] | None = None
    finish_reason: str = "stop"
    usage: dict[str, Any] = Field(default_factory=dict)
    model_snapshot: str
    provider_snapshot: str


class ChatChunk(BaseModel):
    text_delta: str = ""
    event_type: Literal[
        "text_delta",
        "tool_call_start",
        "tool_argument_delta",
        "tool_call_stop",
        "message_stop",
    ] = "text_delta"
    tool_call_id: str | None = None
    tool_name: str | None = None
    tool_index: int | None = Field(default=None, ge=0)
    arguments_delta: str = ""
    tool_call: ToolCall | None = None
    finish_reason: str | None = None
    usage: dict[str, Any] = Field(default_factory=dict)
    model_snapshot: str | None = None
    provider_snapshot: str
    raw_provider_metadata: dict[str, Any] = Field(default_factory=dict)


def call_llm(profile: LlmProfile, request: ChatRequest) -> ChatResult:
    if request.stream:
        return _collect_streaming_result(stream_llm(profile, request), request)
    if profile.protocol == "openai-compatible":
        result = call_openai_compatible(profile, request)
    elif profile.protocol == "anthropic-compatible":
        result = call_anthropic_compatible(profile, request)
    else:
        raise ValueError(f"Unsupported LLM protocol: {profile.protocol}")
    return _validate_result_shape(result, request)


def stream_llm(profile: LlmProfile, request: ChatRequest) -> Iterator[ChatChunk]:
    if profile.protocol == "openai-compatible":
        yield from stream_openai_compatible(profile, request)
        return
    if profile.protocol == "anthropic-compatible":
        yield from stream_anthropic_compatible(profile, request)
        return
    raise ValueError(f"Unsupported LLM protocol: {profile.protocol}")


def _collect_streaming_result(
    chunks: Iterator[ChatChunk],
    request: ChatRequest,
) -> ChatResult:
    content_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    usage: dict[str, Any] = {}
    model_snapshot = ""
    provider_snapshot = ""
    finish_reason = "stop"
    on_text_delta = request.metadata.get("on_text_delta")
    on_tool_event = request.metadata.get("on_tool_event")

    for chunk in chunks:
        provider_snapshot = chunk.provider_snapshot or provider_snapshot
        model_snapshot = chunk.model_snapshot or model_snapshot
        finish_reason = chunk.finish_reason or finish_reason
        if chunk.usage:
            usage = chunk.usage
        if chunk.text_delta:
            content_parts.append(chunk.text_delta)
            _notify_chunk(on_text_delta, chunk)
        if chunk.event_type in {
            "tool_call_start",
            "tool_argument_delta",
            "tool_call_stop",
        }:
            _notify_chunk(on_tool_event, chunk)
        if chunk.event_type == "tool_call_stop" and chunk.tool_call is not None:
            tool_calls.append(chunk.tool_call)

    result = ChatResult(
        content="".join(content_parts),
        tool_calls=tool_calls,
        finish_reason=finish_reason,
        usage=usage,
        model_snapshot=model_snapshot,
        provider_snapshot=provider_snapshot,
    )
    return _validate_result_shape(result, request)


def _validate_result_shape(result: ChatResult, request: ChatRequest) -> ChatResult:
    if request.execution_mode == "tools":
        unknown_tools = sorted({call.name for call in result.tool_calls} - {t.name for t in request.tools})
        if unknown_tools:
            raise RuntimeError(f"Provider called unexposed Tool(s): {', '.join(unknown_tools)}")
        return result
    if request.execution_mode == "structured_result":
        if not result.content.strip():
            raise RuntimeError("Structured Output response did not contain JSON content.")
        try:
            payload = json.loads(result.content)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Structured Output response was not valid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("Structured Output response must be a JSON object.")
        return result.model_copy(update={"structured_output": payload})
    return result


def parse_tool_arguments(raw_arguments: str) -> tuple[dict[str, Any], str | None]:
    if not raw_arguments.strip():
        return {}, None
    try:
        value = json.loads(raw_arguments)
    except json.JSONDecodeError as exc:
        return {}, str(exc)
    if not isinstance(value, dict):
        return {}, "Tool arguments must be a JSON object."
    return value, None


def _notify_chunk(callback: Any, chunk: ChatChunk) -> None:
    if not callable(callback):
        return
    typed_callback: Callable[[ChatChunk], None] = callback
    typed_callback(chunk)
