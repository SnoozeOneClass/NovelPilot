from collections.abc import Callable, Iterator
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.llm.anthropic_compatible import call_anthropic_compatible, stream_anthropic_compatible
from app.llm.openai_compatible import call_openai_compatible, stream_openai_compatible
from app.schemas.profiles import LlmProfile


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    profile_id: str
    messages: list[ChatMessage]
    stream: bool = True
    request_options: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ChatResult(BaseModel):
    content: str
    usage: dict[str, Any] = Field(default_factory=dict)
    model_snapshot: str
    provider_snapshot: str


class ChatChunk(BaseModel):
    text_delta: str = ""
    event_type: Literal["delta", "message_stop"] = "delta"
    usage: dict[str, Any] = Field(default_factory=dict)
    model_snapshot: str | None = None
    provider_snapshot: str
    raw_provider_metadata: dict[str, Any] = Field(default_factory=dict)


def call_llm(profile: LlmProfile, request: ChatRequest) -> ChatResult:
    if request.stream:
        return _collect_streaming_result(stream_llm(profile, request), request)
    if profile.protocol == "openai-compatible":
        return call_openai_compatible(profile, request)
    if profile.protocol == "anthropic-compatible":
        return call_anthropic_compatible(profile, request)
    raise ValueError(f"Unsupported LLM protocol: {profile.protocol}")


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
    usage: dict[str, Any] = {}
    model_snapshot = ""
    provider_snapshot = ""
    on_text_delta = request.metadata.get("on_text_delta")

    for chunk in chunks:
        provider_snapshot = chunk.provider_snapshot or provider_snapshot
        model_snapshot = chunk.model_snapshot or model_snapshot
        if chunk.usage:
            usage = chunk.usage
        if chunk.text_delta:
            content_parts.append(chunk.text_delta)
            _notify_text_delta(on_text_delta, chunk)

    return ChatResult(
        content="".join(content_parts),
        usage=usage,
        model_snapshot=model_snapshot,
        provider_snapshot=provider_snapshot,
    )


def _notify_text_delta(callback: Any, chunk: ChatChunk) -> None:
    if not callable(callback):
        return
    typed_callback: Callable[[ChatChunk], None] = callback
    typed_callback(chunk)
