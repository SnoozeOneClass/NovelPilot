from __future__ import annotations

import asyncio
import inspect
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from app.authoring.domain.models import AuthoringProfileSnapshot, RunStatus, WorkerRole
from app.authoring.errors import (
    LeaseUnavailableError,
    StateCorruptionError,
    TerminalPostconditionError,
    ToolAuthorizationError,
    ToolConflictError,
)
from app.authoring.models.profiles import EpisodeProfileSelection
from app.authoring.models.transport import ActivationRequestBudgetExhausted
from app.authoring.runtime.arbiter import (
    DeterministicFailureArbiter,
    FailureArbiter,
    FailureContext,
)
from app.authoring.runtime.routing import route
from app.authoring.store.store import AuthoringStore
from app.authoring.tools.gateway import EpisodeDeps
from app.authoring.workers.runtime import WorkerRuntime

ProfileResolverFunction = Callable[
    [str, WorkerRole],
    AuthoringProfileSnapshot | EpisodeProfileSelection | Awaitable[EpisodeProfileSelection],
]


class ProfileResolverObject(Protocol):
    async def resolve(
        self, store: AuthoringStore, project_id: str, role: WorkerRole
    ) -> EpisodeProfileSelection: ...


ProfileResolver = ProfileResolverFunction | ProfileResolverObject


class EngineResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    status: RunStatus
    instructions_completed: int = Field(ge=0)
    episodes_failed: int = Field(ge=0)
    last_error: str | None = None


class Engine:
    """One serial, fact-routed Engine for a single authoring project."""

    def __init__(
        self,
        store: AuthoringStore,
        worker_runtime: WorkerRuntime,
        profile_resolver: ProfileResolver,
        *,
        max_instruction_retries: int = 2,
        max_same_instruction: int = 3,
        lease_ttl_seconds: float = 30.0,
        failure_arbiter: FailureArbiter | None = None,
    ) -> None:
        if max_instruction_retries < 0 or max_same_instruction < 1 or lease_ttl_seconds <= 0:
            raise ValueError("invalid bounded failure policy")
        self.store = store
        self.worker_runtime = worker_runtime
        self.profile_resolver = profile_resolver
        self.max_instruction_retries = max_instruction_retries
        self.max_same_instruction = max_same_instruction
        self.lease_ttl_seconds = lease_ttl_seconds
        self.failure_arbiter = failure_arbiter or DeterministicFailureArbiter()

    async def run(
        self,
        project_id: str,
        *,
        max_instructions: int | None = None,
        cancel_event: asyncio.Event | None = None,
        lease_acquired_event: asyncio.Event | None = None,
    ) -> EngineResult:
        owner = str(uuid.uuid4())
        await self.store.acquire_lease(project_id, owner, self.lease_ttl_seconds)
        completed = 0
        failures = 0
        last_error: str | None = None
        try:
            completed = await self.store.reconcile(project_id)
            initial = await self.store.load_state(project_id)
            if initial.status is RunStatus.READY:
                await self.store.set_status(project_id, RunStatus.RUNNING)
            if lease_acquired_event is not None:
                lease_acquired_event.set()

            while max_instructions is None or completed < max_instructions:
                await self.store.renew_lease(project_id, owner, self.lease_ttl_seconds)
                state = await self.store.load_state(project_id)
                if (
                    cancel_event
                    and cancel_event.is_set()
                    and state.status
                    not in {
                        RunStatus.PAUSED,
                        RunStatus.FAILURE_PAUSED,
                        RunStatus.CANCELLED,
                        RunStatus.COMPLETED,
                    }
                ):
                    await self.store.cancel(project_id)
                    state = await self.store.load_state(project_id)
                try:
                    recovered = await self.store.recover_active_instruction(project_id, owner)
                except StateCorruptionError as error:
                    last_error = self._safe_error(error, ())
                    await self.store.set_status(
                        project_id,
                        RunStatus.FAILURE_PAUSED,
                        failure_reason=last_error,
                    )
                    break
                if recovered:
                    completed += 1
                    continue
                instruction = route(state)
                if instruction is None:
                    return EngineResult(
                        project_id=project_id,
                        status=state.status,
                        instructions_completed=completed,
                        episodes_failed=failures,
                        last_error=last_error,
                    )

                episode_count = await self.store.instruction_episode_count(
                    project_id, instruction.instruction_key
                )
                arbiter_retries = await self.store.arbiter_retry_count(
                    project_id, instruction.instruction_key
                )
                if episode_count >= self.max_same_instruction + arbiter_retries:
                    last_error = "the same instruction repeated without a new fact version"
                    if not await self._arbitrate(
                        project_id, instruction.instruction_key, "deadlock", episode_count, 0
                    ):
                        await self.store.set_status(
                            project_id, RunStatus.FAILURE_PAUSED, failure_reason=last_error
                        )
                        break
                    continue

                failure_count = await self.store.instruction_failure_count(
                    project_id, instruction.instruction_key
                )
                if failure_count > self.max_instruction_retries + arbiter_retries:
                    last_error = "the instruction exhausted its durable retry budget"
                    if not await self._arbitrate(
                        project_id,
                        instruction.instruction_key,
                        "retry_exhausted",
                        episode_count,
                        failure_count,
                    ):
                        await self.store.set_status(
                            project_id, RunStatus.FAILURE_PAUSED, failure_reason=last_error
                        )
                        break
                    continue

                await self.store.set_active_instruction(
                    project_id,
                    instruction.instruction_key,
                    instruction.kind.value,
                    instruction.logical_target,
                    instruction.fact_version,
                )
                try:
                    selection = await self._resolve_profile(project_id, instruction.worker)
                except Exception as error:  # noqa: BLE001 - Profile preflight has one typed failure path.
                    last_error = f"{type(error).__name__}: {error}"
                    await self.store.record_runtime_event(
                        project_id,
                        "profile_resolution_failed",
                        {
                            "instruction_key": instruction.instruction_key,
                            "worker": instruction.worker.value,
                            "error_type": type(error).__name__,
                        },
                    )
                    await self.store.set_status(
                        project_id,
                        RunStatus.FAILURE_PAUSED,
                        failure_reason=last_error,
                    )
                    break
                profile = selection.snapshot
                profile.require("text_output", "tool_calling")
                deps = EpisodeDeps(
                    store=self.store,
                    instruction=instruction,
                    profile=profile,
                    lease_owner=owner,
                    model=selection.model,
                    fallback_model=selection.fallback_model,
                    fallback_profile=selection.fallback_snapshot,
                )
                try:
                    await self.store.start_episode(
                        episode_id=deps.episode_id,
                        project_id=project_id,
                        worker=instruction.worker.value,
                        instruction_key=instruction.instruction_key,
                        profile_snapshot=profile.model_dump(mode="json"),
                        fallback_profile_snapshot=(
                            None
                            if selection.fallback_snapshot is None
                            else selection.fallback_snapshot.model_dump(mode="json")
                        ),
                        instruction_kind=instruction.kind.value,
                        logical_target=instruction.logical_target,
                    )
                except BaseException:
                    await selection.aclose()
                    raise
                started = time.perf_counter()
                heartbeat_stop = asyncio.Event()
                heartbeat = asyncio.create_task(self._heartbeat(project_id, owner, heartbeat_stop))
                try:
                    result = await self.worker_runtime.run(
                        instruction, deps, cancel_event=cancel_event
                    )
                    if heartbeat.done():
                        heartbeat.result()
                    if result.terminal_tool != deps.terminal_tool:
                        raise TerminalPostconditionError(
                            f"{instruction.kind.value} ended without {deps.terminal_tool}"
                        )
                    usage: dict[str, object] | None = None
                    if result.request_count:
                        actual_profile = (
                            profile
                            if result.actual_profile_fingerprint is None
                            else self._actual_profile(selection, result.actual_profile_fingerprint)
                        )
                        cost = int(
                            result.input_tokens * actual_profile.input_price_per_million
                            + result.output_tokens * actual_profile.output_price_per_million
                            + (result.cache_read_tokens + result.cache_write_tokens)
                            * actual_profile.cache_price_per_million
                        )
                        usage = {
                            "profile_fingerprint": actual_profile.fingerprint,
                            "input_tokens": result.input_tokens,
                            "output_tokens": result.output_tokens,
                            "cache_tokens": result.cache_read_tokens + result.cache_write_tokens,
                            "latency_ms": int((time.perf_counter() - started) * 1000),
                            "metadata": {
                                "profile_id": actual_profile.profile_id,
                                "model_id": actual_profile.model_id,
                                "metadata_version": actual_profile.metadata_version,
                                "fallback_from": result.fallback_from,
                                "fallback_reason": result.fallback_reason,
                            },
                            "cost_microunits": cost,
                        }
                    await self.store.finish_episode(
                        deps.episode_id,
                        project_id,
                        succeeded=True,
                        usage=usage,
                        instruction=instruction,
                        lease_owner=owner,
                    )
                    completed += 1
                except asyncio.CancelledError:
                    interrupted_state = await self.store.load_state(project_id)
                    if interrupted_state.status is RunStatus.PAUSED:
                        await self.store.finish_episode(
                            deps.episode_id,
                            project_id,
                            succeeded=False,
                            failure="paused at a safe boundary",
                        )
                        break
                    await self.store.finish_episode(
                        deps.episode_id, project_id, succeeded=False, failure="cancelled"
                    )
                    if interrupted_state.status is not RunStatus.CANCELLED:
                        await self.store.cancel(project_id)
                    raise
                except Exception as error:
                    failures += 1
                    last_error = self._safe_error(error, selection.redaction_secrets)
                    await self.store.finish_episode(
                        deps.episode_id, project_id, succeeded=False, failure=last_error
                    )
                    if isinstance(error, LeaseUnavailableError):
                        raise
                    # A request or optional Agent postamble can fail after the domain
                    # transaction succeeded. Keep that failed attempt visible and acknowledge
                    # only verified work. Domain rejection/cancellation is never success.
                    if not isinstance(
                        error,
                        (
                            StateCorruptionError,
                            ToolAuthorizationError,
                            ToolConflictError,
                            TerminalPostconditionError,
                        ),
                    ):
                        try:
                            recovered = await self.store.recover_active_instruction(
                                project_id,
                                owner,
                                source_episode_id=deps.episode_id,
                            )
                        except StateCorruptionError as corruption:
                            error = corruption
                            last_error = self._safe_error(corruption, selection.redaction_secrets)
                            recovered = False
                        if recovered:
                            completed += 1
                            continue
                    interrupted_state = await self.store.load_state(project_id)
                    if interrupted_state.status in {
                        RunStatus.PAUSED,
                        RunStatus.FAILURE_PAUSED,
                        RunStatus.CANCELLED,
                        RunStatus.COMPLETED,
                    }:
                        break
                    if isinstance(
                        error,
                        (
                            ActivationRequestBudgetExhausted,
                            StateCorruptionError,
                            ToolAuthorizationError,
                            ToolConflictError,
                            TerminalPostconditionError,
                        ),
                    ):
                        await self.store.set_status(
                            project_id,
                            RunStatus.FAILURE_PAUSED,
                            failure_reason=last_error,
                        )
                        break
                    failure_count = await self.store.instruction_failure_count(
                        project_id, instruction.instruction_key
                    )
                    if failure_count > self.max_instruction_retries:
                        episode_count = await self.store.instruction_episode_count(
                            project_id, instruction.instruction_key
                        )
                        if not await self._arbitrate(
                            project_id,
                            instruction.instruction_key,
                            "retry_exhausted",
                            episode_count,
                            failure_count,
                        ):
                            await self.store.set_status(
                                project_id,
                                RunStatus.FAILURE_PAUSED,
                                failure_reason=last_error,
                            )
                            break
                    delay_ms = await self.store.retry_delay_for_episode(
                        deps.episode_id, failure_count
                    )
                    if delay_ms is not None:
                        await self.store.record_runtime_event(
                            project_id,
                            "model_retry_wait",
                            {
                                "episode_id": deps.episode_id,
                                "delay_ms": delay_ms,
                                "failure_count": failure_count,
                            },
                        )
                        await self._wait_for_retry(delay_ms, cancel_event)
                finally:
                    heartbeat_stop.set()
                    await asyncio.gather(heartbeat, return_exceptions=True)
                    await selection.aclose()
            final = await self.store.load_state(project_id)
            return EngineResult(
                project_id=project_id,
                status=final.status,
                instructions_completed=completed,
                episodes_failed=failures,
                last_error=last_error,
            )
        finally:
            await self.store.release_lease(project_id, owner)

    async def _resolve_profile(self, project_id: str, role: WorkerRole) -> EpisodeProfileSelection:
        resolver = self.profile_resolver
        if callable(resolver):
            resolved = resolver(project_id, role)
            if inspect.isawaitable(resolved):
                resolved = await resolved
        else:
            resolved = await resolver.resolve(self.store, project_id, role)
        if isinstance(resolved, AuthoringProfileSnapshot):
            return EpisodeProfileSelection(snapshot=resolved)
        return resolved

    @staticmethod
    def _actual_profile(
        selection: EpisodeProfileSelection, fingerprint: str
    ) -> AuthoringProfileSnapshot:
        if selection.snapshot.fingerprint == fingerprint:
            return selection.snapshot
        if (
            selection.fallback_snapshot is not None
            and selection.fallback_snapshot.fingerprint == fingerprint
        ):
            return selection.fallback_snapshot
        raise TerminalPostconditionError("Worker reported an unknown actual Profile fingerprint")

    @staticmethod
    def _safe_error(error: Exception, secrets: tuple[str, ...]) -> str:
        message = str(error)
        for secret in secrets:
            message = message.replace(secret, "[REDACTED]")
        return f"{type(error).__name__}: {message}"

    async def _heartbeat(self, project_id: str, owner: str, stop_event: asyncio.Event) -> None:
        interval = max(0.01, self.lease_ttl_seconds / 3)
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
                return
            except TimeoutError:
                pass
            await self.store.renew_lease(project_id, owner, self.lease_ttl_seconds)

    @staticmethod
    async def _wait_for_retry(delay_ms: int, interrupt_event: asyncio.Event | None) -> None:
        delay_seconds = delay_ms / 1000
        if interrupt_event is None:
            await asyncio.sleep(delay_seconds)
            return
        try:
            await asyncio.wait_for(interrupt_event.wait(), timeout=delay_seconds)
        except TimeoutError:
            pass

    async def _arbitrate(
        self,
        project_id: str,
        instruction_key: str,
        reason: str,
        episode_count: int,
        failure_count: int,
    ) -> bool:
        context = FailureContext(
            project_id=project_id,
            instruction_key=instruction_key,
            reason=reason,
            episode_count=episode_count,
            failure_count=failure_count,
        )
        decision = await self.failure_arbiter.decide(context)
        if decision.action == "retry_once" and await self.store.arbiter_retry_count(
            project_id, instruction_key
        ):
            decision = decision.model_copy(
                update={"action": "pause", "reason_code": "arbiter_retry_already_used"}
            )
        await self.store.record_failure_decision(
            project_id,
            instruction_key,
            context.model_dump(mode="json"),
            decision.model_dump(mode="json"),
        )
        return decision.action == "retry_once"
