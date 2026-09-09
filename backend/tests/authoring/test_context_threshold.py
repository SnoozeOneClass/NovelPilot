from __future__ import annotations

import asyncio
from dataclasses import replace

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
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

THRESHOLD_CASES = [
    (1_000_000, 65_536, 850_000),
    (128_000, 65_536, 62_208),
    (32_768, 1_024, 24_768),
]


def _profile(window: int, max_output: int) -> AuthoringProfileSnapshot:
    return AuthoringProfileSnapshot(
        profile_id="threshold-test",
        provider_protocol="fake",
        model_id="fake",
        context_window=window,
        max_output_tokens=max_output,
    )


def _restore_pack() -> RestorePack:
    return RestorePack(
        instruction=Instruction(
            project_id="p1",
            worker=WorkerRole.WRITER,
            kind=InstructionKind.WRITE_CHAPTER,
            logical_target="chapter:2",
            expected_phase=Phase.WRITING,
            terminal_postcondition="chapter_committed",
            reason_code="threshold-test",
            fact_version="v1",
        ),
        chapter_plan="Keep the promise and pay its cost.",
        current_outline="The keeper chooses.",
        canon={"keeper": "has promised to stay"},
        review_tasks=("show the cost",),
        successful_checkpoints=("draft_chapter",),
        authorization_boundary="chapter:2 only",
    )


@pytest.mark.parametrize(
    ("window", "max_output", "expected"),
    [
        *THRESHOLD_CASES,
        (1_000_000, 200_000, 799_744),
        (100_003, 1_024, 85_002),
    ],
)
def test_input_threshold_reserves_proportional_floor_and_output_headroom(
    window: int, max_output: int, expected: int
) -> None:
    assert ContextBudgetManager().input_threshold(_profile(window, max_output)) == expected


@pytest.mark.parametrize(
    ("window", "max_output"),
    [(8_000, 256), (7_999, 256), (32_768, 32_512), (32_768, 32_600)],
)
def test_nonpositive_input_budget_fails_closed_in_both_compactors(
    window: int, max_output: int
) -> None:
    async def exercise() -> None:
        manager = ContextBudgetManager()
        profile = _profile(window, max_output)
        restore = _restore_pack()
        with pytest.raises(ContextCompactionError, match="no input token budget"):
            manager.input_threshold(profile)
        with pytest.raises(ContextCompactionError, match="no input token budget"):
            await manager.compact([], profile, restore, stored_summary="durable facts")
        with pytest.raises(ContextCompactionError, match="no input token budget"):
            await PydanticHistoryCompactor(manager).compact(
                [], profile, restore, stored_summary="durable facts"
            )

    asyncio.run(exercise())


@pytest.mark.parametrize(("window", "max_output", "threshold"), THRESHOLD_CASES)
@pytest.mark.parametrize("offset", [-1, 0, 1])
def test_neutral_compaction_triggers_at_threshold_and_preserves_restore_and_tool_pair(
    window: int, max_output: int, threshold: int, offset: int
) -> None:
    async def exercise() -> None:
        manager = ContextBudgetManager()
        profile = _profile(window, max_output)
        restore = _restore_pack()
        messages = [
            EpisodeMessage(kind="user", content="x"),
            EpisodeMessage(kind="tool_call", pair_id="draft-2", content="draft_chapter"),
            EpisodeMessage(
                kind="tool_result", pair_id="draft-2", content="draft saved", committed=True
            ),
        ]
        expected_input = threshold + offset
        padding_tokens = expected_input - manager.estimate_tokens(messages)
        messages[0] = messages[0].model_copy(update={"content": "x" * (1 + padding_tokens * 3)})
        assert manager.estimate_tokens(messages) == expected_input

        result = await manager.compact(
            messages, profile, restore, stored_summary="The previous draft is saved."
        )

        assert result.tokens_before == expected_input
        assert result.tokens_after == manager.estimate_tokens(result.messages)
        if offset < 0:
            assert result.messages == tuple(messages)
            assert result.strategies == ()
            assert result.tokens_after == expected_input
        else:
            assert "persisted_summary" in result.strategies
            assert result.tokens_after < threshold
            assert result.messages[1:3] == tuple(messages[1:])
            assert result.messages[-1] == EpisodeMessage(kind="restore", content=restore.render())

    asyncio.run(exercise())


@pytest.mark.parametrize(("window", "max_output", "threshold"), THRESHOLD_CASES)
@pytest.mark.parametrize("offset", [-1, 0, 1])
@pytest.mark.parametrize("overhead_tokens", [0, 500])
def test_pydantic_compaction_counts_overhead_once_at_the_same_threshold(
    window: int, max_output: int, threshold: int, offset: int, overhead_tokens: int
) -> None:
    async def exercise() -> None:
        compactor = PydanticHistoryCompactor(ContextBudgetManager())
        profile = _profile(window, max_output)
        restore = _restore_pack()
        system = SystemPromptPart("Finish the authorized chapter using its durable facts.")
        prompt = UserPromptPart("x")
        request = ModelRequest(parts=[system, prompt])
        messages: list[ModelMessage] = [
            request,
            ModelResponse(
                parts=[ToolCallPart("draft_chapter", {"chapter_number": 2}, tool_call_id="draft-2")]
            ),
            ModelRequest(
                parts=[ToolReturnPart("draft_chapter", "draft saved", tool_call_id="draft-2")]
            ),
        ]
        expected_input = threshold + offset
        padding_tokens = expected_input - overhead_tokens - compactor._tokens(messages)
        messages[0] = replace(
            request, parts=[system, replace(prompt, content="x" * (1 + padding_tokens * 3))]
        )
        assert compactor._tokens(messages) + overhead_tokens == expected_input

        result = await compactor.compact(
            messages,
            profile,
            restore,
            stored_summary="The previous draft is saved.",
            overhead_tokens=overhead_tokens,
        )

        assert result.tokens_before == expected_input
        assert result.tokens_after == compactor._tokens(result.messages) + overhead_tokens
        if offset < 0:
            assert result.messages == messages
            assert result.strategies == ()
            assert result.tokens_after == expected_input
        else:
            assert "pydantic_store_summary" in result.strategies
            assert "restore_pack" in result.strategies
            assert result.tokens_after < threshold
            assert result.messages[1:] == messages[1:]
            restored_request = result.messages[0]
            assert isinstance(restored_request, ModelRequest)
            assert restored_request.parts[0] == system
            restored_prompt = restored_request.parts[1]
            assert isinstance(restored_prompt, UserPromptPart)
            assert restore.render() in restored_prompt.content

    asyncio.run(exercise())
