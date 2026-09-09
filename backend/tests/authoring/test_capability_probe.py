from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from app.authoring.models.binding import ProfileCredential, ResolvedModelBinding
from app.authoring.models.catalog import StoredProfile
from app.authoring.models.contracts import ProfileSnapshot
from app.authoring.models.probe import (
    PROBE_MARKER,
    ProfileCapabilityProbeError,
    probe_stored_profile,
)
from app.authoring.models.transport import ActivationRequestBudget, RequestCountingModel
from pydantic import SecretStr
from pydantic_ai.models.function import (
    AgentInfo,
    DeltaToolCall,
    DeltaToolCalls,
    FunctionModel,
)


class FunctionProbeResolver:
    def resolve(
        self,
        *,
        profile: ProfileSnapshot,
        expected_profile_fingerprint: str,
        required_capabilities: object,
        model_request_limit: int,
        credential: ProfileCredential,
    ) -> ResolvedModelBinding:
        del expected_profile_fingerprint, required_capabilities, credential
        assert profile.request_options == {"max_tokens": 65_536}
        budget = ActivationRequestBudget(
            model_request_limit=model_request_limit,
            protocol=profile.api_family,
        )

        async def response(
            _messages: list[object],
            info: AgentInfo,
        ) -> AsyncIterator[str | DeltaToolCalls]:
            if info.model_request_parameters.function_tools:
                yield {
                    0: DeltaToolCall(
                        name="capability_echo",
                        json_args=f'{{"marker":"{PROBE_MARKER}"}}',
                        tool_call_id="probe-call",
                    )
                }
            elif info.model_request_parameters.output_mode == "native":
                yield f'{{"marker":"{PROBE_MARKER}"}}'
            else:
                yield PROBE_MARKER

        model = RequestCountingModel(
            FunctionModel(stream_function=response, model_name="profile-probe-test"),
            budget=budget,
        )
        return ResolvedModelBinding(
            model=model,
            budget=budget,
            adapter_key=profile.api_family,
        )


def _stored_profile() -> StoredProfile:
    return StoredProfile(
        id="probe-profile",
        display_name="Probe Profile",
        api_family="anthropic_messages",
        base_url="https://provider.example",
        api_key=SecretStr("probe-secret"),
        model_id="opaque-model",
        request_options={"max_tokens": 65_536},
    )


def test_profile_probe_checks_structured_stream_tool_and_usage() -> None:
    profile = _stored_profile()

    evidence = asyncio.run(
        probe_stored_profile(
            profile,
            require_tool_calling=True,
            resolver=FunctionProbeResolver(),
            checked_at="2026-07-23T00:00:00+00:00",
        )
    )

    assert evidence.profile_fingerprint == profile.configuration_fingerprint
    assert evidence.checked_at == "2026-07-23T00:00:00+00:00"
    assert evidence.capabilities.text_streaming
    assert evidence.capabilities.native_json_schema
    assert evidence.capabilities.tool_calling
    assert evidence.capabilities.usage_reporting


class SecretEchoingResolver:
    def resolve(self, **_kwargs: object) -> ResolvedModelBinding:
        raise RuntimeError(
            "authorization: Bearer probe-secret https://provider.example?signature=probe-secret"
        )


def test_profile_probe_failure_redacts_credentials_and_signed_query_values() -> None:
    with pytest.raises(ProfileCapabilityProbeError) as captured:
        asyncio.run(
            probe_stored_profile(
                _stored_profile(),
                resolver=SecretEchoingResolver(),
            )
        )

    assert "probe-secret" not in str(captured.value)
    assert "[REDACTED]" in str(captured.value)
