from __future__ import annotations

import asyncio

import pytest
from app.authoring.domain.models import AuthoringProfileSnapshot
from app.authoring.models import FallbackModelPlatform, ProviderAttemptFailure
from pydantic import ValidationError


def _profile(
    identifier: str, capabilities: frozenset[str] | None = None
) -> AuthoringProfileSnapshot:
    return AuthoringProfileSnapshot(
        profile_id=identifier,
        provider_protocol="fake",
        model_id=identifier,
        context_window=8_192,
        max_output_tokens=1_024,
        capabilities=capabilities or frozenset({"text_output", "tool_calling"}),
    )


def test_fallback_is_allowed_only_before_output_and_tool_side_effects() -> None:
    async def exercise() -> None:
        called: list[str] = []

        async def request(profile: AuthoringProfileSnapshot) -> str:
            called.append(profile.profile_id)
            if profile.profile_id == "primary":
                raise ProviderAttemptFailure("503", retryable=True)
            return "complete response"

        result = await FallbackModelPlatform(_profile("primary"), _profile("fallback")).call(
            request
        )
        assert called == ["primary", "fallback"]
        assert result.actual_profile.profile_id == "fallback"
        assert result.fallback_from == "primary"

        for emitted, side_effect in ((True, False), (False, True)):

            def unsafe_request(emitted_value: bool, side_effect_value: bool) -> object:
                async def unsafe(_profile_value: AuthoringProfileSnapshot) -> str:
                    raise ProviderAttemptFailure(
                        "interrupted",
                        retryable=True,
                        emitted_output=emitted_value,
                        tool_side_effect=side_effect_value,
                    )

                return unsafe

            with pytest.raises(ProviderAttemptFailure):
                await FallbackModelPlatform(_profile("primary"), _profile("fallback")).call(
                    unsafe_request(emitted, side_effect)  # type: ignore[arg-type]
                )

    asyncio.run(exercise())


def test_profile_metadata_fingerprint_and_fallback_capabilities_are_validated() -> None:
    with pytest.raises(ValidationError, match="fingerprint"):
        AuthoringProfileSnapshot(
            profile_id="bad",
            provider_protocol="fake",
            model_id="fake",
            context_window=4_096,
            max_output_tokens=512,
            fingerprint="stale",
        )
    with pytest.raises(ValueError, match="tool_calling"):
        FallbackModelPlatform(
            _profile("primary"),
            _profile("fallback", frozenset({"text_output"})),
        )
