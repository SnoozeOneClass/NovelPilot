from __future__ import annotations

from dataclasses import dataclass

import httpx
from pydantic_ai.exceptions import ModelHTTPError

from app.authoring.models.transport import (
    ProviderEmptyOutput,
    ProviderStreamIncomplete,
    request_budget_exhaustion,
)

RETRYABLE_STATUS_CODES = frozenset({408, 409, 425, 429, *range(500, 600)})


@dataclass(frozen=True, slots=True)
class RetryableFailure:
    reason: str
    http_status: int | None
    retry_after_seconds: float | None


def retryable_provider_failure(error: BaseException) -> RetryableFailure | None:
    if request_budget_exhaustion(error) is not None:
        return None
    if isinstance(error, ProviderEmptyOutput):
        return RetryableFailure("provider_empty_output", None, None)
    if isinstance(error, ProviderStreamIncomplete):
        return RetryableFailure("provider_stream_incomplete", None, None)
    status = error.status_code if isinstance(error, ModelHTTPError) else None
    if status in RETRYABLE_STATUS_CODES:
        body = str(getattr(error, "body", ""))
        if status == 429 and any(
            word in body.casefold() for word in ("quota", "billing", "credit")
        ):
            return None
        return RetryableFailure(f"provider_http_{status}", status, _retry_after(error))
    if isinstance(
        error, (httpx.ConnectError, httpx.ReadError, httpx.TimeoutException, ConnectionError)
    ):
        return RetryableFailure("provider_connection_failure", None, None)
    return None


def _retry_after(error: BaseException) -> float | None:
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    value = headers.get("retry-after")
    try:
        return None if value is None else max(0.0, float(value))
    except (TypeError, ValueError):
        return None
