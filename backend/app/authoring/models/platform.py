from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from typing import TypeVar

from pydantic import BaseModel, ConfigDict

from app.authoring.domain.models import AuthoringProfileSnapshot

OutputT = TypeVar("OutputT")
ModelCall = Callable[[AuthoringProfileSnapshot], Awaitable[OutputT]]


class ProviderAttemptFailure(RuntimeError):
    """Provider failure annotated with the two irreversible fallback boundaries."""

    def __init__(
        self,
        message: str,
        retryable: bool,
        emitted_output: bool = False,
        tool_side_effect: bool = False,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.emitted_output = emitted_output
        self.tool_side_effect = tool_side_effect


class ModelCallResult[OutputT](BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    output: OutputT
    actual_profile: AuthoringProfileSnapshot
    fallback_from: str | None = None
    fallback_reason: str | None = None


class FallbackModelPlatform:
    """Execute at most one explicit, capability-compatible fallback."""

    def __init__(
        self,
        primary: AuthoringProfileSnapshot,
        fallback: AuthoringProfileSnapshot | None = None,
        required_capabilities: Iterable[str] = ("text_output", "tool_calling"),
    ) -> None:
        required = tuple(required_capabilities)
        primary.require(*required)
        if fallback is not None:
            fallback.require(*required)
        self.primary = primary
        self.fallback = fallback

    async def call(self, request: ModelCall[OutputT]) -> ModelCallResult[OutputT]:
        try:
            output = await request(self.primary)
        except ProviderAttemptFailure as error:
            if (
                self.fallback is None
                or not error.retryable
                or error.emitted_output
                or error.tool_side_effect
            ):
                raise
            output = await request(self.fallback)
            return ModelCallResult(
                output=output,
                actual_profile=self.fallback,
                fallback_from=self.primary.profile_id,
                fallback_reason=str(error),
            )
        return ModelCallResult(output=output, actual_profile=self.primary)
