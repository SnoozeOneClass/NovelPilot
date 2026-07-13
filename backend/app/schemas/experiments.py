from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _StrictExperimentModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExperimentFixtureFile(_StrictExperimentModel):
    path: str
    byte_size: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ExperimentFixtureCheckpoint(_StrictExperimentModel):
    source_project_name: str
    source_project_id: str
    source_title: str | None
    active_arc_id: str
    completed_arc_ids: list[str] = Field(default_factory=list)
    warmup_chapter_ids: list[str] = Field(default_factory=list)
    recommended_target_chapter_count: int = Field(ge=1, le=30)
    target_chapter_count: int = Field(ge=1, le=30)
    checkpoint_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class ExperimentFixtureManifest(_StrictExperimentModel):
    schema_version: Literal[1] = 1
    fixture_version: Literal["fixture-v1"] = "fixture-v1"
    fixture_id: str = Field(
        pattern=(
            r"^fixture-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
            r"[0-9a-f]{4}-[0-9a-f]{12}$"
        )
    )
    created_at: datetime
    checkpoint: ExperimentFixtureCheckpoint
    direct_prompt_path: Literal["direct_prompt.md"] = "direct_prompt.md"
    files: list[ExperimentFixtureFile] = Field(default_factory=list)


class ExperimentFixtureIssue(_StrictExperimentModel):
    code: str
    message: str


class ExperimentFixtureSummary(_StrictExperimentModel):
    fixture_id: str = Field(
        pattern=(
            r"^fixture-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
            r"[0-9a-f]{4}-[0-9a-f]{12}$"
        )
    )
    created_at: datetime
    relative_path: str
    checkpoint: ExperimentFixtureCheckpoint


class ExperimentFixtureStatus(_StrictExperimentModel):
    eligible: bool
    issues: list[ExperimentFixtureIssue] = Field(default_factory=list)
    checkpoint: ExperimentFixtureCheckpoint | None = None
    existing_fixture: ExperimentFixtureSummary | None = None


class ExperimentFixtureCreateResponse(_StrictExperimentModel):
    created: bool
    fixture: ExperimentFixtureSummary
