from typing import Any, Literal

from pydantic import BaseModel, Field


class ContextSource(BaseModel):
    id: str
    path: str
    version: int | None = None
    usage: Literal["direct", "summary"]
    included_fields: list[str] = Field(default_factory=list)
    summary: str | None = None


class ContextExclusion(BaseModel):
    source: str
    reason: str


class ContextSnapshot(BaseModel):
    schema_version: int = 1
    chapter_id: str
    created_at: str
    sources: list[ContextSource] = Field(default_factory=list)
    excluded: list[ContextExclusion] = Field(default_factory=list)
    assembly_rationale: str


class CandidateObservations(BaseModel):
    schema_version: int = 1
    status: Literal["candidate"] = "candidate"
    based_on: str
    events: list[dict[str, Any]] = Field(default_factory=list)
    character_changes: list[dict[str, Any]] = Field(default_factory=list)
    relationship_changes: list[dict[str, Any]] = Field(default_factory=list)
    world_fact_candidates: list[dict[str, Any]] = Field(default_factory=list)
    foreshadowing_candidates: list[dict[str, Any]] = Field(default_factory=list)
    requires_commit: bool = True


class VerificationSignal(BaseModel):
    name: str
    status: Literal["passed", "failed", "warning"]
    evidence: str | None = None


class ChapterVerification(BaseModel):
    schema_version: int = 1
    chapter_id: str
    goal_satisfied: bool
    commit_allowed: bool
    routing_decision: Literal[
        "commit",
        "revise",
        "rewrite",
        "pause",
        "escalate_to_arc",
        "escalate_to_book",
    ]
    signals: list[VerificationSignal] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)


class ArtifactSummary(BaseModel):
    path: str
    kind: str
    title: str
    status: str
    detail: str
    candidate: bool = False
    committed: bool = False
    routing_decision: str | None = None
    signals: list[str] = Field(default_factory=list)
    event_status: Literal["recorded", "missing", "untracked"] = "untracked"
    event_note: str | None = None
    profile_id: str | None = None
    model_snapshot: str | None = None
