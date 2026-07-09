from app.core.config import LLM_PROFILES_PATH, ensure_runtime_dirs
from app.schemas.profiles import (
    LlmProfile,
    LlmProfilePublic,
    LlmProfileUpsert,
    LlmProfilesDocument,
    LlmProfilesPublicDocument,
    to_public_profile,
)
from app.storage.json_files import read_json, write_json


def _profile_to_storage(profile: LlmProfile) -> dict[str, object]:
    return {
        "id": profile.id,
        "name": profile.name,
        "protocol": profile.protocol,
        "base_url": str(profile.base_url),
        "api_key": profile.api_key.get_secret_value(),
        "model": profile.model,
        "enabled": profile.enabled,
    }


def _document_to_storage(document: LlmProfilesDocument) -> dict[str, object]:
    return {
        "schema_version": document.schema_version,
        "active_profile_id": document.active_profile_id,
        "profiles": [_profile_to_storage(profile) for profile in document.profiles],
    }


def load_profiles() -> LlmProfilesDocument:
    ensure_runtime_dirs()
    data = read_json(LLM_PROFILES_PATH)
    if data is None:
        return LlmProfilesDocument()
    return LlmProfilesDocument.model_validate(data)


def save_profiles(document: LlmProfilesDocument) -> None:
    write_json(LLM_PROFILES_PATH, _document_to_storage(document))


def list_public_profiles() -> LlmProfilesPublicDocument:
    document = load_profiles()
    return LlmProfilesPublicDocument(
        schema_version=document.schema_version,
        active_profile_id=document.active_profile_id,
        profiles=[to_public_profile(profile) for profile in document.profiles],
    )


def get_profile(profile_id: str) -> LlmProfile:
    document = load_profiles()
    for profile in document.profiles:
        if profile.id == profile_id:
            return profile
    raise KeyError(profile_id)


def upsert_profile(payload: LlmProfileUpsert) -> LlmProfilePublic:
    document = load_profiles()
    existing = next((item for item in document.profiles if item.id == payload.id), None)
    api_key = payload.api_key or (
        existing.api_key.get_secret_value() if existing is not None else None
    )
    if not api_key:
        raise ValueError("API key is required for a new LLM profile.")

    profile_payload = payload.model_dump()
    profile_payload["api_key"] = api_key
    profile = LlmProfile(**profile_payload)
    remaining = [item for item in document.profiles if item.id != profile.id]
    remaining.append(profile)
    document.profiles = sorted(remaining, key=lambda item: item.name.lower())
    if document.active_profile_id is None:
        document.active_profile_id = profile.id
    save_profiles(document)
    return to_public_profile(profile)


def select_profile(profile_id: str) -> LlmProfilesPublicDocument:
    document = load_profiles()
    if not any(profile.id == profile_id for profile in document.profiles):
        raise KeyError(profile_id)
    document.active_profile_id = profile_id
    save_profiles(document)
    return list_public_profiles()
