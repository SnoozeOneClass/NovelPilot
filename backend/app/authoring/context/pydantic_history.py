from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import Model, ModelRequestParameters, StreamedResponse
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.settings import ModelSettings

from app.authoring.context.manager import ContextBudgetManager, RestorePack
from app.authoring.domain.models import AuthoringProfileSnapshot
from app.authoring.errors import ContextCompactionError
from app.authoring.models.retry import RetryableFailure, retryable_provider_failure
from app.authoring.models.transport import request_budget_exhaustion
from app.authoring.tools.gateway import EpisodeDeps


@dataclass(frozen=True, slots=True)
class PydanticHistoryResult:
    messages: list[ModelMessage]
    tokens_before: int
    tokens_after: int
    strategies: tuple[str, ...]


class PydanticHistoryCompactor:
    def __init__(self, budget: ContextBudgetManager) -> None:
        self.budget = budget

    async def compact(
        self,
        messages: list[ModelMessage],
        profile: AuthoringProfileSnapshot,
        restore_pack: RestorePack,
        stored_summary: str,
        overhead_tokens: int = 0,
        summarizer: Callable[[list[ModelMessage], int], Awaitable[str]] | None = None,
    ) -> PydanticHistoryResult:
        before = self._tokens(messages) + overhead_tokens
        threshold = self.budget.input_threshold(profile)
        if before < threshold:
            return PydanticHistoryResult(messages, before, before, ())

        system_parts = [
            part
            for message in messages
            if isinstance(message, ModelRequest)
            for part in message.parts
            if isinstance(part, SystemPromptPart)
        ]
        summary = (
            stored_summary.strip() or "Older turns are represented by the durable facts below."
        )

        def make_restore(value: str) -> ModelRequest:
            return ModelRequest(
                parts=[
                    *system_parts,
                    UserPromptPart(
                        "Persisted context summary:\n"
                        + value
                        + "\n\nMandatory restore pack:\n"
                        + restore_pack.render()
                    ),
                ]
            )

        restore_message = make_restore(summary)
        suffix = self._latest_complete_tool_exchange(messages)
        compacted: list[ModelMessage] = [restore_message, *suffix]
        after = self._tokens(compacted) + overhead_tokens
        if after >= threshold:
            compacted = [restore_message, *self._shrink_tool_results(suffix)]
            after = self._tokens(compacted) + overhead_tokens
        strategies = ["pydantic_store_summary", "restore_pack"]
        if after >= threshold and summarizer is not None:
            try:
                llm_summary = (await summarizer(messages, threshold)).strip()
            except Exception as error:
                if exhaustion := request_budget_exhaustion(error):
                    raise exhaustion from None
                raise ContextCompactionError("independent context summarizer failed") from error
            if not llm_summary:
                raise ContextCompactionError("independent context summarizer returned no text")
            compacted = [
                make_restore(llm_summary),
                *self._shrink_tool_results(suffix),
            ]
            after = self._tokens(compacted) + overhead_tokens
            strategies.append("independent_llm_summary")
        if after >= threshold:
            raise ContextCompactionError("Store-backed Restore Pack exceeds the model input budget")
        return PydanticHistoryResult(
            compacted,
            before,
            after,
            tuple(strategies),
        )

    @staticmethod
    def _tokens(messages: list[ModelMessage]) -> int:
        encoded = ModelMessagesTypeAdapter.dump_json(messages)
        return max(1, (len(encoded) + 2) // 3)

    @staticmethod
    def _latest_complete_tool_exchange(messages: list[ModelMessage]) -> list[ModelMessage]:
        for request_index in range(len(messages) - 1, -1, -1):
            request = messages[request_index]
            if not isinstance(request, ModelRequest):
                continue
            returned = {
                part.tool_call_id for part in request.parts if isinstance(part, ToolReturnPart)
            }
            if not returned:
                continue
            for response_index in range(request_index - 1, -1, -1):
                response = messages[response_index]
                if not isinstance(response, ModelResponse):
                    continue
                called = {
                    part.tool_call_id for part in response.parts if isinstance(part, ToolCallPart)
                }
                if returned.issubset(called):
                    return messages[response_index:]
            raise ContextCompactionError("Tool result has no matching Tool call in model history")
        return []

    @staticmethod
    def _shrink_tool_results(messages: list[ModelMessage]) -> list[ModelMessage]:
        compacted: list[ModelMessage] = []
        for message in messages:
            if not isinstance(message, ModelRequest):
                compacted.append(message)
                continue
            parts = [
                replace(
                    part,
                    content={
                        "compacted": True,
                        "durable_restore_pack": "in the preceding request",
                    },
                )
                if isinstance(part, ToolReturnPart)
                else part
                for part in message.parts
            ]
            compacted.append(replace(message, parts=parts))
        return compacted


class ContextManagedModel(WrapperModel):
    """Apply Store-backed history compaction immediately before each Provider request."""

    def __init__(
        self,
        model: Model,
        deps: EpisodeDeps,
        profile: AuthoringProfileSnapshot,
        manager: ContextBudgetManager,
    ) -> None:
        super().__init__(model)
        self._deps = deps
        self._authoring_profile = profile
        self._compactor = PydanticHistoryCompactor(manager)
        self._last_input_estimate = 0

    async def _prepare(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> list[ModelMessage]:
        material = await self._deps.store.restore_material(
            self._deps.project_id,
            self._deps.instruction.logical_target,
            self._deps.instruction.instruction_key,
        )
        restore = RestorePack.from_material(self._deps.instruction, material)
        try:
            result = await self._compactor.compact(
                messages,
                self._authoring_profile,
                restore,
                str(material["stored_summary"]),
                overhead_tokens=self._request_overhead_tokens(
                    model_settings, model_request_parameters
                ),
                summarizer=lambda history, threshold: self._summarize_history(
                    history, threshold, model_settings
                ),
            )
        except ContextCompactionError as error:
            failures = await self._deps.store.record_context_compaction_failure(
                self._deps.project_id, self._deps.episode_id, str(error)
            )
            if failures >= self._compactor.budget.max_failures:
                raise ContextCompactionError(
                    f"context compaction failed {failures} times; pause the run"
                ) from error
            raise
        if result.strategies:
            await self._deps.store.record_context_compaction(
                self._deps.project_id,
                self._deps.episode_id,
                result.tokens_before,
                result.tokens_after,
                result.strategies,
            )
        self._last_input_estimate = result.tokens_after
        return result.messages

    def _request_overhead_tokens(
        self,
        model_settings: ModelSettings | None,
        parameters: ModelRequestParameters,
    ) -> int:
        material = {
            "tool_schemas": [repr(tool) for tool in parameters.function_tools],
            "native_tools": [repr(tool) for tool in parameters.native_tools],
            "output_tools": [repr(tool) for tool in parameters.output_tools],
            "request_parameters": repr(parameters),
            "model_settings": model_settings,
            "profile_request_options": self._authoring_profile.request_options,
        }
        return max(1, (len(json.dumps(material, default=str)) + 2) // 3)

    async def _summarize_history(
        self,
        messages: list[ModelMessage],
        threshold: int,
        model_settings: ModelSettings | None,
    ) -> str:
        request_index = self._deps.next_model_request()
        started = time.perf_counter()
        serialized = ModelMessagesTypeAdapter.dump_json(messages).decode("utf-8")
        prompt = (
            "Summarize only durable story decisions, current work, and unresolved constraints. "
            "Do not invent facts.\n\n" + serialized[: max(256, threshold * 3)]
        )
        parameters = ModelRequestParameters(
            function_tools=[], output_tools=[], allow_text_output=True
        )
        await self._deps.store.record_runtime_event(
            self._deps.project_id,
            "model_request_started",
            {
                "episode_id": self._deps.episode_id,
                "request_index": request_index,
                "purpose": "context_summary",
                "profile_id": self._authoring_profile.profile_id,
                "profile_fingerprint": self._authoring_profile.fingerprint,
            },
        )
        try:
            response = await self.wrapped.request(
                [ModelRequest(parts=[UserPromptPart(prompt)])],
                model_settings,
                parameters,
            )
        except Exception as error:
            await self._deps.store.record_model_request(
                project_id=self._deps.project_id,
                episode_id=self._deps.episode_id,
                request_index=request_index,
                profile_fingerprint=self._authoring_profile.fingerprint,
                purpose="context_summary",
                status="failed",
                latency_ms=round((time.perf_counter() - started) * 1000),
                error_type=type(error).__name__,
                metadata={"summary_policy": "durable-context-v1"},
            )
            raise
        text = "".join(part.content for part in response.parts if isinstance(part, TextPart))
        usage = response.usage
        cache_tokens = usage.cache_read_tokens + usage.cache_write_tokens
        await self._deps.store.record_model_request(
            project_id=self._deps.project_id,
            episode_id=self._deps.episode_id,
            request_index=request_index,
            profile_fingerprint=self._authoring_profile.fingerprint,
            purpose="context_summary",
            status="succeeded",
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_write_tokens=usage.cache_write_tokens,
            latency_ms=round((time.perf_counter() - started) * 1000),
            cost_microunits=round(
                usage.input_tokens * self._authoring_profile.input_price_per_million
                + usage.output_tokens * self._authoring_profile.output_price_per_million
                + cache_tokens * self._authoring_profile.cache_price_per_million
            ),
            metadata={"summary_policy": "durable-context-v1"},
        )
        await self._deps.store.record_runtime_event(
            self._deps.project_id,
            "model_request_completed",
            {
                "episode_id": self._deps.episode_id,
                "request_index": request_index,
                "purpose": "context_summary",
                "profile_id": self._authoring_profile.profile_id,
            },
        )
        return text

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> Any:
        prepared = await self._prepare(messages, model_settings, model_request_parameters)
        request_index = self._deps.next_model_request()
        started = time.perf_counter()
        await self._record_worker_request_started(request_index)
        try:
            response = await self.wrapped.request(
                prepared, model_settings, model_request_parameters
            )
        except Exception as error:
            retry = retryable_provider_failure(error)
            delay_ms = self._retry_delay_ms(retry)
            await self._deps.store.record_model_request(
                project_id=self._deps.project_id,
                episode_id=self._deps.episode_id,
                request_index=request_index,
                profile_fingerprint=self._authoring_profile.fingerprint,
                purpose="worker",
                status="failed",
                input_tokens=self._last_input_estimate,
                latency_ms=round((time.perf_counter() - started) * 1000),
                retry_reason=None if retry is None else retry.reason,
                retry_after_ms=delay_ms,
                error_type=type(error).__name__,
                metadata={"request_options": self._authoring_profile.request_options},
            )
            raise
        await self._mark_output_started()
        usage = response.usage
        cache_tokens = usage.cache_read_tokens + usage.cache_write_tokens
        cost = round(
            usage.input_tokens * self._authoring_profile.input_price_per_million
            + usage.output_tokens * self._authoring_profile.output_price_per_million
            + cache_tokens * self._authoring_profile.cache_price_per_million
        )
        await self._deps.store.record_model_request(
            project_id=self._deps.project_id,
            episode_id=self._deps.episode_id,
            request_index=request_index,
            profile_fingerprint=self._authoring_profile.fingerprint,
            purpose="worker",
            status="succeeded",
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_write_tokens=usage.cache_write_tokens,
            latency_ms=round((time.perf_counter() - started) * 1000),
            cost_microunits=cost,
            metadata={"request_options": self._authoring_profile.request_options},
        )
        await self._deps.store.record_runtime_event(
            self._deps.project_id,
            "model_request_completed",
            {
                "episode_id": self._deps.episode_id,
                "request_index": request_index,
                "purpose": "worker",
                "profile_id": self._authoring_profile.profile_id,
            },
        )
        return response

    @asynccontextmanager
    async def request_stream(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
        run_context: RunContext[Any] | None = None,
    ) -> AsyncIterator[StreamedResponse]:
        prepared = await self._prepare(messages, model_settings, model_request_parameters)
        request_index = self._deps.next_model_request()
        started = time.perf_counter()
        await self._record_worker_request_started(request_index)
        try:
            async with self.wrapped.request_stream(
                prepared, model_settings, model_request_parameters, run_context
            ) as response:
                await self._mark_output_started()
                yield response
                usage = response.usage
        except Exception as error:
            retry = retryable_provider_failure(error)
            delay_ms = self._retry_delay_ms(retry)
            await self._deps.store.record_model_request(
                project_id=self._deps.project_id,
                episode_id=self._deps.episode_id,
                request_index=request_index,
                profile_fingerprint=self._authoring_profile.fingerprint,
                purpose="worker",
                status="failed",
                input_tokens=self._last_input_estimate,
                latency_ms=round((time.perf_counter() - started) * 1000),
                retry_reason=None if retry is None else retry.reason,
                retry_after_ms=delay_ms,
                error_type=type(error).__name__,
                metadata={"request_options": self._authoring_profile.request_options},
            )
            raise
        cache_tokens = usage.cache_read_tokens + usage.cache_write_tokens
        await self._deps.store.record_model_request(
            project_id=self._deps.project_id,
            episode_id=self._deps.episode_id,
            request_index=request_index,
            profile_fingerprint=self._authoring_profile.fingerprint,
            purpose="worker",
            status="succeeded",
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_write_tokens=usage.cache_write_tokens,
            latency_ms=round((time.perf_counter() - started) * 1000),
            cost_microunits=round(
                usage.input_tokens * self._authoring_profile.input_price_per_million
                + usage.output_tokens * self._authoring_profile.output_price_per_million
                + cache_tokens * self._authoring_profile.cache_price_per_million
            ),
            metadata={"request_options": self._authoring_profile.request_options},
        )
        await self._deps.store.record_runtime_event(
            self._deps.project_id,
            "model_request_completed",
            {
                "episode_id": self._deps.episode_id,
                "request_index": request_index,
                "purpose": "worker",
                "profile_id": self._authoring_profile.profile_id,
                "streamed": True,
            },
        )

    async def _mark_output_started(self) -> None:
        if self._deps.model_output_started:
            return
        self._deps.model_output_started = True
        await self._deps.store.record_runtime_event(
            self._deps.project_id,
            "model_output_started",
            {"episode_id": self._deps.episode_id},
        )

    async def _record_worker_request_started(self, request_index: int) -> None:
        await self._deps.store.record_runtime_event(
            self._deps.project_id,
            "model_request_started",
            {
                "episode_id": self._deps.episode_id,
                "request_index": request_index,
                "purpose": "worker",
                "profile_id": self._authoring_profile.profile_id,
                "profile_fingerprint": self._authoring_profile.fingerprint,
                "input_tokens_estimate": self._last_input_estimate,
            },
        )

    @staticmethod
    def _retry_delay_ms(retry: RetryableFailure | None) -> int | None:
        if retry is None:
            return None
        if retry.retry_after_seconds is not None:
            return max(0, round(retry.retry_after_seconds * 1000))
        return None
