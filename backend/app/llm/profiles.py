from app.schemas.profiles import LlmProfile
from app.storage.profiles import load_profiles


def get_active_profile() -> LlmProfile | None:
    document = load_profiles()
    if document.active_profile_id is None:
        return None
    for profile in document.profiles:
        if profile.id == document.active_profile_id and profile.enabled:
            return profile
    return None

