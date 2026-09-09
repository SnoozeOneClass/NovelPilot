from __future__ import annotations

from typing import Any, Literal

from pydantic_ai import Agent, ModelRetry, RunContext
from pydantic_ai.models import Model

from app.authoring.domain.models import WorkerRole
from app.authoring.tools.contracts import (
    AuditFoundationInput,
    ChapterFactsInput,
    CheckConsistencyInput,
    CommitChapterInput,
    CompleteBookInput,
    DraftChapterInput,
    EditChapterInput,
    PlanChapterInput,
    ReadChapterInput,
    ResolveOutlineFeedbackInput,
    ReviewDimensions,
    ReviseOutlineInput,
    SaveBookInput,
    SaveFoundationInput,
    SaveReviewInput,
    SaveSummaryInput,
)
from app.authoring.tools.gateway import EpisodeDeps, ToolGateway

ROLE_INSTRUCTIONS: dict[WorkerRole, str] = {
    WorkerRole.ARCHITECT: (
        "You are the Architect in a fully automatic novel-writing runtime. Use only your Tools. "
        "Persist a coherent premise, characters, world rules, rolling outline, or final audit as "
        "required by the supplied Instruction. Never ask the user a creative question."
    ),
    WorkerRole.WRITER: (
        "You are the Writer. Work only on the authorized chapter. Read persisted facts, plan, "
        "draft or edit, check consistency, then call commit_chapter. Never invent another target."
    ),
    WorkerRole.EDITOR: (
        "You are the Editor. Review only the authorized committed boundary using seven dimensions "
        "and short evidence, or save its aggregate summary. Persist the required terminal Tool."
    ),
    WorkerRole.ARBITER: (
        "You are a read-only failure arbiter. Return a bounded structured recommendation from facts."
    ),
}


def _gateway(ctx: RunContext[EpisodeDeps]) -> ToolGateway:
    return ToolGateway(ctx.deps.store)


async def novel_context(ctx: RunContext[EpisodeDeps]) -> dict[str, Any]:
    """Load compact authoritative project, planning, Canon, review, and chapter facts."""
    return await _gateway(ctx).invoke(ctx.deps, "novel_context")


async def read_chapter(ctx: RunContext[EpisodeDeps], chapter_number: int) -> dict[str, Any]:
    """Read committed or saved chapter content. An unstarted authorized new chapter has no content.

    Returns status='not_started' only for that empty new target; create its plan and draft next.
    Missing rewrite or review targets are errors. Saved Writer plans/drafts are included when present.
    """
    return await _gateway(ctx).invoke(
        ctx.deps, "read_chapter", ReadChapterInput(chapter_number=chapter_number)
    )


async def save_book(ctx: RunContext[EpisodeDeps], title: str) -> dict[str, Any]:
    """Save the reader-facing book title."""
    return await _gateway(ctx).invoke(ctx.deps, "save_book", SaveBookInput(title=title))


async def save_foundation(
    ctx: RunContext[EpisodeDeps],
    premise: str,
    compass: str,
    characters: list[dict[str, str]],
    world: dict[str, str],
    outline: list[str],
    planned_through: int,
) -> dict[str, Any]:
    """Persist the initial novel foundation and detailed rolling outline."""
    return await _gateway(ctx).invoke(
        ctx.deps,
        "save_foundation",
        SaveFoundationInput(
            premise=premise,
            compass=compass,
            characters=characters,
            world=world,
            outline=outline,
            planned_through=planned_through,
        ),
    )


async def audit_foundation(
    ctx: RunContext[EpisodeDeps], passed: bool, issues: list[str]
) -> dict[str, Any]:
    """Terminalize foundation creation only when the stored foundation is coherent."""
    return await _gateway(ctx).invoke(
        ctx.deps, "audit_foundation", AuditFoundationInput(passed=passed, issues=issues)
    )


async def revise_outline(
    ctx: RunContext[EpisodeDeps], planned_through: int, outline_extension: list[str]
) -> dict[str, Any]:
    """Extend the detailed rolling outline without exceeding the frozen target."""
    return await _gateway(ctx).invoke(
        ctx.deps,
        "revise_outline",
        ReviseOutlineInput(planned_through=planned_through, outline_extension=outline_extension),
    )


async def resolve_outline_feedback(
    ctx: RunContext[EpisodeDeps], resolutions: list[str]
) -> dict[str, Any]:
    """Record deterministic resolutions for outline audit findings."""
    return await _gateway(ctx).invoke(
        ctx.deps,
        "resolve_outline_feedback",
        ResolveOutlineFeedbackInput(resolutions=resolutions),
    )


async def complete_book(ctx: RunContext[EpisodeDeps], audit_passed: bool) -> dict[str, Any]:
    """Terminalize the book after the frozen target and final audit pass."""
    return await _gateway(ctx).invoke(
        ctx.deps, "complete_book", CompleteBookInput(audit_passed=audit_passed)
    )


async def plan_chapter(
    ctx: RunContext[EpisodeDeps], chapter_number: int, plan: str
) -> dict[str, Any]:
    """Persist the authorized chapter plan."""
    return await _gateway(ctx).invoke(
        ctx.deps,
        "plan_chapter",
        PlanChapterInput(chapter_number=chapter_number, plan=plan),
    )


async def draft_chapter(
    ctx: RunContext[EpisodeDeps], chapter_number: int, content: str
) -> dict[str, Any]:
    """Persist a non-terminal draft for the authorized chapter."""
    return await _gateway(ctx).invoke(
        ctx.deps,
        "draft_chapter",
        DraftChapterInput(chapter_number=chapter_number, content=content),
    )


async def edit_chapter(
    ctx: RunContext[EpisodeDeps], chapter_number: int, content: str
) -> dict[str, Any]:
    """Persist a non-terminal edited version for the authorized chapter."""
    return await _gateway(ctx).invoke(
        ctx.deps,
        "edit_chapter",
        EditChapterInput(chapter_number=chapter_number, content=content),
    )


async def check_consistency(
    ctx: RunContext[EpisodeDeps], chapter_number: int, passed: bool, issues: list[str]
) -> dict[str, Any]:
    """Persist the required consistency gate before chapter commit."""
    return await _gateway(ctx).invoke(
        ctx.deps,
        "check_consistency",
        CheckConsistencyInput(chapter_number=chapter_number, passed=passed, issues=issues),
    )


async def commit_chapter(
    ctx: RunContext[EpisodeDeps],
    chapter_number: int,
    title: str,
    content: str,
    facts: ChapterFactsInput,
) -> dict[str, Any]:
    """Atomically commit正文, chapter facts, Canon, checkpoint, and event."""
    return await _gateway(ctx).invoke(
        ctx.deps,
        "commit_chapter",
        CommitChapterInput(
            chapter_number=chapter_number,
            title=title,
            content=content,
            facts=facts,
        ),
    )


async def save_review(
    ctx: RunContext[EpisodeDeps],
    boundary: int,
    verdict: Literal["accept", "polish", "rewrite"],
    dimensions: ReviewDimensions,
    evidence: list[str],
    chapters: list[int],
) -> dict[str, Any]:
    """Persist a seven-dimensional boundary review and bounded rewrite targets."""
    return await _gateway(ctx).invoke(
        ctx.deps,
        "save_review",
        SaveReviewInput(
            boundary=boundary,
            verdict=verdict,
            dimensions=dimensions,
            evidence=evidence,
            chapters=chapters,
        ),
    )


async def save_arc_summary(
    ctx: RunContext[EpisodeDeps],
    boundary: int,
    summary: str,
    character_state: dict[str, str],
) -> dict[str, Any]:
    """Persist the arc summary and character snapshot at the authorized boundary."""
    return await _gateway(ctx).invoke(
        ctx.deps,
        "save_arc_summary",
        SaveSummaryInput(boundary=boundary, summary=summary, character_state=character_state),
    )


async def save_volume_summary(
    ctx: RunContext[EpisodeDeps],
    boundary: int,
    summary: str,
    character_state: dict[str, str],
) -> dict[str, Any]:
    """Persist a volume summary when the active planning boundary requires one."""
    return await _gateway(ctx).invoke(
        ctx.deps,
        "save_volume_summary",
        SaveSummaryInput(boundary=boundary, summary=summary, character_state=character_state),
    )


ROLE_TOOLS: dict[WorkerRole, tuple[Any, ...]] = {
    WorkerRole.ARCHITECT: (
        novel_context,
        save_book,
        save_foundation,
        audit_foundation,
        revise_outline,
        resolve_outline_feedback,
        complete_book,
    ),
    WorkerRole.WRITER: (
        novel_context,
        read_chapter,
        plan_chapter,
        draft_chapter,
        edit_chapter,
        check_consistency,
        commit_chapter,
    ),
    WorkerRole.EDITOR: (
        novel_context,
        read_chapter,
        save_review,
        save_arc_summary,
        save_volume_summary,
    ),
    WorkerRole.ARBITER: (novel_context,),
}


def build_authoring_agent(model: Model, role: WorkerRole) -> Agent[EpisodeDeps, str]:
    agent = Agent(
        model,
        deps_type=EpisodeDeps,
        output_type=str,
        instructions=ROLE_INSTRUCTIONS[role],
        tools=ROLE_TOOLS[role],
        retries={"tools": 1, "output": 2},
        max_concurrency=1,
        defer_model_check=True,
    )

    @agent.output_validator
    async def require_terminal_tool(ctx: RunContext[EpisodeDeps], output: str) -> str:
        if ctx.deps.terminal_tool not in ctx.deps.successful_tools:
            raise ModelRetry(f"You must call {ctx.deps.terminal_tool} before returning final text.")
        return output

    return agent
