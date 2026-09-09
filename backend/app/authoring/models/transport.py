from __future__ import annotations

import re
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Literal, cast

import httpx
from anthropic import APIConnectionError as AnthropicConnectionError
from openai import APIConnectionError as OpenAIConnectionError
from pydantic_ai import RunContext
from pydantic_ai.exceptions import ModelAPIError
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models import Model, ModelRequestParameters, StreamedResponse
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.settings import ModelSettings

from app.authoring.models.contracts import TRANSPORT_RETRY_LIMIT, provider_request_capacity

RetryDecision = Literal["retry", "failed", "completed"]


class ActivationRequestBudgetExhausted(RuntimeError):
    """An activation exhausted a local request limit and must not be retried afresh."""


class ModelRequestBudgetExhausted(ActivationRequestBudgetExhausted):
    """The Agent attempted more semantic turns than this activation allows."""


class TransportRetryBudgetExhausted(ActivationRequestBudgetExhausted):
    """The next physical replay would exceed the separate retry allowance."""


def request_budget_exhaustion(error: BaseException) -> ActivationRequestBudgetExhausted | None:
    """Find a local limit only through explicit, known Pydantic/SDK wrapper causes.

    An unrelated exception's implicit context is not proof of budget exhaustion.
    In particular, real connection failures must keep their original classification.
    """
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        if isinstance(current, ActivationRequestBudgetExhausted):
            return current
        if not isinstance(
            current, (ModelAPIError, OpenAIConnectionError, AnthropicConnectionError)
        ):
            return None
        seen.add(id(current))
        current = current.__cause__
    return None


class ProviderOutputTruncated(RuntimeError):
    """The Provider ended a response at its output-token boundary."""


class ProviderStreamIncomplete(RuntimeError):
    """A streamed wire response ended without a protocol-complete terminal state."""


class ProviderEmptyOutput(ProviderStreamIncomplete):
    """A complete Provider response contained no task-consumable final output."""


def _wall_clock_ms() -> int:
    return time.time_ns() // 1_000_000


@dataclass(slots=True)
class ProviderAttempt:
    sequence: int
    protocol: str
    method: str
    started_at_ms: int
    headers_at_ms: int | None = None
    first_event_at_ms: int | None = None
    last_event_at_ms: int | None = None
    finished_at_ms: int | None = None
    status_code: int | None = None
    provider_request_id: str | None = None
    error_type: str | None = None
    retry_decision: RetryDecision | None = None
    retry_reason: str | None = None
    retry_delay_ms: int | None = None


@dataclass(slots=True)
class ActivationRequestBudget:
    """Finite semantic turns plus separately authorized physical replays.

    The default physical cap is ``model_request_limit + transport_retry_limit``:
    a Worker with 24 turns has capacity for all 24 normal calls and up to 5 replay
    sends; a probe with 2 turns still stops at 2 normal calls. There are no hidden
    SDK retries. ``begin_agent_run`` explicitly replays the frozen run's ordinals
    without resetting any totals. Each ordinal's first actual send is semantic;
    subsequent sends at that ordinal are retries, including partial/full replays.
    Thus ``provider requests = semantic requests + transport retries`` even when
    a model call fails locally before sending HTTP. Exhaustion is terminal for
    this activation; resetting the run ordinal cannot refill an exhausted budget.
    """

    model_request_limit: int
    protocol: str = "test"
    provider_request_limit: int | None = None
    now_ms: Callable[[], int] = _wall_clock_ms
    transport_retry_limit: int = TRANSPORT_RETRY_LIMIT
    provider_request_count: int = 0
    model_request_count: int = 0
    _current_model_ordinal: int = 0
    _model_call_active: bool = False
    attempts: list[ProviderAttempt] = field(default_factory=list)
    _sent_model_ordinals: set[int] = field(default_factory=set, init=False)
    _exhausted: ActivationRequestBudgetExhausted | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        capacity = provider_request_capacity(self.model_request_limit, self.transport_retry_limit)
        if self.provider_request_limit is None:
            self.provider_request_limit = capacity
        elif type(self.provider_request_limit) is not int or self.provider_request_limit < capacity:
            raise ValueError(
                "provider_request_limit must accommodate model_request_limit + "
                "transport_retry_limit; reduce the explicit retry allowance for a smaller cap."
            )

    @property
    def transport_retry_count(self) -> int:
        return self.provider_request_count - self.model_request_count

    @property
    def can_replay(self) -> bool:
        assert self.provider_request_limit is not None
        return (
            self._exhausted is None
            and self.provider_request_count < self.provider_request_limit
            and self.transport_retry_count < self.transport_retry_limit
        )

    @property
    def latest_attempt(self) -> ProviderAttempt | None:
        return self.attempts[-1] if self.attempts else None

    def begin_agent_run(self) -> None:
        if self._model_call_active:
            raise RuntimeError("Cannot restart an Agent run while a model request is active.")
        self._current_model_ordinal = 0

    def begin_model_call(self) -> None:
        if self._model_call_active:
            raise RuntimeError("Nested model requests are not supported by one task activation.")
        if self._exhausted is not None:
            raise self._exhausted
        ordinal = self._current_model_ordinal + 1
        if ordinal > self.model_request_limit:
            self._exhausted = ModelRequestBudgetExhausted(
                f"Task allows at most {self.model_request_limit} semantic model request(s) "
                "per frozen Agent run."
            )
            raise self._exhausted
        self._check_provider_capacity(ordinal)
        self._current_model_ordinal = ordinal
        self._model_call_active = True

    def end_model_call(self) -> None:
        self._model_call_active = False

    def begin_provider_request(self, request: httpx.Request) -> int:
        # Direct transport tests and diagnostics may not use RequestCountingModel.
        ordinal = self._current_model_ordinal or 1
        self._check_provider_capacity(ordinal)
        self._current_model_ordinal = ordinal
        self.provider_request_count += 1
        if ordinal not in self._sent_model_ordinals:
            self._sent_model_ordinals.add(ordinal)
            self.model_request_count += 1
        sequence = self.provider_request_count
        self.attempts.append(
            ProviderAttempt(
                sequence=sequence,
                protocol=self.protocol,
                method=request.method,
                started_at_ms=self.now_ms(),
            )
        )
        return sequence

    def _check_provider_capacity(self, ordinal: int) -> None:
        if self._exhausted is not None:
            raise self._exhausted
        assert self.provider_request_limit is not None
        if self.provider_request_count >= self.provider_request_limit:
            self._exhausted = ActivationRequestBudgetExhausted(
                f"Task activation exhausted its {self.provider_request_limit} physical requests."
            )
        elif (
            ordinal in self._sent_model_ordinals
            and self.transport_retry_count >= self.transport_retry_limit
        ):
            self._exhausted = TransportRetryBudgetExhausted(
                f"Task activation exhausted its {self.transport_retry_limit} transport retries."
            )
        if self._exhausted is not None:
            raise self._exhausted

    def record_response_headers(self, *, sequence: int, response: httpx.Response) -> None:
        attempt = self._attempt(sequence)
        attempt.headers_at_ms = self.now_ms()
        attempt.status_code = response.status_code
        attempt.provider_request_id = _provider_request_id(response.headers)

    def record_stream_event(self, *, sequence: int) -> None:
        attempt = self._attempt(sequence)
        timestamp = self.now_ms()
        if attempt.first_event_at_ms is None:
            attempt.first_event_at_ms = timestamp
        attempt.last_event_at_ms = timestamp

    def finish_provider_request(
        self,
        *,
        sequence: int,
        error: BaseException | None = None,
    ) -> None:
        attempt = self._attempt(sequence)
        if attempt.finished_at_ms is None:
            attempt.finished_at_ms = self.now_ms()
        if error is not None:
            attempt.error_type = type(error).__name__
        elif (
            attempt.retry_decision is None
            and attempt.status_code is not None
            and attempt.status_code < 400
        ):
            attempt.retry_decision = "completed"

    def record_retry_decision(
        self,
        *,
        retry: bool,
        reason: str,
        delay_seconds: float | None = None,
    ) -> None:
        attempt = self.latest_attempt
        if attempt is None:
            return
        attempt.retry_decision = "retry" if retry else "failed"
        attempt.retry_reason = reason
        attempt.retry_delay_ms = (
            None if delay_seconds is None else max(0, round(delay_seconds * 1_000))
        )

    def assert_terminal_invariants(self) -> None:
        assert self.provider_request_limit is not None
        if self.provider_request_count > self.provider_request_limit:
            raise AssertionError("Physical Provider request budget was exceeded.")
        if self.model_request_count > self.model_request_limit:
            raise AssertionError("Semantic model request budget was exceeded.")
        if self.transport_retry_count > self.transport_retry_limit:
            raise AssertionError("Transport retry budget was exceeded.")
        if self.transport_retry_count < 0:
            raise AssertionError("Model request count cannot exceed physical requests.")
        if self.model_request_count != len(self._sent_model_ordinals):
            raise AssertionError("Semantic request count must reflect actual first sends.")
        if len(self.attempts) != self.provider_request_count:
            raise AssertionError("Every physical Provider request requires one evidence record.")
        if [attempt.sequence for attempt in self.attempts] != list(
            range(1, self.provider_request_count + 1)
        ):
            raise AssertionError("Provider request evidence sequence is not contiguous.")

    def _attempt(self, sequence: int) -> ProviderAttempt:
        try:
            attempt = self.attempts[sequence - 1]
        except IndexError as exc:  # pragma: no cover - internal invariant.
            raise AssertionError(f"Unknown Provider attempt sequence {sequence}.") from exc
        if attempt.sequence != sequence:  # pragma: no cover - internal invariant.
            raise AssertionError("Provider attempt sequence drifted.")
        return attempt


class ObservedResponseStream(httpx.AsyncByteStream):
    """Observe SSE/body consumption, including errors after HTTP 200 headers."""

    def __init__(
        self,
        *,
        wrapped: httpx.AsyncByteStream,
        budget: ActivationRequestBudget,
        sequence: int,
    ) -> None:
        self._wrapped = wrapped
        self._budget = budget
        self._sequence = sequence
        self._finished = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        try:
            async for chunk in self._wrapped:
                self._budget.record_stream_event(sequence=self._sequence)
                yield chunk
        except BaseException as exc:
            self._finished = True
            self._budget.finish_provider_request(sequence=self._sequence, error=exc)
            raise
        else:
            self._finished = True
            self._budget.finish_provider_request(sequence=self._sequence)

    async def aclose(self) -> None:
        try:
            await self._wrapped.aclose()
        finally:
            if not self._finished:
                self._finished = True
                self._budget.finish_provider_request(sequence=self._sequence)


class ObservedTransport(httpx.AsyncBaseTransport):
    """Count one physical call and wrap its response stream without retrying it."""

    def __init__(
        self, *, budget: ActivationRequestBudget, wrapped: httpx.AsyncBaseTransport
    ) -> None:
        self._budget = budget
        self._wrapped = wrapped

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        sequence = self._budget.begin_provider_request(request)
        try:
            response = await self._wrapped.handle_async_request(request)
        except BaseException as exc:
            self._budget.finish_provider_request(sequence=sequence, error=exc)
            raise
        response.request = request
        self._budget.record_response_headers(sequence=sequence, response=response)
        response.stream = ObservedResponseStream(
            wrapped=cast(httpx.AsyncByteStream, response.stream),
            budget=self._budget,
            sequence=sequence,
        )
        return response

    async def aclose(self) -> None:
        await self._wrapped.aclose()


def build_observed_transport(
    *,
    budget: ActivationRequestBudget,
    wrapped: httpx.AsyncBaseTransport | None = None,
) -> httpx.AsyncBaseTransport:
    return ObservedTransport(
        budget=budget,
        wrapped=wrapped or httpx.AsyncHTTPTransport(),
    )


class RequestCountingModel(WrapperModel):
    """Track semantic ordinals and force every Provider call onto streamed wire I/O."""

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
            async with self.wrapped.request_stream(
                messages,
                model_settings,
                model_request_parameters,
            ) as streamed:
                async for _event in streamed:
                    pass
                response = streamed.get()
            raise_for_incomplete_stream(response)
            return response
        except Exception as error:
            if exhaustion := request_budget_exhaustion(error):
                raise exhaustion from None
            raise
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
        except Exception as error:
            if exhaustion := request_budget_exhaustion(error):
                raise exhaustion from None
            raise
        finally:
            self._budget.end_model_call()


def _provider_request_id(headers: httpx.Headers) -> str | None:
    for name in ("request-id", "x-request-id"):
        value = headers.get(name)
        if value:
            sanitized = re.sub(r"[^A-Za-z0-9._:/-]", "_", value.strip())
            return sanitized[:256] or None
    return None


def raise_for_incomplete_stream(response: ModelResponse) -> None:
    """Reject truncation or an incomplete wire stream before semantic validation."""

    if response.finish_reason == "length":
        raise ProviderOutputTruncated(
            "Provider stopped because its maximum output token boundary was reached."
        )
    if response.state != "complete":
        raise ProviderStreamIncomplete(
            f"Provider stream ended in state={response.state!r} without a complete result."
        )
    if not response_has_usable_final_output(response):
        raise ProviderEmptyOutput(
            "Provider returned HTTP success but no usable final output for the frozen task."
        )


def response_has_usable_final_output(response: ModelResponse) -> bool:
    """Return whether Pydantic AI received any actionable non-thinking final part."""

    for part in response.parts:
        part_kind = getattr(part, "part_kind", None)
        if part_kind == "thinking":
            continue
        if part_kind == "text":
            content = getattr(part, "content", None)
            if isinstance(content, str) and content.strip():
                return True
            continue
        # Structured output/tool calls and other non-thinking protocol parts are
        # usable inputs to Pydantic AI even when no plain text accompanies them.
        return True
    return False
