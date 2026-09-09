from __future__ import annotations

import asyncio

import pytest
from app.authoring.context import ContextBudgetManager, EpisodeMessage, RestorePack
from app.authoring.context.pydantic_history import PydanticHistoryCompactor
from app.authoring.domain.models import (
    AuthoringProfileSnapshot,
    Instruction,
    InstructionKind,
    Phase,
    WorkerRole,
)
from app.authoring.errors import ContextCompactionError
from pydantic import ValidationError
from pydantic_ai.messages import ModelMessage, ModelRequest, UserPromptPart


def _instruction() -> Instruction:
    return Instruction(
        project_id="p1",
        worker=WorkerRole.WRITER,
        kind=InstructionKind.WRITE_CHAPTER,
        logical_target="chapter:2",
        expected_phase=Phase.WRITING,
        terminal_postcondition="chapter_committed",
        reason_code="test",
        fact_version="v1",
    )


def test_small_window_forces_summary_and_restores_mandatory_facts() -> None:
    async def exercise() -> None:
        profile = AuthoringProfileSnapshot(
            profile_id="tiny",
            provider_protocol="fake",
            model_id="fake",
            context_window=8_330,
            max_output_tokens=80,
        )
        messages = [
            EpisodeMessage(kind="user", content="old context " * 120),
            EpisodeMessage(kind="assistant", content="checkpoint one is durable"),
            EpisodeMessage(kind="assistant", content="continue from the current chapter plan"),
            EpisodeMessage(kind="tool_call", pair_id="a", content="draft_chapter"),
            EpisodeMessage(kind="tool_result", pair_id="a", content="draft result", committed=True),
        ]
        restore = RestorePack(
            instruction=_instruction(),
            chapter_plan="chapter two plan",
            current_outline="chapter two outcome",
            canon={"hero": "kept the promise"},
            review_tasks=("make the cost concrete",),
            successful_checkpoints=("draft_chapter",),
            authorization_boundary="chapter:2 only",
        )

        async def summarize(_messages: object, _budget: int) -> str:
            return "Previous prose was drafted and its durable checkpoint exists."

        result = await ContextBudgetManager(safety_margin=20).compact(
            messages, profile, restore, summarizer=summarize
        )

        assert result.tokens_after < result.tokens_before
        assert "llm_summary_and_restore" in result.strategies
        assert result.messages[-1].kind == "restore"
        assert "chapter:2 only" in result.messages[-1].content

    asyncio.run(exercise())


def test_pydantic_history_uses_independent_llm_summary_after_store_summary() -> None:
    async def exercise() -> None:
        profile = AuthoringProfileSnapshot(
            profile_id="summary",
            provider_protocol="fake",
            model_id="fake",
            context_window=10_100,
            max_output_tokens=300,
        )
        messages: list[ModelMessage] = [
            ModelRequest(parts=[UserPromptPart("old history " * 2_000)])
        ]
        called = 0

        async def summarize(_messages: list[ModelMessage], _threshold: int) -> str:
            nonlocal called
            called += 1
            return "Independent bounded summary."

        restore = RestorePack(
            instruction=_instruction(),
            chapter_plan="plan",
            current_outline="outline",
            canon={"hero": "promise"},
            successful_checkpoints=(),
            authorization_boundary="chapter:2",
        )
        result = await PydanticHistoryCompactor(ContextBudgetManager(safety_margin=100)).compact(
            messages,
            profile,
            restore,
            stored_summary="oversized store summary " * 1_000,
            summarizer=summarize,
        )
        assert called == 1
        assert "independent_llm_summary" in result.strategies
        assert result.tokens_after < result.tokens_before

    asyncio.run(exercise())


def test_independent_summary_failure_is_a_typed_compaction_failure() -> None:
    async def exercise() -> None:
        profile = AuthoringProfileSnapshot(
            profile_id="summary-failure",
            provider_protocol="fake",
            model_id="fake",
            context_window=10_100,
            max_output_tokens=300,
        )
        messages: list[ModelMessage] = [
            ModelRequest(parts=[UserPromptPart("old history " * 2_000)])
        ]
        restore = RestorePack(
            instruction=_instruction(),
            chapter_plan="plan",
            current_outline="outline",
            canon={"hero": "promise"},
            successful_checkpoints=(),
            authorization_boundary="chapter:2",
        )

        async def fail_summary(_messages: list[ModelMessage], _threshold: int) -> str:
            raise ConnectionError("summary provider failed")

        with pytest.raises(ContextCompactionError, match="summarizer failed"):
            await PydanticHistoryCompactor(ContextBudgetManager(safety_margin=100)).compact(
                messages,
                profile,
                restore,
                stored_summary="oversized store summary " * 1_000,
                summarizer=fail_summary,
            )

    asyncio.run(exercise())


def test_restore_pack_rejects_missing_canon() -> None:
    with pytest.raises(ValidationError, match="canon"):
        RestorePack(
            instruction=_instruction(),
            chapter_plan="plan",
            current_outline="outline",
            canon={},
            successful_checkpoints=(),
            authorization_boundary="chapter:2",
        )


def test_persisted_summary_keeps_restore_pack_and_unpaired_history_is_rejected() -> None:
    async def exercise() -> None:
        profile = AuthoringProfileSnapshot(
            profile_id="tiny",
            provider_protocol="fake",
            model_id="fake",
            context_window=8_330,
            max_output_tokens=80,
        )
        restore = RestorePack(
            instruction=_instruction(),
            chapter_plan="plan",
            current_outline="outline",
            canon={"hero": "promise"},
            successful_checkpoints=("draft_chapter",),
            authorization_boundary="chapter:2",
        )
        paired = [
            EpisodeMessage(kind="user", content="old " * 300),
            EpisodeMessage(kind="tool_call", pair_id="a", content="draft_chapter"),
            EpisodeMessage(kind="tool_result", pair_id="a", content="saved", committed=True),
        ]
        result = await ContextBudgetManager(safety_margin=20).compact(
            paired, profile, restore, stored_summary="durable chapter facts"
        )
        assert "persisted_summary" in result.strategies
        assert result.messages[-1].kind == "restore"
        assert "chapter:2" in result.messages[-1].content

        with pytest.raises(ContextCompactionError, match="not paired"):
            await ContextBudgetManager().compact(
                [EpisodeMessage(kind="tool_call", pair_id="dangling", content="call")],
                profile.model_copy(update={"context_window": 16_384}),
                restore,
            )

    asyncio.run(exercise())
