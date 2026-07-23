from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from app.agents.binding import (
    ModelBindingError,
    ProfileCapabilityError,
    ProfileCredential,
)
from app.agents.contracts import (
    ApiFamily,
    JsonValue,
    ProfileCapabilities,
    ProfileSnapshot,
    validate_profile_base_url,
    validate_profile_request_options,
)
from app.store.content import prepare_canonical_json


class ProfileConfigurationError(RuntimeError):
    """A local profile cannot satisfy its explicit frozen runtime contract."""


class CapabilityEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    checked_at: str
    profile_fingerprint: str = Field(min_length=64, max_length=64)
    source: Literal["pydantic-ai-capability-v1", "legacy-responses-capability-v1"]
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
        return document.selected_profile_id, [self._to_public(profile) for profile in document.profiles]

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
    ) -> None:
        """Atomically replace one Profile's evidence while preserving local secrets."""

        document = self.load()
        updated_profiles: list[StoredProfile] = []
        matched = False
        for profile in document.profiles:
            if profile.id != profile_id:
                updated_profiles.append(profile)
                continue
            matched = True
            if evidence.profile_fingerprint != profile.configuration_fingerprint:
                raise ProfileConfigurationError(
                    f"Capability evidence does not match Profile {profile_id!r}."
                )
            updated_profiles.append(profile.model_copy(update={"capability_test": evidence}))
        if not matched:
            raise ProfileConfigurationError(f"Profile {profile_id!r} does not exist.")

        updated = document.model_copy(update={"profiles": updated_profiles})
        encoded = encode_profiles_document(updated)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_name(f".{self._path.name}.tmp")
        try:
            temporary.write_bytes(encoded)
            os.replace(temporary, self._path)
        finally:
            temporary.unlink(missing_ok=True)

    def resolve(self, profile_id: str) -> ResolvedProfile:
        profile = self.get_stored(profile_id)
        if not profile.enabled:
            raise ProfileConfigurationError(f"Profile {profile_id!r} is disabled.")
        evidence = profile.capability_test
        if evidence is None:
            raise ProfileConfigurationError(
                f"Profile {profile_id!r} has no capability evidence."
            )
        if evidence.source != "pydantic-ai-capability-v1":
            raise ProfileConfigurationError(
                f"Profile {profile_id!r} requires a current production Adapter capability probe."
            )
        if evidence.profile_fingerprint != profile.configuration_fingerprint:
            raise ProfileConfigurationError(
                f"Profile {profile_id!r} capability evidence is stale."
            )
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
            # evidence is missing, stale, or migration-only.
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
            capability_fingerprint=(
                None if capabilities is None else capabilities.fingerprint
            ),
        )


def profile_configuration_fingerprint(
    *,
    api_family: ApiFamily,
    base_url: str,
    model_id: str,
    request_options: dict[str, JsonValue],
) -> str:
    return prepare_canonical_json(
        {
            "api_family": api_family,
            "base_url": base_url.rstrip("/"),
            "model_id": model_id,
            "request_options": request_options,
        }
    ).sha256


def migrate_legacy_profiles(raw: dict[str, Any]) -> dict[str, Any]:
    """One-shot local-config migration; runtime loading never accepts the old schema."""
    migrated: list[dict[str, Any]] = []
    for value in raw.get("profiles", []):
        if not isinstance(value, dict) or value.get("protocol") != "openai-compatible":
            raise ProfileConfigurationError(
                "Only explicitly verified legacy Responses profiles can be migrated."
            )
        capability = value.get("capability_test")
        structured = capability.get("structured_output", {}) if isinstance(capability, dict) else {}
        tool = capability.get("tool_calling", {}) if isinstance(capability, dict) else {}
        ready = bool(isinstance(capability, dict) and capability.get("ready_for_harness"))
        capabilities = ProfileCapabilities(
            text_output=True,
            text_streaming=ready,
            native_json_schema=ready and bool(structured.get("ok")),
            tool_calling=bool(tool.get("ok")),
            usage_reporting=True,
        )
        request_options = value.get("request_options") or {}
        base_url = str(value.get("base_url", "")).rstrip("/")
        model_id = str(value.get("model", ""))
        fingerprint = profile_configuration_fingerprint(
            api_family="openai_responses",
            base_url=base_url,
            model_id=model_id,
            request_options=request_options,
        )
        evidence = None
        if isinstance(capability, dict):
            evidence = {
                "checked_at": str(capability.get("checked_at", "unknown")),
                "profile_fingerprint": fingerprint,
                "source": "legacy-responses-capability-v1",
                "capabilities": capabilities.model_dump(mode="json"),
            }
        migrated.append(
            {
                "id": value.get("id"),
                "display_name": value.get("name"),
                "api_family": "openai_responses",
                "base_url": base_url,
                "api_key": value.get("api_key"),
                "model_id": model_id,
                "request_options": request_options,
                "enabled": bool(value.get("enabled", True)),
                "capability_test": evidence,
            }
        )
    return {
        "schema_version": 2,
        "selected_profile_id": raw.get("active_profile_id"),
        "profiles": migrated,
    }


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
