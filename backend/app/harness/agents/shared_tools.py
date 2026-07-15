from hashlib import sha256
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.paths import ensure_relative_artifact_path
from app.harness.agents.models import AgentRole
from app.harness.agents.persistence import activation_relative, json_document
from app.harness.agents.registry import (
    ToolExecutionContext,
    ToolExecutionPlan,
    ToolHandlerError,
    ToolRegistry,
    ToolSpec,
)
from app.storage.projects import read_project_metadata


ContextPackName = Literal[
    "book_discussion",
    "book_direction",
    "story_arc",
    "chapter_plan",
    "chapter_draft",
    "committed_canon",
    "committed_chapters",
]


class GetLoopContextInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pack: ContextPackName
    max_characters: int = Field(default=30_000, ge=1_000, le=100_000)


class ReadChapterEvidenceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chapter_id: str = Field(min_length=1, max_length=128)
    source: Literal["final"] = "final"
    start_character: int = Field(default=0, ge=0)
    max_characters: int = Field(default=12_000, ge=1, le=50_000)

    @field_validator("chapter_id")
    @classmethod
    def validate_chapter_id(cls, value: str) -> str:
        ensure_relative_artifact_path(value)
        if len(Path(value).parts) != 1:
            raise ValueError("Chapter ID cannot contain path separators.")
        return value


class DecisionSuggestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=4_000)
    rationale: str = Field(default="", max_length=2_000)


class RequestUserDecisionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=600)
    context: str = Field(default="", max_length=4_000)
    suggestions: list[DecisionSuggestion] = Field(min_length=2, max_length=3)

    @model_validator(mode="after")
    def validate_single_question(self) -> "RequestUserDecisionInput":
        stripped = self.question.strip()
        if stripped.count("?") + stripped.count("？") != 1:
            raise ValueError("User decision must contain exactly one question mark.")
        if not stripped.endswith(("?", "？")):
            raise ValueError("User decision must end with its one question mark.")
        labels = [item.label.strip().casefold() for item in self.suggestions]
        messages = [item.message.strip().casefold() for item in self.suggestions]
        if len(labels) != len(set(labels)) or len(messages) != len(set(messages)):
            raise ValueError("User decision suggestions must be unique.")
        return self


class ReportBlockerInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["local", "needs_user", "cross_loop"]
    summary: str = Field(min_length=1, max_length=4_000)
    evidence: list[str] = Field(default_factory=list, max_length=50)
    target_owner: Literal["book", "story_arc"] | None = None
    contract_field: str | None = Field(default=None, max_length=1_000)
    contract_revision: int | None = Field(default=None, ge=1)
    committed_evidence_locator: str | None = Field(default=None, max_length=1_000)
    impossibility_reason: str | None = Field(default=None, max_length=4_000)

    @model_validator(mode="after")
    def validate_cross_loop_evidence(self) -> "ReportBlockerInput":
        cross_fields = [
            self.target_owner,
            self.contract_field,
            self.contract_revision,
            self.committed_evidence_locator,
            self.impossibility_reason,
        ]
        if self.kind == "cross_loop" and any(item is None for item in cross_fields):
            raise ValueError("Cross-Loop blocker requires complete contract evidence.")
        if self.kind != "cross_loop" and any(item is not None for item in cross_fields):
            raise ValueError("Upper-contract fields are only valid for cross-Loop blockers.")
        return self


PACK_ROLES: dict[ContextPackName, frozenset[AgentRole]] = {
    "book_discussion": frozenset({"book"}),
    "book_direction": frozenset({"book", "story_arc", "chapter"}),
    "story_arc": frozenset({"story_arc", "chapter"}),
    "chapter_plan": frozenset({"chapter"}),
    "chapter_draft": frozenset({"chapter"}),
    "committed_canon": frozenset({"book", "story_arc", "chapter"}),
    "committed_chapters": frozenset({"book", "story_arc", "chapter"}),
}


def register_shared_tools(registry: ToolRegistry) -> None:
    all_roles: frozenset[AgentRole] = frozenset({"book", "story_arc", "chapter"})
    registry.register(
        ToolSpec(
            name="get_loop_context",
            version=1,
            description=(
                "Read one role-authorized, budgeted NovelPilot context pack. "
                "Paths and caller identity are derived by the Harness."
            ),
            input_model=GetLoopContextInput,
            allowed_roles=all_roles,
            handler=_get_loop_context,
            read_only=False,
        )
    )
    registry.register(
        ToolSpec(
            name="read_chapter_evidence",
            version=1,
            description="Read a bounded range from committed chapter prose.",
            input_model=ReadChapterEvidenceInput,
            allowed_roles=all_roles,
            handler=_read_chapter_evidence,
            read_only=False,
        )
    )
    registry.register(
        ToolSpec(
            name="request_user_decision",
            version=1,
            description=(
                "Pause for exactly one concrete user decision with two or three suggestions."
            ),
            input_model=RequestUserDecisionInput,
            allowed_roles=all_roles,
            handler=_request_user_decision,
            read_only=False,
            terminal=True,
            expose_arguments=True,
        )
    )
    registry.register(
        ToolSpec(
            name="report_blocker",
            version=1,
            description=(
                "Report a typed local blocker or evidence-backed cross-Loop proposal. "
                "This Tool never activates another Agent."
            ),
            input_model=ReportBlockerInput,
            allowed_roles=all_roles,
            handler=_report_blocker,
            read_only=False,
            terminal=True,
            expose_arguments=True,
        )
    )


def _get_loop_context(
    context: ToolExecutionContext,
    arguments: BaseModel,
) -> ToolExecutionPlan:
    request = _typed(arguments, GetLoopContextInput)
    if context.identity.role not in PACK_ROLES[request.pack]:
        raise ToolHandlerError(
            "context_pack_not_authorized",
            f"Context pack {request.pack} is not allowed for {context.identity.role}.",
            recoverable=True,
            allowed_actions=["choose_authorized_context_pack"],
        )
    sources = _pack_sources(context.project_path, request.pack)
    resolved, remaining = [], request.max_characters
    for relative in sources:
        path = context.project_path / relative
        if not path.is_file():
            continue
        raw = path.read_bytes()
        text = raw.decode("utf-8-sig")
        excerpt = text[:remaining]
        resolved.append(
            {
                "source": relative.as_posix(),
                "sha256": sha256(raw).hexdigest(),
                "characters": len(text),
                "included_characters": len(excerpt),
                "truncated": len(excerpt) < len(text),
                "content": excerpt,
            }
        )
        remaining -= len(excerpt)
        if remaining <= 0:
            break
    snapshot = {
        "schema_version": 1,
        "pack": request.pack,
        "caller": context.identity.model_dump(mode="json"),
        "max_characters": request.max_characters,
        "sources": resolved,
        "excluded": _excluded_context_sources(request.pack),
    }
    snapshot_path = _audit_relative(context, "context")
    return ToolExecutionPlan(
        content=snapshot,
        files={snapshot_path: json_document(snapshot)},
        artifact_paths=[snapshot_path],
        allowed_actions=["request_another_context_pack", "submit_candidate"],
    )


def _read_chapter_evidence(
    context: ToolExecutionContext,
    arguments: BaseModel,
) -> ToolExecutionPlan:
    request = _typed(arguments, ReadChapterEvidenceInput)
    relative = Path("chapters") / request.chapter_id / "final.md"
    path = context.project_path / relative
    if not path.is_file():
        raise ToolHandlerError(
            "committed_chapter_not_found",
            f"Committed chapter evidence does not exist: {request.chapter_id}",
            recoverable=True,
            allowed_actions=["choose_committed_chapter"],
        )
    raw = path.read_bytes()
    text = raw.decode("utf-8-sig")
    start = min(request.start_character, len(text))
    excerpt = text[start : start + request.max_characters]
    payload = {
        "chapter_id": request.chapter_id,
        "source": request.source,
        "source_path": relative.as_posix(),
        "sha256": sha256(raw).hexdigest(),
        "start_character": start,
        "end_character": start + len(excerpt),
        "truncated": start + len(excerpt) < len(text),
        "content": excerpt,
    }
    snapshot_path = _audit_relative(context, "chapter-evidence")
    return ToolExecutionPlan(
        content=payload,
        files={snapshot_path: json_document(payload)},
        artifact_paths=[snapshot_path],
        allowed_actions=["read_chapter_evidence", "submit_candidate"],
    )


def _request_user_decision(
    context: ToolExecutionContext,
    arguments: BaseModel,
) -> ToolExecutionPlan:
    request = _typed(arguments, RequestUserDecisionInput)
    checkpoint_id = f"user-decision:{context.activation_id}:{_call_token(context)}"
    relative = activation_relative(context.identity, context.activation_id) / "wait.json"
    payload = {
        "schema_version": 1,
        "checkpoint_id": checkpoint_id,
        "candidate_run_id": context.candidate_run_id,
        **request.model_dump(mode="json"),
    }
    return ToolExecutionPlan(
        content={
            "checkpoint_id": checkpoint_id,
            "question": request.question,
            "suggestion_count": len(request.suggestions),
            "summary": "Waiting for one user decision.",
        },
        files={relative.as_posix(): json_document(payload)},
        checkpoint_id=checkpoint_id,
        artifact_paths=[relative.as_posix()],
        allowed_actions=["resume_after_user_decision"],
    )


def _report_blocker(
    context: ToolExecutionContext,
    arguments: BaseModel,
) -> ToolExecutionPlan:
    request = _typed(arguments, ReportBlockerInput)
    checkpoint_id = f"blocker:{context.activation_id}:{_call_token(context)}"
    relative = activation_relative(context.identity, context.activation_id) / "blocker.json"
    payload = {
        "schema_version": 1,
        "checkpoint_id": checkpoint_id,
        "candidate_run_id": context.candidate_run_id,
        "routing_status": "proposal_only",
        **request.model_dump(mode="json"),
    }
    return ToolExecutionPlan(
        content={
            "checkpoint_id": checkpoint_id,
            "kind": request.kind,
            "routing_status": "proposal_only",
            "summary": request.summary,
        },
        files={relative.as_posix(): json_document(payload)},
        checkpoint_id=checkpoint_id,
        artifact_paths=[relative.as_posix()],
        allowed_actions=["await_harness_routing"],
    )


def _pack_sources(project_path: Path, pack: ContextPackName) -> list[Path]:
    metadata = read_project_metadata(project_path)
    book = [Path("book/settings.md"), Path("book/outline.md")]
    canon = [
        Path("canon/characters.json"),
        Path("canon/relationships.json"),
        Path("canon/world_facts.json"),
        Path("canon/foreshadowing.json"),
    ]
    arc: list[Path] = []
    if metadata.active_arc_id is not None:
        arc = [
            Path("arcs") / metadata.active_arc_id / "plan.md",
            Path("arcs") / metadata.active_arc_id / "state.json",
        ]
    committed = sorted(
        path.relative_to(project_path)
        for path in (project_path / "chapters").glob("*/final.md")
        if path.is_file()
    )
    if pack == "book_discussion":
        return [Path("book/setup.json")]
    if pack == "book_direction":
        return book
    if pack == "story_arc":
        return [*book, *arc]
    if pack in {"chapter_plan", "chapter_draft"}:
        return [*book, *arc, *canon, *committed]
    if pack == "committed_canon":
        return canon
    return committed


def _excluded_context_sources(pack: ContextPackName) -> list[str]:
    excluded = [
        "chapters/*/draft.md",
        "chapters/*/observations.json",
        "chapters/*/candidate_state_patch.json",
        "other-agent uncommitted candidates",
    ]
    if pack == "book_discussion":
        excluded.append("future story arc and chapter plans")
    return excluded


def _audit_relative(context: ToolExecutionContext, prefix: str) -> str:
    filename = f"{prefix}-{_call_token(context)}.json"
    return (
        activation_relative(context.identity, context.activation_id)
        / "context"
        / filename
    ).as_posix()


def _call_token(context: ToolExecutionContext) -> str:
    return sha256(context.tool_call_id.encode("utf-8")).hexdigest()[:12]


def _typed[T: BaseModel](value: BaseModel, expected: type[T]) -> T:
    if not isinstance(value, expected):
        raise TypeError(f"Expected {expected.__name__}, got {type(value).__name__}.")
    return value
