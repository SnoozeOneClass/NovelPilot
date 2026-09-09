from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

DEFAULT_TARGET_CHAPTERS = 120
TARGET_POLICY_VERSION = "long-form-v1"


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def content_hash(value: Any) -> str:
    raw = value if isinstance(value, str) else canonical_json(value)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class WorkerRole(StrEnum):
    ARCHITECT = "architect"
    WRITER = "writer"
    EDITOR = "editor"
    ARBITER = "arbiter"


class RunStatus(StrEnum):
    READY = "ready"
    RUNNING = "running"
    PAUSED = "paused"
    FAILURE_PAUSED = "failure_paused"
    CANCELLED = "cancelled"
    COMPLETED = "completed"


class Phase(StrEnum):
    FOUNDATION = "foundation"
    WRITING = "writing"
    FINALIZING = "finalizing"
    COMPLETE = "complete"


class InstructionKind(StrEnum):
    CREATE_FOUNDATION = "create_foundation"
    WRITE_CHAPTER = "write_chapter"
    REWRITE_CHAPTER = "rewrite_chapter"
    REVIEW_BOUNDARY = "review_boundary"
    SAVE_SUMMARY = "save_summary"
    EXTEND_OUTLINE = "extend_outline"
    COMPLETE_BOOK = "complete_book"


class TargetLength(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source: Literal["user", "default"]
    target_chapters: int = Field(ge=1, le=10_000)
    target_words: int | None = Field(default=None, ge=1)
    policy_version: str = TARGET_POLICY_VERSION

    @classmethod
    def resolve(
        cls,
        *,
        target_chapters: int | None = None,
        target_words: int | None = None,
        default_chapters: int = DEFAULT_TARGET_CHAPTERS,
    ) -> TargetLength:
        if target_chapters is not None and target_words is not None:
            raise ValueError("target_chapters and target_words are mutually exclusive")
        if target_chapters is not None:
            return cls(source="user", target_chapters=target_chapters)
        if target_words is not None:
            # The conversion policy is frozen with the project.  The word goal remains
            # available for reporting while routing uses one deterministic chapter goal.
            chapters = max(1, (target_words + 2_999) // 3_000)
            return cls(source="user", target_chapters=chapters, target_words=target_words)
        return cls(source="default", target_chapters=default_chapters)


class AuthoringProfileSnapshot(BaseModel):
    """Secret-free model metadata frozen for one Worker episode."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    profile_id: str
    provider_protocol: str
    model_id: str
    context_window: int = Field(ge=1)
    max_output_tokens: int = Field(ge=1)
    capabilities: frozenset[str] = frozenset({"text_output", "tool_calling"})
    request_options: dict[str, Any] = Field(default_factory=dict)
    input_price_per_million: float = Field(default=0.0, ge=0)
    output_price_per_million: float = Field(default=0.0, ge=0)
    cache_price_per_million: float = Field(default=0.0, ge=0)
    metadata_version: int = Field(default=1, ge=1)
    fingerprint: str = ""

    @model_validator(mode="after")
    def freeze_fingerprint(self) -> AuthoringProfileSnapshot:
        expected = content_hash(
            {
                "profile_id": self.profile_id,
                "provider_protocol": self.provider_protocol,
                "model_id": self.model_id,
                "context_window": self.context_window,
                "max_output_tokens": self.max_output_tokens,
                "capabilities": sorted(self.capabilities),
                "request_options": self.request_options,
                "prices": [
                    self.input_price_per_million,
                    self.output_price_per_million,
                    self.cache_price_per_million,
                ],
                "metadata_version": self.metadata_version,
            }
        )
        if self.fingerprint and self.fingerprint != expected:
            raise ValueError("profile fingerprint does not match frozen metadata")
        object.__setattr__(self, "fingerprint", expected)
        return self

    def require(self, *capabilities: str) -> None:
        missing = sorted(set(capabilities).difference(self.capabilities))
        if missing:
            raise ValueError(f"profile lacks required capabilities: {', '.join(missing)}")


class Instruction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    worker: WorkerRole
    kind: InstructionKind
    logical_target: str
    expected_phase: Phase
    required_facts: tuple[str, ...] = ()
    terminal_postcondition: str
    reason_code: str
    fact_version: str
    instruction_key: str = ""

    @model_validator(mode="after")
    def freeze_key(self) -> Instruction:
        expected = content_hash(
            {
                "project_id": self.project_id,
                "kind": self.kind,
                "target": self.logical_target,
                "fact_version": self.fact_version,
            }
        )
        if self.instruction_key and self.instruction_key != expected:
            raise ValueError("instruction key does not match instruction facts")
        object.__setattr__(self, "instruction_key", expected)
        return self


class StateSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    status: RunStatus
    phase: Phase
    target: TargetLength
    fact_version: str
    foundation_present: bool
    foundation_audited: bool
    chapter_count: int = Field(ge=0)
    planned_through: int = Field(ge=0)
    active_instruction_key: str | None = None
    active_instruction_kind: InstructionKind | None = None
    active_logical_target: str | None = None
    active_fact_version: str | None = None
    pending_rewrites: tuple[int, ...] = ()
    reviewed_through: int = Field(ge=0)
    summarized_through: int = Field(ge=0)
    final_audit_complete: bool = False
    corrupted_reason: str | None = None


class ProjectView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    brief: str
    title: str | None
    status: RunStatus
    phase: Phase
    target: TargetLength
    chapter_count: int
    failure_reason: str | None = None
