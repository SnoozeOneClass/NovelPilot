from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, cast

import httpx
from openai import AsyncOpenAI
from pydantic_ai import ModelProfile
from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIResponsesModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.settings import ModelSettings
from pydantic import SecretStr

from app.agents.contracts import CapabilityName, ProfileSnapshot
from app.agents.transport import (
    ActivationRequestBudget,
    RequestCountingModel,
    build_retrying_transport,
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


@dataclass(slots=True)
class OpenAIResponsesAdapter:
    key: str = "openai_responses"
    transport_factory: Callable[[ActivationRequestBudget], httpx.AsyncBaseTransport] | None = None

    def build(
        self,
        *,
        profile: ProfileSnapshot,
        credential: ProfileCredential,
        model_request_limit: int,
    ) -> ResolvedModelBinding:
        budget = ActivationRequestBudget(model_request_limit=model_request_limit)
        transport = (
            self.transport_factory(budget)
            if self.transport_factory is not None
            else build_retrying_transport(budget=budget)
        )
        timeout = httpx.Timeout(connect=10.0, pool=10.0, write=60.0, read=600.0)
        http_client = httpx.AsyncClient(transport=transport, timeout=timeout)
        client = AsyncOpenAI(
            api_key=credential.api_key.get_secret_value(),
            base_url=profile.base_url,
            http_client=http_client,
            max_retries=0,
        )
        provider = OpenAIProvider(openai_client=client)
        framework_profile = ModelProfile(
            supports_tools=profile.capabilities.tool_calling,
            supports_json_schema_output=profile.capabilities.native_json_schema,
            default_structured_output_mode="native",
        )
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


class ModelBindingResolver:
    """Resolve solely by api_family after validating the frozen Profile contract."""

    def __init__(self, adapters: list[Adapter] | None = None) -> None:
        configured = adapters or [OpenAIResponsesAdapter()]
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
