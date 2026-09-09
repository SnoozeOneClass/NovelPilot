from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from app.authoring.domain.models import content_hash
from app.authoring.models.binding import (
    ModelBindingError,
    ProfileCapabilityError,
    ProfileCredential,
)
from app.authoring.models.contracts import (
    ApiFamily,
    JsonValue,
    ProfileCapabilities,
    ProfileSnapshot,
    validate_profile_base_url,
    validate_profile_request_options,
)
from app.authoring.models.persistence import atomic_write, profile_settings_lock


class ProfileConfigurationError(RuntimeError):
    """A local profile cannot satisfy its explicit frozen runtime contract."""


class CapabilityEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    checked_at: str
    profile_fingerprint: str = Field(min_length=64, max_length=64)
    source: Literal["pydantic-ai-capability-v1"]
    capabilities: ProfileCapabilities


class StoredProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    display_name: str
    api_family: ApiFamily
    base_url: str
    api_key: SecretStr
    model_id: str
    request_options: dict[str, JsonValue] = Field(default_factory=dict)
    enabled: bool = True
    capability_test: CapabilityEvidence | None = None

    @field_validator("id", "display_name", "api_family", "model_id")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Profile identity fields must be non-blank.")
        return value

    @model_validator(mode="after")
    def _protocol_request_options(self) -> StoredProfile:
        validate_profile_base_url(self.api_family, self.base_url)
        validate_profile_request_options(self.api_family, self.request_options)
        return self

    @property
    def configuration_fingerprint(self) -> str:
        return profile_configuration_fingerprint(
            api_family=self.api_family,
            base_url=self.base_url,
            model_id=self.model_id,
            request_options=self.request_options,
        )


class ProfilesDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[2] = 2
    selected_profile_id: str | None = None
    profiles: list[StoredProfile] = Field(default_factory=list)


class PublicProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    display_name: str
    api_family: ApiFamily
    base_url: str
    model_id: str
    request_options: dict[str, JsonValue]
    enabled: bool
    has_api_key: bool
    capability_status: Literal["missing", "stale", "ready"]
    capabilities: ProfileCapabilities | None
    configuration_fingerprint: str
    capability_fingerprint: str | None


@dataclass(frozen=True, slots=True)
class ResolvedProfile:
    snapshot: ProfileSnapshot
    credential: ProfileCredential


@dataclass(frozen=True, slots=True)
class ProfileFailureMaterial:
    """Secret-safe material for persisting a zero-request Profile preflight failure."""

    snapshot: ProfileSnapshot
    credential: ProfileCredential
    error: ModelBindingError


class ProfileCatalog:
    """Read local secrets, but expose only secret-free snapshots to Agent task plans."""

    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> ProfilesDocument:
        if not self._path.exists():
            return ProfilesDocument()
        try:
            return ProfilesDocument.model_validate_json(self._path.read_bytes())
        except (OSError, ValueError) as exc:
            raise ProfileConfigurationError(
                f"Profile configuration {self._path} is invalid for schema version 2."
            ) from exc

    def list_public(self) -> tuple[str | None, list[PublicProfile]]:
        document = self.load()
        return document.selected_profile_id, [
            self._to_public(profile) for profile in document.profiles
        ]

    def get_stored(self, profile_id: str) -> StoredProfile:
        document = self.load()
        profile = next((item for item in document.profiles if item.id == profile_id), None)
        if profile is None:
            raise ProfileConfigurationError(f"Profile {profile_id!r} does not exist.")
        return profile

    def record_capability_evidence(
        self,
        *,
        profile_id: str,
        evidence: CapabilityEvidence,
        expected_profile: StoredProfile | None = None,
    ) -> None:
        """Atomically replace one Profile's evidence while preserving local secrets."""

        with profile_settings_lock(self._path):
            updated = with_capability_evidence(
                self.load(),
                profile_id=profile_id,
                evidence=evidence,
                expected_profile=expected_profile,
            )
            atomic_write(self._path, encode_profiles_document(updated))

    def resolve(self, profile_id: str) -> ResolvedProfile:
        profile = self.get_stored(profile_id)
        if not profile.enabled:
            raise ProfileConfigurationError(f"Profile {profile_id!r} is disabled.")
        evidence = profile.capability_test
        if evidence is None:
            raise ProfileConfigurationError(f"Profile {profile_id!r} has no capability evidence.")
        if evidence.source != "pydantic-ai-capability-v1":
            raise ProfileConfigurationError(
                f"Profile {profile_id!r} requires a current production Adapter capability probe."
            )
        if evidence.profile_fingerprint != profile.configuration_fingerprint:
            raise ProfileConfigurationError(f"Profile {profile_id!r} capability evidence is stale.")
        snapshot = ProfileSnapshot.create(
            profile_id=profile.id,
            display_name=profile.display_name,
            api_family=profile.api_family,
            base_url=profile.base_url,
            model_id=profile.model_id,
            capabilities=evidence.capabilities,
            request_options=profile.request_options,
        )
        return ResolvedProfile(
            snapshot=snapshot,
            credential=ProfileCredential.from_plaintext(profile.api_key.get_secret_value()),
        )

    def failure_material(
        self,
        profile_id: str,
        *,
        message: str,
    ) -> ProfileFailureMaterial:
        """Build a non-runnable snapshot used only to terminalize a failed preflight.

        The returned error is handed to ``AgentExecutor.fail_preflight``; that
        path never resolves an Adapter. A placeholder credential is used only
        when the local Profile document itself cannot be read, and is never sent
        or persisted.
        """

        capabilities = ProfileCapabilities()
        try:
            profile = self.get_stored(profile_id)
        except ProfileConfigurationError:
            snapshot = ProfileSnapshot.create(
                profile_id=profile_id,
                display_name="Unavailable local Profile",
                api_family="openai_responses",
                base_url="https://profile-unavailable.invalid/v1",
                model_id="unavailable",
                capabilities=capabilities,
            )
            return ProfileFailureMaterial(
                snapshot=snapshot,
                credential=ProfileCredential.from_plaintext("unavailable-local-credential"),
                error=ModelBindingError(message),
            )

        if profile.capability_test is not None:
            # Preserve the last declared task contract so a successful current
            # probe followed by explicit Retry can reuse the frozen semantic plan.
            # ProfileCatalog.resolve still blocks every Provider call while this
            # evidence is missing or stale.
            capabilities = profile.capability_test.capabilities
        credential_value = profile.api_key.get_secret_value()
        credential = ProfileCredential.from_plaintext(
            credential_value or "unavailable-local-credential"
        )
        snapshot = ProfileSnapshot.create(
            profile_id=profile.id,
            display_name=profile.display_name,
            api_family=profile.api_family,
            base_url=profile.base_url,
            model_id=profile.model_id,
            capabilities=capabilities,
            request_options=profile.request_options,
        )
        error: ModelBindingError
        if profile.enabled:
            error = ProfileCapabilityError(message)
        else:
            error = ModelBindingError(message)
        return ProfileFailureMaterial(
            snapshot=snapshot,
            credential=credential,
            error=error,
        )

    @staticmethod
    def _to_public(profile: StoredProfile) -> PublicProfile:
        evidence = profile.capability_test
        if evidence is None:
            status: Literal["missing", "stale", "ready"] = "missing"
            capabilities = None
        elif (
            evidence.source != "pydantic-ai-capability-v1"
            or evidence.profile_fingerprint != profile.configuration_fingerprint
        ):
            status = "stale"
            capabilities = evidence.capabilities
        else:
            status = "ready"
            capabilities = evidence.capabilities
        return PublicProfile(
            id=profile.id,
            display_name=profile.display_name,
            api_family=profile.api_family,
            base_url=profile.base_url,
            model_id=profile.model_id,
            request_options=profile.request_options,
            enabled=profile.enabled,
            has_api_key=bool(profile.api_key.get_secret_value()),
            capability_status=status,
            capabilities=capabilities,
            configuration_fingerprint=profile.configuration_fingerprint,
            capability_fingerprint=(None if capabilities is None else capabilities.fingerprint),
        )


def profile_configuration_fingerprint(
    *,
    api_family: ApiFamily,
    base_url: str,
    model_id: str,
    request_options: dict[str, JsonValue],
) -> str:
    return content_hash(
        {
            "api_family": api_family,
            "base_url": base_url.rstrip("/"),
            "model_id": model_id,
            "request_options": request_options,
        }
    )


def with_capability_evidence(
    document: ProfilesDocument,
    *,
    profile_id: str,
    evidence: CapabilityEvidence,
    expected_profile: StoredProfile | None = None,
) -> ProfilesDocument:
    """Bind server-produced evidence to the exact Profile used by a probe."""

    updated_profiles: list[StoredProfile] = []
    matched = False
    for profile in document.profiles:
        if profile.id != profile_id:
            updated_profiles.append(profile)
            continue
        matched = True
        if expected_profile is not None and profile != expected_profile:
            # The configuration fingerprint deliberately excludes credentials.
            # Comparing the captured Profile also detects key and enabled edits.
            raise ProfileConfigurationError("Profile changed during capability probing.")
        if evidence.profile_fingerprint != profile.configuration_fingerprint:
            raise ProfileConfigurationError("Capability evidence does not match the Profile.")
        updated_profiles.append(profile.model_copy(update={"capability_test": evidence}))
    if not matched:
        raise ProfileConfigurationError("Profile does not exist.")
    return document.model_copy(update={"profiles": updated_profiles})


def encode_profiles_document(value: dict[str, Any] | ProfilesDocument) -> bytes:
    validated = ProfilesDocument.model_validate(value)
    payload = {
        "schema_version": validated.schema_version,
        "selected_profile_id": validated.selected_profile_id,
        "profiles": [
            {
                "id": profile.id,
                "display_name": profile.display_name,
                "api_family": profile.api_family,
                "base_url": profile.base_url,
                "api_key": profile.api_key.get_secret_value(),
                "model_id": profile.model_id,
                "request_options": profile.request_options,
                "enabled": profile.enabled,
                "capability_test": (
                    None
                    if profile.capability_test is None
                    else profile.capability_test.model_dump(mode="json")
                ),
            }
            for profile in validated.profiles
        ],
    }
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
