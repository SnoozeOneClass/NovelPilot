from pathlib import Path

from fastapi import APIRouter, HTTPException

from app.harness.loops.book import (
    assess_setup_followup_question,
    personalize_next_setup_question,
)
from app.llm.profiles import get_active_profile
from app.llm.redaction import redact_profile_secrets
from app.schemas.events import HarnessEvent
from app.schemas.profiles import LlmProfile
from app.schemas.projects import ProjectMetadata
from app.schemas.setup import SetupAnswerRequest, SetupStateDocument
from app.storage.events import append_event
from app.storage.projects import get_active_project_path, read_project_metadata
from app.storage import setup as setup_storage

router = APIRouter()


@router.get("/state", response_model=SetupStateDocument)
def get_setup_state() -> SetupStateDocument:
    project_path = get_active_project_path()
    if project_path is None:
        raise HTTPException(status_code=404, detail="No active project.")
    return setup_storage.read_setup_state(project_path)


@router.post("/answer", response_model=SetupStateDocument)
def answer_setup_question(request: SetupAnswerRequest) -> SetupStateDocument:
    project_path = get_active_project_path()
    if project_path is None:
        raise HTTPException(status_code=404, detail="No active project.")
    metadata = read_project_metadata(project_path)
    try:
        state = setup_storage.answer_setup_question(project_path, request)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    append_event(
        project_path,
        HarnessEvent(
            project_id=metadata.project_id,
            kind="setup_answered",
            loop_layer="book",
            atomic_action="collect_book_settings",
            status="completed",
            message="Book setup answer recorded.",
            payload=request.model_dump(),
        ),
    )
    return _advance_setup_question_if_possible(project_path, metadata, state)


@router.post("/approve", response_model=SetupStateDocument)
def approve_setup() -> SetupStateDocument:
    project_path = get_active_project_path()
    if project_path is None:
        raise HTTPException(status_code=404, detail="No active project.")
    metadata = read_project_metadata(project_path)
    try:
        state = setup_storage.read_setup_state(project_path)
        state = _ensure_setup_ready_for_approval(project_path, metadata, state)
        if not state.approved and state.next_question is not None:
            return state
        state = setup_storage.approve_setup(project_path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    append_event(
        project_path,
        HarnessEvent(
            project_id=metadata.project_id,
            kind="book_loop_approved",
            loop_layer="book",
            atomic_action="approve_book_loop",
            status="completed",
            artifact_path="book/settings.md",
            message="Book loop approved by user.",
        ),
    )
    return state


def _ensure_setup_ready_for_approval(
    project_path: Path,
    metadata: ProjectMetadata,
    state: SetupStateDocument,
) -> SetupStateDocument:
    if state.approved or state.next_question is not None:
        return state

    profile = get_active_profile()
    if profile is None:
        return state
    if state.ready_for_approval and state.readiness_profile_id == profile.id:
        return state
    return _add_followup_question_if_needed(project_path, metadata, profile, state)


def _advance_setup_question_if_possible(
    project_path: Path,
    metadata: ProjectMetadata,
    state: SetupStateDocument,
) -> SetupStateDocument:
    next_question = state.next_question
    if next_question is not None and next_question.source == "llm":
        return state

    profile = get_active_profile()
    if profile is None:
        return state

    if next_question is None:
        return _add_followup_question_if_needed(project_path, metadata, profile, state)

    try:
        personalized_question = personalize_next_setup_question(profile, state, next_question)
    except Exception as exc:
        append_event(
            project_path,
            HarnessEvent(
                project_id=metadata.project_id,
                kind="setup_question_personalization_failed",
                loop_layer="book",
                atomic_action="personalize_setup_question",
                status="failed",
                routing_decision="fallback_to_default_question",
                message="Book setup question personalization failed; using default question.",
                payload={
                    "question_id": next_question.id,
                    "reason": redact_profile_secrets(str(exc), profile),
                },
            ),
        )
        return state

    updated_state = setup_storage.replace_setup_question(
        project_path,
        state,
        personalized_question,
    )
    append_event(
        project_path,
        HarnessEvent(
            project_id=metadata.project_id,
            kind="setup_question_personalized",
            loop_layer="book",
            atomic_action="personalize_setup_question",
            status="completed",
            routing_decision="ask_user",
            message="Book setup next question personalized from prior answers.",
            payload={
                "question_id": personalized_question.id,
                "profile_id": profile.id,
                "model_snapshot": personalized_question.model_snapshot,
            },
        ),
    )
    return updated_state


def _add_followup_question_if_needed(
    project_path: Path,
    metadata: ProjectMetadata,
    profile: LlmProfile,
    state: SetupStateDocument,
) -> SetupStateDocument:
    try:
        followup_question = assess_setup_followup_question(profile, state)
    except Exception as exc:
        append_event(
            project_path,
            HarnessEvent(
                project_id=metadata.project_id,
                kind="setup_followup_assessment_failed",
                loop_layer="book",
                atomic_action="assess_setup_readiness",
                status="failed",
                routing_decision="ready_for_approval",
                message="Book setup follow-up assessment failed; setup can proceed to approval.",
                payload={"reason": redact_profile_secrets(str(exc), profile)},
            ),
        )
        return setup_storage.mark_ready_for_approval(project_path, state, profile.id)

    if followup_question is None:
        updated_state = setup_storage.mark_ready_for_approval(project_path, state, profile.id)
        append_event(
            project_path,
            HarnessEvent(
                project_id=metadata.project_id,
                kind="setup_ready_for_approval",
                loop_layer="book",
                atomic_action="assess_setup_readiness",
                status="completed",
                routing_decision="approve_book_loop",
                message="Book setup has enough information for approval.",
                payload={"profile_id": profile.id},
            ),
        )
        return updated_state

    updated_state = setup_storage.append_setup_question(
        project_path,
        state,
        followup_question,
    )
    append_event(
        project_path,
        HarnessEvent(
            project_id=metadata.project_id,
            kind="setup_followup_question_created",
            loop_layer="book",
            atomic_action="assess_setup_readiness",
            status="completed",
            routing_decision="ask_user",
            message="Book setup needs one more user decision before approval.",
            payload={
                "question_id": followup_question.id,
                "profile_id": profile.id,
                "model_snapshot": followup_question.model_snapshot,
            },
        ),
    )
    return updated_state
