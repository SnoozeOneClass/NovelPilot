from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


OperationMode = Literal["full_auto", "participatory"]
ProjectKind = Literal["novel", "benchmark_mother"]
BenchmarkFixtureLifecycleStatus = Literal["preparing", "freeze_failed", "frozen"]
RetryBudgetScopeVersion = Literal["action-local-v1"]
RETRY_BUDGET_SCOPE_VERSION: RetryBudgetScopeVersion = "action-local-v1"
RunStatus = Literal[
    "idle",
    "running",
    "pause_requested",
    "paused",
    "waiting_for_user",
    "waiting_for_provider",
    "failed",
]


class AgentPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    book_profile_id: str | None = None
    story_arc_profile_id: str | None = None
    chapter_profile_id: str | None = None
    evaluator_profile_id: str | None = None
    book_max_turns: int = Field(default=20, ge=1, le=200)
    story_arc_max_turns: int = Field(default=20, ge=1, le=200)
    chapter_max_turns: int = Field(default=30, ge=1, le=200)
    tool_schema_repair_limit: int = Field(default=2, ge=0, le=20)
    semantic_revision_limit: int = Field(default=2, ge=0, le=20)
    transport_retry_limit: int = Field(default=3, ge=0, le=20)


class BenchmarkFixtureLifecycle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: BenchmarkFixtureLifecycleStatus = "preparing"
    fixture_id: str | None = Field(
        default=None,
        pattern=(
            r"^fixture-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
            r"[0-9a-f]{4}-[0-9a-f]{12}$"
        ),
    )
    checkpoint_fingerprint: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    failure_code: str | None = Field(default=None, max_length=100)
    failure_message: str | None = Field(default=None, max_length=1_000)

    @model_validator(mode="after")
    def validate_frozen_identity(self) -> "BenchmarkFixtureLifecycle":
        if self.status == "frozen" and (
            self.fixture_id is None or self.checkpoint_fingerprint is None
        ):
            raise ValueError("Frozen benchmark fixtures require an ID and fingerprint.")
        return self


class ProjectMetadata(BaseModel):
    schema_version: int = 1
    project_id: str = Field(default_factory=lambda: str(uuid4()))
    title: str | None = Field(default=None, min_length=1, max_length=200)
    operation_mode: OperationMode = "full_auto"
    project_kind: ProjectKind = "novel"
    benchmark_fixture: BenchmarkFixtureLifecycle | None = None
    active_profile_id: str | None = None
    agent_policy: AgentPolicy = Field(default_factory=AgentPolicy)
    active_arc_id: str | None = None
    active_chapter_id: str | None = None
    run_status: RunStatus = "idle"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("Project title must not be blank.")
        return stripped

    @model_validator(mode="after")
    def validate_project_kind(self) -> "ProjectMetadata":
        if self.project_kind == "benchmark_mother":
            if self.operation_mode != "participatory":
                raise ValueError("Benchmark mother projects require participatory mode.")
            if self.benchmark_fixture is None:
                self.benchmark_fixture = BenchmarkFixtureLifecycle()
        elif self.benchmark_fixture is not None:
            raise ValueError("Ordinary novel projects cannot carry benchmark fixture state.")
        return self


class ProjectSummary(BaseModel):
    name: str
    title: str | None
    path: str
    metadata: ProjectMetadata


class CreateProjectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_mode: OperationMode = "full_auto"
    project_kind: ProjectKind = "novel"

    @model_validator(mode="after")
    def validate_project_kind(self) -> "CreateProjectRequest":
        if self.project_kind == "benchmark_mother" and self.operation_mode != "participatory":
            raise ValueError("Benchmark mother projects require participatory mode.")
        return self


class UpdateOperationModeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_mode: OperationMode


class UpdateAgentPolicyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_policy: AgentPolicy


class OpenProjectRequest(BaseModel):
    name: str = Field(min_length=1)


class DeleteProjectsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_ids: list[str] = Field(min_length=1)

    @field_validator("project_ids")
    @classmethod
    def normalize_project_ids(cls, value: list[str]) -> list[str]:
        normalized = [project_id.strip() for project_id in value]
        if any(not project_id for project_id in normalized):
            raise ValueError("Project IDs must not be blank.")
        if len(set(normalized)) != len(normalized):
            raise ValueError("Project IDs must be unique.")
        return normalized


class DeletedProject(BaseModel):
    project_id: str
    name: str


class DeleteProjectsResponse(BaseModel):
    deleted: list[DeletedProject]
    active_project_closed: bool


class ActiveProjectDocument(BaseModel):
    name: str
    path: str

