from __future__ import annotations

from app.authoring.domain.models import (
    Instruction,
    InstructionKind,
    Phase,
    RunStatus,
    StateSnapshot,
    WorkerRole,
)
from app.authoring.errors import StateCorruptionError

ARC_SIZE = 3


_WORKERS: dict[InstructionKind, WorkerRole] = {
    InstructionKind.CREATE_FOUNDATION: WorkerRole.ARCHITECT,
    InstructionKind.WRITE_CHAPTER: WorkerRole.WRITER,
    InstructionKind.REWRITE_CHAPTER: WorkerRole.WRITER,
    InstructionKind.REVIEW_BOUNDARY: WorkerRole.EDITOR,
    InstructionKind.SAVE_SUMMARY: WorkerRole.EDITOR,
    InstructionKind.EXTEND_OUTLINE: WorkerRole.ARCHITECT,
    InstructionKind.COMPLETE_BOOK: WorkerRole.ARCHITECT,
}

_TERMINALS: dict[InstructionKind, str] = {
    InstructionKind.CREATE_FOUNDATION: "foundation_audited",
    InstructionKind.WRITE_CHAPTER: "chapter_committed",
    InstructionKind.REWRITE_CHAPTER: "rewrite_completed",
    InstructionKind.REVIEW_BOUNDARY: "review_saved",
    InstructionKind.SAVE_SUMMARY: "arc_summary_saved",
    InstructionKind.EXTEND_OUTLINE: "outline_extended",
    InstructionKind.COMPLETE_BOOK: "book_completed",
}


def _instruction(
    snapshot: StateSnapshot,
    kind: InstructionKind,
    target: str,
    reason: str,
    phase: Phase | None = None,
    fact_version: str | None = None,
    instruction_key: str = "",
) -> Instruction:
    return Instruction(
        project_id=snapshot.project_id,
        worker=_WORKERS[kind],
        kind=kind,
        logical_target=target,
        expected_phase=phase or snapshot.phase,
        required_facts=(snapshot.fact_version,),
        terminal_postcondition=_TERMINALS[kind],
        reason_code=reason,
        fact_version=fact_version or snapshot.fact_version,
        instruction_key=instruction_key,
    )


def route(snapshot: StateSnapshot) -> Instruction | None:
    """Choose the next Worker from persisted facts only.

    The function intentionally performs no IO and emits no logs or events.
    """

    if snapshot.status in {
        RunStatus.PAUSED,
        RunStatus.FAILURE_PAUSED,
        RunStatus.CANCELLED,
        RunStatus.COMPLETED,
    }:
        return None
    if snapshot.corrupted_reason:
        raise StateCorruptionError(snapshot.corrupted_reason)

    if snapshot.active_instruction_key is not None:
        if (
            snapshot.active_instruction_kind is None
            or snapshot.active_logical_target is None
            or snapshot.active_fact_version is None
        ):
            raise StateCorruptionError("active instruction identity is incomplete")
        return _instruction(
            snapshot,
            snapshot.active_instruction_kind,
            snapshot.active_logical_target,
            "resume_interrupted_instruction",
            fact_version=snapshot.active_fact_version,
            instruction_key=snapshot.active_instruction_key,
        )

    if not snapshot.foundation_present or not snapshot.foundation_audited:
        return _instruction(
            snapshot,
            InstructionKind.CREATE_FOUNDATION,
            "foundation",
            "foundation_missing_or_unaudited",
            phase=Phase.FOUNDATION,
        )

    if snapshot.pending_rewrites:
        chapter = snapshot.pending_rewrites[0]
        return _instruction(
            snapshot,
            InstructionKind.REWRITE_CHAPTER,
            f"chapter:{chapter}",
            "review_rewrite_pending",
            phase=Phase.WRITING,
        )

    boundary_due = snapshot.chapter_count > snapshot.reviewed_through and (
        snapshot.chapter_count % ARC_SIZE == 0
        or snapshot.chapter_count >= snapshot.target.target_chapters
    )
    if boundary_due:
        return _instruction(
            snapshot,
            InstructionKind.REVIEW_BOUNDARY,
            f"boundary:{snapshot.chapter_count}",
            "boundary_review_missing",
            phase=Phase.WRITING,
        )

    if snapshot.reviewed_through > snapshot.summarized_through:
        return _instruction(
            snapshot,
            InstructionKind.SAVE_SUMMARY,
            f"boundary:{snapshot.reviewed_through}",
            "reviewed_boundary_summary_missing",
            phase=Phase.WRITING,
        )

    if snapshot.chapter_count >= snapshot.target.target_chapters:
        return _instruction(
            snapshot,
            InstructionKind.COMPLETE_BOOK,
            "book",
            "frozen_target_reached",
            phase=Phase.FINALIZING,
        )

    if snapshot.chapter_count >= snapshot.planned_through:
        return _instruction(
            snapshot,
            InstructionKind.EXTEND_OUTLINE,
            f"chapter:{snapshot.chapter_count + 1}",
            "detailed_outline_exhausted",
            phase=Phase.WRITING,
        )

    return _instruction(
        snapshot,
        InstructionKind.WRITE_CHAPTER,
        f"chapter:{snapshot.chapter_count + 1}",
        "next_planned_chapter",
        phase=Phase.WRITING,
    )
