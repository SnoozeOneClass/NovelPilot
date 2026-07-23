from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx
from pydantic_ai.models import Model, ModelRequestParameters, StreamedResponse
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.retries import AsyncTenacityTransport, RetryConfig, wait_retry_after
from pydantic_ai.settings import ModelSettings
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai import RunContext
from tenacity import retry_if_exception_type, stop_after_attempt, wait_exponential

from app.agents.contracts import PROVIDER_REQUEST_LIMIT, TRANSPORT_RETRY_LIMIT

RETRYABLE_STATUS_CODES = frozenset({408, 409, 425, 429, *range(500, 600)})
_QUOTA_MARKERS = (
    "insufficient_quota",
    "billing_hard_limit_reached",
    "billing_not_active",
    "credit balance",
    "credits exhausted",
    "payment required",
)


class ActivationRequestBudgetExhausted(RuntimeError):
    """The next network request would exceed the activation-wide physical budget."""


class ModelRequestBudgetExhausted(RuntimeError):
    """Pydantic AI attempted more semantic requests than this task contract allows."""


@dataclass(frozen=True, slots=True)
class ProviderAttempt:
    sequence: int
    method: str
    status_code: int | None
    error_type: str | None


@dataclass(slots=True)
class ActivationRequestBudget:
    """One counter shared by output repair and every HTTP transport retry."""

    model_request_limit: int
    provider_request_limit: int = PROVIDER_REQUEST_LIMIT
    provider_request_count: int = 0
    model_request_count: int = 0
    _model_call_attempts: int = 0
    _active_model_call_has_request: bool = False
    attempts: list[ProviderAttempt] = field(default_factory=list)

    @property
    def transport_retry_count(self) -> int:
        return self.provider_request_count - self.model_request_count

    def begin_model_call(self) -> None:
        self._model_call_attempts += 1
        if self._model_call_attempts > self.model_request_limit:
            raise ModelRequestBudgetExhausted(
                f"Task allows at most {self.model_request_limit} model request(s)."
            )
        if self._active_model_call_has_request:
            raise RuntimeError("Nested model requests are not supported by one task activation.")
        self._active_model_call_has_request = False

    def end_model_call(self) -> None:
        self._active_model_call_has_request = False

    def begin_provider_request(self, request: httpx.Request) -> int:
        if self.provider_request_count >= self.provider_request_limit:
            raise ActivationRequestBudgetExhausted(
                f"Task activation exhausted its {self.provider_request_limit} physical requests."
            )
        self.provider_request_count += 1
        if not self._active_model_call_has_request:
            self.model_request_count += 1
            self._active_model_call_has_request = True
        return self.provider_request_count

    def record_attempt(
        self,
        *,
        sequence: int,
        request: httpx.Request,
        status_code: int | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.attempts.append(
            ProviderAttempt(
                sequence=sequence,
                method=request.method,
                status_code=status_code,
                error_type=type(error).__name__ if error is not None else None,
            )
        )

    def assert_terminal_invariants(self) -> None:
        if self.provider_request_count > self.provider_request_limit:
            raise AssertionError("Physical Provider request budget was exceeded.")
        if self.transport_retry_count > TRANSPORT_RETRY_LIMIT:
            raise AssertionError("Transport retry budget was exceeded.")
        if self.transport_retry_count < 0:
            raise AssertionError("Model request count cannot exceed physical requests.")


class ActivationBudgetTransport(httpx.AsyncBaseTransport):
    def __init__(self, *, budget: ActivationRequestBudget, wrapped: httpx.AsyncBaseTransport) -> None:
        self._budget = budget
        self._wrapped = wrapped

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        sequence = self._budget.begin_provider_request(request)
        try:
            response = await self._wrapped.handle_async_request(request)
        except BaseException as exc:
            self._budget.record_attempt(sequence=sequence, request=request, error=exc)
            raise
        self._budget.record_attempt(
            sequence=sequence,
            request=request,
            status_code=response.status_code,
        )
        return response

    async def aclose(self) -> None:
        await self._wrapped.aclose()


class RetryableResponseTransport(httpx.AsyncBaseTransport):
    """Convert only the approved transient statuses into Tenacity retry signals."""

    def __init__(self, wrapped: httpx.AsyncBaseTransport) -> None:
        self._wrapped = wrapped

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self._wrapped.handle_async_request(request)
        response.request = request
        if response.status_code not in RETRYABLE_STATUS_CODES:
            return response
        if response.status_code == 429 and await _is_explicit_quota_failure(response):
            return response
        await response.aread()
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError:
            await response.aclose()
            raise
        return response  # pragma: no cover - retryable status always raises.

    async def aclose(self) -> None:
        await self._wrapped.aclose()


async def _is_explicit_quota_failure(response: httpx.Response) -> bool:
    await response.aread()
    body = response.content.decode("utf-8", errors="replace").casefold()
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        payload_text = body
    else:
        payload_text = json.dumps(payload, ensure_ascii=False, sort_keys=True).casefold()
    return any(marker in payload_text for marker in _QUOTA_MARKERS)


def build_retrying_transport(
    *,
    budget: ActivationRequestBudget,
    wrapped: httpx.AsyncBaseTransport | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> httpx.AsyncBaseTransport:
    physical = ActivationBudgetTransport(
        budget=budget,
        wrapped=wrapped or httpx.AsyncHTTPTransport(),
    )
    classified = RetryableResponseTransport(physical)
    config: RetryConfig = {
        "retry": retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
        "wait": wait_retry_after(
            fallback_strategy=wait_exponential(multiplier=1, max=60),
            max_wait=300,
        ),
        "stop": stop_after_attempt(PROVIDER_REQUEST_LIMIT),
        "reraise": True,
    }
    if sleep is not None:
        config["sleep"] = sleep
    return AsyncTenacityTransport(config, wrapped=classified)


class RequestCountingModel(WrapperModel):
    """Count framework model requests without changing the wrapped Adapter semantics."""

    def __init__(self, wrapped: Model, *, budget: ActivationRequestBudget) -> None:
        super().__init__(wrapped)
        self._budget = budget

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        self._budget.begin_model_call()
        try:
            return await self.wrapped.request(messages, model_settings, model_request_parameters)
        finally:
            self._budget.end_model_call()

    @asynccontextmanager
    async def request_stream(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
        run_context: RunContext[Any] | None = None,
    ) -> AsyncIterator[StreamedResponse]:
        self._budget.begin_model_call()
        try:
            async with self.wrapped.request_stream(
                messages,
                model_settings,
                model_request_parameters,
                run_context,
            ) as response:
                yield response
        finally:
            self._budget.end_model_call()
