from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class SetupOption(BaseModel):
    id: str
    label: str
    description: str


class SetupQuestion(BaseModel):
    id: str
    title: str
    prompt: str
    options: list[SetupOption]
    required: bool = True
    source: Literal["default", "llm"] = "default"
    profile_id: str | None = None
    model_snapshot: str | None = None


class SetupAnswer(BaseModel):
    question_id: str
    answer: str
    answered_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class SetupStateDocument(BaseModel):
    schema_version: int = 1
    approved: bool = False
    approved_at: datetime | None = None
    ready_for_approval: bool = False
    readiness_assessed_at: datetime | None = None
    readiness_profile_id: str | None = None
    questions: list[SetupQuestion]
    answers: list[SetupAnswer] = Field(default_factory=list)
    next_question: SetupQuestion | None = None


class SetupAnswerRequest(BaseModel):
    question_id: str = Field(min_length=1)
    answer: str = Field(min_length=1)

    @field_validator("answer")
    @classmethod
    def answer_must_not_be_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Setup answer must not be blank.")
        return stripped
