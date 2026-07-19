import json
import re
from hashlib import sha256
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.harness.agents.models import AgentRole
from app.harness.agents.evidence_matching import resolve_semantic_evidence_quote
from app.harness.agents.persistence import activation_relative, json_document
from app.harness.agents.registry import (
    ToolExecutionContext,
    ToolExecutionPlan,
    ToolHandlerError,
    ToolRegistry,
    ToolSpec,
)
from app.harness.agents.semantic_boundary import semantic_model_value
from app.storage.projects import read_project_metadata
from app.storage.json_files import read_json


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


class ReportBlockerInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["local", "needs_user", "cross_loop"]
    summary: str = Field(min_length=1, max_length=4_000)
    semantic_evidence: list[str] = Field(default_factory=list, max_length=50)
    upper_scope: Literal["book_contract", "story_arc_contract"] | None = None
    contract_concern: str | None = Field(default=None, max_length=1_000)
    evidence_hint: str | None = Field(default=None, max_length=4_000)
    impossibility_reason: str | None = Field(default=None, max_length=4_000)


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
            name="request_user_decision",
            version=1,
            description=(
                "Pause for one concrete user decision with two or three semantic suggestions."
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
    max_characters = 30_000
    if context.identity.role not in PACK_ROLES[request.pack]:
        raise ToolHandlerError(
            "context_pack_not_authorized",
            f"Context pack {request.pack} is not allowed for {context.identity.role}.",
            recoverable=True,
            allowed_actions=["choose_authorized_context_pack"],
        )
    sources = _pack_sources(context.project_path, request.pack)
    resolved, remaining = [], max_characters
    for relative in sources:
        path = context.project_path / relative
        if not path.is_file():
            continue
        raw = path.read_bytes()
        text = raw.decode("utf-8-sig")
        semantic_text = _semantic_source_text(path, text)
        excerpt = semantic_text[:remaining]
        resolved.append(
            {
                "source": relative.as_posix(),
                "sha256": sha256(raw).hexdigest(),
                "raw_characters": len(text),
                "characters": len(semantic_text),
                "included_characters": len(excerpt),
                "truncated": len(excerpt) < len(semantic_text),
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
        "max_characters": max_characters,
        "sources": resolved,
        "excluded": _excluded_context_sources(request.pack),
    }
    snapshot_path = _audit_relative(context, "context")
    public_snapshot = {
        "pack": request.pack,
        "sources": [
            {
                "characters": item["characters"],
                "included_characters": item["included_characters"],
                "truncated": item["truncated"],
                "content": item["content"],
            }
            for item in resolved
        ],
    }
    return ToolExecutionPlan(
        content=public_snapshot,
        files={snapshot_path: json_document(snapshot)},
        artifact_paths=[snapshot_path],
        allowed_actions=["request_another_context_pack", "submit_candidate"],
    )


def _request_user_decision(
    context: ToolExecutionContext,
    arguments: BaseModel,
) -> ToolExecutionPlan:
    request = _typed(arguments, RequestUserDecisionInput)
    question = _normalize_question(request.question)
    checkpoint_id = f"user-decision:{context.activation_id}:{_call_token(context)}"
    relative = activation_relative(context.identity, context.activation_id) / "wait.json"
    payload = {
        "schema_version": 1,
        "checkpoint_id": checkpoint_id,
        "candidate_run_id": context.candidate_run_id,
        **request.model_dump(mode="json"),
        "question": question,
    }
    return ToolExecutionPlan(
        content={
            "checkpoint_id": checkpoint_id,
            "question": question,
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
    bound = _bind_blocker_control(context, request)
    payload = {
        "schema_version": 1,
        "checkpoint_id": checkpoint_id,
        "candidate_run_id": context.candidate_run_id,
        "routing_status": "proposal_only",
        **bound,
    }
    return ToolExecutionPlan(
        content={
            "checkpoint_id": checkpoint_id,
            "kind": bound["kind"],
            "routing_status": "proposal_only",
            "summary": request.summary,
        },
        files={relative.as_posix(): json_document(payload)},
        checkpoint_id=checkpoint_id,
        artifact_paths=[relative.as_posix()],
        allowed_actions=["await_harness_routing"],
    )


def _normalize_question(question: str) -> str:
    normalized = re.sub(r"[?？]+", "，", question.strip()).rstrip("，,。!！ ")
    return normalized + "？"


def _bind_blocker_control(
    context: ToolExecutionContext,
    request: ReportBlockerInput,
) -> dict[str, object]:
    base: dict[str, object] = {
        "kind": request.kind,
        "summary": request.summary,
        "evidence": request.semantic_evidence,
        "target_owner": None,
        "contract_field": None,
        "contract_revision": None,
        "committed_evidence_locator": None,
        "impossibility_reason": None,
    }
    if request.kind != "cross_loop":
        return base
    if not all(
        (
            request.upper_scope,
            request.contract_concern,
            request.evidence_hint,
            request.impossibility_reason,
        )
    ):
        raise ToolHandlerError(
            "cross_loop_semantics_incomplete",
            "Cross-Loop escalation requires an upper scope, concern, evidence meaning, and reason.",
            recoverable=True,
            allowed_actions=["retry:report_blocker"],
        )
    metadata = read_project_metadata(context.project_path)
    if request.upper_scope == "story_arc_contract":
        if context.identity.role != "chapter" or metadata.active_arc_id is None:
            raise ToolHandlerError(
                "cross_loop_owner_unavailable",
                "Only a Chapter may semantically escalate to its active Story Arc.",
                recoverable=False,
            )
        owner = "story_arc"
        state = read_json(
            context.project_path / "arcs" / metadata.active_arc_id / "state.json",
            default=None,
        )
        revision = state.get("version") if isinstance(state, dict) else None
        locator = f"arcs/{metadata.active_arc_id}/plan.md"
        candidates = [locator]
    else:
        if context.identity.role == "book":
            raise ToolHandlerError(
                "cross_loop_owner_unavailable",
                "Book cannot escalate to itself.",
                recoverable=False,
            )
        owner = "book"
        state = read_json(context.project_path / "book" / "state.json", default=None)
        revision = state.get("version") if isinstance(state, dict) else None
        candidates = [
            "book/direction.md",
            "book/settings.md",
            "book/outline.md",
            "book/constraints.json",
            "book/state.json",
        ]
        locator = _bind_committed_evidence_locator(
            context.project_path,
            candidates,
            [
                request.evidence_hint or "",
                request.contract_concern or "",
                request.impossibility_reason or "",
            ],
        )
    if not isinstance(revision, int) or isinstance(revision, bool):
        raise ToolHandlerError(
            "cross_loop_contract_unavailable",
            "Harness cannot read the current upper-contract revision.",
            recoverable=False,
        )
    if request.upper_scope == "story_arc_contract":
        locator = _bind_committed_evidence_locator(
            context.project_path,
            candidates,
            [
                request.evidence_hint or "",
                request.contract_concern or "",
                request.impossibility_reason or "",
            ],
        )
    return {
        **base,
        "target_owner": owner,
        "contract_field": request.contract_concern,
        "contract_revision": revision,
        "committed_evidence_locator": locator,
        "impossibility_reason": request.impossibility_reason,
    }


def _bind_committed_evidence_locator(
    project_path: Path,
    candidates: list[str],
    hints: list[str],
) -> str:
    matched: list[str] = []
    for locator in candidates:
        path = project_path / locator
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8-sig")
        if resolve_semantic_evidence_quote(text, hints) is not None:
            matched.append(locator)
    if len(matched) != 1:
        raise ToolHandlerError(
            "cross_loop_evidence_unresolved",
            "Harness could not bind the semantic blocker to one committed evidence source.",
            recoverable=True,
            content={"candidate_source_count": len(candidates)},
            allowed_actions=["retry:report_blocker"],
        )
    return matched[0]


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


def _semantic_source_text(path: Path, text: str) -> str:
    if path.suffix.casefold() == ".json":
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return "[invalid structured context omitted]"
        return json.dumps(
            semantic_model_value(payload),
            ensure_ascii=False,
            indent=2,
        )
    if path.suffix.casefold() == ".jsonl":
        semantic_lines: list[str] = []
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            semantic_lines.append(
                json.dumps(semantic_model_value(payload), ensure_ascii=False)
            )
        return "\n".join(semantic_lines)
    return text


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
