from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, HttpUrl, SecretStr


LlmProtocol = Literal["openai-compatible", "anthropic-compatible"]


class LlmCapabilityCheck(BaseModel):
    ok: bool
    message: str = Field(min_length=1, max_length=1_000)


class LlmCapabilitySnapshot(BaseModel):
    schema_version: int = 1
    checked_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    profile_fingerprint: str = Field(min_length=64, max_length=64)
    tool_calling: LlmCapabilityCheck
    structured_output: LlmCapabilityCheck
    ready_for_harness: bool


class LlmProfile(BaseModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    protocol: LlmProtocol
    base_url: HttpUrl
    api_key: SecretStr
    model: str = Field(min_length=1)
    request_options: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    capability_test: LlmCapabilitySnapshot | None = None


class LlmProfilePublic(BaseModel):
    id: str
    name: str
    protocol: LlmProtocol
    base_url: str
    model: str
    request_options: dict[str, Any] = Field(default_factory=dict)
    enabled: bool
    has_api_key: bool
    capability_test: LlmCapabilitySnapshot | None = None


class LlmProfilesDocument(BaseModel):
    schema_version: int = 1
    active_profile_id: str | None = None
    profiles: list[LlmProfile] = Field(default_factory=list)


class LlmProfilesPublicDocument(BaseModel):
    schema_version: int = 1
    active_profile_id: str | None = None
    profiles: list[LlmProfilePublic] = Field(default_factory=list)


class LlmProfileTestResult(BaseModel):
    profile_id: str
    ok: bool
    model_snapshot: str
    provider_snapshot: str
    message: str
    capability_test: LlmCapabilitySnapshot


class LlmProfileUpsert(BaseModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    protocol: LlmProtocol
    base_url: HttpUrl
    api_key: str | None = None
    model: str = Field(min_length=1)
    request_options: dict[str, Any] | None = None
    enabled: bool = True


def to_public_profile(profile: LlmProfile) -> LlmProfilePublic:
    return LlmProfilePublic(
        id=profile.id,
        name=profile.name,
        protocol=profile.protocol,
        base_url=str(profile.base_url),
        model=profile.model,
        request_options=profile.request_options,
        enabled=profile.enabled,
        has_api_key=bool(profile.api_key.get_secret_value()),
        capability_test=profile.capability_test,
    )
