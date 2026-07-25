from __future__ import annotations

from app.agents.contracts import ChapterObservationResult, SemanticCanonProposal
from app.domain.chapter.canon import bind_canon_patch


def _proposal(*, hint: str) -> SemanticCanonProposal:
    return SemanticCanonProposal(
        category="world_facts",
        operation="add",
        subject="Mutable testimony",
        semantic_change="Written testimony can change while a witness watches.",
        evidence_hint=hint,
    )


def _observations(
    *proposals: SemanticCanonProposal,
) -> ChapterObservationResult:
    return ChapterObservationResult(
        summary="The chapter establishes that documentary evidence can mutate.",
        canon_proposals=list(proposals),
    )


def test_semantic_evidence_hint_does_not_require_a_prose_substring() -> None:
    hint = "The witness observes the document rewrite itself."
    patch = bind_canon_patch(
        chapter_id="chapter-1",
        prose="Blue ink spread into a confession that had not been there before.",
        observations=_observations(_proposal(hint=hint)),
    )

    assert patch.schema_id == "chapter-canon-patch-v2"
    assert len(patch.operations) == 1
    assert patch.operations[0].evidence.hint == hint
    assert patch.operations[0].evidence.exact_span is None


def test_harness_attaches_only_one_unambiguous_exact_span() -> None:
    unique = bind_canon_patch(
        chapter_id="chapter-1",
        prose="The blue ink changed\nwhile she watched.",
        observations=_observations(
            _proposal(hint="the blue ink changed while she watched")
        ),
    )
    span = unique.operations[0].evidence.exact_span
    assert span is not None
    assert span.text == "The blue ink changed\nwhile she watched"

    ambiguous = bind_canon_patch(
        chapter_id="chapter-2",
        prose="The bell rang. Later, the bell rang.",
        observations=_observations(_proposal(hint="the bell rang")),
    )
    assert ambiguous.operations[0].evidence.exact_span is None


def test_duplicate_semantic_canon_proposals_are_normalized() -> None:
    patch = bind_canon_patch(
        chapter_id="chapter-1",
        prose="The page changed in Mara's hands.",
        observations=_observations(
            _proposal(hint="The page changed"),
            _proposal(hint="Mara witnesses a mutable document"),
        ),
    )

    assert len(patch.operations) == 1
