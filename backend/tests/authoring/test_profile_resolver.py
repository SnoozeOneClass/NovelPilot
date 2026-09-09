from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from app.authoring.domain.models import InstructionKind, TargetLength, WorkerRole
from app.authoring.models import (
    AuthoringMetadataDocument,
    AuthoringModelMetadata,
    AuthoringProfileResolver,
)
from app.authoring.models.catalog import (
    CapabilityEvidence,
    ProfileCatalog,
    ProfileConfigurationError,
    ProfilesDocument,
    StoredProfile,
    encode_profiles_document,
)
from app.authoring.models.contracts import ProfileCapabilities
from app.authoring.runtime import route
from app.authoring.store import AuthoringStore


def _stored(identifier: str) -> StoredProfile:
    profile = StoredProfile(
        id=identifier,
        display_name=identifier,
        api_family="openai_responses",
        base_url="https://provider.invalid/v1",
        api_key="secret-for-contract-only",
        model_id=identifier,
        request_options={"max_tokens": 512},
    )
    evidence = CapabilityEvidence(
        checked_at="2026-09-04T00:00:00Z",
        profile_fingerprint=profile.configuration_fingerprint,
        source="pydantic-ai-capability-v1",
        capabilities=ProfileCapabilities(tool_calling=True, text_streaming=True),
    )
    return profile.model_copy(update={"capability_test": evidence})


def _write_catalog(path: Path, profiles: list[StoredProfile]) -> None:
    path.write_bytes(
        encode_profiles_document(
            ProfilesDocument(selected_profile_id=profiles[0].id, profiles=profiles)
        )
    )


def _write_metadata(path: Path, profiles: list[StoredProfile]) -> None:
    document = AuthoringMetadataDocument(
        profiles=[
            AuthoringModelMetadata(
                profile_id=profile.id,
                configuration_fingerprint=profile.configuration_fingerprint,
                context_window=8_192,
                max_output_tokens=512,
                input_price_per_million=1.25,
                output_price_per_million=5.0,
            )
            for profile in profiles
        ]
    )
    path.write_text(document.model_dump_json(indent=2), encoding="utf-8")


def test_catalog_resolver_freezes_role_binding_and_compatible_fallback(tmp_path: Path) -> None:
    async def exercise() -> None:
        primary = _stored("primary")
        fallback = _stored("fallback")
        profiles_path = tmp_path / "profiles.json"
        metadata_path = tmp_path / "metadata.json"
        _write_catalog(profiles_path, [primary, fallback])
        _write_metadata(metadata_path, [primary, fallback])
        store = AuthoringStore(tmp_path / "authoring.sqlite3")
        await store.migrate()
        await store.create_project(
            brief="idea",
            target=TargetLength.resolve(target_chapters=1),
            project_id="p1",
            profile_bindings={"writer": "primary", "fallback:writer": "fallback"},
        )
        resolver = AuthoringProfileResolver(ProfileCatalog(profiles_path), metadata_path)

        selection = await resolver.resolve(store, "p1", WorkerRole.WRITER)
        try:
            assert selection.snapshot.profile_id == "primary"
            assert selection.fallback_snapshot is not None
            assert selection.fallback_snapshot.profile_id == "fallback"
            assert selection.snapshot.context_window == 8_192
            assert selection.snapshot.input_price_per_million == 1.25
            assert "secret-for-contract-only" not in selection.snapshot.model_dump_json()
            assert "secret-for-contract-only" not in repr(selection)
            assert selection.redaction_secrets

            state = await store.load_state("p1")
            instruction = route(state)
            assert instruction is not None
            await store.set_active_instruction(
                "p1",
                instruction.instruction_key,
                InstructionKind.CREATE_FOUNDATION.value,
                instruction.logical_target,
                instruction.fact_version,
            )
            with pytest.raises(ValueError, match="Episode boundary"):
                await store.update_profile_bindings("p1", {"writer": "fallback"})
            await store.set_active_instruction("p1", None, None, None)
            await store.update_profile_bindings("p1", {"writer": "fallback"})

            next_selection = await resolver.resolve(store, "p1", WorkerRole.WRITER)
            try:
                assert selection.snapshot.profile_id == "primary"
                assert next_selection.snapshot.profile_id == "fallback"
            finally:
                await next_selection.aclose()
        finally:
            await selection.aclose()

    asyncio.run(exercise())


def test_resolver_rejects_same_primary_and_fallback_profile(tmp_path: Path) -> None:
    async def exercise() -> None:
        profile = _stored("only")
        profiles_path = tmp_path / "profiles.json"
        metadata_path = tmp_path / "metadata.json"
        _write_catalog(profiles_path, [profile])
        _write_metadata(metadata_path, [profile])
        store = AuthoringStore(tmp_path / "authoring.sqlite3")
        await store.migrate()
        await store.create_project(
            brief="idea",
            target=TargetLength.resolve(target_chapters=1),
            project_id="p1",
            profile_bindings={"default": "only", "fallback": "only"},
        )
        resolver = AuthoringProfileResolver(ProfileCatalog(profiles_path), metadata_path)

        with pytest.raises(ProfileConfigurationError, match="must be different"):
            await resolver.resolve(store, "p1", WorkerRole.WRITER)

    asyncio.run(exercise())


def test_stale_authoring_metadata_fails_before_binding_or_provider_request(tmp_path: Path) -> None:
    async def exercise() -> None:
        profile = _stored("primary")
        profiles_path = tmp_path / "profiles.json"
        metadata_path = tmp_path / "metadata.json"
        _write_catalog(profiles_path, [profile])
        metadata_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "profiles": [
                        {
                            "profile_id": "primary",
                            "configuration_fingerprint": "0" * 64,
                            "context_window": 8192,
                            "max_output_tokens": 512,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        store = AuthoringStore(tmp_path / "authoring.sqlite3")
        await store.migrate()
        await store.create_project(
            brief="idea",
            target=TargetLength.resolve(target_chapters=1),
            project_id="p1",
            profile_bindings={"default": "primary"},
        )
        resolver = AuthoringProfileResolver(ProfileCatalog(profiles_path), metadata_path)

        with pytest.raises(ProfileConfigurationError, match="metadata is stale"):
            await resolver.resolve(store, "p1", WorkerRole.WRITER)

        assert await store.scalar("SELECT count(*) FROM worker_episodes") == 0

    asyncio.run(exercise())
