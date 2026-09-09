from __future__ import annotations

import asyncio
import json

import httpx
import httpx2
import pytest
from app.authoring.models.binding import (
    AnthropicMessagesAdapter,
    ModelBindingError,
    ModelBindingResolver,
    OpenAIResponsesAdapter,
    ProfileCapabilityError,
    ProfileCredential,
    ProfileFingerprintError,
    UnknownApiFamilyError,
)
from app.authoring.models.contracts import ApiFamily, ProfileCapabilities, ProfileSnapshot
from pydantic import ValidationError
from pydantic_ai import Agent, models
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.openai import OpenAIResponsesModel


def _profile(
    *,
    model_id: str,
    api_family: ApiFamily = "openai_responses",
) -> ProfileSnapshot:
    capabilities = ProfileCapabilities(
        text_streaming=True,
        native_json_schema=True,
        tool_calling=False,
    )
    request_options = {"max_tokens": 65_536} if api_family == "anthropic_messages" else {}
    return ProfileSnapshot.create(
        profile_id=f"profile-{model_id}",
        display_name=model_id,
        api_family=api_family,
        base_url=(
            "https://provider.example"
            if api_family == "anthropic_messages"
            else "https://provider.example/v1"
        ),
        model_id=model_id,
        capabilities=capabilities,
        request_options=request_options,
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


def test_anthropic_model_id_is_opaque_and_uses_messages_adapter() -> None:
    resolver = ModelBindingResolver()
    credential = ProfileCredential.from_plaintext("test-secret")
    profile = _profile(
        model_id="gpt-name-does-not-select-responses",
        api_family="anthropic_messages",
    )

    binding = resolver.resolve(
        profile=profile,
        expected_profile_fingerprint=profile.fingerprint,
        required_capabilities=("native_json_schema",),
        model_request_limit=2,
        credential=credential,
    )
    try:
        assert binding.adapter_key == "anthropic_messages"
        assert isinstance(binding.model.wrapped, AnthropicModel)
        assert binding.model.model_name == "gpt-name-does-not-select-responses"
    finally:
        asyncio.run(binding.aclose())


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

    unknown = _profile(model_id="opaque-model", api_family="anthropic_messages")
    openai_only = ModelBindingResolver(adapters=[OpenAIResponsesAdapter()])
    with pytest.raises(UnknownApiFamilyError, match="anthropic_messages"):
        openai_only.resolve(
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


def test_anthropic_profile_requires_explicit_positive_max_tokens() -> None:
    capabilities = ProfileCapabilities(text_streaming=True, native_json_schema=True)
    with pytest.raises(ValidationError, match="explicit generous max_tokens"):
        ProfileSnapshot.create(
            profile_id="anthropic-profile",
            display_name="Anthropic profile",
            api_family="anthropic_messages",
            base_url="https://provider.example",
            model_id="opaque-model",
            capabilities=capabilities,
        )
    with pytest.raises(ValidationError, match="positive integer"):
        ProfileSnapshot.create(
            profile_id="anthropic-profile",
            display_name="Anthropic profile",
            api_family="anthropic_messages",
            base_url="https://provider.example",
            model_id="opaque-model",
            capabilities=capabilities,
            request_options={"max_tokens": True},
        )


def test_anthropic_sdk_rejects_removed_sampling_options_before_request() -> None:
    profile = ProfileSnapshot.create(
        profile_id="anthropic-profile",
        display_name="Anthropic profile",
        api_family="anthropic_messages",
        base_url="https://provider.example",
        model_id="opaque-model",
        capabilities=ProfileCapabilities(text_streaming=True),
        request_options={"max_tokens": 65_536, "temperature": 0.5},
    )
    adapter = AnthropicMessagesAdapter(
        transport_factory=lambda: httpx2.MockTransport(
            lambda request: httpx2.Response(200, request=request, json={})
        )
    )

    with pytest.raises(ModelBindingError, match="does not support request option"):
        ModelBindingResolver(adapters=[adapter]).resolve(
            profile=profile,
            expected_profile_fingerprint=profile.fingerprint,
            required_capabilities=("text_streaming",),
            model_request_limit=1,
            credential=ProfileCredential.from_plaintext("test-secret"),
        )


def test_protocol_base_url_joining_and_nested_secrets_fail_closed() -> None:
    capabilities = ProfileCapabilities(text_streaming=True, native_json_schema=True)
    with pytest.raises(ValidationError, match="must end in /v1"):
        ProfileSnapshot.create(
            profile_id="responses-profile",
            display_name="Responses profile",
            api_family="openai_responses",
            base_url="https://provider.example",
            model_id="opaque-model",
            capabilities=capabilities,
        )
    with pytest.raises(ValidationError, match="exclude the terminal /v1"):
        ProfileSnapshot.create(
            profile_id="anthropic-profile",
            display_name="Anthropic profile",
            api_family="anthropic_messages",
            base_url="https://provider.example/v1",
            model_id="opaque-model",
            capabilities=capabilities,
            request_options={"max_tokens": 65_536},
        )
    with pytest.raises(ValidationError, match="credentials or signed URL"):
        ProfileSnapshot.create(
            profile_id="responses-profile",
            display_name="Responses profile",
            api_family="openai_responses",
            base_url="https://provider.example/v1",
            model_id="opaque-model",
            capabilities=capabilities,
            request_options={"extra_headers": {"Authorization": "Bearer must-not-enter-snapshot"}},
        )


@pytest.mark.parametrize(
    ("api_family", "expected_path"),
    [
        ("openai_responses", "/v1/responses"),
        pytest.param(
            "anthropic_messages",
            "/v1/messages",
        ),
    ],
)
def test_protocol_adapters_emit_only_their_declared_wire_contract(
    api_family: ApiFamily,
    expected_path: str,
) -> None:
    captured: list[tuple[str, dict[str, object], object]] = []

    def handler(request: object):
        assert isinstance(request, (httpx.Request, httpx2.Request))
        captured.append(
            (
                request.url.path,
                json.loads(request.content.decode("utf-8")),
                request.headers,
            )
        )
        response_type = httpx.Response if api_family == "openai_responses" else httpx2.Response
        return response_type(
            400,
            request=request,
            json={"error": {"type": "invalid_request_error", "message": "probe stop"}},
        )

    adapter = (
        OpenAIResponsesAdapter(transport_factory=lambda: httpx.MockTransport(handler))
        if api_family == "openai_responses"
        else AnthropicMessagesAdapter(transport_factory=lambda: httpx2.MockTransport(handler))
    )
    profile = ProfileSnapshot.create(
        profile_id=f"{api_family}-capture",
        display_name="Protocol capture",
        api_family=api_family,
        base_url=(
            "https://provider.example/v1"
            if api_family == "openai_responses"
            else "https://provider.example"
        ),
        model_id="opaque-model",
        capabilities=ProfileCapabilities(text_streaming=True, native_json_schema=True),
        request_options=({"max_tokens": 65_536} if api_family == "anthropic_messages" else {}),
    )
    binding = ModelBindingResolver(adapters=[adapter]).resolve(
        profile=profile,
        expected_profile_fingerprint=profile.fingerprint,
        required_capabilities=("text_streaming",),
        model_request_limit=1,
        credential=ProfileCredential.from_plaintext("test-secret"),
    )

    async def exercise() -> None:
        binding.budget.begin_agent_run()
        async with binding:
            agent = Agent(binding.model, output_type=str)
            with pytest.raises(ModelHTTPError):
                async with agent.run_stream("hello"):
                    pass

    with models.override_allow_model_requests(True):
        asyncio.run(exercise())

    binding.budget.assert_terminal_invariants()
    assert binding.budget.provider_request_count == 1
    assert binding.budget.attempts[0].status_code == 400
    assert len(captured) == 1
    path, payload, raw_headers = captured[0]
    assert isinstance(raw_headers, (httpx.Headers, httpx2.Headers))
    headers = raw_headers
    assert path == expected_path
    assert payload["stream"] is True
    if api_family == "openai_responses":
        assert "max_output_tokens" not in payload
        assert "anthropic-version" not in headers
    else:
        assert payload["max_tokens"] == 65_536
        assert headers["anthropic-version"]
