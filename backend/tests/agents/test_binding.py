from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError
from pydantic_ai.models.openai import OpenAIResponsesModel

from app.agents.binding import (
    ModelBindingResolver,
    ProfileCapabilityError,
    ProfileCredential,
    ProfileFingerprintError,
    UnknownApiFamilyError,
)
from app.agents.contracts import ProfileCapabilities, ProfileSnapshot


def _profile(*, model_id: str, api_family: str = "openai_responses") -> ProfileSnapshot:
    capabilities = ProfileCapabilities(
        text_streaming=True,
        native_json_schema=True,
        tool_calling=False,
    )
    return ProfileSnapshot.create(
        profile_id=f"profile-{model_id}",
        display_name=model_id,
        api_family=api_family,
        base_url="https://provider.example/v1",
        model_id=model_id,
        capabilities=capabilities,
    )


def test_model_id_is_opaque_within_one_api_family() -> None:
    resolver = ModelBindingResolver()
    credential = ProfileCredential.from_plaintext("test-secret")
    grok = _profile(model_id="grok-4.5")
    gpt = _profile(model_id="gpt-observation-candidate")

    grok_binding = resolver.resolve(
        profile=grok,
        expected_profile_fingerprint=grok.fingerprint,
        required_capabilities=("native_json_schema",),
        model_request_limit=2,
        credential=credential,
    )
    gpt_binding = resolver.resolve(
        profile=gpt,
        expected_profile_fingerprint=gpt.fingerprint,
        required_capabilities=("native_json_schema",),
        model_request_limit=2,
        credential=credential,
    )
    try:
        assert grok_binding.adapter_key == gpt_binding.adapter_key == "openai_responses"
        assert isinstance(grok_binding.model.wrapped, OpenAIResponsesModel)
        assert type(grok_binding.model.wrapped) is type(gpt_binding.model.wrapped)
        assert grok_binding.model.model_name == "grok-4.5"
        assert gpt_binding.model.model_name == "gpt-observation-candidate"
    finally:
        asyncio.run(grok_binding.aclose())
        asyncio.run(gpt_binding.aclose())


def test_binding_preflight_failures_make_zero_provider_requests() -> None:
    resolver = ModelBindingResolver()
    credential = ProfileCredential.from_plaintext("test-secret")
    missing = ProfileSnapshot.create(
        profile_id="missing-native",
        display_name="Missing native schema",
        api_family="openai_responses",
        base_url="https://provider.example/v1",
        model_id="opaque-model",
        capabilities=ProfileCapabilities(text_streaming=True, native_json_schema=False),
    )
    with pytest.raises(ProfileCapabilityError, match="native_json_schema"):
        resolver.resolve(
            profile=missing,
            expected_profile_fingerprint=missing.fingerprint,
            required_capabilities=("native_json_schema",),
            model_request_limit=2,
            credential=credential,
        )

    unknown = _profile(model_id="opaque-model", api_family="future_wire_protocol")
    with pytest.raises(UnknownApiFamilyError, match="future_wire_protocol"):
        resolver.resolve(
            profile=unknown,
            expected_profile_fingerprint=unknown.fingerprint,
            required_capabilities=("native_json_schema",),
            model_request_limit=2,
            credential=credential,
        )

    profile = _profile(model_id="opaque-model")
    with pytest.raises(ProfileFingerprintError):
        resolver.resolve(
            profile=profile,
            expected_profile_fingerprint="0" * 64,
            required_capabilities=("native_json_schema",),
            model_request_limit=2,
            credential=credential,
        )


def test_profile_cannot_override_t1_or_embed_secrets_in_url() -> None:
    capabilities = ProfileCapabilities(text_streaming=True, native_json_schema=True)
    with pytest.raises(ValidationError, match="transport policy"):
        ProfileSnapshot.create(
            profile_id="profile-a",
            display_name="Profile A",
            api_family="openai_responses",
            base_url="https://provider.example/v1",
            model_id="opaque-model",
            capabilities=capabilities,
            request_options={"timeout": 1},
        )
    with pytest.raises(ValidationError, match="credentials"):
        ProfileSnapshot.create(
            profile_id="profile-a",
            display_name="Profile A",
            api_family="openai_responses",
            base_url="https://secret@provider.example/v1",
            model_id="opaque-model",
            capabilities=capabilities,
        )
