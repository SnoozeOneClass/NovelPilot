from fastapi import APIRouter, HTTPException

from app.llm.gateway import ChatMessage, ChatRequest, call_llm
from app.llm.redaction import redact_profile_secrets
from app.schemas.profiles import (
    LlmProfilePublic,
    LlmProfileTestResult,
    LlmProfileUpsert,
    LlmProfilesPublicDocument,
)
from app.storage import profiles as profile_storage
from app.storage.projects import (
    get_active_project_path,
    read_project_metadata,
    write_project_metadata,
)

router = APIRouter()


@router.get("", response_model=LlmProfilesPublicDocument)
def list_profiles() -> LlmProfilesPublicDocument:
    return profile_storage.list_public_profiles()


@router.post("", response_model=LlmProfilePublic)
def upsert_profile(payload: LlmProfileUpsert) -> LlmProfilePublic:
    try:
        profile = profile_storage.upsert_profile(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _sync_active_project_profile(profile_storage.list_public_profiles().active_profile_id)
    return profile


@router.put("/{profile_id}", response_model=LlmProfilePublic)
def update_profile(profile_id: str, payload: LlmProfileUpsert) -> LlmProfilePublic:
    if profile_id != payload.id:
        raise HTTPException(status_code=400, detail="Profile id cannot be changed.")
    try:
        profile = profile_storage.upsert_profile(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _sync_active_project_profile(profile_storage.list_public_profiles().active_profile_id)
    return profile


@router.post("/{profile_id}/select", response_model=LlmProfilesPublicDocument)
def select_profile(profile_id: str) -> LlmProfilesPublicDocument:
    try:
        profiles = profile_storage.select_profile(profile_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Profile not found: {profile_id}") from exc
    _sync_active_project_profile(profile_id)
    return profiles


@router.post("/{profile_id}/test", response_model=LlmProfileTestResult)
def test_profile(profile_id: str) -> LlmProfileTestResult:
    try:
        profile = profile_storage.get_profile(profile_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Profile not found: {profile_id}") from exc
    if not profile.enabled:
        raise HTTPException(status_code=400, detail="Profile is disabled.")

    try:
        result = call_llm(
            profile,
            ChatRequest(
                profile_id=profile.id,
                messages=[
                    ChatMessage(
                        role="system",
                        content="You are a connectivity probe for Novelpilot.",
                    ),
                    ChatMessage(
                        role="user",
                        content="Reply with one short sentence confirming this profile works.",
                    ),
                ],
                temperature=0,
            ),
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=redact_profile_secrets(str(exc), profile),
        ) from exc
    except RuntimeError as exc:
        raise HTTPException(
            status_code=502,
            detail=redact_profile_secrets(str(exc), profile),
        ) from exc

    return LlmProfileTestResult(
        profile_id=profile.id,
        ok=True,
        model_snapshot=result.model_snapshot,
        provider_snapshot=result.provider_snapshot,
        message=result.content.strip()[:500],
    )


def _sync_active_project_profile(profile_id: str | None) -> None:
    if profile_id is None:
        return
    project_path = get_active_project_path()
    if project_path is None:
        return
    metadata = read_project_metadata(project_path)
    if metadata.active_profile_id == profile_id:
        return
    metadata.active_profile_id = profile_id
    write_project_metadata(project_path, metadata)
