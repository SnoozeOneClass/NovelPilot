import re
import time
from collections.abc import Callable

from app.llm.gateway import ChatRequest, ChatResult
from app.schemas.profiles import LlmProfile


LlmCall = Callable[[LlmProfile, ChatRequest], ChatResult]
TransportRetryCallback = Callable[[int, int, Exception], None]
SleepCall = Callable[[float], None]

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
    "unexpected_eof",
    "unexpected eof",
    " eof",
    "ssl",
    "tls",
    "auth_unavailable",
    "service unavailable",
    "bad gateway",
    "gateway timeout",
)


def call_llm_with_transport_retries(
    profile: LlmProfile,
    request: ChatRequest,
    *,
    retry_limit: int,
    llm_call: LlmCall,
    on_retry: TransportRetryCallback | None = None,
    sleep_call: SleepCall = time.sleep,
    base_delay_seconds: float = 0.5,
) -> ChatResult:
    """Call one provider request with its own bounded transport retry budget."""

    if retry_limit < 0:
        raise ValueError("Provider transport retry limit must not be negative.")
    if base_delay_seconds < 0:
        raise ValueError("Provider retry delay must not be negative.")

    retries = 0
    while True:
        try:
            return llm_call(profile, request)
        except Exception as exc:
            if not is_retryable_provider_error(exc) or retries >= retry_limit:
                raise
            retries += 1
            if on_retry is not None:
                on_retry(retries, retry_limit, exc)
            delay = min(base_delay_seconds * (2 ** (retries - 1)), 4.0)
            if delay:
                sleep_call(delay)


def is_retryable_provider_error(error: BaseException) -> bool:
    if isinstance(error, (ConnectionError, TimeoutError)):
        return True
    return is_retryable_provider_error_message(str(error))


def is_retryable_provider_error_message(message: str) -> bool:
    lowered = message.casefold()
    if any(marker in lowered for marker in _NON_RETRYABLE_AUTH_TEXT):
        return False
    match = _HTTP_STATUS_PATTERN.search(message)
    if match is not None:
        status = int(match.group(1))
        return status in {408, 409, 425, 429} or 500 <= status <= 599
    return any(marker in lowered for marker in _RETRYABLE_TEXT)
