from __future__ import annotations

import pytest

from app.agents.contracts import ChapterObservationResult, SemanticCanonProposal
from app.domain.chapter.canon import (
    CANON_CATEGORIES,
    CanonCategory,
    CanonPatchConflictError,
    apply_canon_patch,
    bind_canon_patch,
)


def _proposal(*, hint: str) -> SemanticCanonProposal:
    return SemanticCanonProposal(
        category="world_facts",
        subject="Mutable testimony",
        semantic_change="Written testimony can change while a witness watches.",
        resolved=False,
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

    assert patch.schema_id == "chapter-canon-patch-v3"
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


@pytest.mark.parametrize(
    ("category", "subject"),
    [
        ("world_facts", "锁闭数据室内显露的半张机械潮位纸"),
        ("foreshadowing", "程雾当场死亡旧叙事的裂缝"),
        ("characters", "顾向潮的旧案立场与权限"),
    ],
)
def test_new_semantic_subjects_are_harness_upserts(
    category: CanonCategory,
    subject: str,
) -> None:
    proposal = SemanticCanonProposal(
        category=category,
        subject=subject,
        semantic_change="本章建立了新的、受证据边界约束的语义事实。",
        resolved=False,
        evidence_hint="本章中的行动与物证共同支持这一有限事实。",
    )
    patch = bind_canon_patch(
        chapter_id="chapter-new-subject",
        prose="人物核对行动与物证，并只记录能够确认的有限事实。",
        observations=_observations(proposal),
    )

    applied = apply_canon_patch(
        chapter_id="chapter-new-subject",
        chapter_baseline_id="chapter-baseline-new-subject",
        prose_ref_id="prose-ref-new-subject",
        current={canon_category: [] for canon_category in CANON_CATEGORIES},
        patch=patch,
    )

    entries = applied.categories[proposal.category]
    assert len(entries) == 1
    assert entries[0].subject == subject
    assert entries[0].semantic_state == proposal.semantic_change
    assert entries[0].source_chapter_baseline_id == "chapter-baseline-new-subject"
    assert entries[0].source_prose_ref_id == "prose-ref-new-subject"


def test_same_semantic_subject_is_replaced_by_harness() -> None:
    first = _proposal(hint="The first account is recorded.")
    initial_patch = bind_canon_patch(
        chapter_id="chapter-1",
        prose="The first account is recorded.",
        observations=_observations(first),
    )
    initial = apply_canon_patch(
        chapter_id="chapter-1",
        chapter_baseline_id="chapter-baseline-1",
        prose_ref_id="prose-ref-1",
        current={category: [] for category in CANON_CATEGORIES},
        patch=initial_patch,
    )
    changed = SemanticCanonProposal(
        category="world_facts",
        subject=first.subject,
        semantic_change="The testimony is now independently corroborated.",
        resolved=True,
        evidence_hint="An analogue record independently corroborates the testimony.",
    )
    changed_patch = bind_canon_patch(
        chapter_id="chapter-2",
        prose="An analogue record independently corroborates the testimony.",
        observations=_observations(changed),
    )

    applied = apply_canon_patch(
        chapter_id="chapter-2",
        chapter_baseline_id="chapter-baseline-2",
        prose_ref_id="prose-ref-2",
        current=initial.categories,
        patch=changed_patch,
    )

    entries = applied.categories["world_facts"]
    assert len(entries) == 1
    assert entries[0].semantic_state == changed.semantic_change
    assert entries[0].resolved
    assert entries[0].source_chapter_id == "chapter-2"
    assert entries[0].source_chapter_baseline_id == "chapter-baseline-2"
    assert entries[0].source_prose_ref_id == "prose-ref-2"


def test_evidence_only_correction_rebinds_only_the_entry_from_its_source_baseline() -> None:
    proposal = _proposal(hint="The original derived evidence hint.")
    initial = apply_canon_patch(
        chapter_id="chapter-1",
        chapter_baseline_id="chapter-baseline-1",
        prose_ref_id="prose-ref-1",
        current={category: [] for category in CANON_CATEGORIES},
        patch=bind_canon_patch(
            chapter_id="chapter-1",
            prose="The page visibly changes in Mara's hands.",
            observations=_observations(proposal),
        ),
    )
    corrected = proposal.model_copy(
        update={"evidence_hint": "Mara directly witnesses the page change."}
    )
    corrected_patch = bind_canon_patch(
        chapter_id="chapter-1",
        prose="The page visibly changes in Mara's hands.",
        observations=_observations(corrected),
    )

    ordinary_repeat = apply_canon_patch(
        chapter_id="chapter-1",
        chapter_baseline_id="chapter-baseline-2",
        prose_ref_id="prose-ref-1",
        current=initial.categories,
        patch=corrected_patch,
    )
    assert not ordinary_repeat.changed
    assert (
        ordinary_repeat.categories["world_facts"][0].source_chapter_baseline_id
        == "chapter-baseline-1"
    )

    evidence_correction = apply_canon_patch(
        chapter_id="chapter-1",
        chapter_baseline_id="chapter-baseline-2",
        prose_ref_id="prose-ref-1",
        current=initial.categories,
        patch=corrected_patch,
        replace_evidence_for_chapter_baseline_id="chapter-baseline-1",
    )
    corrected_entry = evidence_correction.categories["world_facts"][0]
    assert evidence_correction.changed
    assert corrected_entry.evidence.hint == corrected.evidence_hint
    assert corrected_entry.source_chapter_baseline_id == "chapter-baseline-2"
    assert corrected_entry.source_prose_ref_id == "prose-ref-1"

    wrong_source = apply_canon_patch(
        chapter_id="chapter-1",
        chapter_baseline_id="chapter-baseline-3",
        prose_ref_id="prose-ref-1",
        current=initial.categories,
        patch=corrected_patch,
        replace_evidence_for_chapter_baseline_id="another-baseline",
    )
    assert not wrong_source.changed


def test_evidence_only_correction_cannot_rewind_a_descendant_canon_state() -> None:
    initial_proposal = _proposal(hint="The first Chapter establishes mutable testimony.")
    initial = apply_canon_patch(
        chapter_id="chapter-1",
        chapter_baseline_id="chapter-baseline-1",
        prose_ref_id="prose-ref-1",
        current={category: [] for category in CANON_CATEGORIES},
        patch=bind_canon_patch(
            chapter_id="chapter-1",
            prose="The testimony changes while Mara watches.",
            observations=_observations(initial_proposal),
        ),
    )
    descendant_proposal = initial_proposal.model_copy(
        update={
            "semantic_change": "The mutable testimony is independently corroborated.",
            "resolved": True,
            "evidence_hint": "A later Chapter confirms the alteration independently.",
        }
    )
    descendant = apply_canon_patch(
        chapter_id="chapter-2",
        chapter_baseline_id="chapter-baseline-2",
        prose_ref_id="prose-ref-2",
        current=initial.categories,
        patch=bind_canon_patch(
            chapter_id="chapter-2",
            prose="An analogue copy independently confirms the alteration.",
            observations=_observations(descendant_proposal),
        ),
    )
    corrected_ancestor = initial_proposal.model_copy(
        update={
            "semantic_change": "The testimony appears mutable to Mara.",
            "evidence_hint": "The corrected historical index preserves Mara's limited view.",
        }
    )

    applied = apply_canon_patch(
        chapter_id="chapter-1",
        chapter_baseline_id="chapter-baseline-3",
        prose_ref_id="prose-ref-1",
        current=descendant.categories,
        patch=bind_canon_patch(
            chapter_id="chapter-1",
            prose="The testimony changes while Mara watches.",
            observations=_observations(corrected_ancestor),
        ),
        replace_evidence_for_chapter_baseline_id="chapter-baseline-1",
    )

    assert not applied.changed
    preserved = applied.categories["world_facts"][0]
    assert preserved.semantic_state == descendant_proposal.semantic_change
    assert preserved.source_chapter_baseline_id == "chapter-baseline-2"
    assert preserved.source_prose_ref_id == "prose-ref-2"


def test_incompatible_assertions_for_one_subject_are_rejected() -> None:
    first = _proposal(hint="The first account is recorded.")
    conflicting = first.model_copy(
        update={
            "semantic_change": "The same testimony is proven false.",
            "evidence_hint": "A second account contradicts it.",
        }
    )
    patch = bind_canon_patch(
        chapter_id="chapter-conflict",
        prose="Two records disagree.",
        observations=_observations(first, conflicting),
    )

    with pytest.raises(
        CanonPatchConflictError,
        match="multiple incompatible semantic assertions",
    ):
        apply_canon_patch(
            chapter_id="chapter-conflict",
            chapter_baseline_id="chapter-baseline-conflict",
            prose_ref_id="prose-ref-conflict",
            current={category: [] for category in CANON_CATEGORIES},
            patch=patch,
        )
