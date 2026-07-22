import json
import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Literal, Mapping

import anthropic
import httpx
import openai


ProviderProtocol = Literal["openai-compatible", "anthropic-compatible"]
ProviderFailureKind = Literal[
    "connection",
    "timeout",
    "rate_limit",
    "http",
    "response",
]
ProviderFailureStage = Literal["request", "stream", "response"]

_HTTP_STATUS_PATTERN = re.compile(r"provider returned\s+(\d{3})", re.IGNORECASE)
_NON_RETRYABLE_AUTH_TEXT = (
    "auth_unavailable",
    "no auth available",
    "invalid api key",
    "invalid_api_key",
    "authentication failed",
    "unauthorized",
    "forbidden",
)
_RETRYABLE_TEXT = (
    "provider request failed",
    "provider stream failed",
    "temporary provider failure",
    "connection reset",
    "connection aborted",
    "connection refused",
    "connection closed",
    "connection broken",
    "remote end closed",
    "server disconnected",
    "network is unreachable",
    "broken pipe",
    "incompleteread",
    "timed out",
    "timeout",
    "internal_error",
    "unexpected_eof",
    "unexpected eof",
    " eof",
    "ssl",
    "tls",
    "service unavailable",
    "bad gateway",
    "gateway timeout",
)


class ProviderCallError(RuntimeError):
    """Normalized SDK failure consumed by the Harness retry boundary."""

    def __init__(
        self,
        *,
        protocol: ProviderProtocol,
        kind: ProviderFailureKind,
        stage: ProviderFailureStage,
        detail: str,
        retryable: bool,
        status_code: int | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        self.protocol = protocol
        self.kind = kind
        self.stage = stage
        self.detail = detail.strip() or "Provider request failed."
        self.retryable = retryable
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds
        super().__init__(self._message())

    def _message(self) -> str:
        provider = (
            "OpenAI-compatible"
            if self.protocol == "openai-compatible"
            else "Anthropic-compatible"
        )
        if self.status_code is not None:
            return f"{provider} provider returned {self.status_code}: {self.detail}"
        if self.stage == "stream":
            return f"{provider} provider stream failed: {self.detail}"
        if self.kind == "response":
            return f"{provider} provider response invalid: {self.detail}"
        return f"{provider} provider request failed: {self.detail}"


def translate_sdk_error(
    protocol: ProviderProtocol,
    error: BaseException,
    *,
    stage: ProviderFailureStage,
) -> ProviderCallError | None:
    if isinstance(error, ProviderCallError):
        return error
    if protocol == "openai-compatible":
        return _translate_openai_error(error, stage=stage)
    return _translate_anthropic_error(error, stage=stage)


def is_retryable_provider_error_message(message: str) -> bool:
    lowered = message.casefold()
    if any(marker in lowered for marker in _NON_RETRYABLE_AUTH_TEXT):
        return False
    match = _HTTP_STATUS_PATTERN.search(message)
    if match is not None:
        return provider_status_is_retryable(int(match.group(1)), message)
    return any(marker in lowered for marker in _RETRYABLE_TEXT)


def provider_status_is_retryable(status_code: int, detail: str) -> bool:
    lowered = detail.casefold()
    if any(marker in lowered for marker in _NON_RETRYABLE_AUTH_TEXT):
        return False
    return status_code in {408, 409, 425, 429} or 500 <= status_code <= 599


def _translate_openai_error(
    error: BaseException,
    *,
    stage: ProviderFailureStage,
) -> ProviderCallError | None:
    if isinstance(error, openai.APITimeoutError):
        return _connection_error(
            "openai-compatible",
            error,
            kind="timeout",
            stage=stage,
        )
    if isinstance(error, openai.APIConnectionError):
        return _connection_error(
            "openai-compatible",
            error,
            kind="connection",
            stage=stage,
        )
    if isinstance(error, openai.APIStatusError):
        return _status_error("openai-compatible", error, stage=stage)
    if isinstance(error, openai.APIResponseValidationError):
        return _response_error("openai-compatible", error, stage=stage)
    if isinstance(error, openai.APIError):
        return _generic_sdk_error("openai-compatible", error, stage=stage)
    if isinstance(error, (httpx.TimeoutException, TimeoutError)):
        return _connection_error(
            "openai-compatible",
            error,
            kind="timeout",
            stage=stage,
        )
    if isinstance(error, (httpx.TransportError, ConnectionError)):
        return _connection_error(
            "openai-compatible",
            error,
            kind="connection",
            stage=stage,
        )
    return None


def _translate_anthropic_error(
    error: BaseException,
    *,
    stage: ProviderFailureStage,
) -> ProviderCallError | None:
    if isinstance(error, anthropic.APITimeoutError):
        return _connection_error(
            "anthropic-compatible",
            error,
            kind="timeout",
            stage=stage,
        )
    if isinstance(error, anthropic.APIConnectionError):
        return _connection_error(
            "anthropic-compatible",
            error,
            kind="connection",
            stage=stage,
        )
    if isinstance(error, anthropic.APIStatusError):
        return _status_error("anthropic-compatible", error, stage=stage)
    if isinstance(error, anthropic.APIResponseValidationError):
        return _response_error("anthropic-compatible", error, stage=stage)
    if isinstance(error, anthropic.APIError):
        return _generic_sdk_error("anthropic-compatible", error, stage=stage)
    if isinstance(error, (httpx.TimeoutException, TimeoutError)):
        return _connection_error(
            "anthropic-compatible",
            error,
            kind="timeout",
            stage=stage,
        )
    if isinstance(error, (httpx.TransportError, ConnectionError)):
        return _connection_error(
            "anthropic-compatible",
            error,
            kind="connection",
            stage=stage,
        )
    return None


def _connection_error(
    protocol: ProviderProtocol,
    error: BaseException,
    *,
    kind: Literal["connection", "timeout"],
    stage: ProviderFailureStage,
) -> ProviderCallError:
    cause = error.__cause__
    detail = str(cause).strip() if cause is not None else ""
    if not detail:
        detail = str(error)
    return ProviderCallError(
        protocol=protocol,
        kind=kind,
        stage=stage,
        detail=detail,
        retryable=True,
    )


def _status_error(
    protocol: ProviderProtocol,
    error: Any,
    *,
    stage: ProviderFailureStage,
) -> ProviderCallError:
    status_code = int(error.status_code)
    detail = _error_detail(error)
    return ProviderCallError(
        protocol=protocol,
        kind="rate_limit" if status_code == 429 else "http",
        stage=stage,
        detail=detail,
        retryable=provider_status_is_retryable(status_code, detail),
        status_code=status_code,
        retry_after_seconds=_retry_after_seconds(error.response.headers),
    )


def _response_error(
    protocol: ProviderProtocol,
    error: BaseException,
    *,
    stage: ProviderFailureStage,
) -> ProviderCallError:
    return ProviderCallError(
        protocol=protocol,
        kind="response",
        stage=stage,
        detail=_error_detail(error),
        retryable=False,
    )


def _generic_sdk_error(
    protocol: ProviderProtocol,
    error: BaseException,
    *,
    stage: ProviderFailureStage,
) -> ProviderCallError:
    detail = _error_detail(error)
    retryable = is_retryable_provider_error_message(detail)
    return ProviderCallError(
        protocol=protocol,
        kind="connection" if retryable else "response",
        stage=stage,
        detail=detail,
        retryable=retryable,
    )


def _error_detail(error: BaseException) -> str:
    body = getattr(error, "body", None)
    if body is not None:
        if isinstance(body, str):
            detail = body
        else:
            detail = json.dumps(body, ensure_ascii=False, default=str)
        if detail.strip():
            return detail[:8_000]
    cause = error.__cause__
    if cause is not None and str(cause).strip():
        return str(cause)[:8_000]
    return str(error)[:8_000]


def _retry_after_seconds(headers: Mapping[str, str]) -> float | None:
    milliseconds = headers.get("retry-after-ms")
    if milliseconds is not None:
        try:
            value = float(milliseconds) / 1_000
        except ValueError:
            pass
        else:
            return max(value, 0.0)

    raw = headers.get("retry-after")
    if raw is None:
        return None
    try:
        return max(float(raw), 0.0)
    except ValueError:
        pass
    try:
        retry_at = parsedate_to_datetime(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=UTC)
    return max((retry_at - datetime.now(UTC)).total_seconds(), 0.0)
