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


class CanonEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    hint: str = Field(
        description="Model-authored semantic evidence rationale; never an authority locator."
    )
    exact_span: ExactEvidenceSpan | None = Field(
        default=None,
        description=(
            "Optional exact prose span derived by the Harness only when the hint has one "
            "unambiguous match."
        ),
    )


class BoundCanonOperation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    operation_id: str
    entity_id: str
    category: CanonCategory
    operation: Literal["upsert"] = "upsert"
    subject: str
    semantic_change: str
    resolved: bool
    evidence: CanonEvidence


class BoundCanonPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_id: Literal["chapter-canon-patch-v3"] = "chapter-canon-patch-v3"
    chapter_id: str
    operations: list[BoundCanonOperation]


class CanonEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    entity_id: str
    subject: str
    semantic_state: str
    resolved: bool
    source_chapter_id: str
    source_chapter_baseline_id: str
    source_prose_ref_id: str
    evidence: CanonEvidence


class AppliedCanonPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    categories: dict[CanonCategory, list[CanonEntry]]
    changed_categories: tuple[CanonCategory, ...]

    @property
    def changed(self) -> bool:
        return bool(self.changed_categories)


class CanonPatchConflictError(ValueError):
    """A semantic operation conflicts with the frozen Canon baseline."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        operation: BoundCanonOperation | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.operation = operation


def bind_canon_patch(
    *,
    chapter_id: str,
    prose: str,
    observations: ChapterObservationResult,
) -> BoundCanonPatch:
    operations: list[BoundCanonOperation] = []
    seen_operation_ids: set[str] = set()
    for proposal in observations.canon_proposals:
        operation = _bind_operation(
            chapter_id=chapter_id,
            prose=prose,
            proposal=proposal,
        )
        if operation.operation_id in seen_operation_ids:
            continue
        seen_operation_ids.add(operation.operation_id)
        operations.append(operation)
    return BoundCanonPatch(chapter_id=chapter_id, operations=operations)


def apply_canon_patch(
    *,
    chapter_id: str,
    chapter_baseline_id: str,
    prose_ref_id: str,
    current: dict[CanonCategory, list[CanonEntry]],
    patch: BoundCanonPatch,
    replace_evidence_for_chapter_baseline_id: str | None = None,
) -> AppliedCanonPatch:
    if patch.chapter_id != chapter_id:
        raise CanonPatchConflictError(
            "Canon patch is bound to another Chapter.",
            code="canon_patch_chapter_mismatch",
        )
    result = {category: list(current[category]) for category in CANON_CATEGORIES}
    changed: set[CanonCategory] = set()
    seen_by_entity: dict[str, BoundCanonOperation] = {}
    for operation in patch.operations:
        previous = seen_by_entity.get(operation.entity_id)
        if previous is not None:
            if (
                previous.semantic_change != operation.semantic_change
                or previous.resolved != operation.resolved
            ):
                raise CanonPatchConflictError(
                    (
                        f"Canon subject {operation.subject!r} has multiple incompatible "
                        "semantic assertions in one Chapter."
                    ),
                    code="canon_subject_assertion_conflict",
                    operation=operation,
                )
            continue
        seen_by_entity[operation.entity_id] = operation
        entries = result[operation.category]
        matching_index = next(
            (
                index
                for index, entry in enumerate(entries)
                if entry.entity_id == operation.entity_id
            ),
            None,
        )
        if matching_index is None:
            entries.append(
                _entry(
                    chapter_id,
                    chapter_baseline_id,
                    prose_ref_id,
                    operation,
                )
            )
            changed.add(operation.category)
        else:
            existing = entries[matching_index]
            if (
                replace_evidence_for_chapter_baseline_id is not None
                and existing.source_chapter_baseline_id
                != replace_evidence_for_chapter_baseline_id
            ):
                # Evidence correction is historical-index maintenance, not a
                # license to rewind a later Chapter's current Canon projection.
                continue
            if (
                existing.semantic_state != operation.semantic_change
                or existing.resolved != operation.resolved
                or (
                    replace_evidence_for_chapter_baseline_id is not None
                    and existing.source_chapter_baseline_id
                    == replace_evidence_for_chapter_baseline_id
                    and (
                        existing.evidence != operation.evidence
                        or existing.source_chapter_baseline_id
                        != chapter_baseline_id
                        or existing.source_prose_ref_id != prose_ref_id
                    )
                )
            ):
                entries[matching_index] = _entry(
                    chapter_id,
                    chapter_baseline_id,
                    prose_ref_id,
                    operation,
                )
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
    evidence_hint = proposal.evidence_hint.strip()
    semantic_change = proposal.semantic_change.strip()
    evidence = CanonEvidence(
        hint=evidence_hint,
        exact_span=_find_unique_exact_span(prose, evidence_hint),
    )
    entity_id = canon_entity_id(proposal.category, proposal.subject)
    operation_id = hashlib.sha256(
        canonical_json_bytes(
            {
                "chapter_id": chapter_id,
                "entity_id": entity_id,
                "semantic_change": semantic_change,
                "resolved": proposal.resolved,
            }
        )
    ).hexdigest()
    return BoundCanonOperation(
        operation_id=operation_id,
        entity_id=entity_id,
        category=proposal.category,
        subject=proposal.subject.strip(),
        semantic_change=semantic_change,
        resolved=proposal.resolved,
        evidence=evidence,
    )


def _find_unique_exact_span(prose: str, hint: str) -> ExactEvidenceSpan | None:
    stripped = hint.strip()
    tokens = stripped.split()
    if not tokens:
        return None
    pattern = r"\s+".join(re.escape(token) for token in tokens)
    matches = list(re.finditer(pattern, prose, flags=re.IGNORECASE))
    if len(matches) != 1:
        return None
    match = matches[0]
    return ExactEvidenceSpan(
        text=prose[match.start() : match.end()],
        start=match.start(),
        end=match.end(),
    )


def _entry(
    chapter_id: str,
    chapter_baseline_id: str,
    prose_ref_id: str,
    operation: BoundCanonOperation,
) -> CanonEntry:
    return CanonEntry(
        entity_id=operation.entity_id,
        subject=operation.subject,
        semantic_state=operation.semantic_change,
        resolved=operation.resolved,
        source_chapter_id=chapter_id,
        source_chapter_baseline_id=chapter_baseline_id,
        source_prose_ref_id=prose_ref_id,
        evidence=operation.evidence,
    )
