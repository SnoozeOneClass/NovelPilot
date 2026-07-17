from collections.abc import Callable
from pathlib import Path
from threading import Lock
from typing import Any

from fastapi import APIRouter, HTTPException

from app.harness.agents.evaluator import persist_evaluation_views
from app.harness.agents.events import project_agent_event
from app.harness.agents.loop_runners import AgentControlCheckpoint
from app.harness.agents.models import EvaluationRecord
from app.harness.run_control import (
    active_project_transition_lock,
    begin_active_runner,
    end_active_runner,
)
from app.harness.stream_progress import StreamProgressAccumulator
from app.harness.loops.book import (
    assemble_discussion_context,
    build_review_context_snapshot,
    continue_book_discussion,
    review_book_direction,
    synthesize_book_direction,
)
from app.llm.gateway import ChatChunk
from app.llm.profiles import get_active_profile
from app.llm.redaction import profile_secret_values, redact_profile_secrets
from app.schemas.events import EventStatus, HarnessEvent
from app.schemas.profiles import LlmProfile
from app.schemas.projects import ProjectMetadata
from app.schemas.setup import (
    SetupApprovalRequest,
    SetupStateDocument,
    SetupTurnRequest,
)
from app.storage import setup as setup_storage
from app.storage.events import append_event
from app.storage.profiles import load_profiles
from app.storage.projects import (
    ProjectReadOnlyError,
    ensure_creative_mutation_allowed,
    get_active_project_path,
    read_project_metadata,
)

router = APIRouter()
_setup_lock = Lock()


@router.get("/state", response_model=SetupStateDocument)
def get_setup_state() -> SetupStateDocument:
    project_path = _active_project_path()
    setup_storage.flush_pending_setup_events(project_path)
    try:
        return setup_storage.read_setup_state(project_path)
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/turn", response_model=SetupStateDocument)
def continue_setup_discussion(request: SetupTurnRequest) -> SetupStateDocument:
    with _setup_lock:
        project_path = _active_project_path()
        _ensure_setup_mutable(project_path)
        setup_storage.flush_pending_setup_events(project_path)
        metadata = read_project_metadata(project_path)
        state = setup_storage.read_setup_state(project_path)
        if state.approved:
            raise HTTPException(status_code=409, detail="Book direction is already approved.")
        profile = _active_profile_or_409()
        safe_message = redact_profile_secrets(request.message, profile)

        assembly = assemble_discussion_context(state, safe_message)
        snapshot = {
            **assembly.snapshot,
            "project_id": metadata.project_id,
            "turn": state.turn_count + 1,
            "profile_id": profile.id,
            "model": profile.model,
        }
        context_path = setup_storage.write_discussion_context_snapshot(
            project_path,
            turn=state.turn_count + 1,
            snapshot=snapshot,
        )
        _append_event(
            project_path,
            metadata,
            kind="book_discussion_context_assembled",
            action="assemble_book_discussion_context",
            status="completed",
            artifact_path=context_path,
            routing="call_book_discussion_model",
            message="Controlled context assembled for the next book discussion turn.",
            payload={"profile_id": profile.id, "turn": state.turn_count + 1},
        )
        _append_event(
            project_path,
            metadata,
            kind="atomic_action_started",
            action="continue_book_discussion",
            status="started",
            message="Book direction discussion model started.",
            payload={"profile_id": profile.id, "turn": state.turn_count + 1},
        )

        stream_callback = _setup_stream_callback(
            project_path,
            metadata,
            action="continue_book_discussion",
        )
        try:
            result = continue_book_discussion(
                profile,
                state,
                safe_message,
                assembly,
                stream_callback,
                on_event=_setup_agent_event_callback(
                    project_path,
                    metadata,
                    action="continue_book_discussion",
                ),
                on_tool_event=stream_callback,
            )
        except AgentControlCheckpoint as checkpoint:
            _raise_setup_control_checkpoint(
                project_path,
                metadata,
                checkpoint,
                action="continue_book_discussion",
            )
        except Exception as exc:
            reason = redact_profile_secrets(str(exc), profile)
            _append_event(
                project_path,
                metadata,
                kind="book_discussion_turn_failed",
                action="continue_book_discussion",
                status="failed",
                artifact_path=context_path,
                routing="retry_book_discussion_turn",
                message="Book direction discussion failed; no candidate state was advanced.",
                payload={"profile_id": profile.id, "reason": reason},
            )
            raise HTTPException(status_code=502, detail=reason) from exc

        try:
            updated = setup_storage.record_discussion_turn(
                project_path,
                state,
                user_message=safe_message,
                result=result,
                context_snapshot_path=context_path,
                profile_id=profile.id,
            )
        except setup_storage.SetupRevisionConflict as exc:
            _append_event(
                project_path,
                metadata,
                kind="book_discussion_stale_result_discarded",
                action="continue_book_discussion",
                status="failed",
                artifact_path=context_path,
                routing="reload_book_discussion",
                message="Book discussion state changed; the stale model result was discarded.",
                payload={"profile_id": profile.id, "expected_revision": state.revision},
            )
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        response_path = (Path(context_path).parent / "response.json").as_posix()
        _append_event(
            project_path,
            metadata,
            kind="book_direction_draft_updated",
            action="continue_book_discussion",
            status="completed",
            artifact_path="book/direction_draft.md",
            routing="continue_discussion",
            message="The complete candidate Book Direction draft was updated.",
            payload={
                "profile_id": profile.id,
                "model_snapshot": result.model_snapshot,
                "turn": updated.turn_count,
                "candidate": True,
            },
        )
        _append_event(
            project_path,
            metadata,
            kind="book_discussion_turn_completed",
            action="continue_book_discussion",
            status="completed",
            artifact_path=response_path,
            routing=(
                "review_available" if result.readiness.status == "ready" else "continue_discussion"
            ),
            message="Book direction discussion turn completed and the candidate draft was updated.",
            payload={
                "profile_id": profile.id,
                "model_snapshot": result.model_snapshot,
                "turn": updated.turn_count,
                "readiness": result.readiness.status,
                "direction_draft_path": "book/direction_draft.md",
            },
        )
        return updated


@router.post("/prepare-review", response_model=SetupStateDocument)
def prepare_setup_review() -> SetupStateDocument:
    with _setup_lock:
        project_path = _active_project_path()
        _ensure_setup_mutable(project_path)
        setup_storage.flush_pending_setup_events(project_path)
        metadata = read_project_metadata(project_path)
        state = setup_storage.read_setup_state(project_path)
        if state.approved:
            return state
        if not state.direction_draft.strip():
            raise HTTPException(
                status_code=409,
                detail="Discuss the novel direction before requesting a review.",
            )
        retrying_blocked_candidate = bool(
            state.candidate is not None and not state.candidate.approval_allowed
        )
        if not state.selected_title and not retrying_blocked_candidate:
            raise HTTPException(
                status_code=409,
                detail="Confirm the formal book title before requesting a review.",
            )
        if state.readiness.status != "ready" and not retrying_blocked_candidate:
            raise HTTPException(
                status_code=409,
                detail="Book Agent has not marked the direction ready for review.",
            )
        if state.candidate is not None and state.candidate.approval_allowed:
            raise HTTPException(
                status_code=409,
                detail=(
                    "The current Book Direction candidate has already been reviewed. "
                    "Approve it or continue the discussion before preparing another candidate."
                ),
            )
        profile = _active_profile_or_409()
        candidate_revision = state.candidate_revision_counter + 1
        context_snapshot = {
            **build_review_context_snapshot(state),
            "project_id": metadata.project_id,
            "candidate_revision": candidate_revision,
            "profile_id": profile.id,
            "model": profile.model,
        }
        context_path = setup_storage.write_review_context_snapshot(
            project_path,
            candidate_revision=candidate_revision,
            snapshot=context_snapshot,
        )
        _append_event(
            project_path,
            metadata,
            kind="book_direction_review_context_assembled",
            action="assemble_book_direction_review_context",
            status="completed",
            artifact_path=context_path,
            routing="synthesize_book_direction",
            message="Candidate book direction review context assembled.",
            payload={"profile_id": profile.id, "candidate_revision": candidate_revision},
        )

        try:
            _append_event(
                project_path,
                metadata,
                kind="atomic_action_started",
                action="synthesize_book_direction",
                status="started",
                message="Synthesizing a candidate Book Direction for user review.",
                payload={"profile_id": profile.id, "candidate_revision": candidate_revision},
            )
            stream_callback = _setup_stream_callback(
                project_path,
                metadata,
                action="synthesize_book_direction",
            )
            synthesis = synthesize_book_direction(
                profile,
                state,
                stream_callback,
                on_event=_setup_agent_event_callback(
                    project_path,
                    metadata,
                    action="synthesize_book_direction",
                ),
                on_tool_event=stream_callback,
            )
            _append_event(
                project_path,
                metadata,
                kind="atomic_action_started",
                action="review_book_direction",
                status="started",
                message="Independently reviewing the candidate Book Direction.",
                payload={"profile_id": profile.id, "candidate_revision": candidate_revision},
            )
            review, review_model_snapshot, _review_usage = review_book_direction(
                profile,
                state,
                synthesis,
                _setup_stream_callback(
                    project_path,
                    metadata,
                    action="review_book_direction",
                ),
            )
        except AgentControlCheckpoint as checkpoint:
            if checkpoint.run_result.outcome == "waiting_user":
                try:
                    updated = setup_storage.record_agent_user_decision(
                        project_path,
                        state,
                        payload=checkpoint.payload,
                        checkpoint_path=checkpoint.artifact_path,
                        profile_id=profile.id,
                        model_snapshot=(
                            checkpoint.run_result.model_snapshot or profile.model
                        ),
                    )
                except (OSError, ValueError) as exc:
                    raise HTTPException(status_code=500, detail=str(exc)) from exc
                _append_event(
                    project_path,
                    metadata,
                    kind="agent_waiting_for_user",
                    action="synthesize_and_review_book_direction",
                    status="requested",
                    artifact_path=checkpoint.artifact_path,
                    routing="continue_book_discussion",
                    message="Book Agent requested one explicit user decision after evaluation.",
                    payload={
                        "checkpoint_id": checkpoint.payload.get("checkpoint_id"),
                        "question": checkpoint.payload.get("question"),
                        "suggestions": checkpoint.payload.get("suggestions"),
                    },
                )
                return updated
            _raise_setup_control_checkpoint(
                project_path,
                metadata,
                checkpoint,
                action="synthesize_and_review_book_direction",
            )
        except Exception as exc:
            reason = redact_profile_secrets(str(exc), profile)
            _append_event(
                project_path,
                metadata,
                kind="book_direction_review_failed",
                action="synthesize_and_review_book_direction",
                status="failed",
                artifact_path=context_path,
                routing="retry_book_direction_review",
                message="Book direction synthesis or review failed; approval remains locked.",
                payload={"profile_id": profile.id, "reason": reason},
            )
            raise HTTPException(status_code=502, detail=reason) from exc

        if review.commit_allowed and not state.selected_title:
            title_payload = {
                "checkpoint_id": f"book-title:{candidate_revision}",
                "question": "以下哪个书名最适合作为正式书名？",
                "context": "全书方向已经收敛，请完成规划阶段的最后一个书名决定。",
                "suggestions": [
                    {
                        "label": item.title,
                        "message": f"采用《{item.title}》作为正式书名。",
                        "rationale": item.rationale,
                        "recommended": index == 0,
                    }
                    for index, item in enumerate(synthesis.recommended_titles[:3])
                ],
            }
            try:
                updated = setup_storage.record_agent_user_decision(
                    project_path,
                    state,
                    payload=title_payload,
                    checkpoint_path=context_path,
                    profile_id=profile.id,
                    model_snapshot=synthesis.model_snapshot,
                )
            except (OSError, ValueError) as exc:
                raise HTTPException(status_code=500, detail=str(exc)) from exc
            _append_event(
                project_path,
                metadata,
                kind="book_title_decision_requested",
                action="synthesize_and_review_book_direction",
                status="requested",
                artifact_path=context_path,
                routing="continue_book_discussion",
                message="Book Agent requested the final formal-title decision.",
                payload={"question": title_payload["question"]},
            )
            return updated

        try:
            updated = setup_storage.save_book_direction_candidate(
                project_path,
                state,
                synthesis=synthesis,
                review=review,
                profile_id=profile.id,
                review_model_snapshot=review_model_snapshot,
                context_snapshot_path=context_path,
            )
        except setup_storage.SetupRevisionConflict as exc:
            _append_event(
                project_path,
                metadata,
                kind="book_direction_stale_review_discarded",
                action="synthesize_and_review_book_direction",
                status="failed",
                artifact_path=context_path,
                routing="reload_book_discussion",
                message="Book discussion state changed; the stale review result was discarded.",
                payload={"profile_id": profile.id, "expected_revision": state.revision},
            )
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        candidate = updated.candidate
        if candidate is None:
            raise HTTPException(status_code=500, detail="Candidate book direction was not stored.")
        if synthesis.evaluation_record is not None:
            evaluation = EvaluationRecord.model_validate(synthesis.evaluation_record)
            review_root = Path(candidate.direction_path).parent
            persist_evaluation_views(
                project_path,
                evaluation,
                evaluation_path=(review_root / "evaluation.json").as_posix(),
                review_path=(review_root / "review.md").as_posix(),
                verification_path=candidate.verification_path,
            )
        for artifact_path, artifact_kind in [
            (candidate.direction_path, "book_direction_candidate_written"),
            (candidate.constraints_path, "book_direction_constraints_written"),
            (candidate.title_suggestions_path, "book_title_candidates_written"),
            (candidate.rolling_plan_path, "book_rolling_contract_candidate_written"),
        ]:
            _append_event(
                project_path,
                metadata,
                kind=artifact_kind,
                action="synthesize_book_direction",
                status="completed",
                artifact_path=artifact_path,
                routing="review_book_direction",
                message="Candidate book-level artifact written for review.",
                payload={
                    "profile_id": profile.id,
                    "model_snapshot": synthesis.model_snapshot,
                    "candidate_revision": candidate.revision,
                    "candidate": True,
                },
            )
        _append_event(
            project_path,
            metadata,
            kind="book_direction_candidate_reviewed",
            action="review_book_direction",
            status="completed",
            artifact_path=candidate.verification_path,
            routing=("await_user_approval" if candidate.approval_allowed else "continue_discussion"),
            message=(
                "Candidate Book Direction is ready for explicit user approval."
                if candidate.approval_allowed
                else "Candidate Book Direction has blocking issues and remains unapproved."
            ),
            payload={
                "profile_id": profile.id,
                "model_snapshot": review_model_snapshot,
                "candidate_revision": candidate.revision,
                "approval_allowed": candidate.approval_allowed,
                "direction_path": candidate.direction_path,
                "constraints_path": candidate.constraints_path,
                "title_suggestions_path": candidate.title_suggestions_path,
                "rolling_plan_path": candidate.rolling_plan_path,
            },
        )
        return updated


@router.post("/approve", response_model=SetupStateDocument)
def approve_setup(request: SetupApprovalRequest) -> SetupStateDocument:
    with _setup_lock:
        if _contains_configured_profile_secret(request.title):
            raise HTTPException(
                status_code=409,
                detail=(
                    "Book title contains configured provider credentials or endpoint data. "
                    "Choose a different title."
                ),
            )
        project_path: Path | None = None
        try:
            project_path = _begin_setup_approval_lease()
            _ensure_setup_mutable(project_path)
            setup_storage.flush_pending_setup_events(project_path)
            metadata = read_project_metadata(project_path)
            try:
                state = setup_storage.approve_setup(project_path, request)
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc

            candidate = state.candidate
            _append_event(
                project_path,
                metadata,
                kind="book_loop_approved",
                action="approve_book_direction",
                status="completed",
                artifact_path="book/direction.md",
                routing="start_or_resume_harness",
                message="User explicitly approved the reviewed Book Direction.",
                payload={
                    "candidate_revision": candidate.revision if candidate else None,
                    "profile_id": candidate.profile_id if candidate else None,
                    "model_snapshot": candidate.model_snapshot if candidate else None,
                    "title": state.approved_title,
                    "title_selection_source": state.title_selection_source,
                    "committed_artifacts": [
                        "project.json",
                        "book/direction.md",
                        "book/constraints.json",
                        "book/settings.md",
                        "book/outline.md",
                        "book/state.json",
                    ],
                },
            )
            for artifact_path in [
                "book/constraints.json",
                "book/settings.md",
                "book/outline.md",
                "book/state.json",
            ]:
                _append_event(
                    project_path,
                    metadata,
                    kind="approved_book_artifact_written",
                    action="approve_book_direction",
                    status="completed",
                    artifact_path=artifact_path,
                    routing="start_or_resume_harness",
                    message=(
                        "Approved book-level artifact committed after explicit user approval."
                    ),
                    payload={
                        "candidate_revision": candidate.revision if candidate else None,
                        "committed": True,
                        "title": state.approved_title,
                    },
                )
            return state
        finally:
            if project_path is not None:
                end_active_runner(project_path)


def _contains_configured_profile_secret(title: str) -> bool:
    return any(
        value in title
        for profile in load_profiles().profiles
        for value in profile_secret_values(profile)
        if value
    )


def _active_project_path() -> Path:
    project_path = get_active_project_path()
    if project_path is None:
        raise HTTPException(status_code=404, detail="No active project.")
    return project_path


def _ensure_setup_mutable(project_path: Path) -> None:
    try:
        ensure_creative_mutation_allowed(project_path)
    except ProjectReadOnlyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _begin_setup_approval_lease() -> Path:
    with active_project_transition_lock():
        project_path = _active_project_path()
        if begin_active_runner(project_path):
            return project_path
        raise HTTPException(
            status_code=409,
            detail=(
                "Cannot approve the book direction while a harness runner is active."
            ),
        )


def _active_profile_or_409() -> LlmProfile:
    profile = get_active_profile()
    if profile is None:
        raise HTTPException(
            status_code=409,
            detail="Select an enabled LLM profile before continuing the book discussion.",
        )
    return profile


def _append_event(
    project_path: Path,
    metadata: ProjectMetadata,
    *,
    kind: str,
    action: str,
    status: EventStatus,
    message: str,
    artifact_path: str | None = None,
    routing: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    event = HarnessEvent(
        project_id=metadata.project_id,
        kind=kind,
        loop_layer="book",
        atomic_action=action,
        status=status,
        artifact_path=artifact_path,
        routing_decision=routing,
        message=message,
        payload=payload or {},
    )
    try:
        append_event(project_path, event)
    except (OSError, ValueError):
        setup_storage.enqueue_pending_setup_event(project_path, event)


def _setup_stream_callback(
    project_path: Path,
    metadata: ProjectMetadata,
    *,
    action: str,
) -> Callable[[ChatChunk], None]:
    progress = StreamProgressAccumulator()

    def emit_delta(chunk: ChatChunk) -> None:
        received_characters = progress.observe(chunk)
        if received_characters is None:
            return
        _append_event(
            project_path,
            metadata,
            kind="llm_stream_progress",
            action=action,
            status="delta",
            message="Model response is streaming.",
            payload={"received_characters": received_characters},
        )

    return emit_delta


def _setup_agent_event_callback(
    project_path: Path,
    metadata: ProjectMetadata,
    *,
    action: str,
) -> Callable[[dict[str, Any]], None]:
    def emit_agent_event(payload: dict[str, Any]) -> None:
        projected = project_agent_event(payload)
        if projected is None:
            return
        _append_event(
            project_path,
            metadata,
            kind=projected.kind,
            action=action,
            status=projected.status,
            artifact_path=projected.artifact_path,
            routing=projected.routing_decision,
            message=projected.message,
            payload=projected.payload,
        )

    return emit_agent_event


def _raise_setup_control_checkpoint(
    project_path: Path,
    metadata: ProjectMetadata,
    checkpoint: AgentControlCheckpoint,
    *,
    action: str,
) -> None:
    payload = {
        key: value
        for key, value in checkpoint.payload.items()
        if key
        in {
            "checkpoint_id",
            "candidate_run_id",
            "kind",
            "summary",
            "evidence",
            "target_owner",
            "contract_field",
            "contract_revision",
            "committed_evidence_locator",
            "impossibility_reason",
            "routing_status",
            "question",
            "suggestions",
            "context",
        }
    }
    waiting_user = checkpoint.run_result.outcome == "waiting_user"
    _append_event(
        project_path,
        metadata,
        kind=("agent_waiting_for_user" if waiting_user else "agent_blocker_recorded"),
        action=action,
        status="requested",
        artifact_path=checkpoint.artifact_path,
        routing=("pause" if waiting_user else "await_harness_routing"),
        message=(
            "Book Agent requested one explicit user decision."
            if waiting_user
            else "Book Agent blocker recorded at a durable checkpoint."
        ),
        payload=payload,
    )
    raise HTTPException(
        status_code=409,
        detail={
            "code": "agent_control_checkpoint",
            "outcome": checkpoint.run_result.outcome,
            "artifact_path": checkpoint.artifact_path,
            "checkpoint": payload,
        },
    )
