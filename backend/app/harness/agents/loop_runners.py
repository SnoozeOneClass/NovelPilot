import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import ValidationError

from app.harness.agents.domain_tools import (
    BookDirectionCandidateInput,
    BookDiscussionUpdateInput,
    StoryArcCandidateInput,
    SubmitChapterCandidateInput,
    build_default_tool_registry,
)
from app.harness.agents.evaluator import evaluate_candidate, evaluation_input_fingerprint
from app.harness.agents.models import (
    AgentIdentity,
    AgentRunResult,
    EvaluationEvidence,
    EvaluationInput,
    EvaluationIssue,
    EvaluationRecord,
    EvaluationResult,
    ToolExecutionResult,
)
from app.harness.agents.policy import ResolvedAgentPolicy
from app.harness.agents.persistence import (
    activation_relative,
    read_agent_state,
    write_activation_document,
)
from app.harness.agents.runtime import AgentActivation, AgentRuntime
from app.harness.loops.book import (
    BookDirectionSynthesis,
    BookDiscussionTurnResult,
    DiscussionContextAssembly,
)
from app.llm.gateway import ChatChunk, ChatMessage
from app.llm.redaction import redact_profile_secrets
from app.llm.retry import is_retryable_provider_error
from app.schemas.patches import CandidateStatePatch
from app.schemas.projects import ProjectMetadata
from app.schemas.profiles import LlmProfile
from app.schemas.arcs import StoryArcPlanProposal
from app.schemas.artifacts import ChapterVerification, VerificationSignal
from app.schemas.setup import (
    BookDirectionReview,
    BookDirectionReviewIssue,
    SetupStateDocument,
    missing_confirmed_decisions,
)
from app.storage.json_files import read_json
from app.storage.profiles import require_harness_capabilities


AgentEventCallback = Callable[[dict[str, object]], None]

STORY_ARC_AGENT_TOOLS = (
    "get_loop_context",
    "read_chapter_evidence",
    "submit_story_arc_candidate",
    "report_blocker",
)

CHAPTER_AGENT_TOOLS = (
    "get_loop_context",
    "read_chapter_evidence",
    "plan_chapter_candidate",
    "write_chapter_draft",
    "edit_chapter_draft",
    "inspect_chapter_consistency",
    "submit_chapter_candidate",
    "report_blocker",
)


class AgentCandidateError(RuntimeError):
    pass


class AgentControlCheckpoint(RuntimeError):
    def __init__(
        self,
        run_result: AgentRunResult,
        artifact_path: str,
        payload: dict[str, object],
    ) -> None:
        super().__init__(f"Agent stopped at control checkpoint: {run_result.outcome}")
        self.run_result = run_result
        self.artifact_path = artifact_path
        self.payload = payload


@dataclass(frozen=True)
class StoryArcAgentResult:
    proposal: StoryArcPlanProposal
    evaluation: EvaluationRecord
    run_result: AgentRunResult
    candidate_artifact_path: str


@dataclass(frozen=True)
class ChapterAgentResult:
    submission: SubmitChapterCandidateInput
    evaluation: EvaluationRecord
    verification: ChapterVerification
    run_result: AgentRunResult
    candidate_root: str


@dataclass(frozen=True)
class ChapterPatchEvidenceRepairResult:
    patch: CandidateStatePatch
    run_result: AgentRunResult
    candidate_artifact_path: str


def run_book_discussion_agent(
    project_path: Path,
    metadata: ProjectMetadata,
    state: SetupStateDocument,
    user_message: str,
    assembly: DiscussionContextAssembly,
    policy: ResolvedAgentPolicy,
    *,
    on_event: AgentEventCallback | None = None,
    on_text_delta: Callable[[ChatChunk], None] | None = None,
    on_tool_event: Callable[[ChatChunk], None] | None = None,
    runtime: AgentRuntime | None = None,
) -> BookDiscussionTurnResult:
    _require_policy_capabilities(policy)
    identity = AgentIdentity(project_id=metadata.project_id, role="book")
    candidate_run_id = f"book-discussion-{state.revision + 1}-{uuid4().hex[:8]}"
    runner = runtime or AgentRuntime(build_default_tool_registry())
    result = runner.run(
        AgentActivation(
            project_path=project_path,
            identity=identity,
            candidate_run_id=candidate_run_id,
            phase="discussion",
            expected_revision=state.revision,
            allowed_tools=(
                "get_loop_context",
                "submit_book_discussion_update",
                "report_blocker",
            ),
            system_prompt=_book_discussion_agent_prompt(),
            messages=(
                ChatMessage(
                    role="user",
                    content=(
                        f"Harness expected_revision={state.revision}.\n\n{assembly.prompt}"
                    ),
                ),
            ),
            policy=policy,
            initial_checkpoint_id=f"book-discussion:{state.revision}",
            on_event=on_event,
            on_text_delta=on_text_delta,
            on_tool_event=on_tool_event,
        )
    )
    payload = _terminal_payload(project_path, result, "submit_book_discussion_update")
    try:
        candidate = BookDiscussionUpdateInput.model_validate(payload)
    except ValidationError as exc:
        raise AgentCandidateError(
            "Book Agent terminal candidate failed local validation."
        ) from exc
    return BookDiscussionTurnResult(
        reply=candidate.reply,
        direction_draft=candidate.direction_draft,
        discussion_summary=candidate.discussion_summary,
        confirmed_decisions=candidate.confirmed_decisions,
        superseded_decisions=candidate.superseded_decisions,
        unresolved_questions=candidate.unresolved_questions,
        assumptions=candidate.assumptions,
        contradictions=candidate.contradictions,
        selected_title=candidate.selected_title,
        question=candidate.question,
        suggestions=candidate.suggestions,
        readiness=candidate.readiness,
        model_snapshot=result.model_snapshot or policy.profile.model,
        provider_snapshot=result.provider_snapshot or policy.profile.protocol,
        usage=result.usage,
    )


def run_book_direction_agent(
    project_path: Path,
    metadata: ProjectMetadata,
    state: SetupStateDocument,
    policy: ResolvedAgentPolicy,
    *,
    on_event: AgentEventCallback | None = None,
    on_text_delta: Callable[[ChatChunk], None] | None = None,
    on_tool_event: Callable[[ChatChunk], None] | None = None,
    runtime: AgentRuntime | None = None,
) -> tuple[BookDirectionSynthesis, EvaluationRecord, BookDirectionReview]:
    candidate_revision = state.candidate_revision_counter + 1
    return _run_book_direction_candidate_agent(
        project_path,
        metadata,
        state,
        policy,
        candidate_revision=candidate_revision,
        candidate_run_id=f"book-direction-{candidate_revision}-{uuid4().hex[:8]}",
        system_prompt=_book_direction_agent_prompt(
            state_revision=state.revision,
            candidate_revision=candidate_revision,
        ),
        input_payload={
            "state_revision": state.revision,
            "candidate_revision": candidate_revision,
            "direction_draft": state.direction_draft,
            "discussion_summary": state.discussion_summary,
            "confirmed_decisions": state.confirmed_decisions,
            "unresolved_questions": state.unresolved_questions,
            "assumptions": state.assumptions,
            "contradictions": state.contradictions,
            "selected_title": state.selected_title,
            "previous_blocked_review": (
                state.candidate.review.model_dump(mode="json")
                if state.candidate is not None and not state.candidate.approval_allowed
                else None
            ),
        },
        on_event=on_event,
        on_text_delta=on_text_delta,
        on_tool_event=on_tool_event,
        runtime=runtime,
    )


def run_book_revision_agent(
    project_path: Path,
    metadata: ProjectMetadata,
    state: SetupStateDocument,
    policy: ResolvedAgentPolicy,
    *,
    target_direction_version: int,
    revision_request: dict[str, object],
    candidate_run_id: str | None = None,
    on_event: AgentEventCallback | None = None,
    on_text_delta: Callable[[ChatChunk], None] | None = None,
    on_tool_event: Callable[[ChatChunk], None] | None = None,
    runtime: AgentRuntime | None = None,
) -> tuple[BookDirectionSynthesis, EvaluationRecord, BookDirectionReview]:
    resolved_candidate_run_id = candidate_run_id or (
        f"book-revision-{target_direction_version}-{uuid4().hex[:8]}"
    )
    identity = AgentIdentity(project_id=metadata.project_id, role="book")
    agent_state = read_agent_state(project_path, identity)
    if (
        agent_state.lifecycle == "completed"
        and agent_state.candidate_run_id == resolved_candidate_run_id
        and agent_state.activation_id is not None
    ):
        candidate_path = (
            activation_relative(identity, agent_state.activation_id)
            / "c"
            / "book-direction.json"
        ).as_posix()
        if (project_path / candidate_path).is_file():
            recovered = AgentRunResult(
                outcome="candidate",
                identity=identity,
                candidate_run_id=resolved_candidate_run_id,
                activation_id=agent_state.activation_id,
                turns_used=(
                    agent_state.budgets.used_turns
                    if agent_state.budgets is not None
                    else 0
                ),
                terminal_result=ToolExecutionResult(
                    status="ok",
                    tool_name="submit_book_direction_candidate",
                    tool_call_id=f"recovered:{agent_state.activation_id}",
                    content={"recovered": True},
                    message="Recovered the durable Book revision candidate.",
                    checkpoint_id=agent_state.last_checkpoint_id,
                    terminal=True,
                    artifact_paths=[candidate_path],
                    replayed=True,
                ),
                model_snapshot=policy.profile.model,
                provider_snapshot=policy.profile.protocol,
            )
            return _book_direction_attempt(
                project_path,
                state,
                policy,
                identity,
                target_direction_version,
                recovered,
                on_event,
            )
    return _run_book_direction_candidate_agent(
        project_path,
        metadata,
        state,
        policy,
        candidate_revision=target_direction_version,
        candidate_run_id=resolved_candidate_run_id,
        system_prompt=_book_revision_agent_prompt(
            state_revision=state.revision,
            candidate_revision=target_direction_version,
        ),
        input_payload={
            "state_revision": state.revision,
            "candidate_revision": target_direction_version,
            "approved_direction": state.direction_draft,
            "confirmed_decisions": state.confirmed_decisions,
            "must_preserve": state.confirmed_decisions,
            "revision_request": revision_request,
        },
        on_event=on_event,
        on_text_delta=on_text_delta,
        on_tool_event=on_tool_event,
        runtime=runtime,
    )


def _run_book_direction_candidate_agent(
    project_path: Path,
    metadata: ProjectMetadata,
    state: SetupStateDocument,
    policy: ResolvedAgentPolicy,
    *,
    candidate_revision: int,
    candidate_run_id: str,
    system_prompt: str,
    input_payload: dict[str, object],
    on_event: AgentEventCallback | None,
    on_text_delta: Callable[[ChatChunk], None] | None,
    on_tool_event: Callable[[ChatChunk], None] | None,
    runtime: AgentRuntime | None,
) -> tuple[BookDirectionSynthesis, EvaluationRecord, BookDirectionReview]:
    _require_policy_capabilities(policy)
    identity = AgentIdentity(project_id=metadata.project_id, role="book")
    runner = runtime or AgentRuntime(build_default_tool_registry())
    activation = AgentActivation(
        project_path=project_path,
        identity=identity,
        candidate_run_id=candidate_run_id,
        phase="direction",
        expected_revision=state.revision,
        allowed_tools=(
            "get_loop_context",
            "submit_book_direction_candidate",
            "request_user_decision",
            "report_blocker",
        ),
        system_prompt=system_prompt,
        messages=(
            ChatMessage(
                role="user",
                content=json.dumps(input_payload, ensure_ascii=False),
            ),
        ),
        policy=policy,
        initial_checkpoint_id=f"book-direction:{state.candidate_revision_counter}",
        on_event=on_event,
        on_text_delta=on_text_delta,
        on_tool_event=on_tool_event,
    )
    result = runner.run(activation)
    synthesis, evaluation, review = _book_direction_attempt(
        project_path,
        state,
        policy,
        identity,
        candidate_revision,
        result,
        on_event,
    )
    while evaluation.result.outcome == "local_repair":
        if not runner.request_semantic_revision(
            activation,
            evaluation_id=evaluation.evaluation_id,
            candidate_artifact_id=evaluation.candidate_artifact_id,
        ):
            break
        activation = replace(
            activation,
            messages=(
                ChatMessage(
                    role="user",
                    content=_semantic_repair_prompt(
                        "Book Direction",
                        {
                            "expected_revision": state.revision,
                            "candidate_revision": candidate_revision,
                            **_book_synthesis_payload(synthesis),
                        },
                        evaluation,
                    ),
                ),
            ),
        )
        result = runner.run(activation)
        synthesis, evaluation, review = _book_direction_attempt(
            project_path,
            state,
            policy,
            identity,
            candidate_revision,
            result,
            on_event,
        )
    if evaluation.result.outcome == "needs_user":
        decision_activation = replace(
            activation,
            system_prompt=_book_evaluation_user_decision_prompt(),
            allowed_tools=("request_user_decision",),
            messages=(
                ChatMessage(
                    role="user",
                    content=json.dumps(
                        {
                            "candidate_kind": "Book Direction",
                            "selected_title": state.selected_title,
                            "evaluation_id": evaluation.evaluation_id,
                            "evaluation": evaluation.result.model_dump(mode="json"),
                        },
                        ensure_ascii=False,
                    ),
                ),
            ),
        )
        decision_result = runner.run(decision_activation)
        _terminal_payload(project_path, decision_result, "request_user_decision")
        raise AgentCandidateError("Book Agent did not stop at the user-decision checkpoint.")
    return synthesis, evaluation, review


def _book_direction_attempt(
    project_path: Path,
    state: SetupStateDocument,
    policy: ResolvedAgentPolicy,
    identity: AgentIdentity,
    candidate_revision: int,
    result: AgentRunResult,
    on_event: AgentEventCallback | None,
) -> tuple[BookDirectionSynthesis, EvaluationRecord, BookDirectionReview]:
    payload = _terminal_payload(project_path, result, "submit_book_direction_candidate")
    try:
        candidate = BookDirectionCandidateInput.model_validate(payload)
    except ValidationError as exc:
        raise AgentCandidateError(
            "Book Direction Agent terminal candidate failed local validation."
        ) from exc
    synthesis = BookDirectionSynthesis(
        direction_markdown=candidate.direction_markdown,
        constraints=candidate.constraints,
        confirmed_decision_coverage=candidate.confirmed_decision_coverage,
        recommended_titles=candidate.recommended_titles,
        rolling_plan_markdown=candidate.rolling_plan_markdown,
        model_snapshot=result.model_snapshot or policy.profile.model,
        provider_snapshot=result.provider_snapshot or policy.profile.protocol,
        usage=result.usage,
    )
    candidate_path = _terminal_artifact(result)
    evaluation_input = _book_direction_evaluation_input(
        state,
        synthesis,
        candidate_path=candidate_path,
        candidate_revision=candidate_revision,
        identity=identity,
        candidate_run_id=result.candidate_run_id,
    )
    evaluation_relative = (
        activation_relative(identity, result.activation_id) / "evaluation.json"
    ).as_posix()
    expected_fingerprint = evaluation_input_fingerprint(
        policy.evaluator_profile,
        evaluation_input,
    )
    existing_payload = read_json(project_path / evaluation_relative, default=None)
    evaluation: EvaluationRecord | None = None
    if existing_payload is not None:
        existing = EvaluationRecord.model_validate(existing_payload)
        if (
            existing.candidate_run_id == result.candidate_run_id
            and existing.candidate_artifact_id == candidate_path
            and existing.candidate_revision == candidate_revision
            and existing.input_fingerprint == expected_fingerprint
        ):
            evaluation = existing
    if evaluation is None:
        evaluation, review = evaluate_book_direction_candidate(
            state,
            synthesis,
            policy,
            identity=identity,
            candidate_path=candidate_path,
            candidate_revision=candidate_revision,
            candidate_run_id=result.candidate_run_id,
            on_event=on_event,
        )
        evaluation_path = _persist_activation_evaluation(project_path, result, evaluation)
    else:
        review = _book_review_from_evaluation(evaluation)
        evaluation_path = evaluation_relative
    _emit_evaluation_completed(on_event, evaluation, evaluation_path)
    return synthesis, evaluation, review


def evaluate_book_direction_candidate(
    state: SetupStateDocument,
    synthesis: BookDirectionSynthesis,
    policy: ResolvedAgentPolicy,
    *,
    identity: AgentIdentity,
    candidate_path: str,
    candidate_revision: int,
    candidate_run_id: str | None = None,
    on_event: AgentEventCallback | None = None,
) -> tuple[EvaluationRecord, BookDirectionReview]:
    _require_policy_capabilities(policy)
    evaluation_input = _book_direction_evaluation_input(
        state,
        synthesis,
        candidate_path=candidate_path,
        candidate_revision=candidate_revision,
        identity=identity,
        candidate_run_id=candidate_run_id,
    )
    evaluation = _run_evaluator(
        policy.evaluator_profile,
        evaluation_input,
        on_event,
        transport_retry_limit=policy.transport_retry_limit,
    )
    evaluation = apply_book_direction_prechecks(state, synthesis, evaluation)
    return evaluation, _book_review_from_evaluation(evaluation)


def run_story_arc_agent(
    project_path: Path,
    metadata: ProjectMetadata,
    policy: ResolvedAgentPolicy,
    *,
    arc_id: str,
    intent: str,
    expected_revision: int,
    instruction: str,
    candidate_run_id: str | None = None,
    on_event: AgentEventCallback | None = None,
    on_text_delta: Callable[[ChatChunk], None] | None = None,
    on_tool_event: Callable[[ChatChunk], None] | None = None,
    runtime: AgentRuntime | None = None,
) -> StoryArcAgentResult:
    _require_policy_capabilities(policy)
    if intent not in {"create", "revise"}:
        raise ValueError(f"Unsupported Story Arc intent: {intent}")
    identity = AgentIdentity(
        project_id=metadata.project_id,
        role="story_arc",
        scope_id=arc_id,
    )
    resolved_candidate_run_id = candidate_run_id or (
        f"story-arc-{arc_id}-{expected_revision + 1}-{uuid4().hex[:8]}"
    )
    runner = runtime or AgentRuntime(build_default_tool_registry())
    activation = AgentActivation(
        project_path=project_path,
        identity=identity,
        candidate_run_id=resolved_candidate_run_id,
        phase="planning" if intent == "create" else "revision",
        expected_revision=expected_revision,
        allowed_tools=STORY_ARC_AGENT_TOOLS,
        system_prompt=_story_arc_agent_prompt(
            arc_id=arc_id,
            intent=intent,
            expected_revision=expected_revision,
        ),
        messages=(ChatMessage(role="user", content=instruction),),
        policy=policy,
        initial_checkpoint_id=f"story-arc:{arc_id}:{expected_revision}",
        on_event=on_event,
        on_text_delta=on_text_delta,
        on_tool_event=on_tool_event,
    )
    result = runner.run(activation)
    attempt = _story_arc_attempt(
        project_path,
        policy,
        identity,
        arc_id,
        intent,
        expected_revision,
        result,
        on_event,
    )
    while attempt.evaluation.result.outcome == "local_repair":
        if not runner.request_semantic_revision(
            activation,
            evaluation_id=attempt.evaluation.evaluation_id,
            candidate_artifact_id=attempt.evaluation.candidate_artifact_id,
        ):
            break
        activation = replace(
            activation,
            messages=(
                ChatMessage(
                    role="user",
                    content=_semantic_repair_prompt(
                        "Story Arc",
                        {
                            "arc_id": arc_id,
                            "intent": intent,
                            "expected_revision": expected_revision,
                            **attempt.proposal.model_dump(mode="json"),
                        },
                        attempt.evaluation,
                    ),
                ),
            ),
        )
        result = runner.run(activation)
        attempt = _story_arc_attempt(
            project_path,
            policy,
            identity,
            arc_id,
            intent,
            expected_revision,
            result,
            on_event,
        )
    return attempt


def recover_completed_story_arc_agent(
    project_path: Path,
    metadata: ProjectMetadata,
    policy: ResolvedAgentPolicy,
    *,
    arc_id: str,
    intent: str,
    expected_revision: int,
    on_event: AgentEventCallback | None = None,
) -> StoryArcAgentResult | None:
    identity = AgentIdentity(
        project_id=metadata.project_id,
        role="story_arc",
        scope_id=arc_id,
    )
    state = read_agent_state(project_path, identity)
    if (
        state.lifecycle != "completed"
        or state.candidate_run_id is None
        or state.activation_id is None
    ):
        return None
    candidate_path = (
        activation_relative(identity, state.activation_id) / "c" / "story-arc.json"
    ).as_posix()
    if not (project_path / candidate_path).is_file():
        return None
    candidate_payload = read_json(project_path / candidate_path, default={})
    if (
        not isinstance(candidate_payload, dict)
        or candidate_payload.get("intent") != intent
        or candidate_payload.get("expected_revision") != expected_revision
    ):
        return None
    result = AgentRunResult(
        outcome="candidate",
        identity=identity,
        candidate_run_id=state.candidate_run_id,
        activation_id=state.activation_id,
        turns_used=state.budgets.used_turns if state.budgets is not None else 0,
        terminal_result=ToolExecutionResult(
            status="ok",
            tool_name="submit_story_arc_candidate",
            tool_call_id=f"recovered:{state.activation_id}",
            content={"recovered": True},
            message="Recovered the durable Story Arc candidate.",
            checkpoint_id=state.last_checkpoint_id,
            terminal=True,
            artifact_paths=[candidate_path],
            replayed=True,
        ),
        model_snapshot=policy.profile.model,
        provider_snapshot=policy.profile.protocol,
    )
    return _story_arc_attempt(
        project_path,
        policy,
        identity,
        arc_id,
        intent,
        expected_revision,
        result,
        on_event,
    )


def _story_arc_attempt(
    project_path: Path,
    policy: ResolvedAgentPolicy,
    identity: AgentIdentity,
    arc_id: str,
    intent: str,
    expected_revision: int,
    result: AgentRunResult,
    on_event: AgentEventCallback | None,
) -> StoryArcAgentResult:
    payload = _terminal_payload(project_path, result, "submit_story_arc_candidate")
    try:
        candidate = StoryArcCandidateInput.model_validate(payload)
    except ValidationError as exc:
        raise AgentCandidateError("Story Arc candidate failed local validation.") from exc
    if candidate.intent != intent:
        raise AgentCandidateError(
            f"Story Arc candidate intent {candidate.intent!r} does not match {intent!r}."
        )
    candidate_path = _terminal_artifact(result)
    evaluation_input = EvaluationInput(
        identity=identity,
        candidate_run_id=result.candidate_run_id,
        checkpoint="story_arc_candidate",
        candidate_artifact_id=candidate_path,
        candidate_revision=expected_revision + 1,
        candidate_content=json.dumps(
            candidate.model_dump(mode="json"),
            ensure_ascii=False,
        ),
        evidence=_story_arc_evidence(project_path, arc_id),
        deterministic_prechecks={
            "target_chapter_count": candidate.target_chapter_count,
            "has_plan": bool(candidate.plan_markdown.strip()),
            "ownership_matches": candidate.arc_id == arc_id,
        },
        rubric_version="story-arc-v1",
    )
    evaluation_relative = (
        activation_relative(identity, result.activation_id) / "evaluation.json"
    ).as_posix()
    expected_fingerprint = evaluation_input_fingerprint(
        policy.evaluator_profile,
        evaluation_input,
    )
    existing_payload = read_json(project_path / evaluation_relative, default=None)
    evaluation: EvaluationRecord | None = None
    if existing_payload is not None:
        existing = EvaluationRecord.model_validate(existing_payload)
        if (
            existing.candidate_run_id == result.candidate_run_id
            and existing.candidate_artifact_id == candidate_path
            and existing.candidate_revision == expected_revision + 1
            and existing.input_fingerprint == expected_fingerprint
        ):
            evaluation = existing
    if evaluation is None:
        evaluation = _run_evaluator(
            policy.evaluator_profile,
            evaluation_input,
            on_event,
            transport_retry_limit=policy.transport_retry_limit,
        )
        evaluation_path = _persist_activation_evaluation(project_path, result, evaluation)
    else:
        evaluation_path = evaluation_relative
    _emit_evaluation_completed(on_event, evaluation, evaluation_path)
    return StoryArcAgentResult(
        proposal=StoryArcPlanProposal(
            plan_markdown=candidate.plan_markdown,
            target_chapter_count=candidate.target_chapter_count,
        ),
        evaluation=evaluation,
        run_result=result,
        candidate_artifact_path=candidate_path,
    )


def run_chapter_agent(
    project_path: Path,
    metadata: ProjectMetadata,
    policy: ResolvedAgentPolicy,
    *,
    chapter_id: str,
    expected_revision: int,
    instruction: str,
    candidate_run_id: str | None = None,
    on_event: AgentEventCallback | None = None,
    on_text_delta: Callable[[ChatChunk], None] | None = None,
    on_tool_event: Callable[[ChatChunk], None] | None = None,
    runtime: AgentRuntime | None = None,
) -> ChapterAgentResult:
    _require_policy_capabilities(policy)
    identity = AgentIdentity(
        project_id=metadata.project_id,
        role="chapter",
        scope_id=chapter_id,
    )
    candidate_run_id = candidate_run_id or (
        f"chapter-{chapter_id}-{expected_revision + 1}-{uuid4().hex[:8]}"
    )
    runner = runtime or AgentRuntime(build_default_tool_registry())
    activation = AgentActivation(
        project_path=project_path,
        identity=identity,
        candidate_run_id=candidate_run_id,
        phase="chapter",
        expected_revision=expected_revision,
        allowed_tools=CHAPTER_AGENT_TOOLS,
        system_prompt=_chapter_agent_prompt(
            chapter_id=chapter_id,
            expected_revision=expected_revision,
        ),
        messages=(ChatMessage(role="user", content=instruction),),
        policy=policy,
        initial_checkpoint_id=f"chapter:{chapter_id}:{expected_revision}",
        on_event=on_event,
        on_text_delta=on_text_delta,
        on_tool_event=on_tool_event,
    )
    result = runner.run(activation)
    attempt = _chapter_attempt(
        project_path,
        metadata,
        policy,
        identity,
        chapter_id,
        result,
        on_event,
    )
    while attempt.evaluation.result.outcome == "local_repair":
        if not runner.request_semantic_revision(
            activation,
            evaluation_id=attempt.evaluation.evaluation_id,
            candidate_artifact_id=attempt.evaluation.candidate_artifact_id,
        ):
            break
        candidate_root = project_path / attempt.candidate_root
        activation = replace(
            activation,
            messages=(
                ChatMessage(
                    role="user",
                    content=_semantic_repair_prompt(
                        "Chapter",
                        {
                            "submission": attempt.submission.model_dump(mode="json"),
                            "plan": _read_candidate_text(
                                project_path,
                                candidate_root.relative_to(project_path) / "plan.md",
                            ),
                            "draft": _read_candidate_text(
                                project_path,
                                candidate_root.relative_to(project_path) / "draft.md",
                            ),
                        },
                        attempt.evaluation,
                    ),
                ),
            ),
        )
        result = runner.run(activation)
        attempt = _chapter_attempt(
            project_path,
            metadata,
            policy,
            identity,
            chapter_id,
            result,
            on_event,
        )
    return attempt


def recover_completed_chapter_agent(
    project_path: Path,
    metadata: ProjectMetadata,
    policy: ResolvedAgentPolicy,
    *,
    chapter_id: str,
    on_event: AgentEventCallback | None = None,
) -> ChapterAgentResult | None:
    """Resume projection/evaluation without asking the Chapter Agent to write again."""
    identity = AgentIdentity(
        project_id=metadata.project_id,
        role="chapter",
        scope_id=chapter_id,
    )
    state = read_agent_state(project_path, identity)
    if (
        state.lifecycle != "completed"
        or state.candidate_run_id is None
        or state.activation_id is None
    ):
        return None
    manifest_path = (
        activation_relative(identity, state.activation_id) / "c" / "manifest.json"
    ).as_posix()
    if not (project_path / manifest_path).is_file():
        return None
    result = AgentRunResult(
        outcome="candidate",
        identity=identity,
        candidate_run_id=state.candidate_run_id,
        activation_id=state.activation_id,
        turns_used=state.budgets.used_turns if state.budgets is not None else 0,
        terminal_result=ToolExecutionResult(
            status="ok",
            tool_name="submit_chapter_candidate",
            tool_call_id=f"recovered:{state.activation_id}",
            content={"recovered": True},
            message="Recovered the durable Chapter Agent candidate.",
            checkpoint_id=state.last_checkpoint_id,
            terminal=True,
            artifact_paths=[manifest_path],
            replayed=True,
        ),
        model_snapshot=policy.profile.model,
        provider_snapshot=policy.profile.protocol,
    )
    return _chapter_attempt(
        project_path,
        metadata,
        policy,
        identity,
        chapter_id,
        result,
        on_event,
    )


def run_chapter_patch_evidence_repair_agent(
    project_path: Path,
    metadata: ProjectMetadata,
    policy: ResolvedAgentPolicy,
    *,
    chapter_id: str,
    expected_revision: int,
    instruction: str,
    candidate_run_id: str | None = None,
    on_event: AgentEventCallback | None = None,
    on_text_delta: Callable[[ChatChunk], None] | None = None,
    on_tool_event: Callable[[ChatChunk], None] | None = None,
    runtime: AgentRuntime | None = None,
) -> ChapterPatchEvidenceRepairResult:
    _require_policy_capabilities(policy)
    identity = AgentIdentity(
        project_id=metadata.project_id,
        role="chapter",
        scope_id=chapter_id,
    )
    runner = runtime or AgentRuntime(build_default_tool_registry())
    result = runner.run(
        AgentActivation(
            project_path=project_path,
            identity=identity,
            candidate_run_id=candidate_run_id
            or f"chapter-patch-{chapter_id}-{expected_revision + 1}-{uuid4().hex[:8]}",
            phase="state_patch_repair",
            expected_revision=expected_revision,
            allowed_tools=(
                "get_loop_context",
                "read_chapter_evidence",
                "submit_chapter_patch_evidence_repair",
                "report_blocker",
            ),
            system_prompt=_chapter_patch_evidence_repair_prompt(
                chapter_id=chapter_id,
                expected_revision=expected_revision,
            ),
            messages=(ChatMessage(role="user", content=instruction),),
            policy=policy,
            initial_checkpoint_id=f"chapter-patch:{chapter_id}:{expected_revision}",
            on_event=on_event,
            on_text_delta=on_text_delta,
            on_tool_event=on_tool_event,
        )
    )
    payload = _terminal_payload(
        project_path,
        result,
        "submit_chapter_patch_evidence_repair",
    )
    try:
        patch = CandidateStatePatch.model_validate(payload)
    except ValidationError as exc:
        raise AgentCandidateError(
            "Chapter state-patch evidence repair failed local validation."
        ) from exc
    return ChapterPatchEvidenceRepairResult(
        patch=patch,
        run_result=result,
        candidate_artifact_path=_terminal_artifact(result),
    )


def _chapter_attempt(
    project_path: Path,
    metadata: ProjectMetadata,
    policy: ResolvedAgentPolicy,
    identity: AgentIdentity,
    chapter_id: str,
    result: AgentRunResult,
    on_event: AgentEventCallback | None,
) -> ChapterAgentResult:
    payload = _terminal_payload(project_path, result, "submit_chapter_candidate")
    try:
        submission = SubmitChapterCandidateInput.model_validate(
            {
                name: payload.get(name)
                for name in SubmitChapterCandidateInput.model_fields
            }
        )
    except ValidationError as exc:
        raise AgentCandidateError("Chapter candidate failed local validation.") from exc
    manifest_path = _terminal_artifact(result)
    root = Path(manifest_path).parent
    plan = _read_candidate_text(project_path, root / "plan.md")
    draft = _read_candidate_text(project_path, root / "draft.md")
    evaluation_input = EvaluationInput(
        identity=identity,
        candidate_run_id=result.candidate_run_id,
        checkpoint="chapter_candidate",
        candidate_artifact_id=manifest_path,
        candidate_revision=submission.candidate_revision,
        candidate_content=json.dumps(
            {
                "plan": plan,
                "draft": draft,
                "observations": submission.observations.model_dump(mode="json"),
                "state_patch": submission.state_patch.model_dump(mode="json"),
            },
            ensure_ascii=False,
        )[:500_000],
        evidence=_chapter_evidence(project_path, metadata),
        deterministic_prechecks={
            "plan_revision": submission.plan_revision,
            "draft_revision": submission.draft_revision,
            "draft_characters": len(draft),
            "has_observation_source": bool(submission.observations.based_on),
        },
        rubric_version="chapter-candidate-v1",
    )
    evaluation_relative = (
        activation_relative(identity, result.activation_id) / "evaluation.json"
    ).as_posix()
    expected_fingerprint = evaluation_input_fingerprint(
        policy.evaluator_profile,
        evaluation_input,
    )
    existing_payload = read_json(project_path / evaluation_relative, default=None)
    evaluation: EvaluationRecord | None = None
    if existing_payload is not None:
        existing = EvaluationRecord.model_validate(existing_payload)
        if (
            existing.candidate_run_id == result.candidate_run_id
            and existing.candidate_artifact_id == manifest_path
            and existing.candidate_revision == submission.candidate_revision
            and existing.input_fingerprint == expected_fingerprint
        ):
            evaluation = existing
    if evaluation is None:
        evaluation = _run_evaluator(
            policy.evaluator_profile,
            evaluation_input,
            on_event,
            transport_retry_limit=policy.transport_retry_limit,
        )
        evaluation_path = _persist_activation_evaluation(project_path, result, evaluation)
    else:
        evaluation_path = evaluation_relative
    _emit_evaluation_completed(on_event, evaluation, evaluation_path)
    return ChapterAgentResult(
        submission=submission,
        evaluation=evaluation,
        verification=chapter_verification_from_evaluation(chapter_id, evaluation),
        run_result=result,
        candidate_root=root.as_posix(),
    )


def chapter_verification_from_evaluation(
    chapter_id: str,
    evaluation: EvaluationRecord,
) -> ChapterVerification:
    result = evaluation.result
    routing: Literal[
        "commit",
        "revise",
        "pause",
        "escalate_to_arc",
        "escalate_to_book",
    ] = {
        "pass": "commit",
        "local_repair": "revise",
        "needs_user": "pause",
        "cross_loop_escalation": (
            "escalate_to_book"
            if result.upstream_blocker is not None
            and result.upstream_blocker.owner == "book"
            else "escalate_to_arc"
        ),
    }[result.outcome]
    signals = [
        VerificationSignal(
            name=item.name,
            status=("passed" if bool(item.value) else "warning"),
            evidence=item.evidence_locator,
        )
        for item in result.signals
    ]
    signals.extend(
        VerificationSignal(
            name=item.category,
            status="failed" if item.severity == "blocking" else "warning",
            evidence=item.evidence_locator,
        )
        for item in result.issues
    )
    return ChapterVerification(
        chapter_id=chapter_id,
        goal_satisfied=result.contract_satisfied,
        commit_allowed=result.outcome == "pass" and result.contract_satisfied,
        routing_decision=routing,
        signals=signals,
        reasons=[item.explanation for item in result.issues]
        or ([] if result.outcome == "pass" else [result.summary]),
    )


def apply_book_direction_prechecks(
    state: SetupStateDocument,
    synthesis: BookDirectionSynthesis,
    evaluation: EvaluationRecord,
) -> EvaluationRecord:
    missing = missing_confirmed_decisions(
        state.confirmed_decisions,
        constraints=synthesis.constraints,
        coverage=synthesis.confirmed_decision_coverage,
    )
    if not missing:
        return evaluation
    issue = EvaluationIssue(
        category="confirmed_decision_coverage",
        severity="blocking",
        candidate_locator="confirmed_decision_coverage",
        evidence_locator="book/setup.json#confirmed_decisions",
        explanation="The candidate does not cite coverage for: " + "; ".join(missing),
    )
    original = evaluation.result
    repair = "Add explicit candidate evidence for every confirmed user decision."
    revised = EvaluationResult(
        schema_version=1,
        outcome=(
            original.outcome if original.outcome != "pass" else "local_repair"
        ),
        contract_satisfied=False,
        summary=original.summary,
        issues=[*original.issues, issue],
        signals=original.signals,
        repair_brief=original.repair_brief or repair,
        upstream_blocker=original.upstream_blocker,
    )
    return evaluation.model_copy(update={"result": revised})


def _run_evaluator(
    profile: LlmProfile,
    evaluation_input: EvaluationInput,
    on_event: AgentEventCallback | None,
    *,
    transport_retry_limit: int,
) -> EvaluationRecord:
    if on_event is not None:
        on_event(
            {
                "kind": "agent_evaluation_started",
                "candidate_artifact_id": evaluation_input.candidate_artifact_id,
                "phase": evaluation_input.checkpoint,
            }
        )
    def on_transport_retry(retry: int, limit: int, exc: Exception) -> None:
        if on_event is None:
            return
        on_event(
            {
                "kind": "agent_transport_retry",
                "candidate_artifact_id": evaluation_input.candidate_artifact_id,
                "phase": evaluation_input.checkpoint,
                "retry": retry,
                "limit": limit,
                "message": redact_profile_secrets(str(exc), profile),
            }
        )

    try:
        return evaluate_candidate(
            profile,
            evaluation_input,
            transport_retry_limit=transport_retry_limit,
            on_transport_retry=on_transport_retry,
        )
    except Exception as exc:
        if on_event is not None:
            provider_failure = is_retryable_provider_error(exc)
            on_event(
                {
                    "kind": "agent_evaluation_failed",
                    "candidate_artifact_id": evaluation_input.candidate_artifact_id,
                    "phase": evaluation_input.checkpoint,
                    "category": (
                        "transport_provider" if provider_failure else "local_semantic"
                    ),
                    "code": (
                        "provider_retry_exhausted"
                        if provider_failure
                        else "evaluation_failed"
                    ),
                }
            )
        raise


def _emit_evaluation_completed(
    on_event: AgentEventCallback | None,
    evaluation: EvaluationRecord,
    evaluation_path: str,
) -> None:
    if on_event is None:
        return
    on_event(
        {
            "kind": "agent_evaluation_completed",
            "evaluation_id": evaluation.evaluation_id,
            "candidate_artifact_id": evaluation.candidate_artifact_id,
            "outcome": evaluation.result.outcome,
            "evidence_paths": [evaluation.candidate_artifact_id, evaluation_path],
        }
    )


def _persist_activation_evaluation(
    project_path: Path,
    run_result: AgentRunResult,
    evaluation: EvaluationRecord,
) -> str:
    return write_activation_document(
        project_path,
        run_result.identity,
        run_result.activation_id,
        "evaluation.json",
        evaluation.model_dump(mode="json"),
    )


def _book_synthesis_payload(synthesis: BookDirectionSynthesis) -> dict[str, object]:
    return {
        "direction_markdown": synthesis.direction_markdown,
        "constraints": synthesis.constraints.model_dump(mode="json"),
        "confirmed_decision_coverage": [
            item.model_dump(mode="json")
            for item in synthesis.confirmed_decision_coverage
        ],
        "recommended_titles": [
            item.model_dump(mode="json") for item in synthesis.recommended_titles
        ],
        "rolling_plan_markdown": synthesis.rolling_plan_markdown,
    }


def _semantic_repair_prompt(
    candidate_kind: str,
    candidate_payload: dict[str, object],
    evaluation: EvaluationRecord,
) -> str:
    book_contract = (
        " For a Book candidate, copy every fixed-input confirmed decision verbatim into "
        "constraints.confirmed and confirmed_decision_coverage.decision. Do not paraphrase, "
        "split, merge, or promote repair guidance into a new confirmed decision."
        if candidate_kind == "Book Direction"
        else ""
    )
    return (
        "The stateless Evaluator rejected the current candidate with local_repair. "
        "Revise only this current candidate; do not rewrite committed prose or canon. "
        "Create a complete replacement candidate through the same terminal Tool. For a "
        "Chapter replacement, build a fresh candidate workspace starting its internal plan, "
        "draft, and candidate revisions at 1."
        + book_contract
        + "\n\n"
        + json.dumps(
            {
                "candidate_kind": candidate_kind,
                "current_candidate": candidate_payload,
                "evaluation_id": evaluation.evaluation_id,
                "evaluation": evaluation.result.model_dump(mode="json"),
            },
            ensure_ascii=False,
        )
    )


def _terminal_payload(
    project_path: Path,
    result: AgentRunResult,
    expected_tool: str,
) -> dict[str, object]:
    if result.outcome in {"waiting_user", "blocked"}:
        path = _terminal_artifact(result)
        payload = read_json(project_path / path)
        if not isinstance(payload, dict):
            raise AgentCandidateError(
                "Agent control checkpoint artifact is missing or invalid."
            )
        raise AgentControlCheckpoint(result, path, payload)
    if result.outcome != "candidate" or result.terminal_result is None:
        detail = result.failure.message if result.failure is not None else result.outcome
        raise AgentCandidateError(f"Agent did not submit a candidate: {detail}")
    if result.terminal_result.tool_name != expected_tool:
        raise AgentCandidateError(
            f"Agent stopped with {result.terminal_result.tool_name}, expected {expected_tool}."
        )
    path = _terminal_artifact(result)
    payload = read_json(project_path / path)
    if not isinstance(payload, dict):
        raise AgentCandidateError("Agent terminal candidate artifact is missing or invalid.")
    return payload


def _terminal_artifact(result: AgentRunResult) -> str:
    if result.terminal_result is None or not result.terminal_result.artifact_paths:
        raise AgentCandidateError("Agent terminal Tool did not record a candidate artifact.")
    return result.terminal_result.artifact_paths[0]


def _book_direction_evaluation_input(
    state: SetupStateDocument,
    synthesis: BookDirectionSynthesis,
    *,
    candidate_path: str,
    candidate_revision: int,
    identity: AgentIdentity,
    candidate_run_id: str | None = None,
) -> EvaluationInput:
    evidence = [
        EvaluationEvidence(
            locator="book/setup.json#confirmed_decisions",
            excerpt=json.dumps(state.confirmed_decisions, ensure_ascii=False) or "[]",
        ),
        EvaluationEvidence(
            locator="book/setup.json#unresolved_questions",
            excerpt=json.dumps(state.unresolved_questions, ensure_ascii=False) or "[]",
        ),
        EvaluationEvidence(
            locator="book/setup.json#contradictions",
            excerpt=json.dumps(state.contradictions, ensure_ascii=False) or "[]",
        ),
        EvaluationEvidence(
            locator="book/direction_draft.md",
            excerpt=state.direction_draft[:4_000] or "尚无草稿",
        ),
    ]
    return EvaluationInput(
        identity=identity,
        candidate_run_id=candidate_run_id,
        checkpoint="book_direction_candidate",
        candidate_artifact_id=candidate_path,
        candidate_revision=candidate_revision,
        candidate_content=json.dumps(
            {
                "direction_markdown": synthesis.direction_markdown,
                "constraints": synthesis.constraints.model_dump(mode="json"),
                "confirmed_decision_coverage": [
                    item.model_dump(mode="json")
                    for item in synthesis.confirmed_decision_coverage
                ],
                "recommended_titles": [
                    item.model_dump(mode="json") for item in synthesis.recommended_titles
                ],
                "rolling_plan_markdown": synthesis.rolling_plan_markdown,
            },
            ensure_ascii=False,
        ),
        evidence=evidence,
        deterministic_prechecks={
            "has_direction": bool(synthesis.direction_markdown.strip()),
            "title_count": len(synthesis.recommended_titles),
            "confirmed_decision_count": len(state.confirmed_decisions),
            "coverage_count": len(synthesis.confirmed_decision_coverage),
        },
        rubric_version="book-direction-v1",
    )


def _book_review_from_evaluation(record: EvaluationRecord) -> BookDirectionReview:
    result = record.result
    issues = [
        BookDirectionReviewIssue(
            severity=issue.severity,
            kind=issue.category,
            message=issue.explanation,
            evidence=[issue.candidate_locator, issue.evidence_locator],
        )
        for issue in result.issues
    ]
    if result.outcome != "pass" and not issues:
        issues.append(
            BookDirectionReviewIssue(
                severity="blocking",
                kind=result.outcome,
                message=result.repair_brief or result.summary,
                evidence=[record.candidate_artifact_id],
            )
        )
    return BookDirectionReview(
        status="passed" if result.outcome == "pass" else "blocked",
        summary=result.summary,
        issues=issues,
        signals=[f"{signal.name}={signal.value}" for signal in result.signals],
    )


def _book_discussion_agent_prompt() -> str:
    return (
        "You are the persistent logical Book Agent inside NovelPilot's deterministic Harness. "
        "Use only exposed Tools. Read context only when needed. Update the complete working "
        "Book Direction, respond to the user's latest input, and choose the single highest-value "
        "concrete question yourself. Do not ask the user which topic to discuss next. "
        "Keep confirmed_decisions cumulative and copy every existing entry verbatim; never "
        "paraphrase, split, merge, or remove one. Add a new confirmed decision only when the "
        "latest human message explicitly confirms it; review feedback and repair guidance are "
        "not new user-confirmed decisions. When clarification is needed, call "
        "submit_book_discussion_update with exactly one question "
        "and two or three actionable suggestions. After every substantive story decision has "
        "converged, if selected_title is still empty, make the final question exactly: "
        "‘以下哪个书名最适合作为正式书名？’ Offer two or three concrete title choices whose "
        "suggestion messages contain "
        "the exact title, while the Web UI supplies the custom-input option. Set selected_title "
        "only when the latest human message explicitly chooses or supplies that title. Do not "
        "mark readiness=ready until selected_title is non-empty. When the direction and title "
        "are genuinely ready, submit readiness=ready with no question or suggestions. Never "
        "claim approval or commit state."
    )


def _book_direction_agent_prompt(*, state_revision: int, candidate_revision: int) -> str:
    return (
        "You are the Book Agent preparing one candidate for Harness evaluation. Use only exposed "
        "Tools. Copy every fixed-input confirmed user decision verbatim into both "
        "constraints.confirmed and confirmed_decision_coverage.decision; do not paraphrase, "
        "split, or merge those strings. Preserve the already user-confirmed selected_title as "
        "the authoritative formal title; do not ask the user to choose another title. Surface "
        "open decisions, produce three to five unique compatibility title entries with the "
        "selected title first, and create a rolling plan. Call "
        "submit_book_direction_candidate with "
        f"expected_revision={state_revision} and candidate_revision={candidate_revision}. "
        "Do not review, approve, or commit the candidate yourself."
    )


def _book_evaluation_user_decision_prompt() -> str:
    return (
        "The stateless Evaluator determined that the Book Direction cannot be repaired without "
        "one explicit human decision. Inspect the cited evaluation evidence, choose the single "
        "highest-value concrete decision yourself, and call request_user_decision exactly once. "
        "Ask exactly one question and provide two or three actionable answer suggestions. Do not "
        "ask which topic to discuss, do not submit another candidate, and do not expose hidden "
        "reasoning. The Web UI adds the custom-input option."
    )


def _book_revision_agent_prompt(*, state_revision: int, candidate_revision: int) -> str:
    return (
        "You are the Book Agent preparing a revision candidate for an already approved Book "
        "Direction. Use only exposed Tools. Treat committed prose and canon as immutable "
        "history. Revise only future or unfulfilled Book instructions needed to resolve the "
        "evidenced blocker, while preserving every unaffected approved decision. Return a "
        "complete replacement direction and rolling plan so the Harness can evaluate a single "
        "candidate. Keep three to five title suggestions for schema compatibility, but do not "
        "change the approved project title. Call submit_book_direction_candidate with "
        f"expected_revision={state_revision} and candidate_revision={candidate_revision}. "
        "Never approve or "
        "commit the revision; even full-auto mode requires explicit user approval."
    )


def _story_arc_agent_prompt(
    *,
    arc_id: str,
    intent: str,
    expected_revision: int,
) -> str:
    return (
        "You are the persistent logical Story Arc Agent inside NovelPilot's deterministic "
        "Harness. Use only exposed Tools and never activate another Agent. Read bounded context "
        "as needed, plan only the current rolling arc, and preserve committed evidence. "
        "Resolve local creative choices yourself within the approved Book contract; no "
        "user-decision Tool is available. Human participation happens only when the submitted "
        "Story Arc plan is reviewed. "
        + "Submit "
        f"one {intent} candidate for {arc_id} with expected_revision={expected_revision} through "
        "submit_story_arc_candidate. Choose a target chapter count from 1 through 30. Do not "
        "approve or commit the plan. If an upper contract is truly impossible, report an "
        "evidence-complete proposal and let the Harness route it."
    )


def _chapter_agent_prompt(*, chapter_id: str, expected_revision: int) -> str:
    return (
        "You are the persistent logical Chapter Agent inside NovelPilot's deterministic Harness. "
        "Use only exposed Tools and never activate another Agent. For the owned chapter "
        f"{chapter_id}, expected_revision={expected_revision}: "
        "Resolve chapter-local creative choices yourself within the approved Book and Story Arc "
        "contracts; no user-decision Tool or chapter approval gate is available. "
        + "Read bounded context, call "
        "plan_chapter_candidate, write visible prose through write_chapter_draft, optionally use "
        "targeted edits, call inspect_chapter_consistency, then bind plan, draft, candidate "
        "observations, and candidate state patch with submit_chapter_candidate. Start plan, draft, "
        "and candidate revisions at 1 for a fresh workspace. Never write final.md, never commit "
        "canon, and never claim semantic approval. Use report_blocker only with exact evidence."
    )
def _chapter_patch_evidence_repair_prompt(
    *, chapter_id: str, expected_revision: int
) -> str:
    return (
        "You are the Chapter Agent repairing only rejected evidence quotes for an otherwise "
        "accepted current chapter candidate. final.md and every patch operation, target, value, "
        "and rationale are immutable. Read the supplied final prose and rejection reasons. For "
        "each rejected operation index, choose one or more short verbatim substrings that occur "
        "exactly in final.md, then call submit_chapter_patch_evidence_repair with "
        f"chapter_id={chapter_id} and expected_revision={expected_revision}. Do not rewrite prose, "
        "do not change canon intent, and do not request a user decision."
    )


def _story_arc_evidence(project_path: Path, arc_id: str) -> list[EvaluationEvidence]:
    return _evidence_from_paths(
        project_path,
        [
            Path("book/settings.md"),
            Path("book/outline.md"),
            Path("book/state.json"),
            Path("arcs") / arc_id / "plan.md",
            Path("canon/characters.json"),
            Path("canon/relationships.json"),
            Path("canon/world_facts.json"),
            Path("canon/foreshadowing.json"),
        ],
    )


def _chapter_evidence(
    project_path: Path,
    metadata: ProjectMetadata,
) -> list[EvaluationEvidence]:
    paths = [
        Path("book/settings.md"),
        Path("book/outline.md"),
        Path("canon/characters.json"),
        Path("canon/relationships.json"),
        Path("canon/world_facts.json"),
        Path("canon/foreshadowing.json"),
    ]
    if metadata.active_arc_id is not None:
        paths.append(Path("arcs") / metadata.active_arc_id / "plan.md")
    paths.extend(
        sorted(
            path.relative_to(project_path)
            for path in (project_path / "chapters").glob("*/final.md")
            if path.is_file()
        )[-3:]
    )
    return _evidence_from_paths(project_path, paths)


def _evidence_from_paths(
    project_path: Path,
    paths: list[Path],
) -> list[EvaluationEvidence]:
    evidence: list[EvaluationEvidence] = []
    for relative in paths:
        path = project_path / relative
        if not path.is_file():
            continue
        try:
            excerpt = path.read_text(encoding="utf-8-sig")[:4_000].strip()
        except (OSError, UnicodeError) as exc:
            raise AgentCandidateError(
                f"Evaluation evidence could not be read: {relative.as_posix()}"
            ) from exc
        if excerpt:
            evidence.append(
                EvaluationEvidence(locator=relative.as_posix(), excerpt=excerpt)
            )
    return evidence


def _read_candidate_text(project_path: Path, relative: Path) -> str:
    path = project_path / relative
    if not path.is_file():
        raise AgentCandidateError(
            f"Agent candidate artifact is missing: {relative.as_posix()}"
        )
    try:
        value = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise AgentCandidateError(
            f"Agent candidate artifact is unreadable: {relative.as_posix()}"
        ) from exc
    if not value.strip():
        raise AgentCandidateError(
            f"Agent candidate artifact is empty: {relative.as_posix()}"
        )
    return value


def _require_policy_capabilities(policy: ResolvedAgentPolicy) -> None:
    require_harness_capabilities(policy.profile)
    if policy.evaluator_profile.id != policy.profile.id:
        require_harness_capabilities(policy.evaluator_profile)
