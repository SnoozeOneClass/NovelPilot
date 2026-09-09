from __future__ import annotations

import inspect
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any, Protocol, Self, cast

import httpx
import httpx2
from anthropic import AsyncAnthropic, Omit
from anthropic.resources.beta.messages.messages import AsyncMessages
from openai import AsyncOpenAI
from pydantic import SecretStr
from pydantic_ai import ModelProfile
from pydantic_ai.models import Model
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.openai import OpenAIResponsesModel
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.settings import ModelSettings

from app.authoring.models.contracts import (
    CONNECT_TIMEOUT_MS,
    POOL_TIMEOUT_MS,
    READ_TIMEOUT_MS,
    TRANSPORT_RETRY_LIMIT,
    WRITE_TIMEOUT_MS,
    CapabilityName,
    ProfileSnapshot,
)
from app.authoring.models.transport import (
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
    _http_client: httpx.AsyncClient | httpx2.AsyncClient | None = None

    async def aclose(self) -> None:
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    async def __aenter__(self) -> Self:
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
        transport_retry_limit: int = TRANSPORT_RETRY_LIMIT,
    ) -> ResolvedModelBinding: ...


TransportFactory = Callable[[], httpx.AsyncBaseTransport]
AnthropicTransportFactory = Callable[[], httpx2.AsyncBaseTransport]


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
        transport_retry_limit: int = TRANSPORT_RETRY_LIMIT,
    ) -> ResolvedModelBinding:
        budget = ActivationRequestBudget(
            model_request_limit=model_request_limit,
            protocol=self.key,
            transport_retry_limit=transport_retry_limit,
        )
        http_client = _build_http_client(
            budget=budget,
            transport_factory=self.transport_factory,
        )
        client = AsyncOpenAI(
            api_key=credential.api_key.get_secret_value(),
            base_url=profile.base_url,
            http_client=cast(Any, http_client),
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
    transport_factory: AnthropicTransportFactory | None = None

    def build(
        self,
        *,
        profile: ProfileSnapshot,
        credential: ProfileCredential,
        model_request_limit: int,
        transport_retry_limit: int = TRANSPORT_RETRY_LIMIT,
    ) -> ResolvedModelBinding:
        unsupported = _unsupported_anthropic_options()
        configured = sorted(unsupported.intersection(profile.request_options))
        if configured:
            raise ModelBindingError(
                "Installed Anthropic SDK does not support request option(s): "
                + ", ".join(configured)
            )
        budget = ActivationRequestBudget(
            model_request_limit=model_request_limit,
            protocol=self.key,
            transport_retry_limit=transport_retry_limit,
        )
        http_client = _build_anthropic_http_client(
            budget=budget,
            transport_factory=self.transport_factory,
        )
        client = AsyncAnthropic(
            api_key=credential.api_key.get_secret_value(),
            base_url=profile.base_url,
            http_client=http_client,
            max_retries=0,
        )
        _install_anthropic_sdk_compatibility(client, unsupported)
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
        transport_retry_limit: int = TRANSPORT_RETRY_LIMIT,
    ) -> ResolvedModelBinding:
        if profile.fingerprint != expected_profile_fingerprint:
            raise ProfileFingerprintError(
                "Current Profile snapshot does not match the frozen Task Plan."
            )
        missing = [
            name for name in required_capabilities if not profile.capabilities.supports(name)
        ]
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
            transport_retry_limit=transport_retry_limit,
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


class _ObservedHttpx2ResponseStream(httpx2.AsyncByteStream):
    def __init__(
        self,
        wrapped: httpx2.AsyncByteStream,
        budget: ActivationRequestBudget,
        sequence: int,
    ) -> None:
        self._wrapped = wrapped
        self._budget = budget
        self._sequence = sequence
        self._finished = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        try:
            async for chunk in self._wrapped:
                self._budget.record_stream_event(sequence=self._sequence)
                yield chunk
        except BaseException as error:
            self._finished = True
            self._budget.finish_provider_request(sequence=self._sequence, error=error)
            raise
        else:
            self._finished = True
            self._budget.finish_provider_request(sequence=self._sequence)

    async def aclose(self) -> None:
        try:
            await self._wrapped.aclose()
        finally:
            if not self._finished:
                self._finished = True
                self._budget.finish_provider_request(sequence=self._sequence)


class _ObservedHttpx2Transport(httpx2.AsyncBaseTransport):
    def __init__(
        self,
        budget: ActivationRequestBudget,
        wrapped: httpx2.AsyncBaseTransport,
    ) -> None:
        self._budget = budget
        self._wrapped = wrapped

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        sequence = self._budget.begin_provider_request(cast(Any, request))
        try:
            response = await self._wrapped.handle_async_request(request)
        except BaseException as error:
            self._budget.finish_provider_request(sequence=sequence, error=error)
            raise
        response.request = request
        self._budget.record_response_headers(sequence=sequence, response=cast(Any, response))
        response.stream = _ObservedHttpx2ResponseStream(
            wrapped=cast(httpx2.AsyncByteStream, response.stream),
            budget=self._budget,
            sequence=sequence,
        )
        return response

    async def aclose(self) -> None:
        await self._wrapped.aclose()


def _build_anthropic_http_client(
    *,
    budget: ActivationRequestBudget,
    transport_factory: AnthropicTransportFactory | None,
) -> httpx2.AsyncClient:
    wrapped = transport_factory() if transport_factory is not None else httpx2.AsyncHTTPTransport()
    timeout = httpx2.Timeout(
        connect=CONNECT_TIMEOUT_MS / 1_000,
        pool=POOL_TIMEOUT_MS / 1_000,
        write=WRITE_TIMEOUT_MS / 1_000,
        read=READ_TIMEOUT_MS / 1_000,
    )
    return httpx2.AsyncClient(
        transport=_ObservedHttpx2Transport(budget, wrapped),
        timeout=timeout,
    )


def _install_anthropic_sdk_compatibility(client: AsyncAnthropic, unsupported: set[str]) -> None:
    """Bridge Pydantic AI's optional sampling kwargs to Anthropic's httpx2 SDK.

    Anthropic 1.3 removed ``temperature``, ``top_p`` and ``top_k`` from the
    beta Messages signature while Pydantic AI 2.15 still passes its ``Omit``
    sentinels. Dropping only omitted values preserves the wire contract. An
    explicitly configured value fails before a Provider request rather than
    being silently ignored.
    """

    resource = cast(Any, client.beta.messages)
    original = resource.create

    async def compatible_create(*args: object, **kwargs: object) -> object:
        for name in unsupported:
            value = kwargs.get(name)
            if isinstance(value, Omit):
                kwargs.pop(name)
            elif name in kwargs:
                raise ModelBindingError(
                    f"Installed Anthropic SDK does not support request option {name!r}."
                )
        return await original(*args, **kwargs)

    resource.create = compatible_create


def _unsupported_anthropic_options() -> set[str]:
    parameters = inspect.signature(AsyncMessages.create).parameters
    return {name for name in ("temperature", "top_p", "top_k") if name not in parameters}


def _framework_profile(profile: ProfileSnapshot) -> ModelProfile:
    return ModelProfile(
        supports_tools=profile.capabilities.tool_calling,
        supports_json_schema_output=profile.capabilities.native_json_schema,
        default_structured_output_mode="native",
    )
