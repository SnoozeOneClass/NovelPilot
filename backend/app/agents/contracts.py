from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]

AgentRole = Literal["book_strategist", "arc_planner", "chapter_writer", "evaluator"]
ScopeLayer = Literal["book", "arc", "chapter"]
OutputMode = Literal["native_json_schema", "text_streaming"]
ApiFamily = Literal["openai_responses", "anthropic_messages"]
CapabilityName = Literal[
    "text_output",
    "text_streaming",
    "native_json_schema",
    "tool_calling",
    "usage_reporting",
]

TIMEOUT_POLICY_ID = "provider-timeout-t1-v1"
CONNECT_TIMEOUT_MS = 10_000
POOL_TIMEOUT_MS = 10_000
WRITE_TIMEOUT_MS = 60_000
READ_TIMEOUT_MS = 600_000
ACTIVATION_TIMEOUT_MS = 1_800_000
TRANSPORT_RETRY_LIMIT = 5
PROVIDER_REQUEST_LIMIT = 6


class AgentContractError(ValueError):
    """A frozen Agent contract is internally inconsistent."""


class ProfileCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    text_output: bool = True
    text_streaming: bool = False
    native_json_schema: bool = False
    tool_calling: bool = False
    usage_reporting: bool = True
    contract_version: int = Field(default=1, ge=1)

    def supports(self, capability: CapabilityName) -> bool:
        return bool(getattr(self, capability))

    @property
    def fingerprint(self) -> str:
        return _canonical_json_sha(self)


class ProfileSnapshot(BaseModel):
    """Secret-free, immutable evidence for one concrete Provider profile."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    profile_id: str
    display_name: str
    api_family: ApiFamily
    base_url: str
    model_id: str
    request_options: dict[str, JsonValue] = Field(default_factory=dict)
    capabilities: ProfileCapabilities
    capability_fingerprint: str
    snapshot_version: int = Field(default=1, ge=1)

    @field_validator("profile_id", "display_name", "api_family", "model_id")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Profile identity fields must be non-blank.")
        return value

    @field_validator("base_url")
    @classmethod
    def _safe_base_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("base_url must be an absolute HTTP(S) URL.")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("base_url cannot contain credentials, query parameters, or fragments.")
        return value.rstrip("/")

    @field_validator("request_options")
    @classmethod
    def _request_options_cannot_override_harness(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        forbidden = {
            "timeout",
            "connect_timeout",
            "pool_timeout",
            "write_timeout",
            "read_timeout",
            "activation_timeout",
            "max_retries",
            "transport",
            "api_key",
            "authorization",
            "base_url",
        }
        conflicts = sorted(forbidden.intersection(key.casefold() for key in value))
        if conflicts:
            raise ValueError(
                "Profile request_options cannot override Harness transport policy: "
                + ", ".join(conflicts)
            )
        return value

    @model_validator(mode="after")
    def _capability_identity(self) -> ProfileSnapshot:
        validate_profile_base_url(self.api_family, self.base_url)
        validate_profile_request_options(self.api_family, self.request_options)
        if self.capability_fingerprint != self.capabilities.fingerprint:
            raise ValueError("capability_fingerprint does not match the capability snapshot.")
        return self

    @classmethod
    def create(
        cls,
        *,
        profile_id: str,
        display_name: str,
        api_family: ApiFamily,
        base_url: str,
        model_id: str,
        capabilities: ProfileCapabilities,
        request_options: dict[str, JsonValue] | None = None,
    ) -> ProfileSnapshot:
        return cls(
            profile_id=profile_id,
            display_name=display_name,
            api_family=api_family,
            base_url=base_url,
            model_id=model_id,
            request_options=request_options or {},
            capabilities=capabilities,
            capability_fingerprint=capabilities.fingerprint,
        )

    @property
    def fingerprint(self) -> str:
        return _canonical_json_sha(self)


class AgentTaskPlan(BaseModel):
    """The complete secret-free contract for exactly one stateless Agent run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str
    project_id: str
    run_id: str
    task_key: str
    action_key: str
    predecessor_task_id: str | None = None
    role: AgentRole
    task_kind: str
    contract_version: int = Field(ge=1)
    scope_layer: ScopeLayer
    book_id: str
    arc_id: str | None = None
    chapter_id: str | None = None
    workspace_lock_version: int | None = Field(default=None, ge=1)
    workspace_work_cycle_id: str | None = None
    book_baseline_id: str | None = None
    arc_baseline_id: str | None = None
    chapter_baseline_id: str | None = None
    subject_arc_baseline_id: str | None = None
    canon_baseline_id: str
    correction_lineage_id: str | None = None
    correction_lineage_origin: Literal["review_initiated", "user_initiated"] | None = (
        None
    )
    automatic_correction_round: Literal[0, 1] | None = None
    source_arc_parent_review_id: str | None = None
    source_book_parent_review_id: str | None = None
    source_arc_closure_review_id: str | None = None
    source_book_completion_review_id: str | None = None
    source_book_candidate_review_id: str | None = None
    source_arc_candidate_review_id: str | None = None
    source_chapter_candidate_review_id: str | None = None
    source_book_progress_handoff_id: str | None = None
    source_chapter_arc_request_id: str | None = None
    source_arc_book_request_id: str | None = None
    source_arc_closure_id: str | None = None
    source_feedback_id: str | None = None
    semantic_goal: str
    prompt: str
    context_manifest: dict[str, JsonValue]
    context_policy_id: str
    context_policy_version: int = Field(ge=1)
    output_schema_id: str
    output_schema_version: int = Field(ge=1)
    output_schema: dict[str, JsonValue]
    evaluation_strategy_id: str | None = None
    evaluation_strategy_version: int | None = Field(default=None, ge=1)
    rubric_id: str | None = None
    rubric_version: int | None = Field(default=None, ge=1)
    rubric_text: str | None = None
    repairable_components: tuple[str, ...] = Field(
        default=(),
        description=(
            "Harness-declared same-layer candidate envelope for Book/Arc semantic "
            "repair. It is frozen control metadata, never model-authored authority."
        ),
    )
    harness_policy_id: str = "novelpilot-domain-harness"
    harness_policy_version: int = Field(default=1, ge=1)
    toolset: tuple[str, ...] = ()
    output_mode: OutputMode
    required_capabilities: tuple[CapabilityName, ...]
    model_request_limit: int = Field(ge=1, le=2)
    provider_request_limit: int = Field(default=PROVIDER_REQUEST_LIMIT, ge=1)
    transport_retry_limit: int = Field(default=TRANSPORT_RETRY_LIMIT, ge=0)
    connect_timeout_ms: int = CONNECT_TIMEOUT_MS
    pool_timeout_ms: int = POOL_TIMEOUT_MS
    write_timeout_ms: int = WRITE_TIMEOUT_MS
    read_timeout_ms: int = READ_TIMEOUT_MS
    activation_timeout_ms: int = ACTIVATION_TIMEOUT_MS
    timeout_policy_id: str = TIMEOUT_POLICY_ID
    profile_snapshot: ProfileSnapshot
    profile_fingerprint: str

    @field_validator(
        "task_id",
        "project_id",
        "run_id",
        "task_key",
        "action_key",
        "task_kind",
        "book_id",
        "canon_baseline_id",
        "semantic_goal",
        "prompt",
        "context_policy_id",
        "output_schema_id",
        "harness_policy_id",
    )
    @classmethod
    def _identity_non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Frozen task identity and text fields must be non-blank.")
        return value

    @model_validator(mode="after")
    def _fixed_contract(self) -> AgentTaskPlan:
        if self.profile_fingerprint != self.profile_snapshot.fingerprint:
            raise ValueError("profile_fingerprint does not match profile_snapshot.")
        if self.provider_request_limit != PROVIDER_REQUEST_LIMIT:
            raise ValueError("Provider request limit is fixed at six requests per activation.")
        if self.transport_retry_limit != TRANSPORT_RETRY_LIMIT:
            raise ValueError("Transport retry limit is fixed at five retries per activation.")
        actual_t1 = (
            self.connect_timeout_ms,
            self.pool_timeout_ms,
            self.write_timeout_ms,
            self.read_timeout_ms,
            self.activation_timeout_ms,
            self.timeout_policy_id,
        )
        expected_t1 = (
            CONNECT_TIMEOUT_MS,
            POOL_TIMEOUT_MS,
            WRITE_TIMEOUT_MS,
            READ_TIMEOUT_MS,
            ACTIVATION_TIMEOUT_MS,
            TIMEOUT_POLICY_ID,
        )
        if actual_t1 != expected_t1:
            raise ValueError("Task Plan timeout values must match the frozen T1 policy.")
        expected_scope = {
            "book": (False, False),
            "arc": (True, False),
            "chapter": (True, True),
        }[self.scope_layer]
        if (self.arc_id is not None, self.chapter_id is not None) != expected_scope:
            raise ValueError("Scope IDs do not match scope_layer.")
        baseline_shape_is_valid = (
            (
                self.scope_layer == "book"
                and self.arc_baseline_id is None
                and self.chapter_baseline_id is None
            )
            or (
                self.scope_layer == "arc"
                and self.book_baseline_id is not None
                and self.chapter_baseline_id is None
            )
            or (
                self.scope_layer == "chapter"
                and self.book_baseline_id is not None
                and self.arc_baseline_id is not None
            )
        )
        if not baseline_shape_is_valid:
            raise ValueError("Baseline IDs do not match scope_layer.")
        if (
            self.task_kind != "evaluate.book_parent_contract"
            and self.subject_arc_baseline_id is not None
        ):
            raise ValueError(
                "Only a Book parent-contract task may freeze an Arc review subject."
            )
        if (self.rubric_id is None) != (self.rubric_version is None):
            raise ValueError("rubric_id and rubric_version must be present together.")
        if (self.evaluation_strategy_id is None) != (
            self.evaluation_strategy_version is None
        ):
            raise ValueError(
                "evaluation_strategy_id and evaluation_strategy_version "
                "must be present together."
            )
        if self.rubric_id is None:
            if self.rubric_text is not None:
                raise ValueError("rubric_text requires a frozen rubric identity.")
        elif self.rubric_text is None or not self.rubric_text.strip():
            raise ValueError("Evaluator rubrics must freeze substantive rubric text.")
        if len(self.repairable_components) != len(set(self.repairable_components)) or any(
            not component.strip() for component in self.repairable_components
        ):
            raise ValueError("Frozen repairable component names must be unique and non-blank.")
        review_sources = (
            self.source_arc_parent_review_id,
            self.source_book_parent_review_id,
            self.source_arc_closure_review_id,
            self.source_book_completion_review_id,
        )
        if sum(source is not None for source in review_sources) > 1:
            raise ValueError("A Task Plan may bind at most one source review.")
        candidate_review_sources = (
            self.source_book_candidate_review_id,
            self.source_arc_candidate_review_id,
            self.source_chapter_candidate_review_id,
        )
        if sum(source is not None for source in candidate_review_sources) > 1:
            raise ValueError("A Task Plan may bind at most one candidate review.")
        if (self.workspace_lock_version is None) != (
            self.workspace_work_cycle_id is None
        ):
            raise ValueError(
                "Workspace lock and semantic work-cycle identity must be frozen together."
            )
        if (
            self.workspace_work_cycle_id is not None
            and not self.workspace_work_cycle_id.strip()
        ):
            raise ValueError("Workspace semantic work-cycle identity must be non-blank.")
        authority_sources = (
            self.source_chapter_arc_request_id,
            self.source_arc_book_request_id,
            self.source_arc_closure_id,
        )
        if sum(source is not None for source in authority_sources) > 1:
            raise ValueError("A Task Plan may bind at most one source authority object.")
        lineage_fields = (
            self.correction_lineage_id,
            self.correction_lineage_origin,
            self.automatic_correction_round,
        )
        if not all(value is None for value in lineage_fields) and any(
            value is None for value in lineage_fields
        ):
            raise ValueError("Correction lineage identity, origin, and round are atomic.")
        if (
            self.correction_lineage_origin == "review_initiated"
            and self.source_feedback_id is not None
        ):
            raise ValueError("Review-initiated correction cannot bind user feedback.")
        elif (
            self.correction_lineage_origin == "user_initiated"
            and self.source_feedback_id is None
        ):
            raise ValueError("User-initiated correction must bind its feedback item.")
        if self.toolset:
            raise ValueError("O1 tasks cannot expose run-local or domain write tools.")
        if self.output_mode == "native_json_schema":
            if self.required_capabilities != ("native_json_schema",) or self.model_request_limit != 2:
                raise ValueError("Native tasks require native_json_schema and two model requests.")
        elif self.required_capabilities != ("text_streaming",) or self.model_request_limit != 1:
            raise ValueError("Prose tasks require text_streaming and one model request.")
        return self

    @property
    def prompt_fingerprint(self) -> str:
        return hashlib.sha256(self.prompt.encode("utf-8")).hexdigest()

    @property
    def input_fingerprint(self) -> str:
        return _canonical_json_sha(self.context_manifest)

    @property
    def context_policy_fingerprint(self) -> str:
        return _fingerprint_parts(self.context_policy_id, self.context_policy_version)

    @property
    def output_schema_fingerprint(self) -> str:
        return _canonical_json_sha(self.output_schema)

    @property
    def toolset_fingerprint(self) -> str:
        return _canonical_json_sha(list(self.toolset))

    @property
    def fingerprint(self) -> str:
        return _canonical_json_sha(self)


class ChapterDraftResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    prose: str

    @field_validator("prose")
    @classmethod
    def _non_blank_prose(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Chapter prose must be non-blank.")
        return value


class BookDiscussionSuggestion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    label: str = Field(
        min_length=1,
        max_length=100,
        description="Short creator-facing label for this actionable option.",
    )
    message: str = Field(
        min_length=1,
        max_length=4_000,
        description="The complete answer that selecting this option submits.",
    )
    rationale: str = Field(
        default="",
        max_length=2_000,
        description="Why this option helps the whole-book design.",
    )
    recommended: bool = Field(
        default=False,
        description="Whether this is the Book Strategist's preferred option.",
    )
    formal_title: str | None = Field(
        default=None,
        max_length=200,
        description=(
            "The exact formal title selected by this option, or null when this is an "
            "ordinary design answer. Each option is typed independently, so a question "
            "may contain both title and ordinary options."
        ),
    )


class BookDiscussionContinue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["continue"]
    reason: str = Field(
        min_length=1,
        max_length=4_000,
        description="Why one more creator decision is still needed.",
    )
    question: str = Field(
        min_length=1,
        max_length=600,
        description=(
            "One concrete, high-value creator decision. Natural punctuation is allowed; "
            "do not pack multiple independent decisions into this field."
        ),
    )
    suggestions: list[BookDiscussionSuggestion] = Field(
        min_length=2,
        max_length=3,
        description="Two or three actionable answers to the one creator question.",
    )


class BookDiscussionReady(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["ready"]
    reason: str = Field(
        min_length=1,
        max_length=4_000,
        description=(
            "Why the whole-book direction and formal title are ready for synthesis. "
            "Use ready only when the context already contains a selected title or the "
            "latest creator message explicitly selected newly_selected_title."
        ),
    )


BookDiscussionReadiness = Annotated[
    BookDiscussionContinue | BookDiscussionReady,
    Field(discriminator="status"),
]


class BookSupersededDecisionProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    prior_meaning: str = Field(
        min_length=1,
        max_length=4_000,
        description=(
            "Semantic meaning of the earlier confirmed decision affected by the latest "
            "creator message. Do not copy an opaque ID or exact stored string."
        ),
    )
    replacement: str | None = Field(
        default=None,
        max_length=4_000,
        description="Replacement decision, or null when the prior decision is withdrawn.",
    )
    reason: str = Field(
        min_length=1,
        max_length=4_000,
        description="Semantic explanation of how the latest creator message changes it.",
    )


class BookDiscussionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    reply: str = Field(
        min_length=1,
        max_length=20_000,
        description=(
            "Creator-facing explanation for this turn. When continuing, put the single "
            "next decision in readiness.question rather than relying on reply formatting."
        ),
    )
    direction_draft: str = Field(
        min_length=1,
        max_length=100_000,
        description="Complete updated whole-book direction working draft.",
    )
    discussion_summary: str = Field(
        min_length=1,
        max_length=20_000,
        description="Compact cumulative summary of the Book discussion so far.",
    )
    newly_confirmed_decisions: list[str] = Field(
        default_factory=list,
        max_length=50,
        description="Semantic decisions established by the latest creator turn.",
    )
    superseded_decisions: list[BookSupersededDecisionProposal] = Field(
        default_factory=list,
        max_length=50,
        description=(
            "Semantic changes to earlier confirmed decisions. The Harness binds the "
            "latest creator message as provenance; do not reproduce evidence locators."
        ),
    )
    unresolved_questions: list[str] = Field(
        default_factory=list,
        max_length=100,
        description="Whole-book questions that remain unresolved after this turn.",
    )
    assumptions: list[str] = Field(
        default_factory=list,
        max_length=100,
        description="Assumptions currently used by the working direction.",
    )
    contradictions: list[str] = Field(
        default_factory=list,
        max_length=100,
        description="Known semantic contradictions that later review must reconcile.",
    )
    newly_selected_title: str | None = Field(
        default=None,
        max_length=200,
        description=(
            "Exact formal title only when the latest creator message explicitly selected "
            "or stated it; otherwise null. Title proposals belong in suggestions."
        ),
    )
    readiness: BookDiscussionReadiness = Field(
        description=(
            "Use the continue shape for one remaining creator decision and the ready "
            "shape only when no further creator question is needed."
        ),
    )


class ArcStateTransition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    start_state: str = Field(
        min_length=1,
        description="Authoritative stage state that the Arc begins from.",
    )
    end_state: str = Field(
        min_length=1,
        description="Observable stage state that the Arc must establish at closure.",
    )


class ArcClosureSignal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    signal_key: str = Field(
        min_length=1,
        pattern=r"^[a-z0-9][a-z0-9_.-]*$",
        description="Stable semantic key used to match closure evidence.",
    )
    description: str = Field(
        min_length=1,
        description="Observable condition whose satisfaction can be evaluated.",
    )
    evidence_expectation: str = Field(
        min_length=1,
        description="What committed Chapter or Canon evidence can prove the signal.",
    )
    required: bool = Field(
        default=True,
        description="Whether Arc closure requires this signal to be satisfied.",
    )


class ArcChapterOutlineEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    title: str = Field(
        min_length=1,
        description="Provisional creator-facing title for this Chapter assignment.",
    )
    core_event: str = Field(
        min_length=1,
        description="The macro narrative event this Chapter must materially advance.",
    )
    hook: str = Field(
        min_length=1,
        description="The intended semantic handoff into the next Chapter or Arc closure.",
    )
    scenes: list[str] = Field(
        min_length=1,
        description="Ordered broad scene intentions used to realize the core event.",
    )

    @field_validator("title", "core_event", "hook")
    @classmethod
    def _non_blank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Arc Chapter outline text must be non-blank.")
        return value

    @field_validator("scenes")
    @classmethod
    def _non_blank_scenes(cls, value: list[str]) -> list[str]:
        if any(not scene.strip() for scene in value):
            raise ValueError("Arc Chapter outline scenes must be non-blank.")
        return value


class ArcPlanProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    title: str = Field(min_length=1, description="Creator-facing title for this Story Arc.")
    desired_state_transition: ArcStateTransition
    conflict_trajectory: list[str] = Field(
        min_length=1,
        description="Ordered stage-level escalation and resolution trajectory.",
    )
    pacing_trajectory: list[str] = Field(
        min_length=1,
        description="Stage-level pacing phases without prescribing every Chapter.",
    )
    character_obligations: list[str] = Field(
        min_length=1,
        description="Character or relationship changes this Arc must establish.",
    )
    foreshadowing_obligations: list[str] = Field(
        default_factory=list,
        description="Foreshadowing promises to plant, advance, or pay off in this Arc.",
    )
    prohibitions: list[str] = Field(
        min_length=1,
        description="Book constraints and outcomes this Arc must not violate.",
    )
    closure_signals: list[ArcClosureSignal] = Field(
        min_length=1,
        description="Observable contract used by the mandatory Arc closure evaluation.",
    )
    chapter_outline: list[ArcChapterOutlineEntry] = Field(
        default_factory=list,
        description=(
            "Complete ordered macro assignments from the Harness-frozen effective "
            "point through the Arc's semantic closure boundary. Initial Arc plans "
            "must contain at least one entry; an authorized successor may be empty "
            "only when the effective point is already ready for closure. Entries contain no "
            "ordinals or storage identities."
        ),
    )

    @field_validator("closure_signals")
    @classmethod
    def _unique_closure_signals(
        cls, value: list[ArcClosureSignal]
    ) -> list[ArcClosureSignal]:
        keys = [signal.signal_key for signal in value]
        if len(keys) != len(set(keys)):
            raise ValueError("Arc closure signal keys must be unique.")
        if not any(signal.required for signal in value):
            raise ValueError("An Arc contract needs at least one required closure signal.")
        return value

class ChapterPlanProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    title: str = Field(min_length=1, description="Working title for this chapter.")
    purpose: str = Field(
        min_length=1,
        description="How this chapter advances the approved Story Arc.",
    )
    scene_beats: list[str] = Field(
        min_length=1,
        description="Ordered semantic scene beats for this chapter.",
    )
    required_continuity: list[str] = Field(
        default_factory=list,
        description="Frozen continuity facts that the prose must preserve.",
    )


class SemanticCanonProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    category: Literal["characters", "relationships", "world_facts", "foreshadowing"]
    subject: str = Field(
        min_length=1,
        description=(
            "Semantic subject established or changed by the chapter. Use natural meaning; "
            "never copy or invent a Canon ID, reference, or stored locator."
        ),
    )
    semantic_change: str = Field(
        min_length=1,
        description=(
            "The complete current meaning established for this semantic subject, without "
            "choosing a storage operation or committing it."
        ),
    )
    resolved: bool = Field(
        description=(
            "True only when this chapter semantically closes the subject; false when the "
            "subject remains active. The Harness alone derives insert-or-replace behavior."
        ),
    )
    evidence_hint: str = Field(
        min_length=1,
        description=(
            "Human-readable semantic fact or rationale from the frozen prose. Summarize "
            "naturally; do not copy an exact quote, offset, locator, or stored source string."
        ),
    )


class EstablishedFactCandidate(BaseModel):
    """A model-authored fact candidate with no storage or authority identity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    statement: str = Field(
        min_length=1,
        description=(
            "One semantic fact established by the frozen Chapter prose. Preserve "
            "character claims and beliefs as scoped statements rather than upgrading "
            "them to objective truth. Do not return IDs, hashes, offsets, or locators."
        ),
    )
    evidence_hint: str = Field(
        min_length=1,
        description=(
            "A concise natural-language rationale from the frozen prose. Do not copy "
            "an exact quote, byte offset, storage reference, or Harness locator."
        ),
    )

    @field_validator("statement", "evidence_hint")
    @classmethod
    def _trim_non_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Established fact text must be non-blank.")
        return stripped


class ChapterObservationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    summary: str = Field(
        min_length=1,
        description="Semantic summary of what the frozen chapter establishes.",
    )
    established_facts: list[EstablishedFactCandidate] = Field(
        default_factory=list,
        description=(
            "Ordinary semantic facts established by the current frozen prose. Earlier "
            "silence does not make a fact false. These are unbound candidates; the "
            "Harness attaches exact Chapter, baseline, and prose provenance at commit."
        ),
    )
    canon_proposals: list[SemanticCanonProposal] = Field(
        default_factory=list,
        description=(
            "Semantic proposals only; the Harness resolves IDs and optional exact evidence "
            "spans, then commits accepted facts."
        ),
    )


ChapterRepairComponent = Literal["plan", "prose", "observations", "canon"]
GuidanceAuthorityJudgment = Literal[
    "not_present",
    "compatible_with_current_authority",
    "requires_parent_review",
]


class ChapterObservationsRepair(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    component: Literal["observations"]
    summary: str = Field(
        min_length=1,
        description="Replacement semantic summary of what the frozen chapter establishes.",
    )
    established_facts: list[EstablishedFactCandidate] = Field(
        default_factory=list,
        description=(
            "Replacement unbound facts established by the same frozen Chapter prose."
        ),
    )


class ChapterCanonRepair(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    component: Literal["canon"]
    canon_proposals: list[SemanticCanonProposal] = Field(
        default_factory=list,
        description="Replacement semantic Canon proposals for the frozen chapter.",
    )


ChapterObservationRepairChange = Annotated[
    ChapterObservationsRepair | ChapterCanonRepair,
    Field(discriminator="component"),
]


class ChapterObservationRepairPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    changes: list[ChapterObservationRepairChange] = Field(
        min_length=1,
        max_length=2,
        description=(
            "Only observation or Canon components authorized by the repair scope in frozen "
            "context. Omitted components are preserved by the Harness and must not be repeated."
        ),
    )

    @field_validator("changes")
    @classmethod
    def _unique_components(
        cls,
        value: list[ChapterObservationRepairChange],
    ) -> list[ChapterObservationRepairChange]:
        components = [change.component for change in value]
        if len(components) != len(set(components)):
            raise ValueError("A Chapter observation repair may change each component once.")
        return value


EvaluationIssueKind = Literal[
    "explicit_conflict",
    "contract_unfulfilled",
    "unsupported_strong_conclusion",
    "derived_evidence_mismatch",
    "parent_authority_concern",
    "creator_owned_unknown",
]


class EvaluationIssue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: EvaluationIssueKind = Field(
        description=(
            "The closed EP1 blocker kind. Literary preference, style, pacing, and "
            "advisory deviation are not blocker kinds."
        ),
    )
    code: str = Field(
        min_length=1,
        description="Stable issue code inside the selected EP1 blocker kind.",
    )
    subject: str = Field(
        min_length=1,
        description="The concrete semantic fact, contract item, or conclusion at issue.",
    )
    summary: str = Field(min_length=1, description="Clear explanation of the rubric failure.")
    evidence: list[str] = Field(
        min_length=1,
        description=(
            "Affirmative model-visible evidence from the current candidate and allowed "
            "formal sources. Prior silence is not contrary evidence."
        ),
    )
    candidate_claim: str | None = Field(
        default=None,
        description=(
            "Required for explicit_conflict and derived_evidence_mismatch: the "
            "affirmative candidate or derived statement being judged."
        ),
    )
    contrary_formal_statement: str | None = Field(
        default=None,
        description=(
            "Required for explicit_conflict and derived_evidence_mismatch: the "
            "affirmative contradictory formal source, never mere historical silence."
        ),
    )
    contract_item: str | None = Field(
        default=None,
        description="Required only when an explicit closed-world contract item is unfulfilled.",
    )
    support_gap: str | None = Field(
        default=None,
        description=(
            "Required for unsupported_strong_conclusion: the missing support for "
            "culpability, exclusion, core causal closure, certainty escalation, or an "
            "upper-contract change."
        ),
    )
    creator_question: str | None = Field(
        default=None,
        description=(
            "Required only for creator_owned_unknown: one concrete answerable "
            "creator-owned question."
        ),
    )

    @model_validator(mode="after")
    def _ep1_shape(self) -> EvaluationIssue:
        statement_pair = (
            self.candidate_claim is not None
            and bool(self.candidate_claim.strip())
            and self.contrary_formal_statement is not None
            and bool(self.contrary_formal_statement.strip())
        )
        has_any_statement = (
            self.candidate_claim is not None
            or self.contrary_formal_statement is not None
        )
        if (
            self.kind in {"explicit_conflict", "derived_evidence_mismatch"}
            and not statement_pair
        ):
            raise ValueError(
                f"{self.kind} requires affirmative candidate and formal statements."
            )
        if has_any_statement and not statement_pair:
            raise ValueError(
                "Candidate/formal diagnostic statements must be supplied together and "
                "must both be non-blank."
            )

        has_contract_item = self.contract_item is not None and bool(
            self.contract_item.strip()
        )
        if self.contract_item is not None and not has_contract_item:
            raise ValueError("A supplied contract item must be non-blank.")
        if self.kind == "contract_unfulfilled" and not has_contract_item:
            raise ValueError(
                "contract_unfulfilled requires one explicit contract item."
            )

        has_support_gap = self.support_gap is not None and bool(self.support_gap.strip())
        if self.support_gap is not None and not has_support_gap:
            raise ValueError("A supplied support gap must be non-blank.")
        if self.kind == "unsupported_strong_conclusion" and not has_support_gap:
            raise ValueError(
                "unsupported_strong_conclusion requires one support gap."
            )

        has_creator_question = self.creator_question is not None and bool(
            self.creator_question.strip()
        )
        if self.creator_question is not None and not has_creator_question:
            raise ValueError("A supplied creator question must be non-blank.")
        if (self.kind == "creator_owned_unknown") != has_creator_question:
            raise ValueError(
                "Exactly creator_owned_unknown requires one concrete creator question."
            )
        return self


class ChapterEvaluationIssue(EvaluationIssue):
    model_config = ConfigDict(extra="forbid", frozen=True)

    observed_components: list[ChapterRepairComponent] = Field(
        default_factory=list,
        description=(
            "Unique semantic Chapter locations where this issue is visible. Local-repair "
            "issues must name at least one location. On an Arc escalation these locations "
            "are optional diagnostics only and never grant Chapter repair authority."
        ),
    )

    @field_validator("observed_components")
    @classmethod
    def _unique_observed_components(
        cls,
        value: list[ChapterRepairComponent],
    ) -> list[ChapterRepairComponent]:
        if len(value) != len(set(value)):
            raise ValueError("Chapter issue observed components must be unique.")
        return value

    @model_validator(mode="after")
    def _chapter_authority_boundary(self) -> ChapterEvaluationIssue:
        if self.kind == "creator_owned_unknown":
            raise ValueError("Chapter evaluation cannot create a creator wait.")
        return self


class ChapterRepairVerificationIssue(ChapterEvaluationIssue):
    model_config = ConfigDict(extra="forbid", frozen=True)

    recurrence: Literal["new", "persists_after_authorized_repair"] = Field(
        default="new",
        description=(
            "Use persists_after_authorized_repair only when this same semantic issue "
            "remains after the one frozen authorized Chapter correction."
        ),
    )


class _ChapterEvaluationResultBase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    guidance_authority_judgment: GuidanceAuthorityJudgment = Field(
        description=(
            "Judge active applied user guidance itself, independently of whether the "
            "candidate followed it. The model-visible active_applied_guidance_present fact "
            "is authoritative: false requires not_present; true forbids not_present. For "
            "present guidance, use compatible_with_current_authority only when the requested "
            "effect can be fully honored without changing the formal Arc or Book authority, "
            "and requires_parent_review when honoring it would require parent authority."
        ),
    )
    decision: Literal["pass", "local_repair", "escalate_to_arc"] = Field(
        description=(
            "Use local_repair only for a bounded chapter repair; use "
            "escalate_to_arc only for a specific evidence-bound concern about "
            "the immediate parent Arc. Chapter evaluation cannot create a user wait."
        ),
    )
    summary: str = Field(min_length=1, description="Evidence-based evaluation summary.")

    def _validate_decision_payload(
        self,
        issues: Sequence[ChapterEvaluationIssue],
    ) -> None:
        if (
            self.guidance_authority_judgment == "requires_parent_review"
            and self.decision != "escalate_to_arc"
        ):
            raise ValueError(
                "Chapter guidance requiring parent authority must escalate to Arc."
            )
        if self.decision == "pass" and issues:
            raise ValueError("A passing Chapter evaluation cannot carry blocking issues.")
        if self.decision != "pass" and not issues:
            raise ValueError("A non-passing Chapter evaluation requires blocking issues.")
        observed_scope = {
            component
            for issue in issues
            for component in issue.observed_components
        }
        if self.decision == "local_repair" and any(
            not issue.observed_components for issue in issues
        ):
            raise ValueError(
                "Every Chapter local-repair issue requires observed components."
            )
        if self.decision == "local_repair" and any(
            issue.kind == "parent_authority_concern" for issue in issues
        ):
            raise ValueError(
                "A parent-authority concern cannot authorize a Chapter-local repair."
            )
        if self.decision == "escalate_to_arc" and not any(
            issue.kind == "parent_authority_concern" for issue in issues
        ):
            raise ValueError(
                "escalate_to_arc requires an evidence-bound parent-authority concern."
            )
        if self.decision == "escalate_to_arc" and any(
            issue.kind != "parent_authority_concern" for issue in issues
        ):
            raise ValueError(
                "escalate_to_arc may carry only direct-parent concerns."
            )
        if (
            self.decision == "local_repair"
            and "plan" in observed_scope
            and observed_scope != {"plan"}
        ):
            raise ValueError(
                "A Chapter plan repair must be the only repair component; the Harness "
                "invalidates and regenerates every downstream working component."
            )


class LayerEvaluationResult(_ChapterEvaluationResultBase):
    issues: list[ChapterEvaluationIssue] = Field(
        default_factory=list,
        description=(
            "Blocking semantic issues. observed_components are diagnostic locations. "
            "Only after local_repair does the Harness derive a separate narrow repair "
            "authorization from those diagnostics."
        ),
    )

    @model_validator(mode="after")
    def _decision_payload(self) -> LayerEvaluationResult:
        self._validate_decision_payload(self.issues)
        return self


class ChapterRepairVerificationResult(_ChapterEvaluationResultBase):
    issues: list[ChapterRepairVerificationIssue] = Field(
        default_factory=list,
        description=(
            "Blocking semantic issues from repair verification. Each issue additionally "
            "classifies whether it is new or persists from the frozen authorized repair."
        ),
    )

    @model_validator(mode="after")
    def _decision_payload(self) -> ChapterRepairVerificationResult:
        self._validate_decision_payload(self.issues)
        return self


def finalize_chapter_prose(text: str) -> ChapterDraftResult:
    """Pure S1 finalizer: no ID generation, I/O, event, or storage mutation."""
    return ChapterDraftResult(prose=text)


def validate_profile_request_options(
    api_family: ApiFamily,
    request_options: Mapping[str, JsonValue],
) -> None:
    """Validate protocol-owned options without inferring anything from ``model_id``."""

    sensitive_path = _sensitive_request_option_path(request_options)
    if sensitive_path is not None:
        raise ValueError(
            "Profile request_options cannot contain credentials or signed URL material: "
            f"{sensitive_path}."
        )
    if "max_output_tokens" in request_options:
        raise ValueError(
            "Profile request_options use Pydantic AI's portable max_tokens key; "
            "max_output_tokens is a wire-level field."
        )
    if "max_tokens" in request_options:
        max_tokens = request_options["max_tokens"]
        if type(max_tokens) is not int or max_tokens <= 0:
            raise ValueError("Profile request_options.max_tokens must be a positive integer.")
    elif api_family == "anthropic_messages":
        raise ValueError(
            "Anthropic Messages profiles require an explicit generous max_tokens value; "
            "NovelPilot never uses Pydantic AI's implicit 4096 default."
        )


def validate_profile_base_url(api_family: ApiFamily, base_url: str) -> None:
    """Keep SDK-relative endpoint joining explicit for the two supported protocols."""

    path = urlsplit(base_url).path.rstrip("/")
    if api_family == "openai_responses" and not path.endswith("/v1"):
        raise ValueError(
            "OpenAI Responses base_url must end in /v1; the Adapter appends /responses."
        )
    if api_family == "anthropic_messages" and path.endswith("/v1"):
        raise ValueError(
            "Anthropic Messages base_url must exclude the terminal /v1; "
            "the Adapter appends /v1/messages."
        )


def _sensitive_request_option_path(
    value: Mapping[str, JsonValue],
    *,
    prefix: str = "request_options",
) -> str | None:
    sensitive_keys = {
        "authorization",
        "cookie",
        "setcookie",
        "apikey",
        "xapikey",
        "accesstoken",
        "refreshtoken",
        "clientsecret",
        "password",
        "signature",
        "signedurl",
    }
    for key, item in value.items():
        path = f"{prefix}.{key}"
        normalized = re.sub(r"[^a-z0-9]", "", key.casefold())
        if normalized in sensitive_keys:
            return path
        if isinstance(item, dict):
            nested = _sensitive_request_option_path(item, prefix=path)
            if nested is not None:
                return nested
        elif isinstance(item, list):
            for index, nested_item in enumerate(item):
                if isinstance(nested_item, dict):
                    nested = _sensitive_request_option_path(
                        nested_item,
                        prefix=f"{path}[{index}]",
                    )
                    if nested is not None:
                        return nested
        elif isinstance(item, str) and re.search(
            r"(?i)[?&](?:access_token|api_key|key|signature|sig|token)=",
            item,
        ):
            return path
    return None


def _fingerprint_parts(identity: str, version: int) -> str:
    encoded = json.dumps(
        {"identity": identity, "version": version},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _canonical_json_sha(value: object) -> str:
    normalized = _json_value(value)
    encoded = json.dumps(
        normalized,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _json_value(value: object) -> JsonValue:
    if isinstance(value, BaseModel):
        return _json_value(value.model_dump(mode="json"))
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Agent contract fingerprints reject NaN and Infinity.")
        return value
    if isinstance(value, Mapping):
        normalized: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("Agent contract fingerprints require string object keys.")
            normalized[key] = _json_value(item)
        return normalized
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_value(item) for item in value]
    raise TypeError(f"Unsupported Agent contract fingerprint value: {type(value).__name__}.")
