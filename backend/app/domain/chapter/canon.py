from __future__ import annotations

import hashlib
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.agents.contracts import ChapterObservationResult, SemanticCanonProposal
from app.domain.commands import canonical_json_bytes

CanonCategory = Literal["characters", "relationships", "world_facts", "foreshadowing"]
CANON_CATEGORIES: tuple[CanonCategory, ...] = (
    "characters",
    "relationships",
    "world_facts",
    "foreshadowing",
)


class ExactEvidenceSpan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str
    start: int = Field(ge=0)
    end: int = Field(ge=0)


class BoundCanonOperation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    operation_id: str
    entity_id: str
    category: CanonCategory
    operation: Literal["add", "update", "resolve"]
    subject: str
    semantic_change: str
    evidence: ExactEvidenceSpan


class BoundCanonPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_id: Literal["chapter-canon-patch-v1"] = "chapter-canon-patch-v1"
    chapter_id: str
    operations: list[BoundCanonOperation]


class CanonEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    entity_id: str
    subject: str
    semantic_state: str
    resolved: bool
    source_chapter_id: str
    evidence: ExactEvidenceSpan


class AppliedCanonPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    categories: dict[CanonCategory, list[CanonEntry]]
    changed_categories: tuple[CanonCategory, ...]

    @property
    def changed(self) -> bool:
        return bool(self.changed_categories)


class CanonPatchBindingError(ValueError):
    """A semantic proposal cannot be deterministically bound to exact prose evidence."""


class CanonPatchConflictError(ValueError):
    """A semantic operation conflicts with the frozen Canon baseline."""


def bind_canon_patch(
    *,
    chapter_id: str,
    prose: str,
    observations: ChapterObservationResult,
) -> BoundCanonPatch:
    operations = [
        _bind_operation(chapter_id=chapter_id, prose=prose, proposal=proposal)
        for proposal in observations.canon_proposals
    ]
    operation_ids = [operation.operation_id for operation in operations]
    if len(operation_ids) != len(set(operation_ids)):
        raise CanonPatchBindingError("Canon proposals contain a duplicate semantic operation.")
    return BoundCanonPatch(chapter_id=chapter_id, operations=operations)


def apply_canon_patch(
    *,
    chapter_id: str,
    current: dict[CanonCategory, list[CanonEntry]],
    patch: BoundCanonPatch,
) -> AppliedCanonPatch:
    if patch.chapter_id != chapter_id:
        raise CanonPatchConflictError("Canon patch is bound to another Chapter.")
    result = {category: list(current[category]) for category in CANON_CATEGORIES}
    changed: set[CanonCategory] = set()
    for operation in patch.operations:
        entries = result[operation.category]
        matching_index = next(
            (
                index
                for index, entry in enumerate(entries)
                if entry.entity_id == operation.entity_id
            ),
            None,
        )
        if operation.operation == "add":
            if matching_index is None:
                entries.append(_entry(chapter_id, operation, resolved=False))
                changed.add(operation.category)
            else:
                existing = entries[matching_index]
                if existing.semantic_state != operation.semantic_change or existing.resolved:
                    raise CanonPatchConflictError(
                        f"Canon subject {operation.subject!r} already exists; use update."
                    )
        elif operation.operation == "update":
            if matching_index is None:
                raise CanonPatchConflictError(
                    f"Canon subject {operation.subject!r} does not exist for update."
                )
            existing = entries[matching_index]
            if existing.resolved:
                raise CanonPatchConflictError(
                    f"Resolved Canon subject {operation.subject!r} cannot be updated."
                )
            if existing.semantic_state != operation.semantic_change:
                entries[matching_index] = _entry(chapter_id, operation, resolved=False)
                changed.add(operation.category)
        else:
            if matching_index is None:
                raise CanonPatchConflictError(
                    f"Canon subject {operation.subject!r} does not exist for resolve."
                )
            existing = entries[matching_index]
            if not existing.resolved or existing.semantic_state != operation.semantic_change:
                entries[matching_index] = _entry(chapter_id, operation, resolved=True)
                changed.add(operation.category)
        entries.sort(key=lambda item: item.entity_id)
    return AppliedCanonPatch(
        categories=result,
        changed_categories=tuple(
            category for category in CANON_CATEGORIES if category in changed
        ),
    )


def canon_entity_id(category: CanonCategory, subject: str) -> str:
    normalized = " ".join(subject.casefold().split())
    return hashlib.sha256(f"{category}\0{normalized}".encode()).hexdigest()


def canon_manifest_fingerprint(ref_ids: dict[CanonCategory, str]) -> str:
    return hashlib.sha256(
        canonical_json_bytes(
            {
                "schema": "canon-manifest-v1",
                **{f"{category}_ref_id": ref_ids[category] for category in CANON_CATEGORIES},
            }
        )
    ).hexdigest()


def _bind_operation(
    *,
    chapter_id: str,
    prose: str,
    proposal: SemanticCanonProposal,
) -> BoundCanonOperation:
    start, end = _find_exact_span(prose, proposal.evidence_hint)
    evidence = ExactEvidenceSpan(text=prose[start:end], start=start, end=end)
    entity_id = canon_entity_id(proposal.category, proposal.subject)
    operation_id = hashlib.sha256(
        canonical_json_bytes(
            {
                "chapter_id": chapter_id,
                "entity_id": entity_id,
                "operation": proposal.operation,
                "semantic_change": proposal.semantic_change,
                "evidence_start": start,
                "evidence_end": end,
            }
        )
    ).hexdigest()
    return BoundCanonOperation(
        operation_id=operation_id,
        entity_id=entity_id,
        category=proposal.category,
        operation=proposal.operation,
        subject=proposal.subject.strip(),
        semantic_change=proposal.semantic_change.strip(),
        evidence=evidence,
    )


def _find_exact_span(prose: str, hint: str) -> tuple[int, int]:
    stripped = hint.strip()
    direct = prose.find(stripped)
    if direct >= 0:
        return direct, direct + len(stripped)
    tokens = stripped.split()
    if not tokens:
        raise CanonPatchBindingError("Canon evidence hint is blank.")
    pattern = r"\s+".join(re.escape(token) for token in tokens)
    match = re.search(pattern, prose, flags=re.IGNORECASE)
    if match is None:
        raise CanonPatchBindingError(
            "Canon evidence hint cannot be bound to an exact span in the frozen prose."
        )
    return match.start(), match.end()


def _entry(
    chapter_id: str,
    operation: BoundCanonOperation,
    *,
    resolved: bool,
) -> CanonEntry:
    return CanonEntry(
        entity_id=operation.entity_id,
        subject=operation.subject,
        semantic_state=operation.semantic_change,
        resolved=resolved,
        source_chapter_id=chapter_id,
        evidence=operation.evidence,
    )
