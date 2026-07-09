from typing import Literal

from pydantic import BaseModel, Field, HttpUrl, SecretStr


LlmProtocol = Literal["openai-compatible", "anthropic-compatible"]


class LlmProfile(BaseModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    protocol: LlmProtocol
    base_url: HttpUrl
    api_key: SecretStr
    model: str = Field(min_length=1)
    enabled: bool = True


class LlmProfilePublic(BaseModel):
    id: str
    name: str
    protocol: LlmProtocol
    base_url: str
    model: str
    enabled: bool
    has_api_key: bool


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


class LlmProfileUpsert(BaseModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    protocol: LlmProtocol
    base_url: HttpUrl
    api_key: str | None = None
    model: str = Field(min_length=1)
    enabled: bool = True


def to_public_profile(profile: LlmProfile) -> LlmProfilePublic:
    return LlmProfilePublic(
        id=profile.id,
        name=profile.name,
        protocol=profile.protocol,
        base_url=str(profile.base_url),
        model=profile.model,
        enabled=profile.enabled,
        has_api_key=bool(profile.api_key.get_secret_value()),
    )
