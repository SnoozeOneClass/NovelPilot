from __future__ import annotations

import asyncio
import json
import random
import re
import time
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any, Literal, Protocol, cast

import httpx
from anthropic import (
    APIConnectionError as AnthropicAPIConnectionError,
    APIStatusError as AnthropicAPIStatusError,
)
from openai import (
    APIConnectionError as OpenAIAPIConnectionError,
    APIStatusError as OpenAIAPIStatusError,
)
from pydantic import BaseModel
from pydantic_ai import capture_run_messages
from pydantic_ai.exceptions import (
    ModelAPIError,
    ModelHTTPError,
    ToolRetryError,
    UnexpectedModelBehavior,
    UserError,
)
from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter, ModelResponse
from sqlalchemy.ext.asyncio import AsyncEngine

from app.agents.binding import (
    ModelBindingError,
    ModelBindingResolver,
    ProfileCapabilityError,
    ProfileCredential,
    ResolvedModelBinding,
)
from app.agents.contracts import AgentTaskPlan
from app.agents.registry import TaskRegistry
from app.agents.roles import build_agent
from app.agents.transport import (
    ActivationRequestBudget,
    ActivationRequestBudgetExhausted,
    ModelRequestBudgetExhausted,
    ProviderAttempt,
    ProviderEmptyOutput,
    ProviderOutputTruncated,
    ProviderStreamIncomplete,
    raise_for_incomplete_stream,
    response_has_usable_final_output,
)
from app.db.uow import UnitOfWork
from app.store.agent_tasks import AgentTaskStore, framework_fingerprint
from app.store.content import prepare_canonical_json, prepare_redacted_bytes
from app.store.execution import AgentAttemptRecord, EvidenceItemDraft

RETRYABLE_STATUS_CODES = frozenset({408, 409, 425, 429, *range(500, 600)})
_QUOTA_MARKERS = (
    "insufficient_quota",
    "billing_hard_limit_reached",
    "billing_not_active",
    "credit balance",
    "credits exhausted",
    "payment required",
)
_PROVIDER_CONNECTION_ERRORS = (OpenAIAPIConnectionError, AnthropicAPIConnectionError)
_PROVIDER_STATUS_ERRORS = (OpenAIAPIStatusError, AnthropicAPIStatusError)

LiveEventKind = Literal[
    "task_started",
    "attempt_restarting",
    "prose_delta",
    "prose_committed",
    "prose_discarded",
    "task_succeeded",
    "task_failed",
]


@dataclass(frozen=True, slots=True)
class AgentLiveEvent:
    kind: LiveEventKind
    project_id: str
    task_id: str
    attempt_id: str
    delta: str | None = None
    provider_request_number: int | None = None
    provider_request_limit: int | None = None
    reason: str | None = None
    retry_delay_ms: int | None = None


class LivePublisher(Protocol):
    async def publish(self, event: AgentLiveEvent) -> None: ...


class NullLivePublisher:
    async def publish(self, event: AgentLiveEvent) -> None:
        del event


class BindingResolver(Protocol):
    def resolve(
        self,
        *,
        profile: Any,
        expected_profile_fingerprint: str,
        required_capabilities: Any,
        model_request_limit: int,
        credential: ProfileCredential,
    ) -> ResolvedModelBinding: ...


@dataclass(frozen=True, slots=True)
class AgentExecutionResult:
    project_id: str
    task_id: str
    attempt_id: str
    status: Literal["succeeded", "failed"]
    result: BaseModel | None
    error_code: str | None
    provider_request_count: int
    transport_retry_count: int
    model_request_count: int
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True, slots=True)
class ClassifiedExecutionError:
    code: str
    category: str
    http_status: int | None
    message: str
    diagnostic: dict[str, object]


class AgentActivationConflictError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ExecutionUsage:
    requests: int
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True, slots=True)
class RetryableFailure:
    reason: str
    http_status: int | None
    retry_after_seconds: float | None


DeadlineFactory = Callable[[float], AbstractAsyncContextManager[None]]
Sleep = Callable[[float], Awaitable[None]]


class AgentExecutor:
    """Execute one frozen task; it cannot write any Novel domain fact."""

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        registry: TaskRegistry,
        resolver: BindingResolver | None = None,
        live_publisher: LivePublisher | None = None,
        now_ms: Callable[[], int] | None = None,
        deadline_factory: DeadlineFactory | None = None,
        sleep: Sleep | None = None,
    ) -> None:
        self._engine = engine
        self._registry = registry
        self._resolver = resolver or ModelBindingResolver()
        self._live = live_publisher or NullLivePublisher()
        self._now_ms = now_ms or (lambda: time.time_ns() // 1_000_000)
        self._deadline_factory = deadline_factory or asyncio.timeout
        self._sleep = sleep or asyncio.sleep
        self._tasks = AgentTaskStore(engine)

    async def execute(
        self,
        *,
        project_id: str,
        task_id: str,
        attempt_id: str,
        owner_instance_id: str,
        lease_token: str,
        credential: ProfileCredential,
    ) -> AgentExecutionResult:
        plan = await self._tasks.load_plan(project_id=project_id, task_id=task_id)
        definition = self._registry.get(
            role=plan.role,
            task_kind=plan.task_kind,
            contract_version=plan.contract_version,
        )
        _assert_registry_matches_plan(plan, definition)
        attempt = await self._load_attempt(
            project_id=project_id,
            task_id=task_id,
            attempt_id=attempt_id,
        )
        if attempt.framework_fingerprint != framework_fingerprint():
            raise AgentActivationConflictError(
                "Frozen attempt framework fingerprint differs from the running backend."
            )

        started_at_ms = self._now_ms()
        await self._claim_attempt(
            plan=plan,
            attempt_id=attempt_id,
            owner_instance_id=owner_instance_id,
            lease_token=lease_token,
            started_at_ms=started_at_ms,
        )

        await self._publish(
            AgentLiveEvent(
                kind="task_started",
                project_id=project_id,
                task_id=task_id,
                attempt_id=attempt_id,
            )
        )

        heartbeat_task = asyncio.create_task(
            self._heartbeat_attempt(
                project_id=project_id,
                task_id=task_id,
                attempt_id=attempt_id,
                owner_instance_id=owner_instance_id,
                lease_token=lease_token,
            ),
            name=f"agent-attempt-heartbeat:{attempt_id}",
        )
        binding: ResolvedModelBinding | None = None
        fallback_budget = ActivationRequestBudget(
            model_request_limit=plan.model_request_limit,
            protocol=plan.profile_snapshot.api_family,
        )
        captured_messages: list[ModelMessage] = []
        secret = credential.api_key.get_secret_value()
        try:
            binding = self._resolver.resolve(
                profile=plan.profile_snapshot,
                expected_profile_fingerprint=plan.profile_fingerprint,
                required_capabilities=plan.required_capabilities,
                model_request_limit=plan.model_request_limit,
                credential=credential,
            )
            async with binding:
                agent = build_agent(model=binding.model, definition=definition)
                async with self._deadline_factory(plan.activation_timeout_ms / 1000):
                    output, usage = await self._run_agent(
                        plan=plan,
                        attempt_id=attempt_id,
                        definition=definition,
                        agent=agent,
                        budget=binding.budget,
                        captured_messages=captured_messages,
                    )
            binding.budget.assert_terminal_invariants()
            result = await self._persist_success(
                plan=plan,
                attempt_id=attempt_id,
                output=output,
                messages=_sanitize_messages(captured_messages, secret=secret),
                usage=usage,
                budget=binding.budget,
            )
            if plan.output_mode == "text_streaming":
                await self._publish(
                    AgentLiveEvent(
                        kind="prose_committed",
                        project_id=project_id,
                        task_id=task_id,
                        attempt_id=attempt_id,
                    )
                )
            await self._publish(
                AgentLiveEvent(
                    kind="task_succeeded",
                    project_id=project_id,
                    task_id=task_id,
                    attempt_id=attempt_id,
                )
            )
            return result
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):
                raise
            budget = binding.budget if binding is not None else fallback_budget
            budget.assert_terminal_invariants()
            classified = classify_execution_error(exc, secret=secret)
            if budget.latest_attempt is not None and (
                budget.latest_attempt.retry_decision is None
                or isinstance(exc, TimeoutError)
            ):
                budget.record_retry_decision(
                    retry=False,
                    reason=classified.code,
                )
            result = await self._persist_failure(
                plan=plan,
                attempt_id=attempt_id,
                error=classified,
                budget=budget,
                messages=(
                    _sanitize_messages(captured_messages, secret=secret)
                    if captured_messages
                    else None
                ),
                usage=_usage_from_messages(captured_messages),
            )
            if plan.output_mode == "text_streaming":
                await self._publish(
                    AgentLiveEvent(
                        kind="prose_discarded",
                        project_id=project_id,
                        task_id=task_id,
                        attempt_id=attempt_id,
                    )
                )
            await self._publish(
                AgentLiveEvent(
                    kind="task_failed",
                    project_id=project_id,
                    task_id=task_id,
                    attempt_id=attempt_id,
                )
            )
            return result
        finally:
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass

    async def fail_preflight(
        self,
        *,
        project_id: str,
        task_id: str,
        attempt_id: str,
        owner_instance_id: str,
        lease_token: str,
        credential: ProfileCredential,
        error: ModelBindingError,
    ) -> AgentExecutionResult:
        """Terminalize a local Profile/Adapter failure without a Provider request."""

        plan = await self._tasks.load_plan(project_id=project_id, task_id=task_id)
        definition = self._registry.get(
            role=plan.role,
            task_kind=plan.task_kind,
            contract_version=plan.contract_version,
        )
        _assert_registry_matches_plan(plan, definition)
        attempt = await self._load_attempt(
            project_id=project_id,
            task_id=task_id,
            attempt_id=attempt_id,
        )
        if attempt.framework_fingerprint != framework_fingerprint():
            raise AgentActivationConflictError(
                "Frozen attempt framework fingerprint differs from the running backend."
            )
        started_at_ms = self._now_ms()
        await self._claim_attempt(
            plan=plan,
            attempt_id=attempt_id,
            owner_instance_id=owner_instance_id,
            lease_token=lease_token,
            started_at_ms=started_at_ms,
        )
        await self._publish(
            AgentLiveEvent(
                kind="task_started",
                project_id=project_id,
                task_id=task_id,
                attempt_id=attempt_id,
            )
        )
        budget = ActivationRequestBudget(
            model_request_limit=plan.model_request_limit,
            protocol=plan.profile_snapshot.api_family,
        )
        classified = classify_execution_error(
            error,
            secret=credential.api_key.get_secret_value(),
        )
        result = await self._persist_failure(
            plan=plan,
            attempt_id=attempt_id,
            error=classified,
            budget=budget,
            messages=None,
            usage=ExecutionUsage(requests=0, input_tokens=0, output_tokens=0),
        )
        await self._publish(
            AgentLiveEvent(
                kind="task_failed",
                project_id=project_id,
                task_id=task_id,
                attempt_id=attempt_id,
            )
        )
        return result

    async def _claim_attempt(
        self,
        *,
        plan: AgentTaskPlan,
        attempt_id: str,
        owner_instance_id: str,
        lease_token: str,
        started_at_ms: int,
    ) -> None:
        async with UnitOfWork(self._engine, begin_mode="IMMEDIATE") as store:
            claimed = await store.execution.mark_attempt_running(
                project_id=plan.project_id,
                task_id=plan.task_id,
                attempt_id=attempt_id,
                owner_instance_id=owner_instance_id,
                lease_token=lease_token,
                lease_expires_at_ms=started_at_ms + 60_000,
                activation_deadline_at_ms=started_at_ms + plan.activation_timeout_ms,
                started_at_ms=started_at_ms,
            )
            if not claimed:
                raise AgentActivationConflictError(
                    f"Attempt {attempt_id!r} is not an unclaimed queued attempt."
                )

    async def _publish(self, event: AgentLiveEvent) -> None:
        """Live fan-out is deliberately lossy and cannot change task state."""
        try:
            await self._live.publish(event)
        except Exception:
            return

    async def _heartbeat_attempt(
        self,
        *,
        project_id: str,
        task_id: str,
        attempt_id: str,
        owner_instance_id: str,
        lease_token: str,
    ) -> None:
        while True:
            await asyncio.sleep(20)
            timestamp = self._now_ms()
            try:
                async with UnitOfWork(self._engine, begin_mode="IMMEDIATE") as store:
                    alive = await store.execution.heartbeat_attempt(
                        project_id=project_id,
                        task_id=task_id,
                        attempt_id=attempt_id,
                        owner_instance_id=owner_instance_id,
                        lease_token=lease_token,
                        lease_expires_at_ms=timestamp + 60_000,
                        now_ms=timestamp,
                    )
                if not alive:
                    return
            except Exception:
                # A later heartbeat may recover; the activation deadline remains authoritative.
                continue

    async def _load_attempt(
        self, *, project_id: str, task_id: str, attempt_id: str
    ) -> AgentAttemptRecord:
        async with UnitOfWork(self._engine) as store:
            attempt = await store.execution.get_attempt(
                project_id=project_id,
                task_id=task_id,
                attempt_id=attempt_id,
            )
        if attempt is None:
            raise LookupError(f"Attempt {attempt_id!r} does not exist for task {task_id!r}.")
        return attempt

    async def _run_agent(
        self,
        *,
        plan: AgentTaskPlan,
        attempt_id: str,
        definition: Any,
        agent: Any,
        budget: ActivationRequestBudget,
        captured_messages: list[ModelMessage],
    ) -> tuple[BaseModel, ExecutionUsage]:
        while True:
            budget.begin_agent_run()
            provider_count_before = budget.provider_request_count
            run_messages: list[ModelMessage] = []
            try:
                with capture_run_messages() as run_messages:
                    output = await self._run_agent_once(
                        plan=plan,
                        attempt_id=attempt_id,
                        definition=definition,
                        agent=agent,
                    )
            except BaseException as exc:
                captured_messages.extend(run_messages)
                normalized_exc = _normalize_provider_empty_output(
                    exc,
                    messages=run_messages,
                )
                retryable = retryable_provider_failure(
                    normalized_exc,
                    attempt=budget.latest_attempt,
                )
                consumed_request = budget.provider_request_count > provider_count_before
                if retryable is None or not consumed_request or not budget.can_replay:
                    if retryable is not None:
                        budget.record_retry_decision(
                            retry=False,
                            reason=retryable.reason,
                        )
                    if normalized_exc is not exc:
                        raise normalized_exc from exc
                    raise
                delay = _retry_delay_seconds(
                    budget=budget,
                    retry_after_seconds=retryable.retry_after_seconds,
                )
                budget.record_retry_decision(
                    retry=True,
                    reason=retryable.reason,
                    delay_seconds=delay,
                )
                if plan.output_mode == "text_streaming":
                    await self._publish(
                        AgentLiveEvent(
                            kind="prose_discarded",
                            project_id=plan.project_id,
                            task_id=plan.task_id,
                            attempt_id=attempt_id,
                        )
                    )
                await self._publish(
                    AgentLiveEvent(
                        kind="attempt_restarting",
                        project_id=plan.project_id,
                        task_id=plan.task_id,
                        attempt_id=attempt_id,
                        provider_request_number=budget.provider_request_count,
                        provider_request_limit=budget.provider_request_limit,
                        reason=retryable.reason,
                        retry_delay_ms=round(delay * 1_000),
                    )
                )
                await self._sleep(delay)
                continue
            captured_messages.extend(run_messages)
            return output, _usage_from_messages(captured_messages)

    async def _run_agent_once(
        self,
        *,
        plan: AgentTaskPlan,
        attempt_id: str,
        definition: Any,
        agent: Any,
    ) -> BaseModel:
        if plan.output_mode == "native_json_schema":
            result = await agent.run(plan.prompt)
            raise_for_incomplete_stream(result.response)
            output = result.output
            if not isinstance(output, definition.output_model):
                raise UnexpectedModelBehavior("Framework returned the wrong typed output model.")
            return cast(BaseModel, output)

        chunks: list[str] = []
        async with agent.run_stream(plan.prompt) as streamed:
            try:
                async for delta in streamed.stream_text(delta=True, debounce_by=None):
                    chunks.append(delta)
                    await self._publish(
                        AgentLiveEvent(
                            kind="prose_delta",
                            project_id=plan.project_id,
                            task_id=plan.task_id,
                            attempt_id=attempt_id,
                            delta=delta,
                        )
                    )
                completed = await streamed.get_output()
            except BaseException as exc:
                _raise_stream_boundary_error(streamed.response, cause=exc)
                raise
            _raise_stream_boundary_error(streamed.response)
            if completed != "".join(chunks):
                raise UnexpectedModelBehavior("Stream deltas do not match the completed text output.")
            finalizer = definition.text_finalizer
            if finalizer is None:  # pragma: no cover - registry construction rejects this.
                raise RuntimeError("Text task has no deterministic finalizer.")
            output = finalizer(completed)
        return cast(BaseModel, output)

    async def _persist_success(
        self,
        *,
        plan: AgentTaskPlan,
        attempt_id: str,
        output: BaseModel,
        messages: object,
        usage: ExecutionUsage,
        budget: ActivationRequestBudget,
    ) -> AgentExecutionResult:
        timestamp = self._now_ms()
        prepared_result = prepare_canonical_json(output)
        prepared_messages = prepare_canonical_json(messages)
        usage_payload = {
            "requests": int(usage.requests),
            "input_tokens": int(usage.input_tokens),
            "output_tokens": int(usage.output_tokens),
            "provider_request_count": budget.provider_request_count,
            "transport_retry_count": budget.transport_retry_count,
            "model_request_count": budget.model_request_count,
        }
        prepared_usage = prepare_canonical_json(usage_payload)
        async with UnitOfWork(self._engine, begin_mode="IMMEDIATE") as store:
            result_ref = await store.content.put(
                project_id=plan.project_id,
                prepared=prepared_result,
                semantic_kind="agent.typed_result",
                media_type="application/json",
                schema_id=plan.output_schema_id,
                schema_version=plan.output_schema_version,
                created_at_ms=timestamp,
            )
            message_ref = await store.content.put(
                project_id=plan.project_id,
                prepared=prepared_messages,
                semantic_kind="agent.completion_messages",
                media_type="application/json",
                schema_id="pydantic-ai-messages",
                schema_version=1,
                created_at_ms=timestamp,
            )
            usage_ref = await store.content.put(
                project_id=plan.project_id,
                prepared=prepared_usage,
                semantic_kind="agent.usage",
                media_type="application/json",
                schema_id="agent-usage",
                schema_version=1,
                created_at_ms=timestamp,
            )
            completed = await store.execution.complete_attempt_success(
                project_id=plan.project_id,
                task_id=plan.task_id,
                attempt_id=attempt_id,
                provider_request_count=budget.provider_request_count,
                transport_retry_count=budget.transport_retry_count,
                model_request_count=budget.model_request_count,
                input_tokens=int(usage.input_tokens),
                output_tokens=int(usage.output_tokens),
                usage_ref_id=usage_ref.id,
                result_ref_id=result_ref.id,
                finished_at_ms=timestamp,
            )
            if not completed:
                raise AgentActivationConflictError("Attempt terminal success CAS failed.")
            evidence = [
                EvidenceItemDraft(
                    item_kind="completion_message",
                    content_ref_id=message_ref.id,
                    metadata_json=_canonical_metadata(
                        {"normalized": True, "thinking_removed": True}
                    ),
                ),
                EvidenceItemDraft(
                    item_kind="validation",
                    metadata_json=_canonical_metadata(
                        {
                            "output_schema_id": plan.output_schema_id,
                            "output_schema_version": plan.output_schema_version,
                            "output_schema_fingerprint": plan.output_schema_fingerprint,
                            "status": "passed",
                        }
                    ),
                ),
            ]
            evidence.extend(_retry_evidence(budget))
            await store.execution.insert_evidence_items(
                project_id=plan.project_id,
                task_id=plan.task_id,
                attempt_id=attempt_id,
                items=evidence,
                created_at_ms=timestamp,
            )
        return AgentExecutionResult(
            project_id=plan.project_id,
            task_id=plan.task_id,
            attempt_id=attempt_id,
            status="succeeded",
            result=output,
            error_code=None,
            provider_request_count=budget.provider_request_count,
            transport_retry_count=budget.transport_retry_count,
            model_request_count=budget.model_request_count,
            input_tokens=int(usage.input_tokens),
            output_tokens=int(usage.output_tokens),
        )

    async def _persist_failure(
        self,
        *,
        plan: AgentTaskPlan,
        attempt_id: str,
        error: ClassifiedExecutionError,
        budget: ActivationRequestBudget,
        messages: object | None,
        usage: ExecutionUsage,
    ) -> AgentExecutionResult:
        timestamp = self._now_ms()
        requests = usage.requests
        input_tokens = usage.input_tokens
        output_tokens = usage.output_tokens
        prepared_error = prepare_canonical_json(
            {
                "code": error.code,
                "category": error.category,
                "http_status": error.http_status,
                "message": error.message,
            }
        )
        diagnostic_bytes = json.dumps(
            error.diagnostic,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        prepared_diagnostic = prepare_redacted_bytes(diagnostic_bytes)
        prepared_messages = prepare_canonical_json(messages) if messages is not None else None
        prepared_usage = (
            prepare_canonical_json(
                {
                    "requests": requests,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "provider_request_count": budget.provider_request_count,
                    "transport_retry_count": budget.transport_retry_count,
                    "model_request_count": budget.model_request_count,
                }
            )
            if requests
            else None
        )
        async with UnitOfWork(self._engine, begin_mode="IMMEDIATE") as store:
            error_ref = await store.content.put(
                project_id=plan.project_id,
                prepared=prepared_error,
                semantic_kind="agent.error",
                media_type="application/json",
                schema_id="agent-normalized-error",
                schema_version=1,
                created_at_ms=timestamp,
            )
            diagnostic_ref = await store.content.put(
                project_id=plan.project_id,
                prepared=prepared_diagnostic,
                semantic_kind="agent.diagnostic_attachment",
                media_type="application/json",
                schema_id="provider-diagnostic-redacted",
                schema_version=1,
                created_at_ms=timestamp,
            )
            message_ref = (
                await store.content.put(
                    project_id=plan.project_id,
                    prepared=prepared_messages,
                    semantic_kind="agent.completion_messages",
                    media_type="application/json",
                    schema_id="pydantic-ai-messages",
                    schema_version=1,
                    created_at_ms=timestamp,
                )
                if prepared_messages is not None
                else None
            )
            usage_ref = (
                await store.content.put(
                    project_id=plan.project_id,
                    prepared=prepared_usage,
                    semantic_kind="agent.usage",
                    media_type="application/json",
                    schema_id="agent-usage",
                    schema_version=1,
                    created_at_ms=timestamp,
                )
                if prepared_usage is not None
                else None
            )
            completed = await store.execution.complete_attempt_failure(
                project_id=plan.project_id,
                task_id=plan.task_id,
                attempt_id=attempt_id,
                provider_request_count=budget.provider_request_count,
                transport_retry_count=budget.transport_retry_count,
                model_request_count=budget.model_request_count,
                error_code=error.code,
                error_category=error.category,
                http_status=error.http_status,
                error_ref_id=error_ref.id,
                diagnostic_ref_id=diagnostic_ref.id,
                input_tokens=input_tokens if prepared_usage is not None else None,
                output_tokens=output_tokens if prepared_usage is not None else None,
                usage_ref_id=usage_ref.id if usage_ref is not None else None,
                finished_at_ms=timestamp,
            )
            if not completed:
                raise AgentActivationConflictError("Attempt terminal failure CAS failed.")
            evidence = []
            if message_ref is not None:
                evidence.append(
                    EvidenceItemDraft(
                        item_kind="completion_message",
                        content_ref_id=message_ref.id,
                        metadata_json=_canonical_metadata(
                            {
                                "normalized": True,
                                "attempt_status": "failed",
                                "thinking_removed": True,
                            }
                        ),
                    )
                )
            if error.category == "output_validation":
                evidence.append(
                    EvidenceItemDraft(
                        item_kind="validation",
                        metadata_json=_canonical_metadata(
                            {
                                "output_schema_id": plan.output_schema_id,
                                "output_schema_version": plan.output_schema_version,
                                "output_schema_fingerprint": plan.output_schema_fingerprint,
                                "status": "failed",
                            }
                        ),
                    )
                )
            evidence.append(
                EvidenceItemDraft(
                    item_kind="diagnostic_attachment",
                    content_ref_id=diagnostic_ref.id,
                    metadata_json=_canonical_metadata(
                        {"schema_id": "provider-diagnostic-redacted", "redacted": True}
                    ),
                )
            )
            evidence.extend(_retry_evidence(budget))
            await store.execution.insert_evidence_items(
                project_id=plan.project_id,
                task_id=plan.task_id,
                attempt_id=attempt_id,
                items=evidence,
                created_at_ms=timestamp,
            )
        return AgentExecutionResult(
            project_id=plan.project_id,
            task_id=plan.task_id,
            attempt_id=attempt_id,
            status="failed",
            result=None,
            error_code=error.code,
            provider_request_count=budget.provider_request_count,
            transport_retry_count=budget.transport_retry_count,
            model_request_count=budget.model_request_count,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )


def _assert_registry_matches_plan(plan: AgentTaskPlan, definition: Any) -> None:
    expected = (
        definition.scope_layer,
        definition.output_mode,
        definition.output_schema_id,
        definition.output_schema_version,
        definition.context_policy_id,
        definition.context_policy_version,
        definition.required_capabilities,
        definition.model_request_limit,
        definition.rubric_id,
        definition.rubric_version,
        definition.repairable_components,
    )
    actual = (
        plan.scope_layer,
        plan.output_mode,
        plan.output_schema_id,
        plan.output_schema_version,
        plan.context_policy_id,
        plan.context_policy_version,
        plan.required_capabilities,
        plan.model_request_limit,
        plan.rubric_id,
        plan.rubric_version,
        plan.repairable_components,
    )
    if actual != expected or plan.output_schema_fingerprint != prepare_canonical_json(
        definition.output_schema
    ).sha256:
        raise AgentActivationConflictError("Frozen Task Plan no longer matches its registry contract.")


def _canonical_metadata(value: object) -> str:
    return prepare_canonical_json(value).canonical_bytes.decode("utf-8")


def _sanitize_messages(messages: list[ModelMessage], *, secret: str) -> object:
    """Keep complete visible run messages while removing secrets and hidden reasoning."""

    payload = ModelMessagesTypeAdapter.dump_python(messages, mode="json")
    return _sanitize_evidence_value(payload, secret=secret)


def _sanitize_evidence_value(value: object, *, secret: str) -> object:
    if isinstance(value, list):
        return [
            _sanitize_evidence_value(item, secret=secret)
            for item in value
            if not (
                isinstance(item, dict)
                and item.get("part_kind") == "thinking"
            )
        ]
    if isinstance(value, dict):
        result: dict[str, object] = {}
        for key, item in value.items():
            normalized_key = re.sub(r"[^a-z0-9]", "", str(key).casefold())
            if normalized_key in {
                "authorization",
                "cookie",
                "setcookie",
                "apikey",
                "xapikey",
                "accesstoken",
                "refreshtoken",
                "clientsecret",
                "password",
            }:
                result[str(key)] = "[REDACTED]"
            else:
                result[str(key)] = _sanitize_evidence_value(item, secret=secret)
        return result
    if isinstance(value, str):
        return _redact(value, secret=secret)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _redact(str(value), secret=secret)


def _usage_from_messages(messages: list[ModelMessage]) -> ExecutionUsage:
    requests = 0
    input_tokens = 0
    output_tokens = 0
    for message in messages:
        if isinstance(message, ModelResponse):
            requests += 1
            input_tokens += int(message.usage.input_tokens)
            output_tokens += int(message.usage.output_tokens)
    return ExecutionUsage(
        requests=requests,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


def _retry_evidence(budget: ActivationRequestBudget) -> list[EvidenceItemDraft]:
    items: list[EvidenceItemDraft] = []
    if budget.attempts:
        items.append(
            EvidenceItemDraft(
                item_kind="transport_retry",
                metadata_json=_canonical_metadata(
                    {
                        "schema_id": "provider-request-attempts-v1",
                        "provider_request_count": budget.provider_request_count,
                        "count": budget.transport_retry_count,
                        "attempts": [
                            {
                                "sequence": attempt.sequence,
                                "protocol": attempt.protocol,
                                "method": attempt.method,
                                "started_at_ms": attempt.started_at_ms,
                                "headers_at_ms": attempt.headers_at_ms,
                                "first_event_at_ms": attempt.first_event_at_ms,
                                "last_event_at_ms": attempt.last_event_at_ms,
                                "finished_at_ms": attempt.finished_at_ms,
                                "time_to_first_event_ms": (
                                    None
                                    if attempt.first_event_at_ms is None
                                    else attempt.first_event_at_ms - attempt.started_at_ms
                                ),
                                "status_code": attempt.status_code,
                                "provider_request_id": attempt.provider_request_id,
                                "error_type": attempt.error_type,
                                "retry_decision": attempt.retry_decision,
                                "retry_reason": attempt.retry_reason,
                                "retry_delay_ms": attempt.retry_delay_ms,
                            }
                            for attempt in budget.attempts
                        ],
                    }
                ),
            )
        )
    if budget.model_request_count > 1:
        items.append(
            EvidenceItemDraft(
                item_kind="model_retry",
                metadata_json=_canonical_metadata(
                    {"count": budget.model_request_count - 1, "reason": "typed_output_repair"}
                ),
            )
        )
    return items


def retryable_provider_failure(
    exc: BaseException,
    *,
    attempt: ProviderAttempt | None = None,
) -> RetryableFailure | None:
    """Return a replay decision only for explicit transient Provider failures."""

    if isinstance(exc, ProviderEmptyOutput):
        return RetryableFailure(
            reason="provider_empty_output",
            http_status=_http_status(exc),
            retry_after_seconds=_retry_after_seconds(exc),
        )
    if isinstance(exc, ProviderStreamIncomplete):
        return RetryableFailure(
            reason="provider_stream_incomplete",
            http_status=_http_status(exc),
            retry_after_seconds=_retry_after_seconds(exc),
        )
    status = _http_status(exc)
    if status is not None:
        if status not in RETRYABLE_STATUS_CODES:
            return None
        if status == 429 and _contains_quota_failure(_provider_response_body(exc)):
            return None
        return RetryableFailure(
            reason=f"provider_http_{status}",
            http_status=status,
            retry_after_seconds=_retry_after_seconds(exc),
        )
    if _has_connection_failure(exc):
        return RetryableFailure(
            reason=_transport_failure_reason(exc, attempt=attempt),
            http_status=None,
            retry_after_seconds=None,
        )
    return None


def classify_execution_error(exc: BaseException, *, secret: str = "") -> ClassifiedExecutionError:
    status = _http_status(exc)
    category = "execution"
    code = "agent_execution_failed"
    response_body = _provider_response_body(exc)
    if isinstance(exc, TimeoutError):
        category, code = "timeout", "activation_deadline_exceeded"
    elif isinstance(exc, ProviderOutputTruncated):
        category, code = "output_truncation", "provider_output_truncated"
    elif isinstance(exc, ProviderEmptyOutput):
        category, code = "transport", "provider_empty_output_retries_exhausted"
    elif isinstance(exc, ProviderStreamIncomplete):
        category, code = "transport", "provider_stream_retries_exhausted"
    elif isinstance(exc, ActivationRequestBudgetExhausted):
        category, code = "budget", "provider_request_budget_exhausted"
    elif isinstance(exc, ModelRequestBudgetExhausted):
        category, code = "budget", "model_request_budget_exhausted"
    elif isinstance(exc, ProfileCapabilityError):
        category, code = "capability", "profile_capability_missing"
    elif isinstance(exc, ModelBindingError):
        category, code = "configuration", "model_binding_failed"
    elif status == 401:
        category, code = "authentication", "provider_authentication_failed"
    elif status == 403:
        category, code = "permission", "provider_permission_denied"
    elif status in {402, 429} and _contains_quota_failure(response_body):
        category, code = "quota", "provider_quota_exhausted"
    elif status in RETRYABLE_STATUS_CODES:
        category, code = "transport", "provider_transient_retries_exhausted"
    elif status is not None and 400 <= status < 500:
        category, code = "invalid_request", "provider_invalid_request"
    elif status is not None:
        category, code = "provider", "provider_http_error"
    elif _has_connection_failure(exc):
        category, code = "transport", _transport_exhaustion_code(exc)
    elif isinstance(exc, ModelAPIError):
        category, code = "provider", "provider_api_error"
    elif isinstance(exc, UserError):
        category, code = "configuration", "agent_configuration_invalid"
    elif isinstance(exc, UnexpectedModelBehavior):
        category, code = "output_validation", "typed_output_invalid"

    message = _redact(str(exc), secret=secret)
    diagnostic: dict[str, object] = {
        "schema_id": "provider-diagnostic-redacted",
        "schema_version": 1,
        "exception_type": type(exc).__name__,
        "message": message,
        "http_status": status,
        "redacted": True,
    }
    if response_body is not None:
        diagnostic["provider_response"] = _redact(response_body, secret=secret)
    validation_errors = _validation_error_payload(exc, secret=secret)
    if validation_errors is not None:
        diagnostic["validation_errors"] = validation_errors
    return ClassifiedExecutionError(
        code=code,
        category=category,
        http_status=status,
        message=message,
        diagnostic=diagnostic,
    )


def _raise_stream_boundary_error(
    response: ModelResponse,
    *,
    cause: BaseException | None = None,
) -> None:
    if isinstance(cause, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):
        return
    if cause is not None and _has_connection_failure(cause):
        # Preserve the concrete connect/read/write error so retry evidence can
        # distinguish first-event timeout from a dropped active stream.
        return
    try:
        raise_for_incomplete_stream(response)
    except (ProviderOutputTruncated, ProviderStreamIncomplete) as exc:
        raise exc from cause
    if (
        isinstance(cause, UnexpectedModelBehavior)
        and "streamed response ended without content or tool calls" in str(cause).casefold()
    ):
        raise ProviderStreamIncomplete(
            "Provider stream ended without content or a protocol completion payload."
        ) from cause


def _normalize_provider_empty_output(
    exc: BaseException,
    *,
    messages: list[ModelMessage],
) -> BaseException:
    """Recognize complete thinking-only output even when run_stream entry failed."""

    if isinstance(
        exc,
        (
            KeyboardInterrupt,
            SystemExit,
            asyncio.CancelledError,
            ProviderOutputTruncated,
            ProviderStreamIncomplete,
        ),
    ):
        return exc
    response = next(
        (
            message
            for message in reversed(messages)
            if isinstance(message, ModelResponse)
        ),
        None,
    )
    if (
        response is None
        or response.state != "complete"
        or response.finish_reason == "length"
        or response_has_usable_final_output(response)
    ):
        return exc
    return ProviderEmptyOutput(
        "Provider returned a complete response containing only thinking or blank output."
    )


def _retry_delay_seconds(
    *,
    budget: ActivationRequestBudget,
    retry_after_seconds: float | None,
) -> float:
    if retry_after_seconds is not None:
        return min(300.0, max(0.0, retry_after_seconds))
    retry_number = budget.transport_retry_count + 1
    base = min(60.0, float(2 ** max(0, retry_number - 1)))
    return min(60.0, random.uniform(base * 0.5, base * 1.5))


def _http_status(exc: BaseException) -> int | None:
    for current in _exception_chain(exc):
        if isinstance(current, ModelHTTPError):
            return current.status_code
        if isinstance(current, _PROVIDER_STATUS_ERRORS):
            return int(current.status_code)
        if isinstance(current, httpx.HTTPStatusError):
            return current.response.status_code
    return None


def _provider_response_body(exc: BaseException) -> str | None:
    for current in _exception_chain(exc):
        if isinstance(current, ModelHTTPError):
            return json.dumps(current.body, ensure_ascii=False, sort_keys=True, default=str)
        if isinstance(current, _PROVIDER_STATUS_ERRORS):
            return json.dumps(current.body, ensure_ascii=False, sort_keys=True, default=str)
        if isinstance(current, httpx.HTTPStatusError):
            return current.response.text
    return None


def _has_connection_failure(exc: BaseException) -> bool:
    return any(
        isinstance(current, (httpx.TransportError, *_PROVIDER_CONNECTION_ERRORS))
        for current in _exception_chain(exc)
    )


def _transport_failure_reason(
    exc: BaseException,
    *,
    attempt: ProviderAttempt | None,
) -> str:
    chain = _exception_chain(exc)
    if any(isinstance(current, httpx.ConnectTimeout | httpx.ConnectError) for current in chain):
        return "provider_connect_failed"
    if any(isinstance(current, httpx.PoolTimeout) for current in chain):
        return "provider_pool_timeout"
    if any(isinstance(current, httpx.WriteTimeout | httpx.WriteError) for current in chain):
        return "provider_write_failed"
    if any(isinstance(current, httpx.ReadTimeout) for current in chain):
        return (
            "provider_stream_idle_timeout"
            if attempt is not None and attempt.first_event_at_ms is not None
            else "provider_first_event_timeout"
        )
    if any(
        isinstance(current, httpx.ReadError | httpx.RemoteProtocolError)
        for current in chain
    ):
        return (
            "provider_stream_interrupted"
            if attempt is not None and attempt.first_event_at_ms is not None
            else "provider_response_interrupted"
        )
    return "provider_connection_interrupted"


def _transport_exhaustion_code(exc: BaseException) -> str:
    reason = _transport_failure_reason(exc, attempt=None)
    return {
        "provider_connect_failed": "provider_connect_retries_exhausted",
        "provider_pool_timeout": "provider_pool_retries_exhausted",
        "provider_write_failed": "provider_write_retries_exhausted",
        "provider_first_event_timeout": "provider_read_timeout_retries_exhausted",
        "provider_response_interrupted": "provider_connection_retries_exhausted",
        "provider_connection_interrupted": "provider_connection_retries_exhausted",
    }[reason]


def _contains_quota_failure(body: str | None) -> bool:
    if body is None:
        return False
    normalized = body.casefold()
    return any(marker in normalized for marker in _QUOTA_MARKERS)


def _retry_after_seconds(exc: BaseException) -> float | None:
    for current in _exception_chain(exc):
        response = getattr(current, "response", None)
        if not isinstance(response, httpx.Response):
            continue
        value = response.headers.get("retry-after")
        if value is None:
            continue
        try:
            return max(0.0, float(value))
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(value)
            except (TypeError, ValueError, OverflowError):
                return None
            return max(0.0, retry_at.timestamp() - time.time())
    return None


def _exception_chain(exc: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__
    return chain


def _validation_error_payload(exc: BaseException, *, secret: str) -> object | None:
    cause = exc.__cause__
    if not isinstance(cause, ToolRetryError):
        return None
    return _sanitize_evidence_value(cause.tool_retry.content, secret=secret)


def _redact(value: str, *, secret: str) -> str:
    result = value.replace(secret, "[REDACTED]") if secret else value
    patterns = (
        r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;]+",
        r"(?i)(api[-_ ]?key\s*[:=]\s*)[^\s,;]+",
        r"(?i)(cookie\s*[:=]\s*)[^\r\n]+",
        r"(?i)([?&](?:access_token|api_key|key|signature|sig|token)=)[^&\s\"']+",
    )
    for pattern in patterns:
        result = re.sub(pattern, r"\1[REDACTED]", result)
    return result
