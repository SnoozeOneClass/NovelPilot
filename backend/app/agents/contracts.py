from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]

AgentRole = Literal["book_strategist", "arc_planner", "chapter_writer", "evaluator"]
ScopeLayer = Literal["book", "arc", "chapter"]
OutputMode = Literal["native_json_schema", "text_streaming"]
ApiFamily = str
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
        if self.capability_fingerprint != self.capabilities.fingerprint:
            raise ValueError("capability_fingerprint does not match the capability snapshot.")
        return self

    @classmethod
    def create(
        cls,
        *,
        profile_id: str,
        display_name: str,
        api_family: str,
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
    book_baseline_id: str | None = None
    arc_baseline_id: str | None = None
    chapter_baseline_id: str | None = None
    canon_baseline_id: str
    semantic_goal: str
    prompt: str
    context_manifest: dict[str, JsonValue]
    context_policy_id: str
    context_policy_version: int = Field(ge=1)
    output_schema_id: str
    output_schema_version: int = Field(ge=1)
    output_schema: dict[str, JsonValue]
    rubric_id: str | None = None
    rubric_version: int | None = Field(default=None, ge=1)
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
        if (self.rubric_id is None) != (self.rubric_version is None):
            raise ValueError("rubric_id and rubric_version must be present together.")
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

    label: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=4_000)
    rationale: str = Field(default="", max_length=2_000)
    recommended: bool = False
    formal_title: str | None = Field(default=None, max_length=200)


class BookDiscussionReadiness(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["continue", "ready"]
    reason: str


class BookSupersededDecisionProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    prior_meaning: str = Field(min_length=1, max_length=4_000)
    replacement: str | None = Field(default=None, max_length=4_000)
    reason: str = Field(min_length=1, max_length=4_000)
    user_evidence: str = Field(min_length=1, max_length=4_000)


class BookDiscussionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    reply: str = Field(min_length=1, max_length=20_000)
    direction_draft: str = Field(min_length=1, max_length=100_000)
    discussion_summary: str = Field(min_length=1, max_length=20_000)
    newly_confirmed_decisions: list[str] = Field(default_factory=list, max_length=50)
    superseded_decisions: list[BookSupersededDecisionProposal] = Field(
        default_factory=list,
        max_length=50,
    )
    unresolved_questions: list[str] = Field(default_factory=list, max_length=100)
    assumptions: list[str] = Field(default_factory=list, max_length=100)
    contradictions: list[str] = Field(default_factory=list, max_length=100)
    newly_selected_title: str | None = Field(default=None, max_length=200)
    question: str | None = Field(default=None, max_length=600)
    suggestions: list[BookDiscussionSuggestion] = Field(default_factory=list, max_length=3)
    readiness: BookDiscussionReadiness

    @model_validator(mode="before")
    @classmethod
    def _normalize_blank_question(cls, value: object) -> object:
        if isinstance(value, dict):
            normalized = dict(value)
            question = normalized.get("question")
            if isinstance(question, str) and not question.strip():
                normalized["question"] = None
            return normalized
        return value

    @model_validator(mode="after")
    def _discussion_boundary(self) -> BookDiscussionResult:
        if any(mark in self.reply for mark in ("?", "？")):
            raise ValueError("Book discussion reply cannot contain a second question.")
        if self.readiness.status == "ready":
            if self.question is not None or self.suggestions:
                raise ValueError("A ready Book discussion cannot ask another question.")
            return self
        if self.question is None:
            raise ValueError("A continuing Book discussion requires one direct question.")
        question_marks = self.question.count("?") + self.question.count("？")
        if question_marks != 1 or not self.question.rstrip().endswith(("?", "？")):
            raise ValueError("Book discussion question must contain exactly one question mark.")
        normalized_question = self.question.casefold()
        forbidden = (
            "which issue should we discuss",
            "which topic should we discuss",
            "what should we discuss first",
            "先讨论哪个",
            "先确认哪个",
            "先聊哪个",
        )
        if any(fragment in normalized_question for fragment in forbidden):
            raise ValueError("Book Agent must choose the next concrete decision itself.")
        if not 2 <= len(self.suggestions) <= 3:
            raise ValueError("A continuing Book discussion requires two or three suggestions.")
        labels = [item.label.strip().casefold() for item in self.suggestions]
        messages = [item.message.strip().casefold() for item in self.suggestions]
        if len(labels) != len(set(labels)) or len(messages) != len(set(messages)):
            raise ValueError("Book discussion suggestions must be unique.")
        title_flags = [bool((item.formal_title or "").strip()) for item in self.suggestions]
        if any(title_flags) and not all(title_flags):
            raise ValueError("One Book question cannot mix title and ordinary suggestions.")
        titles = [
            item.formal_title.strip().casefold()
            for item in self.suggestions
            if item.formal_title is not None
        ]
        if len(titles) != len(set(titles)):
            raise ValueError("Book title suggestions must be unique.")
        return self


class BookProgressAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    decision: Literal["continue", "plan_final_arc", "complete", "needs_user"]
    rationale: str
    unresolved_requirements: list[str] = Field(default_factory=list)

    @field_validator("rationale")
    @classmethod
    def _rationale_non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Book progress rationale must be non-blank.")
        return value

    @field_validator("unresolved_requirements")
    @classmethod
    def _requirements_non_blank_unique(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value]
        if any(not item for item in normalized) or len(normalized) != len(set(normalized)):
            raise ValueError("Book progress requirements must be unique and non-blank.")
        return normalized


class ArcPlanProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    title: str
    purpose: str
    beats: list[str] = Field(min_length=1)
    target_chapter_count: int = Field(ge=1, le=30)
    completion_signals: list[str] = Field(min_length=1)

    @field_validator("title", "purpose")
    @classmethod
    def _arc_text_non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Arc title and purpose must be non-blank.")
        return value.strip()

    @field_validator("beats", "completion_signals")
    @classmethod
    def _arc_list_non_blank_unique(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value]
        if any(not item for item in normalized) or len(normalized) != len(set(normalized)):
            raise ValueError("Arc plan lists must contain unique non-blank values.")
        return normalized


class ChapterPlanProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    title: str
    purpose: str
    scene_beats: list[str] = Field(min_length=1)
    required_continuity: list[str] = Field(default_factory=list)


class SemanticCanonProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    category: Literal["characters", "relationships", "world_facts", "foreshadowing"]
    operation: Literal["add", "update", "resolve"]
    subject: str
    semantic_change: str
    evidence_hint: str

    @field_validator("subject", "semantic_change", "evidence_hint")
    @classmethod
    def _semantic_text_non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Canon proposal text fields must be non-blank.")
        return value


class ChapterObservationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    summary: str
    continuity_observations: list[str] = Field(default_factory=list)
    canon_proposals: list[SemanticCanonProposal] = Field(default_factory=list)


class EvaluationIssue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    summary: str
    evidence_hint: str | None = None
    repair_component: str | None = None


class LayerEvaluationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    decision: Literal["pass", "local_repair", "cross_loop_escalation", "needs_user"]
    summary: str
    issues: list[EvaluationIssue] = Field(default_factory=list)
    repair_scope: list[str] = Field(default_factory=list)
    escalation_target: Literal["arc", "book"] | None = None

    @model_validator(mode="after")
    def _decision_payload(self) -> LayerEvaluationResult:
        if self.decision == "local_repair" and not self.repair_scope:
            raise ValueError("local_repair requires a bounded repair_scope.")
        if self.decision != "local_repair" and self.repair_scope:
            raise ValueError("Only local_repair can carry repair_scope.")
        if self.decision == "cross_loop_escalation" and self.escalation_target is None:
            raise ValueError("cross_loop_escalation requires escalation_target.")
        if self.decision != "cross_loop_escalation" and self.escalation_target is not None:
            raise ValueError("Only cross_loop_escalation can carry escalation_target.")
        return self


def finalize_chapter_prose(text: str) -> ChapterDraftResult:
    """Pure S1 finalizer: no ID generation, I/O, event, or storage mutation."""
    return ChapterDraftResult(prose=text)


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
