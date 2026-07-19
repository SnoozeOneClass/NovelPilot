from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


PatchOperationName = Literal["upsert", "delete", "append"]


class PatchEvidence(BaseModel):
    file: str
    quote: str


class CandidatePatchOperation(BaseModel):
    id: str | None = None
    op: PatchOperationName
    target_file: str
    target_id: str
    expected_version: int
    value: dict[str, Any] = Field(default_factory=dict)
    evidence: list[PatchEvidence] = Field(default_factory=list)
    rationale: str


class CandidateStatePatch(BaseModel):
    schema_version: int = 1
    status: Literal["candidate"] = "candidate"
    based_on: dict[str, str]
    operations: list[CandidatePatchOperation]


class PatchValidationResult(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    schema_check: Literal["passed", "failed"] = Field(alias="schema")
    versions: Literal["passed", "failed"]
    evidence: Literal["passed", "failed"]
    conflicts: Literal["passed", "failed"]
    reasons: list[str] = Field(default_factory=list)


class CommittedStatePatch(BaseModel):
    schema_version: int = 1
    status: Literal["committed"] = "committed"
    committed_at: str
    operations: list[CandidatePatchOperation]
    validation: PatchValidationResult
