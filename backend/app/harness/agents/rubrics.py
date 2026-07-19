import json
from hashlib import sha256
from typing import Any, cast

from app.harness.agents.models import (
    CandidateComponentName,
    CandidateKind,
    CandidateSnapshot,
    EvaluationRubricDimension,
    EvaluationRubricSnapshot,
)


_RUBRICS: dict[CandidateKind, EvaluationRubricSnapshot] = {
    "book_direction": EvaluationRubricSnapshot(
        version="book-direction-v3",
        candidate_kind="book_direction",
        dimensions=[
            EvaluationRubricDimension(
                dimension_id="confirmed_decision_coverage",
                instruction=(
                    "Verify that every confirmed user decision is covered and preserved "
                    "verbatim without adding repair guidance as a new decision."
                ),
            ),
            EvaluationRubricDimension(
                dimension_id="direction_constraint_coherence",
                instruction="Check internal coherence between direction and constraints.",
            ),
            EvaluationRubricDimension(
                dimension_id="title_reader_promise",
                instruction=(
                    "Check the required 3-5 title suggestion records against genre and reader "
                    "promise. When one formal title is already locked, the other structurally "
                    "required records are comparison/reference suggestions, not evidence that "
                    "the user must choose a title again."
                ),
            ),
            EvaluationRubricDimension(
                dimension_id="reveal_ending_feasibility",
                instruction="Check reveal pacing and ending feasibility.",
            ),
            EvaluationRubricDimension(
                dimension_id="downstream_planning_feasibility",
                instruction="Check whether Story Arc planning can implement this contract.",
            ),
        ],
    ),
    "story_arc": EvaluationRubricSnapshot(
        version="story-arc-v2",
        candidate_kind="story_arc",
        dimensions=[
            EvaluationRubricDimension(
                dimension_id="book_contract_alignment",
                instruction="Check compatibility with the approved Book contract.",
            ),
            EvaluationRubricDimension(
                dimension_id="canon_continuity",
                instruction="Check committed canon and prior-Chapter continuity.",
            ),
            EvaluationRubricDimension(
                dimension_id="causality_reveal_pacing",
                instruction="Check arc causality and reveal pacing.",
            ),
            EvaluationRubricDimension(
                dimension_id="chapter_count_feasibility",
                instruction="Check feasibility within the target Chapter count.",
            ),
            EvaluationRubricDimension(
                dimension_id="plan_summary_coherence",
                instruction="Check coherence between plan and change summary.",
            ),
        ],
    ),
    "chapter": EvaluationRubricSnapshot(
        version="chapter-candidate-v2",
        candidate_kind="chapter",
        dimensions=[
            EvaluationRubricDimension(
                dimension_id="upper_contract_alignment",
                instruction="Check Book and active Story Arc contract alignment.",
            ),
            EvaluationRubricDimension(
                dimension_id="chronology_physical_continuity",
                instruction="Check chronology, location, objects, and physical continuity.",
            ),
            EvaluationRubricDimension(
                dimension_id="pov_position_knowledge",
                instruction="Check point of view, character position, and knowledge boundaries.",
            ),
            EvaluationRubricDimension(
                dimension_id="candidate_component_alignment",
                instruction=(
                    "Check plan, draft, observations, and state patch for mutual alignment."
                ),
            ),
            EvaluationRubricDimension(
                dimension_id="evidence_and_canon_patch",
                instruction="Check evidence chains and canon patch correctness.",
            ),
            EvaluationRubricDimension(
                dimension_id="prose_and_chapter_function",
                instruction="Check prose completeness and the Chapter's intended function.",
            ),
        ],
    ),
}


def resolve_rubric(candidate_kind: CandidateKind) -> EvaluationRubricSnapshot:
    return _RUBRICS[candidate_kind].model_copy(deep=True)


def candidate_components(candidate: CandidateSnapshot) -> dict[CandidateComponentName, Any]:
    payload = candidate.model_dump(mode="python", exclude={"kind"})
    return cast(dict[CandidateComponentName, Any], payload)


def component_fingerprints(
    candidate: CandidateSnapshot,
) -> dict[CandidateComponentName, str]:
    components = candidate_components(candidate)
    return {
        name: _component_digest(_semantic_component_value(value))
        for name, value in components.items()
    }


def changed_components(
    before: dict[CandidateComponentName, str],
    after: dict[CandidateComponentName, str],
) -> list[CandidateComponentName]:
    if set(before) != set(after):
        raise ValueError("Candidate component sets do not match.")
    return sorted(name for name in before if before[name] != after[name])


def _component_digest(value: Any) -> str:
    if isinstance(value, str):
        encoded = value.encode("utf-8")
    else:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _semantic_component_value(value: Any) -> Any:
    """Remove Harness-derived metadata from semantic change fingerprints.

    Candidate identity is still bound by the complete EvaluationInput fingerprint. These
    per-component digests exist to decide which semantic artifacts changed during repair, so
    regenerated IDs, evidence spans, versions, and paths must not widen repair scope.
    """

    if isinstance(value, dict):
        return {
            key: _semantic_component_value(item)
            for key, item in value.items()
            if not _fingerprint_control_key(key)
        }
    if isinstance(value, list):
        return [_semantic_component_value(item) for item in value]
    return value


def _fingerprint_control_key(key: str) -> bool:
    normalized = key.casefold()
    if normalized in {
        "id",
        "schema_version",
        "status",
        "based_on",
        "file",
        "evidence",
        "evidence_quote",
        "evidence_quotes",
        "candidate_evidence",
        "requires_commit",
        "chapter_id",
        "arc_id",
        "expected_revision",
        "candidate_revision",
        "plan_revision",
        "draft_revision",
        "expected_version",
    }:
        return True
    return (
        normalized.startswith("based_on_")
        or normalized.endswith("_path")
        or normalized.endswith("_locator")
        or normalized.endswith("_fingerprint")
    )
