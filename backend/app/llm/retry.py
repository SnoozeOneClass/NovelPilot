import time
from collections.abc import Callable

from app.llm.gateway import ChatRequest, ChatResult
from app.llm.provider_errors import (
    ProviderCallError,
    is_retryable_provider_error_message as is_retryable_provider_error_message,
)
from app.schemas.profiles import LlmProfile


LlmCall = Callable[[LlmProfile, ChatRequest], ChatResult]
TransportRetryCallback = Callable[[int, int, Exception], None]
SleepCall = Callable[[float], None]

def call_llm_with_transport_retries(
    profile: LlmProfile,
    request: ChatRequest,
    *,
    retry_limit: int,
    llm_call: LlmCall,
    on_retry: TransportRetryCallback | None = None,
    sleep_call: SleepCall = time.sleep,
    base_delay_seconds: float = 0.5,
    max_delay_seconds: float = 30.0,
) -> ChatResult:
    """Call one provider request with its own bounded transport retry budget."""

    if retry_limit < 0:
        raise ValueError("Provider transport retry limit must not be negative.")
    if base_delay_seconds < 0:
        raise ValueError("Provider retry delay must not be negative.")
    if max_delay_seconds < 0:
        raise ValueError("Provider maximum retry delay must not be negative.")

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
            local_delay = min(base_delay_seconds * (2 ** (retries - 1)), 4.0)
            retry_after = (
                exc.retry_after_seconds
                if isinstance(exc, ProviderCallError)
                else None
            )
            delay = min(
                retry_after if retry_after is not None else local_delay,
                max_delay_seconds,
            )
            if delay:
                sleep_call(delay)


def is_retryable_provider_error(error: BaseException) -> bool:
    if isinstance(error, ProviderCallError):
        return error.retryable
    if isinstance(error, (ConnectionError, TimeoutError)):
        return True
    return is_retryable_provider_error_message(str(error))
