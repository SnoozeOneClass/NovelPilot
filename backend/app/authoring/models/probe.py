from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict
from pydantic_ai import Agent, NativeOutput
from pydantic_ai.messages import ModelRequest, ToolCallPart, UserPromptPart
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.settings import ModelSettings
from pydantic_ai.tools import ToolDefinition

from app.authoring.models.binding import (
    ModelBindingResolver,
    ProfileCredential,
    ResolvedModelBinding,
)
from app.authoring.models.catalog import CapabilityEvidence, StoredProfile
from app.authoring.models.contracts import CapabilityName, ProfileCapabilities, ProfileSnapshot

PROBE_MARKER = "NOVELPILOT_CAPABILITY_OK"


class ProfileCapabilityProbeError(RuntimeError):
    """A concrete Profile failed one of the production Adapter capability checks."""


class ProbeStructuredResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    marker: Literal["NOVELPILOT_CAPABILITY_OK"]


class ProbeResolver(Protocol):
    def resolve(
        self,
        *,
        profile: ProfileSnapshot,
        expected_profile_fingerprint: str,
        required_capabilities: tuple[CapabilityName, ...],
        model_request_limit: int,
        credential: ProfileCredential,
    ) -> ResolvedModelBinding: ...


async def probe_stored_profile(
    profile: StoredProfile,
    *,
    require_tool_calling: bool = False,
    resolver: ProbeResolver | None = None,
    checked_at: str | None = None,
) -> CapabilityEvidence:
    """Probe one exact configuration through its production protocol Adapter."""

    capabilities = ProfileCapabilities(
        text_output=True,
        text_streaming=True,
        native_json_schema=True,
        tool_calling=require_tool_calling,
        usage_reporting=True,
    )
    snapshot = ProfileSnapshot.create(
        profile_id=profile.id,
        display_name=profile.display_name,
        api_family=profile.api_family,
        base_url=profile.base_url,
        model_id=profile.model_id,
        request_options=profile.request_options,
        capabilities=capabilities,
    )
    credential = ProfileCredential.from_plaintext(profile.api_key.get_secret_value())
    binding_resolver = resolver or ModelBindingResolver()
    try:
        await _probe_structured(
            snapshot=snapshot,
            credential=credential,
            resolver=binding_resolver,
        )
        await _probe_text_stream(
            snapshot=snapshot,
            credential=credential,
            resolver=binding_resolver,
        )
        if require_tool_calling:
            await _probe_tool_call(
                snapshot=snapshot,
                credential=credential,
                resolver=binding_resolver,
            )
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        sanitized = _redact_probe_message(
            str(exc),
            secret=profile.api_key.get_secret_value(),
        )
        raise ProfileCapabilityProbeError(
            f"Profile {profile.id!r} failed capability probing: {sanitized}"
        ) from exc

    return CapabilityEvidence(
        checked_at=checked_at or datetime.now(UTC).isoformat(),
        profile_fingerprint=profile.configuration_fingerprint,
        source="pydantic-ai-capability-v1",
        capabilities=capabilities,
    )


async def _probe_structured(
    *,
    snapshot: ProfileSnapshot,
    credential: ProfileCredential,
    resolver: ProbeResolver,
) -> None:
    binding = _resolve_probe_binding(
        snapshot=snapshot,
        credential=credential,
        resolver=resolver,
        required_capabilities=("native_json_schema", "usage_reporting"),
    )
    async with binding:
        binding.budget.begin_agent_run()
        agent = Agent(
            binding.model,
            output_type=NativeOutput(ProbeStructuredResult, strict=True),
            retries={"tools": 0, "output": 1},
        )
        result = await agent.run(
            "Return the exact marker NOVELPILOT_CAPABILITY_OK in the required native schema."
        )
        if result.output.marker != PROBE_MARKER:
            raise ProfileCapabilityProbeError("Structured probe returned the wrong marker.")
        _assert_usage_reported(
            result.usage.requests, result.usage.input_tokens, result.usage.output_tokens
        )


async def _probe_text_stream(
    *,
    snapshot: ProfileSnapshot,
    credential: ProfileCredential,
    resolver: ProbeResolver,
) -> None:
    binding = _resolve_probe_binding(
        snapshot=snapshot,
        credential=credential,
        resolver=resolver,
        required_capabilities=("text_streaming", "usage_reporting"),
    )
    async with binding:
        binding.budget.begin_agent_run()
        agent = Agent(binding.model, output_type=str, retries={"tools": 0, "output": 0})
        chunks: list[str] = []
        async with agent.run_stream("Return only the text NOVELPILOT_CAPABILITY_OK.") as streamed:
            async for delta in streamed.stream_text(delta=True, debounce_by=None):
                chunks.append(delta)
            output = await streamed.get_output()
            requests = streamed.usage.requests
            input_tokens = streamed.usage.input_tokens
            output_tokens = streamed.usage.output_tokens
        if output != "".join(chunks) or PROBE_MARKER not in output:
            raise ProfileCapabilityProbeError(
                "Text-stream probe did not return one complete marker stream."
            )
        _assert_usage_reported(requests, input_tokens, output_tokens)


async def _probe_tool_call(
    *,
    snapshot: ProfileSnapshot,
    credential: ProfileCredential,
    resolver: ProbeResolver,
) -> None:
    binding = _resolve_probe_binding(
        snapshot=snapshot,
        credential=credential,
        resolver=resolver,
        required_capabilities=("tool_calling", "usage_reporting"),
    )
    async with binding:
        binding.budget.begin_agent_run()
        response = await binding.model.request(
            [
                ModelRequest(
                    parts=[
                        UserPromptPart(
                            "Call capability_echo once with marker "
                            "NOVELPILOT_CAPABILITY_OK. Do not answer in plain text."
                        )
                    ]
                )
            ],
            ModelSettings(tool_choice="required"),
            ModelRequestParameters(
                function_tools=[
                    ToolDefinition(
                        name="capability_echo",
                        description="Echo a capability marker.",
                        parameters_json_schema={
                            "type": "object",
                            "properties": {"marker": {"type": "string"}},
                            "required": ["marker"],
                            "additionalProperties": False,
                        },
                        strict=True,
                    )
                ],
                allow_text_output=False,
            ),
        )
        calls = [
            part
            for part in response.parts
            if isinstance(part, ToolCallPart) and part.tool_name == "capability_echo"
        ]
        if len(calls) != 1 or PROBE_MARKER not in calls[0].args_as_json_str():
            raise ProfileCapabilityProbeError(
                "Tool probe did not return the required capability_echo call."
            )
        _assert_usage_reported(
            1,
            int(response.usage.input_tokens),
            int(response.usage.output_tokens),
        )


def _resolve_probe_binding(
    *,
    snapshot: ProfileSnapshot,
    credential: ProfileCredential,
    resolver: ProbeResolver,
    required_capabilities: tuple[CapabilityName, ...],
) -> ResolvedModelBinding:
    return resolver.resolve(
        profile=snapshot,
        expected_profile_fingerprint=snapshot.fingerprint,
        required_capabilities=required_capabilities,
        model_request_limit=2,
        credential=credential,
    )


def _assert_usage_reported(requests: int, input_tokens: int, output_tokens: int) -> None:
    if requests < 1 or input_tokens + output_tokens < 1:
        raise ProfileCapabilityProbeError("Provider did not report usable request/token usage.")


def _redact_probe_message(value: str, *, secret: str) -> str:
    result = value.replace(secret, "[REDACTED]") if secret else value
    patterns = (
        r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;]+",
        r"(?i)(api[-_ ]?key\s*[:=]\s*)[^\s,;]+",
        r"(?i)([?&](?:access_token|api_key|key|signature|sig|token)=)[^&\s\"']+",
    )
    for pattern in patterns:
        result = re.sub(pattern, r"\1[REDACTED]", result)
    return result
