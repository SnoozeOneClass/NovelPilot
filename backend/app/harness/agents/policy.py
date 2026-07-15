from pydantic import BaseModel, ConfigDict, Field

from app.harness.agents.models import AgentRole
from app.schemas.profiles import LlmProfile
from app.schemas.projects import AgentPolicy, ProjectMetadata
from app.storage import profiles as profile_storage


class ResolvedAgentPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    role: AgentRole
    profile: LlmProfile
    evaluator_profile: LlmProfile
    max_turns: int = Field(ge=1, le=200)
    tool_schema_repair_limit: int = Field(ge=0, le=20)
    semantic_revision_limit: int = Field(ge=0, le=20)
    transport_retry_limit: int = Field(ge=0, le=20)


def resolve_agent_policy(metadata: ProjectMetadata, role: AgentRole) -> ResolvedAgentPolicy:
    policy = metadata.agent_policy
    profile_id = _role_profile_id(policy, role) or metadata.active_profile_id
    if profile_id is None:
        raise ValueError("Project has no default LLM profile.")
    profile = _load_ready_profile(profile_id)
    evaluator_id = policy.evaluator_profile_id or profile_id
    evaluator_profile = _load_ready_profile(evaluator_id)
    return ResolvedAgentPolicy(
        role=role,
        profile=profile,
        evaluator_profile=evaluator_profile,
        max_turns=_role_max_turns(policy, role),
        tool_schema_repair_limit=policy.tool_schema_repair_limit,
        semantic_revision_limit=policy.semantic_revision_limit,
        transport_retry_limit=policy.transport_retry_limit,
    )


def _load_ready_profile(profile_id: str) -> LlmProfile:
    try:
        profile = profile_storage.get_profile(profile_id)
    except KeyError as exc:
        raise ValueError(f"Configured LLM profile does not exist: {profile_id}") from exc
    if not profile.enabled:
        raise ValueError(f"Configured LLM profile is disabled: {profile_id}")
    profile_storage.require_harness_capabilities(profile)
    return profile


def _role_profile_id(policy: AgentPolicy, role: AgentRole) -> str | None:
    if role == "book":
        return policy.book_profile_id
    if role == "story_arc":
        return policy.story_arc_profile_id
    return policy.chapter_profile_id


def _role_max_turns(policy: AgentPolicy, role: AgentRole) -> int:
    if role == "book":
        return policy.book_max_turns
    if role == "story_arc":
        return policy.story_arc_max_turns
    return policy.chapter_max_turns
