from __future__ import annotations

import asyncio
from collections.abc import AsyncIterable, Callable, Mapping
from typing import Any, Protocol, cast

from pydantic import BaseModel, ConfigDict
from pydantic_ai import Agent, AgentStreamEvent, RunContext

from app.authoring.context import ContextBudgetManager, ContextManagedModel, RestorePack
from app.authoring.domain.models import Instruction, InstructionKind, WorkerRole
from app.authoring.models.retry import retryable_provider_failure
from app.authoring.tools.gateway import EpisodeDeps, ToolGateway
from app.authoring.workers.agents import build_authoring_agent


class EpisodeResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    output_text: str
    terminal_tool: str | None
    successful_tools: tuple[str, ...]
    request_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    actual_profile_id: str | None = None
    actual_profile_fingerprint: str | None = None
    fallback_from: str | None = None
    fallback_reason: str | None = None


class WorkerRuntime(Protocol):
    async def run(
        self,
        instruction: Instruction,
        deps: EpisodeDeps,
        cancel_event: asyncio.Event | None = None,
    ) -> EpisodeResult: ...


class PydanticWorkerRuntime:
    """Narrow Pydantic AI adapter; domain/runtime never depend on its node types.

    Agents are assembled with role Tools at the composition root.  This adapter
    owns framework history and translates only the stable result/usage evidence.
    """

    def __init__(
        self,
        agent_factory: Callable[[WorkerRole], Agent[Any, str]] | None = None,
        context_manager: ContextBudgetManager | None = None,
    ) -> None:
        self._agent_factory = agent_factory
        self._context_manager = context_manager or ContextBudgetManager()

    async def run(
        self,
        instruction: Instruction,
        deps: EpisodeDeps,
        cancel_event: asyncio.Event | None = None,
    ) -> EpisodeResult:
        if cancel_event and cancel_event.is_set():
            raise asyncio.CancelledError
        result, actual_profile, fallback_from, fallback_reason = await self._run_with_fallback(
            instruction, deps, cancel_event
        )
        usage = result.usage
        return EpisodeResult(
            output_text=result.output,
            terminal_tool=deps.terminal_tool
            if deps.terminal_tool in deps.successful_tools
            else None,
            successful_tools=tuple(deps.successful_tools),
            request_count=usage.requests,
            input_tokens=usage.input_tokens or 0,
            output_tokens=usage.output_tokens or 0,
            cache_read_tokens=usage.cache_read_tokens or 0,
            cache_write_tokens=usage.cache_write_tokens or 0,
            actual_profile_id=actual_profile.profile_id,
            actual_profile_fingerprint=actual_profile.fingerprint,
            fallback_from=fallback_from,
            fallback_reason=fallback_reason,
        )

    async def _run_with_fallback(
        self,
        instruction: Instruction,
        deps: EpisodeDeps,
        cancel_event: asyncio.Event | None,
    ) -> tuple[Any, Any, str | None, str | None]:
        try:
            result = await self._run_agent(
                instruction, deps, deps.model, deps.profile, cancel_event
            )
            return result, deps.profile, None, None
        except Exception as error:
            retryable = retryable_provider_failure(error)
            if (
                deps.fallback_model is None
                or deps.fallback_profile is None
                or retryable is None
                or deps.model_output_started
                or deps.tool_side_effects
            ):
                await deps.store.record_runtime_event(
                    deps.project_id,
                    "model_request_failed",
                    {
                        "episode_id": deps.episode_id,
                        "profile_id": deps.profile.profile_id,
                        "error_type": type(error).__name__,
                        "fallback_allowed": False,
                    },
                )
                raise
            await deps.store.record_runtime_event(
                deps.project_id,
                "model_fallback",
                {
                    "episode_id": deps.episode_id,
                    "from_profile": deps.profile.profile_id,
                    "to_profile": deps.fallback_profile.profile_id,
                    "reason": retryable.reason,
                },
            )
            deps.model_output_started = False
            try:
                result = await self._run_agent(
                    instruction,
                    deps,
                    deps.fallback_model,
                    deps.fallback_profile,
                    cancel_event,
                )
            except Exception as fallback_error:
                await deps.store.record_runtime_event(
                    deps.project_id,
                    "model_request_failed",
                    {
                        "episode_id": deps.episode_id,
                        "profile_id": deps.fallback_profile.profile_id,
                        "error_type": type(fallback_error).__name__,
                        "fallback_allowed": False,
                    },
                )
                raise
            return result, deps.fallback_profile, deps.profile.profile_id, retryable.reason

    async def _run_agent(
        self,
        instruction: Instruction,
        deps: EpisodeDeps,
        model: Any,
        profile: Any,
        cancel_event: asyncio.Event | None,
    ) -> Any:
        if self._agent_factory is not None:
            agent = self._agent_factory(instruction.worker)
        else:
            if model is None:
                raise ValueError("Pydantic Worker requires a resolved Provider model")
            managed_model = ContextManagedModel(model, deps, profile, self._context_manager)
            agent = build_authoring_agent(managed_model, instruction.worker)
        material = await deps.store.restore_material(
            deps.project_id,
            instruction.logical_target,
            instruction.instruction_key,
        )
        restore = RestorePack.from_material(instruction, material)
        task = asyncio.create_task(
            agent.run(
                self._render_instruction(instruction)
                + "\n\nMandatory restore pack:\n"
                + restore.render(),
                deps=cast(Any, deps),
                event_stream_handler=(
                    self._event_handler if "text_streaming" in profile.capabilities else None
                ),
            )
        )
        if cancel_event is not None:
            cancel_task = asyncio.create_task(cancel_event.wait())
            done, _ = await asyncio.wait({task, cancel_task}, return_when=asyncio.FIRST_COMPLETED)
            if cancel_task in done and cancel_event.is_set():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise asyncio.CancelledError
            cancel_task.cancel()
            await asyncio.gather(cancel_task, return_exceptions=True)
        return await task

    @staticmethod
    async def _event_handler(ctx: RunContext[Any], events: AsyncIterable[AgentStreamEvent]) -> None:
        observed: set[str] = set()
        async for event in events:
            event_kind = str(getattr(event, "event_kind", type(event).__name__))
            part = getattr(event, "part", None)
            delta = getattr(event, "delta", None)
            part_kind = str(
                getattr(part, "part_kind", getattr(delta, "part_delta_kind", event_kind))
            )
            if "thinking" in part_kind:
                lifecycle = "model_thinking_streamed"
            elif "tool" in part_kind or "tool" in event_kind:
                lifecycle = "model_tool_streamed"
            elif "text" in part_kind:
                lifecycle = "model_text_streamed"
            else:
                continue
            if lifecycle in observed:
                continue
            observed.add(lifecycle)
            await ctx.deps.store.record_runtime_event(
                ctx.deps.project_id,
                lifecycle,
                {
                    "episode_id": ctx.deps.episode_id,
                    "event_kind": event_kind,
                    "part_kind": part_kind,
                },
            )

    @staticmethod
    def _render_instruction(instruction: Instruction) -> str:
        return (
            f"Execute {instruction.kind.value} for {instruction.logical_target}. "
            f"Finish by calling the terminal Tool for postcondition "
            f"{instruction.terminal_postcondition}. Do not act outside this target."
        )


class ScriptedAutoWorkerRuntime:
    """Deterministic no-network Worker used by contract tests and local smoke runs."""

    def __init__(self, gateway: ToolGateway, inject_one_rewrite: bool = True) -> None:
        self.gateway = gateway
        self.inject_one_rewrite = inject_one_rewrite

    async def run(
        self,
        instruction: Instruction,
        deps: EpisodeDeps,
        cancel_event: asyncio.Event | None = None,
    ) -> EpisodeResult:
        if cancel_event and cancel_event.is_set():
            raise asyncio.CancelledError
        handlers: Mapping[InstructionKind, Callable[[EpisodeDeps], Any]] = {
            InstructionKind.CREATE_FOUNDATION: self._foundation,
            InstructionKind.WRITE_CHAPTER: self._write_chapter,
            InstructionKind.REWRITE_CHAPTER: self._rewrite_chapter,
            InstructionKind.REVIEW_BOUNDARY: self._review,
            InstructionKind.SAVE_SUMMARY: self._summary,
            InstructionKind.EXTEND_OUTLINE: self._extend,
            InstructionKind.COMPLETE_BOOK: self._complete,
        }
        await handlers[instruction.kind](deps)
        return EpisodeResult(
            output_text=f"completed {instruction.kind.value}",
            terminal_tool=deps.terminal_tool
            if deps.terminal_tool in deps.successful_tools
            else None,
            successful_tools=tuple(deps.successful_tools),
        )

    async def _foundation(self, deps: EpisodeDeps) -> None:
        context = await self.gateway.invoke(deps, "novel_context")
        title = str(context["brief"]).strip().split("。", maxsplit=1)[0][:32] or "自动长篇"
        await self.gateway.invoke(deps, "save_book", {"title": title})
        target = int(context["target"]["target_chapters"])
        planned = min(3, target)
        await self.gateway.invoke(
            deps,
            "save_foundation",
            {
                "premise": context["brief"],
                "compass": "主角必须通过选择推动因果链",
                "characters": [{"name": "林序", "goal": "完成不可逆的承诺"}],
                "world": {"rule": "每次能力使用都有代价"},
                "outline": [f"第{number}章推进冲突" for number in range(1, planned + 1)],
                "planned_through": planned,
            },
        )
        await self.gateway.invoke(deps, "audit_foundation", {"passed": True, "issues": []})

    async def _write_chapter(self, deps: EpisodeDeps) -> None:
        number = int(deps.instruction.logical_target.split(":", maxsplit=1)[1])
        context = await self.gateway.invoke(deps, "novel_context")
        plan = f"第{number}章：承接既有事实，制造选择并留下可追踪后果。"
        content = (
            f"林序在第{number}次钟声响起时看见了新的线索。"
            f"他没有等待答案，而是作出选择，让第{number}章的冲突向前推进。"
            f"代价随即显现，第{number}次承诺也因此变得无法撤回。"
        )
        await self.gateway.invoke(deps, "plan_chapter", {"chapter_number": number, "plan": plan})
        await self.gateway.invoke(
            deps, "draft_chapter", {"chapter_number": number, "content": content}
        )
        await self.gateway.invoke(
            deps,
            "check_consistency",
            {"chapter_number": number, "passed": True, "issues": []},
        )
        await self.gateway.invoke(
            deps,
            "commit_chapter",
            {
                "chapter_number": number,
                "title": f"钟声与选择 {number}",
                "content": content,
                "facts": {
                    "summary": f"林序在第{number}章作出推进主线的选择",
                    "character_changes": {"林序": f"承诺阶段 {number}"},
                    "open_threads": [f"第{number}章代价"],
                    "context_seen": bool(context["foundation"]),
                },
            },
        )

    async def _rewrite_chapter(self, deps: EpisodeDeps) -> None:
        number = int(deps.instruction.logical_target.split(":", maxsplit=1)[1])
        chapter = await self.gateway.invoke(deps, "read_chapter", {"chapter_number": number})
        content = str(chapter["content"]).rstrip() + "\n\n他终于说出具体的代价，并承担了后果。"
        await self.gateway.invoke(
            deps, "edit_chapter", {"chapter_number": number, "content": content}
        )
        await self.gateway.invoke(
            deps,
            "check_consistency",
            {"chapter_number": number, "passed": True, "issues": []},
        )
        await self.gateway.invoke(
            deps,
            "commit_chapter",
            {
                "chapter_number": number,
                "title": chapter["title"],
                "content": content,
                "facts": {
                    "summary": f"第{number}章返工后明确了选择的代价",
                    "character_changes": {"林序": "主动承担后果"},
                    "open_threads": [],
                },
            },
        )

    async def _review(self, deps: EpisodeDeps) -> None:
        boundary = int(deps.instruction.logical_target.split(":", maxsplit=1)[1])
        dimensions = {
            "causality": 4,
            "character": 4,
            "pacing": 4,
            "continuity": 5,
            "stakes": 4,
            "prose": 4,
            "payoff": 3,
        }
        should_rewrite = False
        if self.inject_one_rewrite and boundary >= 2:
            chapter = await self.gateway.invoke(deps, "read_chapter", {"chapter_number": 2})
            should_rewrite = "承担了后果" not in str(chapter["content"])
        await self.gateway.invoke(
            deps,
            "save_review",
            {
                "boundary": boundary,
                "verdict": "polish" if should_rewrite else "accept",
                "dimensions": dimensions,
                "evidence": ["选择的后果需要更具体"],
                "chapters": [2] if should_rewrite else [],
            },
        )

    async def _summary(self, deps: EpisodeDeps) -> None:
        boundary = int(deps.instruction.logical_target.split(":", maxsplit=1)[1])
        await self.gateway.invoke(
            deps,
            "save_arc_summary",
            {
                "boundary": boundary,
                "summary": f"截至第{boundary}章，主角的承诺与代价持续升级。",
                "character_state": {"林序": "继续推进主线"},
            },
        )

    async def _extend(self, deps: EpisodeDeps) -> None:
        context = await self.gateway.invoke(deps, "novel_context")
        current = int(context["planned_through"])
        target = int(context["target"]["target_chapters"])
        planned = min(target, current + 3)
        await self.gateway.invoke(
            deps,
            "revise_outline",
            {
                "planned_through": planned,
                "outline_extension": [
                    f"第{number}章兑现此前代价" for number in range(current + 1, planned + 1)
                ],
            },
        )

    async def _complete(self, deps: EpisodeDeps) -> None:
        await self.gateway.invoke(deps, "complete_book", {"audit_passed": True})
