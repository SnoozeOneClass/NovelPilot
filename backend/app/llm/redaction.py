from collections.abc import Sequence

from app.schemas.profiles import LlmProfile


def profile_secret_values(profile: LlmProfile) -> list[str]:
    values = [
        profile.api_key.get_secret_value(),
        str(profile.base_url),
        str(profile.base_url).rstrip("/"),
    ]
    return [value for value in values if value]


def redact_sensitive_values(text: str, values: Sequence[str]) -> str:
    redacted = text
    for value in values:
        if value:
            redacted = redacted.replace(value, "[redacted]")
    return redacted


def redact_profile_secrets(text: str, profile: LlmProfile | None) -> str:
    if profile is None:
        return text
    return redact_sensitive_values(text, profile_secret_values(profile))
