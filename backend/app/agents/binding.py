from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol, cast

import httpx
from anthropic import AsyncAnthropic
from openai import AsyncOpenAI
from pydantic import SecretStr
from pydantic_ai import ModelProfile
from pydantic_ai.models import Model
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.openai import OpenAIResponsesModel
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.settings import ModelSettings

from app.agents.contracts import (
    CONNECT_TIMEOUT_MS,
    POOL_TIMEOUT_MS,
    READ_TIMEOUT_MS,
    WRITE_TIMEOUT_MS,
    CapabilityName,
    ProfileSnapshot,
)
from app.agents.transport import (
    ActivationRequestBudget,
    RequestCountingModel,
    build_observed_transport,
)


class ModelBindingError(RuntimeError):
    """A Profile cannot be mapped to its declared wire-level Adapter."""


class UnknownApiFamilyError(ModelBindingError):
    pass


class ProfileCapabilityError(ModelBindingError):
    pass


class ProfileFingerprintError(ModelBindingError):
    pass


class ProfileCredentialError(ModelBindingError):
    pass


@dataclass(frozen=True, slots=True)
class ProfileCredential:
    api_key: SecretStr

    @classmethod
    def from_plaintext(cls, api_key: str) -> ProfileCredential:
        if not api_key:
            raise ProfileCredentialError("Profile API credential is missing.")
        return cls(api_key=SecretStr(api_key))


@dataclass(slots=True)
class ResolvedModelBinding:
    model: Model
    budget: ActivationRequestBudget
    adapter_key: str
    _http_client: httpx.AsyncClient | None = None

    async def aclose(self) -> None:
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    async def __aenter__(self) -> ResolvedModelBinding:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.aclose()


class Adapter(Protocol):
    key: str

    def build(
        self,
        *,
        profile: ProfileSnapshot,
        credential: ProfileCredential,
        model_request_limit: int,
    ) -> ResolvedModelBinding: ...


TransportFactory = Callable[[], httpx.AsyncBaseTransport]


@dataclass(slots=True)
class OpenAIResponsesAdapter:
    key: str = "openai_responses"
    transport_factory: TransportFactory | None = None

    def build(
        self,
        *,
        profile: ProfileSnapshot,
        credential: ProfileCredential,
        model_request_limit: int,
    ) -> ResolvedModelBinding:
        budget = ActivationRequestBudget(
            model_request_limit=model_request_limit,
            protocol=self.key,
        )
        http_client = _build_http_client(
            budget=budget,
            transport_factory=self.transport_factory,
        )
        client = AsyncOpenAI(
            api_key=credential.api_key.get_secret_value(),
            base_url=profile.base_url,
            http_client=http_client,
            max_retries=0,
        )
        provider = OpenAIProvider(openai_client=client)
        framework_profile = _framework_profile(profile)
        settings = cast(ModelSettings, dict(profile.request_options))
        raw_model = OpenAIResponsesModel(
            profile.model_id,  # opaque endpoint identifier; never inspected here.
            provider=provider,
            profile=framework_profile,
            settings=settings,
        )
        return ResolvedModelBinding(
            model=RequestCountingModel(raw_model, budget=budget),
            budget=budget,
            adapter_key=self.key,
            _http_client=http_client,
        )


@dataclass(slots=True)
class AnthropicMessagesAdapter:
    key: str = "anthropic_messages"
    transport_factory: TransportFactory | None = None

    def build(
        self,
        *,
        profile: ProfileSnapshot,
        credential: ProfileCredential,
        model_request_limit: int,
    ) -> ResolvedModelBinding:
        budget = ActivationRequestBudget(
            model_request_limit=model_request_limit,
            protocol=self.key,
        )
        http_client = _build_http_client(
            budget=budget,
            transport_factory=self.transport_factory,
        )
        client = AsyncAnthropic(
            api_key=credential.api_key.get_secret_value(),
            base_url=profile.base_url,
            http_client=http_client,
            max_retries=0,
        )
        provider = AnthropicProvider(anthropic_client=client)
        framework_profile = _framework_profile(profile)
        settings = cast(ModelSettings, dict(profile.request_options))
        raw_model = AnthropicModel(
            cast(Any, profile.model_id),  # Opaque endpoint identifier; never inspected here.
            provider=provider,
            profile=framework_profile,
            settings=settings,
        )
        return ResolvedModelBinding(
            model=RequestCountingModel(raw_model, budget=budget),
            budget=budget,
            adapter_key=self.key,
            _http_client=http_client,
        )


class ModelBindingResolver:
    """Resolve solely by api_family after validating the frozen Profile contract."""

    def __init__(self, adapters: list[Adapter] | None = None) -> None:
        configured = adapters or [OpenAIResponsesAdapter(), AnthropicMessagesAdapter()]
        self._adapters = {adapter.key: adapter for adapter in configured}
        if len(self._adapters) != len(configured):
            raise ValueError("Duplicate ModelBinding adapter key.")

    def resolve(
        self,
        *,
        profile: ProfileSnapshot,
        expected_profile_fingerprint: str,
        required_capabilities: tuple[CapabilityName, ...],
        model_request_limit: int,
        credential: ProfileCredential,
    ) -> ResolvedModelBinding:
        if profile.fingerprint != expected_profile_fingerprint:
            raise ProfileFingerprintError("Current Profile snapshot does not match the frozen Task Plan.")
        missing = [name for name in required_capabilities if not profile.capabilities.supports(name)]
        if missing:
            raise ProfileCapabilityError(
                "Profile does not satisfy required capabilities: " + ", ".join(missing)
            )
        try:
            adapter = self._adapters[profile.api_family]
        except KeyError as exc:
            raise UnknownApiFamilyError(
                f"No ModelBinding adapter is implemented for api_family={profile.api_family!r}."
            ) from exc
        return adapter.build(
            profile=profile,
            credential=credential,
            model_request_limit=model_request_limit,
        )


def _build_http_client(
    *,
    budget: ActivationRequestBudget,
    transport_factory: TransportFactory | None,
) -> httpx.AsyncClient:
    wrapped = transport_factory() if transport_factory is not None else None
    timeout = httpx.Timeout(
        connect=CONNECT_TIMEOUT_MS / 1_000,
        pool=POOL_TIMEOUT_MS / 1_000,
        write=WRITE_TIMEOUT_MS / 1_000,
        read=READ_TIMEOUT_MS / 1_000,
    )
    return httpx.AsyncClient(
        transport=build_observed_transport(budget=budget, wrapped=wrapped),
        timeout=timeout,
    )


def _framework_profile(profile: ProfileSnapshot) -> ModelProfile:
    return ModelProfile(
        supports_tools=profile.capabilities.tool_calling,
        supports_json_schema_output=profile.capabilities.native_json_schema,
        default_structured_output_mode="native",
    )
