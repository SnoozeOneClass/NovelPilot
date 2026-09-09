from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from app.authoring.context.manager import ContextBudgetManager
from app.authoring.domain.models import AuthoringProfileSnapshot
from app.authoring.errors import ContextCompactionError
from app.authoring.models.catalog import (
    CapabilityEvidence,
    ProfileCatalog,
    ProfileConfigurationError,
    ProfilesDocument,
    StoredProfile,
    encode_profiles_document,
    with_capability_evidence,
)
from app.authoring.models.contracts import (
    ApiFamily,
    JsonValue,
    validate_profile_base_url,
    validate_profile_request_options,
)
from app.authoring.models.persistence import atomic_write, profile_settings_lock
from app.authoring.models.probe import ProfileCapabilityProbeError, probe_stored_profile
from app.authoring.models.profiles import (
    AuthoringMetadataDocument,
    AuthoringModelMetadata,
    encode_authoring_metadata,
    load_authoring_metadata,
    prepare_authoring_model_metadata,
)


class ModelSettingsConflictError(ProfileConfigurationError):
    """Settings changed while a capability probe was in flight."""


class ModelSettingsInputError(ValueError):
    """An actionable settings error whose message contains no input values."""


class ModelSettingsProfileInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    display_name: str = Field(min_length=1)
    api_family: ApiFamily
    base_url: str
    model_id: str = Field(min_length=1)
    enabled: bool = True
    api_key: SecretStr | None = Field(default=None, exclude=True, repr=False)
    request_options: dict[str, JsonValue] = Field(default_factory=dict)
    context_window: int = Field(strict=True, ge=1)
    max_output_tokens: int = Field(strict=True, ge=1)
    input_price_per_million: float = Field(default=0, ge=0, allow_inf_nan=False)
    output_price_per_million: float = Field(default=0, ge=0, allow_inf_nan=False)
    cache_price_per_million: float = Field(default=0, ge=0, allow_inf_nan=False)

    @field_validator("display_name", "model_id")
    @classmethod
    def non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Profile identity fields must be non-blank.")
        return value

    @model_validator(mode="after")
    def valid_configuration(self) -> ModelSettingsProfileInput:
        try:
            validate_profile_base_url(self.api_family, self.base_url)
        except ValueError:
            raise ValueError(
                "base_url must be an HTTP(S) endpoint for the selected protocol, "
                "without credentials, query parameters or fragments."
            ) from None
        try:
            validate_profile_request_options(self.api_family, self.request_options)
            _validate_nested_options(self.request_options)
        except ValueError:
            # The catalog validator may name arbitrary nested option keys.
            # API errors must never echo those user-controlled paths or values.
            raise ValueError(
                "request_options must use a positive max_tokens value when supplied "
                "(required for Anthropic) and cannot contain credentials or transport overrides."
            ) from None
        if (
            "max_tokens" in self.request_options
            and self.request_options["max_tokens"] != self.max_output_tokens
        ):
            raise ValueError("max_output_tokens must equal request_options.max_tokens.")
        metadata = AuthoringModelMetadata(
            profile_id="settings",
            configuration_fingerprint="0" * 64,
            context_window=self.context_window,
            max_output_tokens=self.max_output_tokens,
        )
        _validate_input_budget(metadata)
        return self


class ModelSettingsProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    profile_id: str
    display_name: str
    api_family: ApiFamily
    base_url: str
    model_id: str
    enabled: bool
    has_api_key: bool
    request_options: dict[str, JsonValue]
    capability_status: Literal["missing", "stale", "ready"]
    context_window: int | None
    max_output_tokens: int | None
    input_price_per_million: float
    output_price_per_million: float
    cache_price_per_million: float
    metadata_version: int | None


class ModelSettingsDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    selected_profile_id: str | None
    profiles: list[ModelSettingsProfile]


class ModelSettingsDefaultInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    profile_id: str = Field(min_length=1)


def _validate_input_budget(metadata: AuthoringModelMetadata) -> None:
    try:
        ContextBudgetManager().input_threshold(
            AuthoringProfileSnapshot(
                profile_id=metadata.profile_id,
                provider_protocol="settings",
                model_id="settings",
                context_window=metadata.context_window,
                max_output_tokens=metadata.max_output_tokens,
            )
        )
    except ContextCompactionError:
        raise ValueError(
            "context_window must leave a positive input budget after reserves."
        ) from None


def _validate_nested_options(value: JsonValue) -> None:
    # The catalog handles nested objects. Also visit objects inside JSON lists
    # before accepting write-only HTTP input so credentials cannot hide there.
    if isinstance(value, dict):
        validate_profile_request_options("openai_responses", value)
        for item in value.values():
            _validate_nested_options(item)
    elif isinstance(value, list):
        for item in value:
            _validate_nested_options(item)


def _metadata_is_current(profile: StoredProfile, metadata: AuthoringModelMetadata | None) -> bool:
    if metadata is None or metadata.configuration_fingerprint != profile.configuration_fingerprint:
        return False
    if (
        profile.request_options.get("max_tokens", metadata.max_output_tokens)
        != metadata.max_output_tokens
    ):
        return False
    try:
        _validate_input_budget(metadata)
    except ValueError:
        return False
    return True


def _projection(
    document: ProfilesDocument,
    metadata_document: AuthoringMetadataDocument,
    *,
    current_metadata_only: bool = False,
) -> ModelSettingsDocument:
    metadata_by_id = {item.profile_id: item for item in metadata_document.profiles}
    profiles: list[ModelSettingsProfile] = []
    for profile in document.profiles:
        metadata = metadata_by_id.get(profile.id)
        evidence = profile.capability_test
        has_api_key = bool(profile.api_key.get_secret_value().strip())
        status: Literal["missing", "stale", "ready"] = "missing"
        if evidence is not None:
            status = "stale"
            if (
                profile.enabled
                and has_api_key
                and evidence.profile_fingerprint == profile.configuration_fingerprint
                and evidence.capabilities.text_output
                and evidence.capabilities.tool_calling
                and _metadata_is_current(profile, metadata)
            ):
                status = "ready"
        if current_metadata_only and not _metadata_is_current(profile, metadata):
            metadata = None
        profiles.append(
            ModelSettingsProfile(
                profile_id=profile.id,
                display_name=profile.display_name,
                api_family=profile.api_family,
                base_url=profile.base_url,
                model_id=profile.model_id,
                enabled=profile.enabled,
                has_api_key=has_api_key,
                request_options=profile.request_options,
                capability_status=status,
                context_window=None if metadata is None else metadata.context_window,
                max_output_tokens=None if metadata is None else metadata.max_output_tokens,
                input_price_per_million=0 if metadata is None else metadata.input_price_per_million,
                output_price_per_million=0
                if metadata is None
                else metadata.output_price_per_million,
                cache_price_per_million=0 if metadata is None else metadata.cache_price_per_million,
                metadata_version=None if metadata is None else metadata.metadata_version,
            )
        )
    result = ModelSettingsDocument(
        selected_profile_id=document.selected_profile_id, profiles=profiles
    )
    encoded = result.model_dump_json()
    for profile in document.profiles:
        secret = profile.api_key.get_secret_value()
        if secret and json.dumps(secret, ensure_ascii=False)[1:-1] in encoded:
            raise ProfileConfigurationError("Public model fields cannot contain API key values.")
    return result


class ModelSettingsService:
    """Edit local model settings without trusting client-supplied execution evidence."""

    def __init__(self, catalog: ProfileCatalog, metadata_path: Path) -> None:
        self.catalog = catalog
        self.metadata_path = metadata_path
        if catalog.path.resolve() == metadata_path.resolve():
            raise ProfileConfigurationError("Profile and metadata paths must be different.")

    def get(self, *, current_metadata_only: bool = False) -> ModelSettingsDocument:
        with profile_settings_lock(self.catalog.path):
            document = self.catalog.load()
            try:
                metadata = load_authoring_metadata(self.metadata_path, allow_missing=True)
            except ProfileConfigurationError:
                metadata = AuthoringMetadataDocument(profiles=[])
            return _projection(document, metadata, current_metadata_only=current_metadata_only)

    def save(self, profile_id: str, body: ModelSettingsProfileInput) -> ModelSettingsDocument:
        with profile_settings_lock(self.catalog.path):
            document = self.catalog.load()
            metadata_document = load_authoring_metadata(self.metadata_path, allow_missing=True)
            existing = next((item for item in document.profiles if item.id == profile_id), None)
            api_key = body.api_key
            if api_key is None or not api_key.get_secret_value().strip():
                api_key = SecretStr("") if existing is None else existing.api_key
            if body.enabled and not api_key.get_secret_value().strip():
                raise ModelSettingsInputError("An enabled Profile requires an API key.")
            profile = StoredProfile(
                id=profile_id,
                display_name=body.display_name,
                api_family=body.api_family,
                base_url=body.base_url,
                model_id=body.model_id,
                enabled=body.enabled,
                api_key=api_key,
                request_options=body.request_options,
            )
            if (
                existing is not None
                and profile.configuration_fingerprint == existing.configuration_fingerprint
                and profile.api_key == existing.api_key
            ):
                profile = profile.model_copy(update={"capability_test": existing.capability_test})
            updated_metadata, _ = prepare_authoring_model_metadata(
                profile,
                metadata_document,
                context_window=body.context_window,
                max_output_tokens=body.max_output_tokens,
                input_price_per_million=body.input_price_per_million,
                output_price_per_million=body.output_price_per_million,
                cache_price_per_million=body.cache_price_per_million,
            )
            profiles = [profile if item.id == profile_id else item for item in document.profiles]
            if existing is None:
                profiles.append(profile)
            updated = document.model_copy(update={"profiles": profiles})
            # Validate both documents before publishing. A changed connection or
            # credential is published without evidence first, so failure/crash
            # before the sidecar write cannot expose a partially updated ready Profile.
            profile_bytes = encode_profiles_document(updated)
            metadata_bytes = encode_authoring_metadata(updated_metadata)
            result = _projection(updated, updated_metadata)
            staging = updated
            if (
                updated != document
                and updated_metadata != metadata_document
                and profile.capability_test is not None
            ):
                staging = updated.model_copy(
                    update={
                        "profiles": [
                            item.model_copy(update={"capability_test": None})
                            if item.id == profile_id
                            else item
                            for item in updated.profiles
                        ]
                    }
                )
            if updated != document:
                atomic_write(self.catalog.path, encode_profiles_document(staging))
            if updated_metadata != metadata_document:
                atomic_write(self.metadata_path, metadata_bytes)
            if staging != updated:
                atomic_write(self.catalog.path, profile_bytes)
            return result

    async def validate(self, profile_id: str) -> ModelSettingsDocument:
        profile, metadata = await asyncio.to_thread(self._probe_input, profile_id)
        evidence = await probe_stored_profile(profile, require_tool_calling=True)
        if not all(
            (
                evidence.capabilities.text_output,
                evidence.capabilities.text_streaming,
                evidence.capabilities.native_json_schema,
                evidence.capabilities.tool_calling,
                evidence.capabilities.usage_reporting,
            )
        ):
            raise ProfileCapabilityProbeError(
                "The model did not pass all required capability checks."
            )
        return await asyncio.to_thread(self._save_probe, profile, metadata, evidence)

    def _probe_input(self, profile_id: str) -> tuple[StoredProfile, AuthoringModelMetadata]:
        with profile_settings_lock(self.catalog.path):
            profile = self.catalog.get_stored(profile_id)
            metadata = next(
                (
                    item
                    for item in load_authoring_metadata(self.metadata_path).profiles
                    if item.profile_id == profile_id
                ),
                None,
            )
            if not profile.enabled or not profile.api_key.get_secret_value().strip():
                raise ModelSettingsInputError(
                    "Capability validation requires an enabled Profile with an API key."
                )
            if metadata is None or not _metadata_is_current(profile, metadata):
                raise ModelSettingsInputError(
                    "Save matching model context and output limits before validation."
                )
            return profile, metadata

    def _save_probe(
        self,
        profile: StoredProfile,
        metadata: AuthoringModelMetadata,
        evidence: CapabilityEvidence,
    ) -> ModelSettingsDocument:
        with profile_settings_lock(self.catalog.path):
            document = self.catalog.load()
            metadata_document = load_authoring_metadata(self.metadata_path)
            current_metadata = next(
                (item for item in metadata_document.profiles if item.profile_id == profile.id), None
            )
            current = next((item for item in document.profiles if item.id == profile.id), None)
            if current != profile or current_metadata != metadata:
                raise ModelSettingsConflictError("Model settings changed during validation; retry.")
            updated = with_capability_evidence(
                document, profile_id=profile.id, evidence=evidence, expected_profile=profile
            )
            atomic_write(self.catalog.path, encode_profiles_document(updated))
            return _projection(updated, metadata_document)

    def select_default(self, profile_id: str) -> ModelSettingsDocument:
        with profile_settings_lock(self.catalog.path):
            document = self.catalog.load()
            metadata = load_authoring_metadata(self.metadata_path, allow_missing=True)
            projected = _projection(document, metadata)
            if not any(
                item.profile_id == profile_id and item.capability_status == "ready"
                for item in projected.profiles
            ):
                raise ModelSettingsInputError(
                    "The default model must be enabled and successfully validated."
                )
            updated = document.model_copy(update={"selected_profile_id": profile_id})
            if updated != document:
                atomic_write(self.catalog.path, encode_profiles_document(updated))
            return _projection(updated, metadata)
