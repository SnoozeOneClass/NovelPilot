from pydantic import BaseModel, Field


class RunAdvanceRequest(BaseModel):
    stop_after_chapter: bool = False
    max_steps: int = Field(default=36, ge=1, le=120)
