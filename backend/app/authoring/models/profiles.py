from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_ai.models import Model

from app.authoring.domain.models import AuthoringProfileSnapshot, WorkerRole
from app.authoring.models.binding import ModelBindingResolver, ResolvedModelBinding
from app.authoring.models.catalog import (
    ProfileCatalog,
    ProfileConfigurationError,
    ResolvedProfile,
    StoredProfile,
)
from app.authoring.models.contracts import CapabilityName, ProfileSnapshot
from app.authoring.models.persistence import atomic_write, profile_settings_lock
from app.authoring.store import AuthoringStore

CAPABILITY_FRESHNESS_POLICY = "configuration-and-sidecar-fingerprint-no-wall-clock-ttl-v1"


class AuthoringModelMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    profile_id: str
    configuration_fingerprint: str = Field(min_length=64, max_length=64)
    context_window: int = Field(ge=1)
    max_output_tokens: int = Field(ge=1)
    input_price_per_million: float = Field(default=0, ge=0, allow_inf_nan=False)
    output_price_per_million: float = Field(default=0, ge=0, allow_inf_nan=False)
    cache_price_per_million: float = Field(default=0, ge=0, allow_inf_nan=False)
    metadata_version: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def output_fits_window(self) -> AuthoringModelMetadata:
        if self.max_output_tokens >= self.context_window:
            raise ValueError("max_output_tokens must be smaller than context_window")
        return self


class AuthoringMetadataDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    profiles: list[AuthoringModelMetadata]

    @model_validator(mode="after")
    def unique_profile_metadata(self) -> AuthoringMetadataDocument:
        identifiers = [item.profile_id for item in self.profiles]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("authoring model metadata contains duplicate Profile ids")
        return self


@dataclass(slots=True)
class EpisodeProfileSelection:
    snapshot: AuthoringProfileSnapshot
    model: Model | None = None
    binding: ResolvedModelBinding | None = None
    fallback_snapshot: AuthoringProfileSnapshot | None = None
    fallback_model: Model | None = None
    fallback_binding: ResolvedModelBinding | None = None
    redaction_secrets: tuple[str, ...] = field(default=(), repr=False)

    async def aclose(self) -> None:
        if self.binding is not None:
            await self.binding.aclose()
        if self.fallback_binding is not None:
            await self.fallback_binding.aclose()


class AuthoringProfileResolver:
    """Narrow adapter from the legacy secret catalog to authoring Episode profiles.

    The legacy profile document and Provider adapters keep their semantics. A
    separate secret-free sidecar supplies context and price metadata required by
    the long-running authoring harness. Capability evidence stays valid while
    both configuration and metadata fingerprints match; it deliberately has no
    wall-clock TTL so a long book cannot expire its frozen Profile mid-run.
    """

    def __init__(
        self,
        catalog: ProfileCatalog,
        metadata_path: Path,
        binding_resolver: ModelBindingResolver | None = None,
        model_request_limit: int = 24,
    ) -> None:
        self.catalog = catalog
        self.metadata_path = metadata_path
        self.binding_resolver = binding_resolver or ModelBindingResolver()
        self.model_request_limit = model_request_limit

    def load_metadata(self) -> AuthoringMetadataDocument:
        return load_authoring_metadata(self.metadata_path)

    def validate_bindings(self, bindings: dict[str, str]) -> None:
        selected = self.catalog.load().selected_profile_id
        profile_ids = set(bindings.values())
        for role in ("architect", "writer", "editor", "arbiter"):
            primary = bindings.get(role) or bindings.get("default") or selected
            if primary is None:
                raise ProfileConfigurationError("No default authoring Profile is configured.")
            profile_ids.add(primary)
            fallback = bindings.get(f"fallback:{role}") or bindings.get("fallback")
            if fallback == primary:
                raise ProfileConfigurationError(
                    f"Fallback Profile must differ from the {role} Profile."
                )
        for profile_id in profile_ids:
            self._validated_snapshot(profile_id)

    async def resolve(
        self, store: AuthoringStore, project_id: str, role: WorkerRole
    ) -> EpisodeProfileSelection:
        bindings = await store.profile_bindings(project_id)
        document = self.catalog.load()
        profile_id = (
            bindings.get(role.value) or bindings.get("default") or document.selected_profile_id
        )
        if profile_id is None:
            raise ProfileConfigurationError(
                f"No Profile is bound for authoring role {role.value!r}."
            )
        primary = self._resolve_one(profile_id)
        fallback_id = bindings.get(f"fallback:{role.value}") or bindings.get("fallback")
        if fallback_id == profile_id:
            await primary[2].aclose()
            raise ProfileConfigurationError("Primary and fallback Profiles must be different.")
        try:
            fallback = None if fallback_id is None else self._resolve_one(fallback_id)
        except BaseException:
            await primary[2].aclose()
            raise
        return EpisodeProfileSelection(
            snapshot=primary[0],
            model=primary[2].model,
            binding=primary[2],
            fallback_snapshot=None if fallback is None else fallback[0],
            fallback_model=None if fallback is None else fallback[2].model,
            fallback_binding=None if fallback is None else fallback[2],
            redaction_secrets=tuple(
                secret
                for secret in (
                    primary[1].credential.api_key.get_secret_value(),
                    None if fallback is None else fallback[1].credential.api_key.get_secret_value(),
                )
                if secret
            ),
        )

    def _resolve_one(
        self, profile_id: str
    ) -> tuple[AuthoringProfileSnapshot, ResolvedProfile, ResolvedModelBinding]:
        snapshot, resolved = self._validated_snapshot(profile_id)
        required = cast(tuple[CapabilityName, ...], ("text_output", "tool_calling"))
        binding = self.binding_resolver.resolve(
            profile=resolved.snapshot,
            expected_profile_fingerprint=resolved.snapshot.fingerprint,
            required_capabilities=required,
            model_request_limit=self.model_request_limit,
            credential=resolved.credential,
        )
        return snapshot, resolved, binding

    def _validated_snapshot(
        self, profile_id: str
    ) -> tuple[AuthoringProfileSnapshot, ResolvedProfile]:
        # API/CLI edits publish a catalog and a metadata sidecar. Freeze one
        # coherent pair before constructing an Episode's immutable selection.
        with profile_settings_lock(self.catalog.path):
            resolved = self.catalog.resolve(profile_id)
            stored = self.catalog.get_stored(profile_id)
            metadata = next(
                (item for item in self.load_metadata().profiles if item.profile_id == profile_id),
                None,
            )
            if metadata is None:
                raise ProfileConfigurationError(
                    f"Profile {profile_id!r} has no explicit authoring model metadata."
                )
            if metadata.configuration_fingerprint != stored.configuration_fingerprint:
                raise ProfileConfigurationError(
                    f"Profile {profile_id!r} authoring model metadata is stale."
                )
            request_max = stored.request_options.get("max_tokens")
            if request_max is not None and request_max != metadata.max_output_tokens:
                raise ProfileConfigurationError(
                    f"Profile {profile_id!r} authoring max output metadata disagrees with request options."
                )
            snapshot = self._authoring_snapshot(resolved.snapshot, metadata)
            snapshot.require("text_output", "tool_calling")
            return snapshot, resolved

    @staticmethod
    def _authoring_snapshot(
        profile: ProfileSnapshot, metadata: AuthoringModelMetadata
    ) -> AuthoringProfileSnapshot:
        capability_values = profile.capabilities.model_dump(mode="python")
        capabilities = frozenset(
            key
            for key, value in capability_values.items()
            if key != "contract_version" and value is True
        )
        return AuthoringProfileSnapshot(
            profile_id=profile.profile_id,
            provider_protocol=profile.api_family,
            model_id=profile.model_id,
            context_window=metadata.context_window,
            max_output_tokens=metadata.max_output_tokens,
            capabilities=capabilities,
            request_options=profile.request_options,
            input_price_per_million=metadata.input_price_per_million,
            output_price_per_million=metadata.output_price_per_million,
            cache_price_per_million=metadata.cache_price_per_million,
            metadata_version=metadata.metadata_version,
        )


def upsert_authoring_model_metadata(
    catalog: ProfileCatalog,
    metadata_path: Path,
    *,
    profile_id: str,
    context_window: int,
    max_output_tokens: int,
    input_price_per_million: float = 0,
    output_price_per_million: float = 0,
    cache_price_per_million: float = 0,
) -> AuthoringModelMetadata:
    """Atomically bind explicit authoring limits/prices to current Profile configuration."""

    with profile_settings_lock(catalog.path):
        document = load_authoring_metadata(metadata_path, allow_missing=True)
        updated, metadata = prepare_authoring_model_metadata(
            catalog.get_stored(profile_id),
            document,
            context_window=context_window,
            max_output_tokens=max_output_tokens,
            input_price_per_million=input_price_per_million,
            output_price_per_million=output_price_per_million,
            cache_price_per_million=cache_price_per_million,
        )
        if updated != document:
            atomic_write(metadata_path, encode_authoring_metadata(updated))
        return metadata


def load_authoring_metadata(
    path: Path, *, allow_missing: bool = False
) -> AuthoringMetadataDocument:
    if not path.exists():
        if allow_missing:
            return AuthoringMetadataDocument(profiles=[])
        raise ProfileConfigurationError("Authoring model metadata does not exist.")
    try:
        return AuthoringMetadataDocument.model_validate_json(path.read_bytes())
    except (OSError, ValueError) as error:
        raise ProfileConfigurationError("Existing authoring model metadata is invalid.") from error


def encode_authoring_metadata(document: AuthoringMetadataDocument) -> bytes:
    return (document.model_dump_json(indent=2) + "\n").encode("utf-8")


def prepare_authoring_model_metadata(
    profile: StoredProfile,
    document: AuthoringMetadataDocument,
    *,
    context_window: int,
    max_output_tokens: int,
    input_price_per_million: float = 0,
    output_price_per_million: float = 0,
    cache_price_per_million: float = 0,
) -> tuple[AuthoringMetadataDocument, AuthoringModelMetadata]:
    """Validate and version one metadata edit before either settings file is written."""

    configured_max = profile.request_options.get("max_tokens")
    if configured_max is not None and configured_max != max_output_tokens:
        raise ProfileConfigurationError(
            "max_output_tokens must equal the Profile request_options.max_tokens value"
        )
    existing = next((item for item in document.profiles if item.profile_id == profile.id), None)
    metadata_version = 1 if existing is None else existing.metadata_version
    metadata = AuthoringModelMetadata(
        profile_id=profile.id,
        configuration_fingerprint=profile.configuration_fingerprint,
        context_window=context_window,
        max_output_tokens=max_output_tokens,
        input_price_per_million=input_price_per_million,
        output_price_per_million=output_price_per_million,
        cache_price_per_million=cache_price_per_million,
        metadata_version=metadata_version,
    )
    if existing == metadata:
        return document, metadata
    if existing is not None:
        metadata = metadata.model_copy(update={"metadata_version": metadata_version + 1})
    profiles: list[AuthoringModelMetadata] = []
    replaced = False
    for item in document.profiles:
        if item.profile_id == profile.id:
            profiles.append(metadata)
            replaced = True
        else:
            profiles.append(item)
    if not replaced:
        profiles.append(metadata)
    return AuthoringMetadataDocument(profiles=profiles), metadata
