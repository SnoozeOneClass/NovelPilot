from hashlib import sha256

from app.core.config import LLM_PROFILES_PATH, ensure_runtime_dirs
from app.schemas.profiles import (
    LlmCapabilitySnapshot,
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
        "request_options": profile.request_options,
        "enabled": profile.enabled,
        "capability_test": (
            profile.capability_test.model_dump(mode="json")
            if profile.capability_test is not None
            else None
        ),
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
    request_options = payload.request_options
    if request_options is None:
        request_options = existing.request_options if existing is not None else {}

    profile_payload = payload.model_dump()
    profile_payload["api_key"] = api_key
    profile_payload["request_options"] = request_options
    profile_payload["capability_test"] = None
    profile = LlmProfile(**profile_payload)
    if existing is not None and profile_fingerprint(existing) == profile_fingerprint(profile):
        profile.capability_test = existing.capability_test
    remaining = [item for item in document.profiles if item.id != profile.id]
    remaining.append(profile)
    document.profiles = sorted(remaining, key=lambda item: item.name.lower())
    if document.active_profile_id is None:
        document.active_profile_id = profile.id
    save_profiles(document)
    return to_public_profile(profile)


def record_capability_test(
    profile_id: str,
    capability_test: LlmCapabilitySnapshot,
) -> LlmProfilePublic:
    document = load_profiles()
    profile = next((item for item in document.profiles if item.id == profile_id), None)
    if profile is None:
        raise KeyError(profile_id)
    if capability_test.profile_fingerprint != profile_fingerprint(profile):
        raise ValueError("Capability result does not match the current profile configuration.")
    profile.capability_test = capability_test
    save_profiles(document)
    return to_public_profile(profile)


def profile_fingerprint(profile: LlmProfile) -> str:
    identity = "\0".join(
        [profile.protocol, str(profile.base_url).rstrip("/"), profile.model]
    )
    return sha256(identity.encode("utf-8")).hexdigest()


def require_harness_capabilities(profile: LlmProfile) -> None:
    snapshot = profile.capability_test
    if snapshot is None or snapshot.profile_fingerprint != profile_fingerprint(profile):
        raise ValueError(
            f"LLM profile '{profile.id}' must pass Tool Calling and Structured Output tests."
        )
    if not snapshot.ready_for_harness:
        raise ValueError(
            f"LLM profile '{profile.id}' does not support the required Harness protocol."
        )


def select_profile(profile_id: str) -> LlmProfilesPublicDocument:
    document = load_profiles()
    if not any(profile.id == profile_id for profile in document.profiles):
        raise KeyError(profile_id)
    document.active_profile_id = profile_id
    save_profiles(document)
    return list_public_profiles()
