from typing import Any

from pydantic import BaseModel, Field


class VersionedState(BaseModel):
    schema_version: int = 1
    version: int = 1
    items: dict[str, Any] = Field(default_factory=dict)

