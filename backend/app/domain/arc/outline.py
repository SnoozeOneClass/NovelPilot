from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from app.agents.contracts import ArcChapterOutlineEntry, ArcPlanProposal, JsonValue
from app.store.arcs import ArcBaselineRecord


class ArcOutlineProjectionError(RuntimeError):
    """The persisted Arc outline lineage cannot be projected unambiguously."""

    code = "arc_outline_projection_invalid"

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.invariant = reason_code


@dataclass(frozen=True, slots=True)
class ResolvedArcOutlineEntry:
    book_ordinal: int
    arc_ordinal: int
    assignment: ArcChapterOutlineEntry
    source_arc_baseline_id: str
    source_arc_baseline_version: int
    source_plan_ref_id: str


def resolve_outline_entry(
    *,
    baseline: ArcBaselineRecord,
    plan: ArcPlanProposal,
    arc_ordinal: int,
    book_ordinal: int | None = None,
) -> ResolvedArcOutlineEntry:
    """Resolve one Arc ordinal through one immutable baseline interval."""

    offset = arc_ordinal - baseline.planned_after_arc_chapter_count - 1
    if offset < 0 or offset >= len(plan.chapter_outline):
        raise ArcOutlineProjectionError(
            "outline_offset_out_of_range",
            (
                f"Arc Chapter ordinal {arc_ordinal} is outside baseline "
                f"v{baseline.baseline_version}'s planned future interval."
            ),
        )
    expected_book_ordinal = (
        baseline.planned_after_cumulative_chapter_count + offset + 1
    )
    if book_ordinal is not None and book_ordinal != expected_book_ordinal:
        raise ArcOutlineProjectionError(
            "book_arc_ordinal_mismatch",
            (
                f"Book Chapter ordinal {book_ordinal} does not match Arc ordinal "
                f"{arc_ordinal} in baseline v{baseline.baseline_version}."
            ),
        )
    return ResolvedArcOutlineEntry(
        book_ordinal=expected_book_ordinal,
        arc_ordinal=arc_ordinal,
        assignment=plan.chapter_outline[offset],
        source_arc_baseline_id=baseline.id,
        source_arc_baseline_version=baseline.baseline_version,
        source_plan_ref_id=baseline.plan_ref_id,
    )


def arc_contract_summary(plan: ArcPlanProposal) -> dict[str, JsonValue]:
    """Return only the stage-level contract needed by Chapter semantic work."""

    return {
        "title": plan.title,
        "desired_state_transition": plan.desired_state_transition.model_dump(
            mode="json"
        ),
        "character_obligations": list(plan.character_obligations),
        "foreshadowing_obligations": list(plan.foreshadowing_obligations),
        "prohibitions": list(plan.prohibitions),
        "closure_signals": [
            signal.model_dump(mode="json") for signal in plan.closure_signals
        ],
    }


def render_chapter_outline_window(
    *,
    contract_plan: ArcPlanProposal,
    current: ResolvedArcOutlineEntry,
    next_entry: ResolvedArcOutlineEntry | None,
    include_next: bool,
) -> tuple[str, str]:
    """Render a canonical semantic window and its deterministic content hash."""

    document: dict[str, Any] = {
        "arc_contract_summary": arc_contract_summary(contract_plan),
        "current": current.assignment.model_dump(mode="json"),
    }
    if include_next:
        document["next"] = (
            None
            if next_entry is None
            else next_entry.assignment.model_dump(mode="json")
        )
    rendered = json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return rendered, hashlib.sha256(rendered.encode("utf-8")).hexdigest()
