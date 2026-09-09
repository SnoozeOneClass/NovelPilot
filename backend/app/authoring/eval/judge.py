from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai import Agent
from pydantic_ai.models import Model

from app.authoring.domain.models import AuthoringProfileSnapshot

JUDGE_ID = "authoring-seven-dimension-judge"
JUDGE_VERSION = 1
JUDGE_PROMPT = (
    "Evaluate the completed novel using causality, character, pacing, continuity, stakes, prose, "
    "and payoff. Return 1-5 scores, a concise rationale, and short evidence excerpts. "
    "Do not repair the manuscript or override deterministic workflow failures."
)


class JudgeDimensions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    causality: int = Field(ge=1, le=5)
    character: int = Field(ge=1, le=5)
    pacing: int = Field(ge=1, le=5)
    continuity: int = Field(ge=1, le=5)
    stakes: int = Field(ge=1, le=5)
    prose: int = Field(ge=1, le=5)
    payoff: int = Field(ge=1, le=5)


class JudgeResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    judge_id: str
    version: int = Field(ge=1)
    score: int = Field(ge=1, le=5)
    dimensions: JudgeDimensions
    rationale: str
    evidence: tuple[str, ...]


class JudgePayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    score: int = Field(ge=1, le=5)
    dimensions: JudgeDimensions
    rationale: str
    evidence: tuple[str, ...]


class VersionedJudge(Protocol):
    async def evaluate(self, manuscript: str) -> JudgeResult: ...


class PydanticLLMJudge:
    def __init__(self, model: Model, profile: AuthoringProfileSnapshot) -> None:
        self.model = model
        self.profile = profile
        self.request_evidence: dict[str, object] | None = None

    async def evaluate(self, manuscript: str) -> JudgeResult:
        agent = Agent(
            self.model,
            output_type=JudgePayload,
            instructions=JUDGE_PROMPT,
            retries={"output": 1},
            max_concurrency=1,
            defer_model_check=True,
        )
        result = await agent.run(manuscript)
        usage = result.usage
        cache_tokens = usage.cache_read_tokens + usage.cache_write_tokens
        self.request_evidence = {
            "profile_id": self.profile.profile_id,
            "profile_fingerprint": self.profile.fingerprint,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cache_read_tokens": usage.cache_read_tokens,
            "cache_write_tokens": usage.cache_write_tokens,
            "cost_microunits": round(
                usage.input_tokens * self.profile.input_price_per_million
                + usage.output_tokens * self.profile.output_price_per_million
                + cache_tokens * self.profile.cache_price_per_million
            ),
        }
        payload = result.output
        return JudgeResult(
            judge_id=JUDGE_ID,
            version=JUDGE_VERSION,
            score=payload.score,
            dimensions=payload.dimensions,
            rationale=payload.rationale,
            evidence=payload.evidence,
        )


class DeterministicFakeJudge:
    """Test double for the optional Judge contract; never presented as real quality evidence."""

    async def evaluate(self, manuscript: str) -> JudgeResult:
        chapter_count = manuscript.count("\n## ")
        return JudgeResult(
            judge_id="deterministic-fake-judge",
            version=1,
            score=4 if chapter_count else 1,
            dimensions=JudgeDimensions(
                causality=4,
                character=4,
                pacing=4,
                continuity=4,
                stakes=4,
                prose=4,
                payoff=4 if chapter_count else 1,
            ),
            rationale="Contract-only score based on presence of exported chapters.",
            evidence=(f"chapter_headings={chapter_count}",),
        )
