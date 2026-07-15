from __future__ import annotations

import json
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from pathlib import Path

from app.harness.agents.evaluator import persist_evaluation_views
from app.harness.agents.models import EvaluationRecord
from app.harness.loops.book import BookDirectionSynthesis
from app.schemas.book_revisions import (
    BookRevisionApprovalRequest,
    BookRevisionSourceLoop,
    BookRevisionState,
)
from app.schemas.projects import ProjectMetadata
from app.schemas.setup import BookDirectionCandidate, BookDirectionReview
from app.storage.file_lock import exclusive_file_lock
from app.storage.json_files import read_json
from app.storage.setup import read_setup_state
from app.storage.transactions import commit_file_transaction, recover_file_transactions


class BookRevisionConflict(ValueError):
    pass


def read_latest_book_revision(project_path: Path) -> BookRevisionState | None:
    payload = read_json(_latest_path(project_path), default=None)
    if payload is None:
        return None
    return BookRevisionState.model_validate(payload)


def read_pending_book_revision(project_path: Path) -> BookRevisionState | None:
    state = read_latest_book_revision(project_path)
    if state is None or state.status != "awaiting_approval":
        return None
    return state


def read_approved_book_revision_with_pending_downstream(
    project_path: Path,
) -> BookRevisionState | None:
    state = read_latest_book_revision(project_path)
    if (
        state is None
        or state.status != "approved"
        or state.downstream_status != "pending"
    ):
        return None
    return state


def save_book_revision_candidate(
    project_path: Path,
    *,
    route_id: str,
    base_book_version: int,
    source_loop: BookRevisionSourceLoop,
    source_artifact: str,
    source_candidate_run_id: str | None,
    summary: str,
    contract_field: str,
    committed_evidence_locator: str,
    impossibility_reason: str,
    synthesis: BookDirectionSynthesis,
    evaluation: EvaluationRecord,
    review: BookDirectionReview,
    profile_id: str,
) -> BookRevisionState:
    if evaluation.result.outcome != "pass" or not review.commit_allowed:
        raise ValueError("Only a passing Book revision candidate may await approval.")

    with _book_revision_lock(project_path):
        recover_file_transactions(project_path)
        current = read_latest_book_revision(project_path)
        if current is not None and current.status == "awaiting_approval":
            raise BookRevisionConflict(
                "Another Book revision candidate is already awaiting approval."
            )
        book_state = _read_book_state(project_path)
        current_version = _required_int(book_state, "version")
        if current_version != base_book_version:
            raise BookRevisionConflict(
                "Book contract changed before the revision candidate could be stored."
            )

        direction_version = _int_or_default(book_state, "book_direction_version", 1) + 1
        if evaluation.candidate_revision != direction_version:
            raise BookRevisionConflict(
                "Book revision evaluation does not match the next direction version."
            )
        revision_id = f"revision-{direction_version:04d}-{route_id.removeprefix('route-')[:8]}"
        root = Path("book") / "revisions" / revision_id
        direction_path = root / "candidate_direction.md"
        constraints_path = root / "candidate_constraints.json"
        title_suggestions_path = root / "candidate_titles.json"
        rolling_plan_path = root / "candidate_outline.md"
        evaluation_path = root / "evaluation.json"
        review_path = root / "review.md"
        verification_path = root / "verification.json"
        state_path = root / "state.json"

        candidate = BookDirectionCandidate(
            revision=direction_version,
            direction_markdown=synthesis.direction_markdown,
            constraints=synthesis.constraints,
            confirmed_decision_coverage=synthesis.confirmed_decision_coverage,
            recommended_titles=synthesis.recommended_titles,
            rolling_plan_markdown=synthesis.rolling_plan_markdown,
            review=review,
            direction_path=direction_path.as_posix(),
            constraints_path=constraints_path.as_posix(),
            title_suggestions_path=title_suggestions_path.as_posix(),
            rolling_plan_path=rolling_plan_path.as_posix(),
            verification_path=verification_path.as_posix(),
            profile_id=profile_id,
            model_snapshot=synthesis.model_snapshot,
            review_model_snapshot=evaluation.evaluator_model_snapshot,
        )
        state = BookRevisionState(
            revision_id=revision_id,
            route_id=route_id,
            base_book_version=base_book_version,
            target_book_version=base_book_version + 1,
            source_loop=source_loop,
            source_artifact=source_artifact,
            source_candidate_run_id=source_candidate_run_id,
            summary=summary,
            contract_field=contract_field,
            committed_evidence_locator=committed_evidence_locator,
            impossibility_reason=impossibility_reason,
            candidate=candidate,
            evaluation_id=evaluation.evaluation_id,
            evaluation_path=evaluation_path.as_posix(),
            review_path=review_path.as_posix(),
            verification_path=verification_path.as_posix(),
        )
        persist_evaluation_views(
            project_path,
            evaluation,
            evaluation_path=evaluation_path.as_posix(),
            review_path=review_path.as_posix(),
            verification_path=verification_path.as_posix(),
        )
        state_document = _json_document(state.model_dump(mode="json"))
        commit_file_transaction(
            project_path,
            kind=f"book-revision-candidate-{revision_id}",
            files={
                direction_path.as_posix(): synthesis.direction_markdown.rstrip() + "\n",
                constraints_path.as_posix(): _json_document(
                    {
                        "schema_version": 1,
                        "candidate": True,
                        "base_book_version": base_book_version,
                        "confirmed_decision_coverage": [
                            item.model_dump(mode="json")
                            for item in synthesis.confirmed_decision_coverage
                        ],
                        **synthesis.constraints.model_dump(mode="json"),
                    }
                ),
                title_suggestions_path.as_posix(): _json_document(
                    {
                        "schema_version": 1,
                        "candidate": True,
                        "recommended_titles": [
                            item.model_dump(mode="json")
                            for item in synthesis.recommended_titles
                        ],
                    }
                ),
                rolling_plan_path.as_posix(): synthesis.rolling_plan_markdown.rstrip()
                + "\n",
                state_path.as_posix(): state_document,
                _latest_relative(): state_document,
            },
        )
        return state


def approve_book_revision(
    project_path: Path,
    request: BookRevisionApprovalRequest,
) -> BookRevisionState:
    with _book_revision_lock(project_path):
        with exclusive_file_lock(project_path / ".project.lock"):
            recover_file_transactions(project_path)
            state = read_latest_book_revision(project_path)
            if state is None:
                raise FileNotFoundError("No Book revision candidate is awaiting approval.")
            if state.status != "awaiting_approval":
                raise BookRevisionConflict("The Book revision is no longer awaiting approval.")
            if state.revision_id != request.revision_id:
                raise BookRevisionConflict("The requested Book revision is stale.")
            if state.base_book_version != request.expected_base_book_version:
                raise BookRevisionConflict("The expected Book contract version is stale.")
            if not state.candidate.approval_allowed:
                raise ValueError("The Book revision evaluation does not permit approval.")
            if not read_setup_state(project_path).approved:
                raise ValueError("Initial Book setup must remain approved before revision.")

            book_state = _read_book_state(project_path)
            current_version = _required_int(book_state, "version")
            if current_version != state.base_book_version:
                raise BookRevisionConflict(
                    "The approved Book contract changed after this candidate was evaluated."
                )
            metadata_payload = read_json(project_path / "project.json", default=None)
            if metadata_payload is None:
                raise FileNotFoundError("Project metadata is missing.")
            metadata = ProjectMetadata.model_validate(metadata_payload)
            if metadata.run_status in {"running", "pause_requested"}:
                raise BookRevisionConflict(
                    "Book revision approval requires a stopped Harness checkpoint."
                )
            now = datetime.now(UTC)
            updated = state.model_copy(
                update={
                    "status": "approved",
                    "approved_at": now,
                    "downstream_status": (
                        "pending" if metadata.active_arc_id is not None else "not_required"
                    ),
                },
                deep=True,
            )
            candidate = updated.candidate
            next_book_state = {
                **book_state,
                "schema_version": max(_int_or_default(book_state, "schema_version", 1), 2),
                "version": updated.target_book_version,
                "setup_approved": True,
                "approved_at": now.isoformat(),
                "book_direction_version": candidate.revision,
                "approved_direction_path": "book/direction.md",
                "approved_constraints_path": "book/constraints.json",
                "rolling_plan_path": "book/outline.md",
                "confirmed_decisions": candidate.constraints.confirmed,
                "must_preserve": candidate.constraints.must_preserve,
                "must_avoid": candidate.constraints.must_avoid,
                "creative_freedoms": candidate.constraints.creative_freedoms,
                "open_decisions": candidate.constraints.open_decisions,
                "current_strategy": "rolling_story_arc_planning",
                "source_book_revision_id": updated.revision_id,
            }
            metadata.updated_at = now
            state_document = _json_document(updated.model_dump(mode="json"))
            commit_file_transaction(
                project_path,
                kind=f"book-revision-approval-{updated.revision_id}",
                files={
                    "book/direction.md": candidate.direction_markdown.rstrip() + "\n",
                    "book/settings.md": candidate.direction_markdown.rstrip() + "\n",
                    "book/outline.md": candidate.rolling_plan_markdown.rstrip() + "\n",
                    "book/constraints.json": _json_document(
                        {
                            "schema_version": 1,
                            "candidate": False,
                            "approved_at": now.isoformat(),
                            "source_book_revision_id": updated.revision_id,
                            "source_candidate_revision": candidate.revision,
                            "confirmed_decision_coverage": [
                                item.model_dump(mode="json")
                                for item in candidate.confirmed_decision_coverage
                            ],
                            **candidate.constraints.model_dump(mode="json"),
                        }
                    ),
                    "book/state.json": _json_document(next_book_state),
                    (
                        Path("book") / "revisions" / updated.revision_id / "state.json"
                    ).as_posix(): state_document,
                    _latest_relative(): state_document,
                    "project.json": _json_document(metadata.model_dump(mode="json")),
                },
            )
            return updated


def mark_book_revision_downstream_completed(
    project_path: Path,
    revision_id: str,
    *,
    artifact_paths: list[str],
) -> BookRevisionState:
    with _book_revision_lock(project_path):
        state = read_latest_book_revision(project_path)
        if state is None or state.revision_id != revision_id:
            raise BookRevisionConflict("The Book revision downstream marker is stale.")
        if state.status != "approved":
            raise BookRevisionConflict("An unapproved Book revision cannot update downstream work.")
        updated = state.model_copy(
            update={
                "downstream_status": "completed",
                "downstream_artifact_paths": list(dict.fromkeys(artifact_paths)),
            },
            deep=True,
        )
        document = _json_document(updated.model_dump(mode="json"))
        commit_file_transaction(
            project_path,
            kind=f"book-revision-downstream-{revision_id}",
            files={
                (
                    Path("book") / "revisions" / revision_id / "state.json"
                ).as_posix(): document,
                _latest_relative(): document,
            },
        )
        return updated


def _read_book_state(project_path: Path) -> dict[str, object]:
    payload = read_json(project_path / "book" / "state.json", default=None)
    if not isinstance(payload, dict):
        raise FileNotFoundError("Approved Book state is missing.")
    return payload


def _required_int(payload: dict[str, object], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int):
        raise ValueError(f"Book state field {key} must be an integer.")
    return value


def _int_or_default(payload: dict[str, object], key: str, default: int) -> int:
    value = payload.get(key, default)
    return value if isinstance(value, int) else default


def _book_revision_lock(project_path: Path) -> AbstractContextManager[None]:
    return exclusive_file_lock(project_path / "book" / ".revision.lock")


def _latest_path(project_path: Path) -> Path:
    return project_path / _latest_relative()


def _latest_relative() -> str:
    return "book/revisions/latest.json"


def _json_document(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
