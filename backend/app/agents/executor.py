from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import httpx
from openai import APIStatusError, AuthenticationError, BadRequestError, RateLimitError
from pydantic import BaseModel
from pydantic_ai.exceptions import UnexpectedModelBehavior, UserError
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
)
from app.db.uow import UnitOfWork
from app.store.agent_tasks import AgentTaskStore, framework_fingerprint
from app.store.content import prepare_canonical_json, prepare_redacted_bytes
from app.store.execution import AgentAttemptRecord, EvidenceItemDraft

LiveEventKind = Literal[
    "task_started",
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


DeadlineFactory = Callable[[float], AbstractAsyncContextManager[None]]


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
    ) -> None:
        self._engine = engine
        self._registry = registry
        self._resolver = resolver or ModelBindingResolver()
        self._live = live_publisher or NullLivePublisher()
        self._now_ms = now_ms or (lambda: time.time_ns() // 1_000_000)
        self._deadline_factory = deadline_factory or asyncio.timeout
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
        async with UnitOfWork(self._engine, begin_mode="IMMEDIATE") as store:
            claimed = await store.execution.mark_attempt_running(
                project_id=project_id,
                task_id=task_id,
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
        fallback_budget = ActivationRequestBudget(model_request_limit=plan.model_request_limit)
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
                    output, message_bytes, usage = await self._run_agent(
                        plan=plan,
                        attempt_id=attempt_id,
                        definition=definition,
                        agent=agent,
                    )
            binding.budget.assert_terminal_invariants()
            result = await self._persist_success(
                plan=plan,
                attempt_id=attempt_id,
                output=output,
                message_bytes=message_bytes,
                usage=usage,
                budget=binding.budget,
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
            classified = classify_execution_error(exc, secret=credential.api_key.get_secret_value())
            result = await self._persist_failure(
                plan=plan,
                attempt_id=attempt_id,
                error=classified,
                budget=budget,
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
    ) -> tuple[BaseModel, bytes, Any]:
        if plan.output_mode == "native_json_schema":
            result = await agent.run(plan.prompt)
            if not isinstance(result.output, definition.output_model):
                raise UnexpectedModelBehavior("Framework returned the wrong typed output model.")
            return result.output, result.all_messages_json(), result.usage

        chunks: list[str] = []
        async with agent.run_stream(plan.prompt) as streamed:
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
            if completed != "".join(chunks):
                raise UnexpectedModelBehavior("Stream deltas do not match the completed text output.")
            finalizer = definition.text_finalizer
            if finalizer is None:  # pragma: no cover - registry construction rejects this.
                raise RuntimeError("Text task has no deterministic finalizer.")
            output = finalizer(completed)
            messages = streamed.all_messages_json()
            usage = streamed.usage
        await self._publish(
            AgentLiveEvent(
                kind="prose_committed",
                project_id=plan.project_id,
                task_id=plan.task_id,
                attempt_id=attempt_id,
            )
        )
        return output, messages, usage

    async def _persist_success(
        self,
        *,
        plan: AgentTaskPlan,
        attempt_id: str,
        output: BaseModel,
        message_bytes: bytes,
        usage: Any,
        budget: ActivationRequestBudget,
    ) -> AgentExecutionResult:
        timestamp = self._now_ms()
        prepared_result = prepare_canonical_json(output)
        prepared_messages = prepare_canonical_json(json.loads(message_bytes))
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
                    metadata_json=_canonical_metadata({"normalized": True}),
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
    ) -> AgentExecutionResult:
        timestamp = self._now_ms()
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
                finished_at_ms=timestamp,
            )
            if not completed:
                raise AgentActivationConflictError("Attempt terminal failure CAS failed.")
            evidence = [
                EvidenceItemDraft(
                    item_kind="diagnostic_attachment",
                    content_ref_id=diagnostic_ref.id,
                    metadata_json=_canonical_metadata(
                        {"schema_id": "provider-diagnostic-redacted", "redacted": True}
                    ),
                )
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
            status="failed",
            result=None,
            error_code=error.code,
            provider_request_count=budget.provider_request_count,
            transport_retry_count=budget.transport_retry_count,
            model_request_count=budget.model_request_count,
            input_tokens=0,
            output_tokens=0,
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
    )
    if actual != expected or plan.output_schema_fingerprint != prepare_canonical_json(
        definition.output_schema
    ).sha256:
        raise AgentActivationConflictError("Frozen Task Plan no longer matches its registry contract.")


def _canonical_metadata(value: object) -> str:
    return prepare_canonical_json(value).canonical_bytes.decode("utf-8")


def _retry_evidence(budget: ActivationRequestBudget) -> list[EvidenceItemDraft]:
    items: list[EvidenceItemDraft] = []
    if budget.transport_retry_count:
        items.append(
            EvidenceItemDraft(
                item_kind="transport_retry",
                metadata_json=_canonical_metadata(
                    {
                        "count": budget.transport_retry_count,
                        "attempts": [
                            {
                                "sequence": attempt.sequence,
                                "method": attempt.method,
                                "status_code": attempt.status_code,
                                "error_type": attempt.error_type,
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


def classify_execution_error(exc: BaseException, *, secret: str = "") -> ClassifiedExecutionError:
    status: int | None = None
    category = "execution"
    code = "agent_execution_failed"
    if isinstance(exc, TimeoutError):
        category, code = "timeout", "activation_deadline_exceeded"
    elif isinstance(exc, ActivationRequestBudgetExhausted):
        category, code = "budget", "provider_request_budget_exhausted"
    elif isinstance(exc, ModelRequestBudgetExhausted):
        category, code = "budget", "model_request_budget_exhausted"
    elif isinstance(exc, ProfileCapabilityError):
        category, code = "capability", "profile_capability_missing"
    elif isinstance(exc, ModelBindingError):
        category, code = "configuration", "model_binding_failed"
    elif isinstance(exc, AuthenticationError):
        category, code = "authentication", "provider_authentication_failed"
        status = exc.status_code
    elif isinstance(exc, RateLimitError):
        category, code = "quota", "provider_quota_exhausted"
        status = exc.status_code
    elif isinstance(exc, BadRequestError):
        category, code = "invalid_request", "provider_invalid_request"
        status = exc.status_code
    elif isinstance(exc, APIStatusError):
        category, code = "provider", "provider_http_error"
        status = exc.status_code
    elif isinstance(exc, httpx.HTTPStatusError):
        category, code = "transport", "transport_retries_exhausted"
        status = exc.response.status_code
    elif isinstance(exc, httpx.TransportError):
        category, code = "transport", "transport_retries_exhausted"
    elif isinstance(exc, (UnexpectedModelBehavior, UserError)):
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
    response_body = _provider_response_body(exc)
    if response_body is not None:
        diagnostic["provider_response"] = _redact(response_body, secret=secret)
    return ClassifiedExecutionError(
        code=code,
        category=category,
        http_status=status,
        message=message,
        diagnostic=diagnostic,
    )


def _provider_response_body(exc: BaseException) -> str | None:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.text
    if isinstance(exc, APIStatusError):
        try:
            return json.dumps(exc.body, ensure_ascii=False, sort_keys=True, default=str)
        except TypeError:  # pragma: no cover - default=str handles normal SDK bodies.
            return str(exc.body)
    return None


def _redact(value: str, *, secret: str) -> str:
    result = value.replace(secret, "[REDACTED]") if secret else value
    patterns = (
        r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;]+",
        r"(?i)(api[-_ ]?key\s*[:=]\s*)[^\s,;]+",
        r"(?i)(cookie\s*[:=]\s*)[^\r\n]+",
    )
    for pattern in patterns:
        result = re.sub(pattern, r"\1[REDACTED]", result)
    return result
