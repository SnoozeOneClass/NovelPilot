from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field


OperationMode = Literal["full_auto", "participatory"]
RunStatus = Literal[
    "idle",
    "running",
    "pause_requested",
    "paused",
    "waiting_for_user",
    "failed",
]


class ProjectMetadata(BaseModel):
    schema_version: int = 1
    project_id: str = Field(default_factory=lambda: str(uuid4()))
    title: str = Field(min_length=1)
    operation_mode: OperationMode = "full_auto"
    active_profile_id: str | None = None
    active_arc_id: str | None = None
    active_chapter_id: str | None = None
    run_status: RunStatus = "idle"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ProjectSummary(BaseModel):
    name: str
    title: str
    path: str
    metadata: ProjectMetadata


class CreateProjectRequest(BaseModel):
    title: str = Field(min_length=1)
    operation_mode: OperationMode = "full_auto"


class OpenProjectRequest(BaseModel):
    name: str = Field(min_length=1)


class ActiveProjectDocument(BaseModel):
    name: str
    path: str

