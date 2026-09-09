from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
type ApiFamily = Literal["openai_responses", "anthropic_messages"]
type CapabilityName = Literal[
    "text_output", "text_streaming", "native_json_schema", "tool_calling", "usage_reporting"
]

CONNECT_TIMEOUT_MS = 10_000
POOL_TIMEOUT_MS = 10_000
WRITE_TIMEOUT_MS = 60_000
READ_TIMEOUT_MS = 600_000
TRANSPORT_RETRY_LIMIT = 5


def provider_request_capacity(model_request_limit: int, transport_retry_limit: int) -> int:
    """Reserve one physical send per semantic turn, plus a separate retry allowance."""
    for name, value, minimum in (
        ("model_request_limit", model_request_limit, 1),
        ("transport_retry_limit", transport_retry_limit, 0),
    ):
        if type(value) is not int or value < minimum:
            raise ValueError(f"{name} must be a finite integer greater than or equal to {minimum}.")
    return model_request_limit + transport_retry_limit


def _fingerprint(value: BaseModel) -> str:
    payload = json.dumps(
        value.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ProfileCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    text_output: bool = True
    text_streaming: bool = False
    native_json_schema: bool = False
    tool_calling: bool = False
    usage_reporting: bool = True
    contract_version: int = Field(default=1, ge=1)

    def supports(self, capability: CapabilityName) -> bool:
        return bool(getattr(self, capability))

    @property
    def fingerprint(self) -> str:
        return _fingerprint(self)


class ProfileSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    profile_id: str
    display_name: str
    api_family: ApiFamily
    base_url: str
    model_id: str
    request_options: dict[str, JsonValue] = Field(default_factory=dict)
    capabilities: ProfileCapabilities
    capability_fingerprint: str
    snapshot_version: int = Field(default=1, ge=1)

    @field_validator("profile_id", "display_name", "model_id")
    @classmethod
    def non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Profile identity fields must be non-blank.")
        return value

    @model_validator(mode="after")
    def valid_contract(self) -> ProfileSnapshot:
        validate_profile_base_url(self.api_family, self.base_url)
        validate_profile_request_options(self.api_family, self.request_options)
        if self.capability_fingerprint != self.capabilities.fingerprint:
            raise ValueError("capability_fingerprint does not match the capability snapshot.")
        return self

    @classmethod
    def create(
        cls,
        *,
        profile_id: str,
        display_name: str,
        api_family: ApiFamily,
        base_url: str,
        model_id: str,
        capabilities: ProfileCapabilities,
        request_options: dict[str, JsonValue] | None = None,
    ) -> ProfileSnapshot:
        return cls(
            profile_id=profile_id,
            display_name=display_name,
            api_family=api_family,
            base_url=base_url.rstrip("/"),
            model_id=model_id,
            request_options=request_options or {},
            capabilities=capabilities,
            capability_fingerprint=capabilities.fingerprint,
        )

    @property
    def fingerprint(self) -> str:
        return _fingerprint(self)


def validate_profile_request_options(
    api_family: ApiFamily, request_options: Mapping[str, JsonValue]
) -> None:
    sensitive = _sensitive_path(request_options)
    if sensitive:
        raise ValueError(
            f"Profile request_options cannot contain credentials or signed URL material: {sensitive}."
        )
    forbidden = {"timeout", "max_retries", "transport", "api_key", "authorization", "base_url"}
    conflicts = sorted(forbidden.intersection(key.casefold() for key in request_options))
    if conflicts:
        raise ValueError(
            "Profile request_options cannot override Harness transport policy: "
            + ", ".join(conflicts)
        )
    if "max_output_tokens" in request_options:
        raise ValueError("Profile request_options use the portable max_tokens key.")
    if "max_tokens" in request_options:
        value = request_options["max_tokens"]
        if type(value) is not int or value <= 0:
            raise ValueError("Profile request_options.max_tokens must be a positive integer.")
    elif api_family == "anthropic_messages":
        raise ValueError(
            "Anthropic Messages profiles require an explicit generous max_tokens value."
        )


def validate_profile_base_url(api_family: ApiFamily, base_url: str) -> None:
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("base_url must be an absolute HTTP(S) URL.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("base_url cannot contain credentials, query parameters, or fragments.")
    path = parsed.path.rstrip("/")
    if api_family == "openai_responses" and not path.endswith("/v1"):
        raise ValueError("OpenAI Responses base_url must end in /v1.")
    if api_family == "anthropic_messages" and path.endswith("/v1"):
        raise ValueError("Anthropic Messages base_url must exclude the terminal /v1.")


def _sensitive_path(value: Mapping[str, JsonValue], prefix: str = "request_options") -> str | None:
    sensitive = {
        "authorization",
        "cookie",
        "setcookie",
        "apikey",
        "xapikey",
        "accesstoken",
        "refreshtoken",
        "clientsecret",
        "password",
        "signature",
        "signedurl",
    }
    for key, item in value.items():
        path = f"{prefix}.{key}"
        if re.sub(r"[^a-z0-9]", "", key.casefold()) in sensitive:
            return path
        if isinstance(item, dict):
            nested = _sensitive_path(item, path)
            if nested:
                return nested
    return None
