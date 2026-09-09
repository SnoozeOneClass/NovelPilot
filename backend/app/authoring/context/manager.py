from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.authoring.domain.models import AuthoringProfileSnapshot, Instruction
from app.authoring.errors import ContextCompactionError


class EpisodeMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["system", "user", "assistant", "tool_call", "tool_result", "restore"]
    content: str
    pair_id: str | None = None
    committed: bool = False
    rereadable_ref: str | None = None


class RestorePack(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    instruction: Instruction
    chapter_plan: str
    current_outline: str
    next_outline: str | None = None
    canon: dict[str, object]
    review_tasks: tuple[str, ...] = ()
    review_evidence: dict[str, object] = Field(default_factory=dict)
    relevant_history: tuple[dict[str, object], ...] = ()
    successful_checkpoints: tuple[str, ...]
    authorization_boundary: str
    project_context: dict[str, object] = Field(default_factory=dict)
    current_work: dict[str, object] = Field(default_factory=dict)
    next_action: str = ""
    resume_rule: str = ""

    @classmethod
    def from_material(cls, instruction: Instruction, material: Mapping[str, Any]) -> RestorePack:
        steps = {
            "create_foundation": ("save_book", "save_foundation", "audit_foundation"),
            "write_chapter": (
                "plan_chapter",
                "draft_chapter",
                "check_consistency",
                "commit_chapter",
            ),
            "rewrite_chapter": ("edit_chapter", "check_consistency", "commit_chapter"),
            "review_boundary": ("save_review",),
            "save_summary": ("save_arc_summary",),
            "extend_outline": ("revise_outline",),
            "complete_book": ("complete_book",),
        }
        saved = material["successful_checkpoints"]
        current_work = material.get("current_work", {})
        resume_rule = (
            "Continue from saved work. Skip successful steps and reuse their exact persisted facts. "
            "Do not regenerate or resubmit an already saved plan, draft, or committed payload."
            if saved
            else "No steps have been saved for this instruction. Begin next_action using the supplied story facts. "
            "A not_started plan or draft has no saved content to recover."
        )
        if any(current_work.get(name) for name in ("reread", "plan_reread", "committed_reread")):
            resume_rule += (
                " Saved material is available through the explicit read_chapter references in current_work. "
                "Read those saved facts when needed for next_action."
            )
        return cls(
            instruction=instruction,
            chapter_plan=material["chapter_plan"],
            current_outline=material["current_outline"],
            next_outline=material["next_outline"],
            canon=material["canon"],
            review_tasks=material["review_tasks"],
            review_evidence=material["review_evidence"],
            relevant_history=material["relevant_history"],
            successful_checkpoints=saved,
            authorization_boundary=material["authorization_boundary"],
            project_context=material.get("project_context", {}),
            current_work=current_work,
            next_action=next(
                (step for step in steps[instruction.kind] if step not in saved), "done"
            ),
            resume_rule=resume_rule,
        )

    @model_validator(mode="after")
    def required_facts_are_present(self) -> RestorePack:
        required = {
            "chapter_plan": self.chapter_plan,
            "current_outline": self.current_outline,
            "authorization_boundary": self.authorization_boundary,
        }
        missing = sorted(name for name, value in required.items() if not value.strip())
        if missing or not self.canon:
            if not self.canon:
                missing.append("canon")
            raise ValueError(f"restore pack is missing mandatory facts: {', '.join(missing)}")
        return self

    def render(self) -> str:
        return self.model_dump_json(indent=2, exclude_defaults=True)


class CompactionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    messages: tuple[EpisodeMessage, ...]
    tokens_before: int
    tokens_after: int
    strategies: tuple[str, ...]


SummaryFunction = Callable[[Sequence[EpisodeMessage], int], Awaitable[str]]


class ContextBudgetManager:
    """Framework-neutral bounded-history transformer.

    Token counting is conservative and deterministic for local routing.  A real
    Provider adapter may inject its exact tokenizer without changing this API.
    """

    def __init__(self, safety_margin: int = 256, max_failures: int = 2) -> None:
        if safety_margin < 0 or max_failures < 1:
            raise ValueError("invalid context budget policy")
        self.safety_margin = safety_margin
        self.max_failures = max_failures
        self._failures = 0

    def input_threshold(self, profile: AuthoringProfileSnapshot) -> int:
        """Keep proportional headroom, an 8k reserve, and the full output budget."""
        window = profile.context_window
        proportional_ceiling = window * 85 // 100
        reserve = max(
            window - proportional_ceiling,
            8_000,
            profile.max_output_tokens + self.safety_margin,
        )
        threshold = window - reserve
        if threshold <= 0:
            raise ContextCompactionError("profile leaves no input token budget")
        return threshold

    @staticmethod
    def estimate_tokens(messages: Sequence[EpisodeMessage]) -> int:
        return sum(max(1, (len(message.content) + 2) // 3) + 4 for message in messages)

    async def compact(
        self,
        messages: Sequence[EpisodeMessage],
        profile: AuthoringProfileSnapshot,
        restore_pack: RestorePack,
        stored_summary: str | None = None,
        summarizer: SummaryFunction | None = None,
    ) -> CompactionResult:
        threshold = self.input_threshold(profile)
        self._validate_pairs(messages)
        before = self.estimate_tokens(messages)
        if before < threshold:
            return CompactionResult(
                messages=tuple(messages),
                tokens_before=before,
                tokens_after=before,
                strategies=(),
            )
        try:
            result = list(messages)
            strategies: list[str] = []

            # Keep the most recent call/result pair byte-for-byte.  Older
            # committed results retain a durable invocation reference.
            tool_results = [
                index for index, item in enumerate(result) if item.kind == "tool_result"
            ]
            for index in tool_results[:-1]:
                item = result[index]
                if item.committed and len(item.content) > 96:
                    result[index] = item.model_copy(
                        update={
                            "content": f"[committed tool result {item.pair_id}; reload from Store]"
                        }
                    )
            strategies.append("committed_tool_result_reference")
            if self._fits(result, threshold):
                return self._finish(result, before, strategies)

            for index, item in enumerate(result[:-2]):
                if item.rereadable_ref and len(item.content) > 96:
                    result[index] = item.model_copy(
                        update={"content": f"[large text omitted; reread {item.rereadable_ref}]"}
                    )
            strategies.append("rereadable_text_reference")
            if self._fits(result, threshold):
                return self._finish(result, before, strategies)

            if stored_summary:
                keep = self._recent_complete_messages(result)
                restore = EpisodeMessage(kind="restore", content=restore_pack.render())
                result = [
                    EpisodeMessage(
                        kind="system", content=f"Persisted facts summary:\n{stored_summary}"
                    ),
                    *keep,
                    restore,
                ]
                strategies.append("persisted_summary")
                if self._fits(result, threshold):
                    return self._finish(result, before, strategies)

            if summarizer is None:
                raise ContextCompactionError(
                    "context still exceeds budget and no summarizer is available"
                )
            summary = (await summarizer(tuple(result), threshold)).strip()
            if not summary:
                raise ContextCompactionError("context summarizer returned an empty result")
            recent = self._recent_complete_messages(result)
            restore = EpisodeMessage(kind="restore", content=restore_pack.render())
            result = [
                EpisodeMessage(kind="system", content=f"Episode summary:\n{summary}"),
                *recent,
                restore,
            ]
            strategies.append("llm_summary_and_restore")
            self._validate_pairs(result)
            if not self._fits(result, threshold):
                raise ContextCompactionError(
                    "summary and restore pack still exceed the input budget"
                )
            self._failures = 0
            return self._finish(result, before, strategies)
        except (ContextCompactionError, ValueError) as error:
            self._failures += 1
            if self._failures >= self.max_failures:
                raise ContextCompactionError(
                    f"context compaction failed {self._failures} times; pause the run"
                ) from error
            raise ContextCompactionError(str(error)) from error

    def _fits(self, messages: Sequence[EpisodeMessage], threshold: int) -> bool:
        self._validate_pairs(messages)
        return self.estimate_tokens(messages) < threshold

    def _finish(
        self, messages: Sequence[EpisodeMessage], before: int, strategies: Sequence[str]
    ) -> CompactionResult:
        self._validate_pairs(messages)
        after = self.estimate_tokens(messages)
        self._failures = 0
        return CompactionResult(
            messages=tuple(messages),
            tokens_before=before,
            tokens_after=after,
            strategies=tuple(strategies),
        )

    @staticmethod
    def _recent_complete_messages(messages: Sequence[EpisodeMessage]) -> list[EpisodeMessage]:
        start = max(0, len(messages) - 2)
        while True:
            result_pair_ids = {
                item.pair_id for item in messages[start:] if item.kind == "tool_result"
            }
            earlier_calls = [
                index
                for index in range(start)
                if messages[index].kind == "tool_call"
                and messages[index].pair_id in result_pair_ids
            ]
            if not earlier_calls:
                break
            start = min(earlier_calls)
        return list(messages[start:])

    @staticmethod
    def _validate_pairs(messages: Sequence[EpisodeMessage]) -> None:
        calls: dict[str, int] = {}
        results: dict[str, int] = {}
        for item in messages:
            if item.kind == "tool_call":
                if not item.pair_id:
                    raise ContextCompactionError("tool call is missing pair_id")
                calls[item.pair_id] = calls.get(item.pair_id, 0) + 1
            elif item.kind == "tool_result":
                if not item.pair_id:
                    raise ContextCompactionError("tool result is missing pair_id")
                results[item.pair_id] = results.get(item.pair_id, 0) + 1
        if calls != results:
            raise ContextCompactionError("Tool call/result history is not paired")
