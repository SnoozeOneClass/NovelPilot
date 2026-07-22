import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import ValidationError

from app.harness.agents.domain_tools import (
    BoundBookDirectionCandidate,
    BoundBookDiscussionUpdate,
    BoundStoryArcCandidate,
    SubmitChapterCandidateInput,
    build_default_tool_registry,
)
from app.harness.agents.evaluator import evaluate_candidate, evaluation_input_fingerprint
from app.harness.agents.evidence_matching import materialize_semantic_evidence_quote
from app.harness.agents.models import (
    AgentIdentity,
    AgentRunResult,
    BookCandidateSnapshot,
    CandidateKind,
    ChapterCandidateSnapshot,
    EvaluationEvidence,
    EvaluationInput,
    EvaluationIssue,
    EvaluationRecord,
    EvaluationResult,
    RepairChain,
    RepairContract,
    StoryArcCandidateSnapshot,
    ToolExecutionResult,
)
from app.harness.agents.policy import ResolvedAgentPolicy
from app.harness.agents.persistence import (
    activation_relative,
    append_repair_chain_evaluation,
    read_agent_state,
    read_repair_chain,
    write_activation_document,
)
from app.harness.agents.rubrics import component_fingerprints, resolve_rubric
from app.harness.agents.semantic_boundary import semantic_model_value
from app.harness.agents.runtime import AgentActivation, AgentRuntime
from app.harness.loops.book import (
    BookDirectionSynthesis,
    BookDiscussionTurnResult,
    DiscussionContextAssembly,
)
from app.llm.gateway import ChatChunk, ChatMessage
from app.llm.redaction import redact_profile_secrets
from app.llm.retry import is_retryable_provider_error
from app.schemas.patches import CandidateStatePatch, PatchEvidence
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
from app.storage.transactions import commit_file_transaction


AgentEventCallback = Callable[[dict[str, object]], None]

STORY_ARC_AGENT_TOOLS = (
    "get_loop_context",
    "submit_story_arc_candidate",
    "report_blocker",
)

CHAPTER_AGENT_TOOLS = (
    "get_loop_context",
    "plan_chapter_candidate",
    "write_chapter_draft",
    "inspect_chapter_consistency",
    "write_chapter_observations",
    "write_chapter_state_patch",
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
    evaluation_input: EvaluationInput | None = None
    change_summary: str = ""


@dataclass(frozen=True)
class ChapterAgentResult:
    submission: SubmitChapterCandidateInput
    evaluation: EvaluationRecord
    verification: ChapterVerification
    run_result: AgentRunResult
    candidate_root: str
    evaluation_input: EvaluationInput | None = None


@dataclass(frozen=True)
class ChapterPatchEvidenceBindingResult:
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
    selected_title: str | None = None,
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
                    content=assembly.prompt,
                ),
            ),
            policy=policy,
            control_data={
                "confirmed_decisions": state.confirmed_decisions,
                "superseded_decisions": [
                    item.model_dump(mode="json")
                    for item in state.superseded_decisions
                ],
                "selected_title": selected_title or state.selected_title,
                "turn": state.turn_count + 1,
            },
            initial_checkpoint_id=f"book-discussion:{state.revision}",
            on_event=on_event,
            on_text_delta=on_text_delta,
            on_tool_event=on_tool_event,
        )
    )
    payload = _terminal_payload(project_path, result, "submit_book_discussion_update")
    try:
        candidate = BoundBookDiscussionUpdate.model_validate(payload)
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
    review_candidate_revision = state.candidate_revision_counter + 1
    identity = AgentIdentity(project_id=metadata.project_id, role="book")
    agent_state = read_agent_state(project_path, identity)
    pending_candidate_run_id: str | None = None
    if agent_state.candidate_run_id is not None:
        existing_chain = _read_agent_repair_chain(
            project_path,
            identity,
            candidate_run_id=agent_state.candidate_run_id,
            candidate_kind="book_direction",
            semantic_revision_limit=policy.semantic_revision_limit,
        )
        if existing_chain.pending_repair is not None:
            pending_candidate_run_id = agent_state.candidate_run_id
    candidate_run_id = (
        pending_candidate_run_id
        if pending_candidate_run_id is not None
        else f"book-direction-{review_candidate_revision}-{uuid4().hex[:8]}"
    )
    return _run_book_direction_candidate_agent(
        project_path,
        metadata,
        state,
        policy,
        review_candidate_revision=review_candidate_revision,
        candidate_run_id=candidate_run_id,
        system_prompt=_book_direction_agent_prompt(),
        input_payload={
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
        review_candidate_revision=target_direction_version,
        candidate_run_id=resolved_candidate_run_id,
        system_prompt=_book_revision_agent_prompt(),
        input_payload={
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
    review_candidate_revision: int,
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
                content=json.dumps(
                    semantic_model_value(input_payload),
                    ensure_ascii=False,
                ),
            ),
        ),
        policy=policy,
        expected_candidate_revision=review_candidate_revision,
        control_data={
            "confirmed_decisions": state.confirmed_decisions,
            "selected_title": state.selected_title,
        },
        initial_checkpoint_id=f"book-direction:{state.candidate_revision_counter}",
        on_event=on_event,
        on_text_delta=on_text_delta,
        on_tool_event=on_tool_event,
    )
    activation = _resume_pending_semantic_repair(
        activation,
        candidate_kind="book_direction",
    )
    result = runner.run(activation)
    synthesis, evaluation, review = _book_direction_attempt(
        project_path,
        state,
        policy,
        identity,
        review_candidate_revision,
        result,
        on_event,
    )
    while evaluation.result.outcome == "local_repair":
        chain = _read_agent_repair_chain(
            project_path,
            identity,
            candidate_run_id=activation.candidate_run_id,
            candidate_kind="book_direction",
            semantic_revision_limit=policy.semantic_revision_limit,
        )
        contract = _repair_contract_from_chain(chain, evaluation)
        if not runner.request_semantic_revision(
            activation,
            repair_contract=contract,
        ):
            break
        activation = replace(
            activation,
            repair_contract=contract,
            allowed_tools=_repair_tools_for_contract(contract, "book_direction"),
            messages=(
                ChatMessage(
                    role="user",
                    content=_semantic_repair_prompt(
                        "Book Direction",
                        {
                            "expected_revision": state.revision,
                            "candidate_revision": review_candidate_revision,
                            **_book_synthesis_payload(synthesis),
                        },
                        evaluation,
                        chain,
                        contract,
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
            review_candidate_revision,
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
                            "evaluation": _semantic_evaluation_view(
                                evaluation.result
                            ),
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
    review_candidate_revision: int,
    result: AgentRunResult,
    on_event: AgentEventCallback | None,
) -> tuple[BookDirectionSynthesis, EvaluationRecord, BookDirectionReview]:
    payload = _terminal_payload(
        project_path,
        result,
        ("submit_book_direction_candidate", "submit_candidate_repair"),
    )
    try:
        candidate = BoundBookDirectionCandidate.model_validate(payload)
    except ValidationError as exc:
        raise AgentCandidateError(
            "Book Direction Agent terminal candidate failed local validation."
        ) from exc
    if candidate.candidate_revision != review_candidate_revision:
        raise AgentCandidateError(
            "Book Direction candidate revision does not match the fixed Book review target."
        )
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
    chain = _read_agent_repair_chain(
        project_path,
        identity,
        candidate_run_id=result.candidate_run_id,
        candidate_kind="book_direction",
        semantic_revision_limit=policy.semantic_revision_limit,
    )
    chained = _chain_evaluation_for_activation(
        project_path,
        chain,
        result.activation_id,
    )
    if chained is not None:
        cached_evaluation, evaluation_path = chained
        review = _book_review_from_evaluation(cached_evaluation)
        _emit_evaluation_completed(on_event, cached_evaluation, evaluation_path)
        return synthesis, cached_evaluation, review
    evaluation_input = _book_direction_evaluation_input(
        state,
        synthesis,
        candidate_path=candidate_path,
        review_candidate_revision=review_candidate_revision,
        identity=identity,
        candidate_run_id=result.candidate_run_id,
        chain=chain,
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
            and existing.candidate_revision == evaluation_input.candidate_revision
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
            candidate_revision=review_candidate_revision,
            candidate_run_id=result.candidate_run_id,
            on_event=on_event,
            evaluation_input=evaluation_input,
        )
        evaluation = _normalize_runtime_evaluation(evaluation_input, evaluation)
        evaluation_path = _persist_activation_evaluation(project_path, result, evaluation)
    else:
        evaluation = _normalize_runtime_evaluation(evaluation_input, evaluation)
        review = _book_review_from_evaluation(evaluation)
        evaluation_path = evaluation_relative
        write_activation_document(
            project_path,
            identity,
            result.activation_id,
            "evaluation.json",
            evaluation.model_dump(mode="json"),
        )
    _record_evaluation_chain(
        project_path,
        chain,
        run_result=result,
        evaluation_path=evaluation_path,
        evaluation_input=evaluation_input,
        evaluation=evaluation,
    )
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
    evaluation_input: EvaluationInput | None = None,
) -> tuple[EvaluationRecord, BookDirectionReview]:
    _require_policy_capabilities(policy)
    evaluation_input = evaluation_input or _book_direction_evaluation_input(
        state,
        synthesis,
        candidate_path=candidate_path,
        review_candidate_revision=candidate_revision,
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
        system_prompt=_story_arc_agent_prompt(),
        messages=(ChatMessage(role="user", content=instruction),),
        policy=policy,
        initial_checkpoint_id=f"story-arc:{arc_id}:{expected_revision}",
        on_event=on_event,
        on_text_delta=on_text_delta,
        on_tool_event=on_tool_event,
    )
    activation = _resume_pending_semantic_repair(
        activation,
        candidate_kind="story_arc",
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
        chain = _read_agent_repair_chain(
            project_path,
            identity,
            candidate_run_id=activation.candidate_run_id,
            candidate_kind="story_arc",
            semantic_revision_limit=policy.semantic_revision_limit,
        )
        contract = _repair_contract_from_chain(chain, attempt.evaluation)
        if not runner.request_semantic_revision(
            activation,
            repair_contract=contract,
        ):
            break
        activation = replace(
            activation,
            repair_contract=contract,
            allowed_tools=_repair_tools_for_contract(contract, "story_arc"),
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
                            "change_summary": attempt.change_summary,
                        },
                        attempt.evaluation,
                        chain,
                        contract,
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
    payload = _terminal_payload(
        project_path,
        result,
        ("submit_story_arc_candidate", "submit_candidate_repair"),
    )
    try:
        candidate = BoundStoryArcCandidate.model_validate(payload)
    except ValidationError as exc:
        raise AgentCandidateError("Story Arc candidate failed local validation.") from exc
    if candidate.intent != intent:
        raise AgentCandidateError(
            f"Story Arc candidate intent {candidate.intent!r} does not match {intent!r}."
        )
    candidate_path = _terminal_artifact(result)
    chain = _read_agent_repair_chain(
        project_path,
        identity,
        candidate_run_id=result.candidate_run_id,
        candidate_kind="story_arc",
        semantic_revision_limit=policy.semantic_revision_limit,
    )
    chained = _chain_evaluation_for_activation(
        project_path,
        chain,
        result.activation_id,
    )
    if chained is not None:
        cached_evaluation, evaluation_path = chained
        _emit_evaluation_completed(on_event, cached_evaluation, evaluation_path)
        return StoryArcAgentResult(
            proposal=StoryArcPlanProposal(
                plan_markdown=candidate.plan_markdown,
                target_chapter_count=candidate.target_chapter_count,
            ),
            evaluation=cached_evaluation,
            run_result=result,
            candidate_artifact_path=candidate_path,
            evaluation_input=None,
            change_summary=candidate.change_summary,
        )
    evaluation_input = _evaluation_input_for_chain(
        chain,
        identity=identity,
        checkpoint="story_arc_candidate",
        candidate_artifact_id=candidate_path,
        candidate=StoryArcCandidateSnapshot(
            plan=candidate.plan_markdown,
            target_chapter_count=candidate.target_chapter_count,
            change_summary=candidate.change_summary,
        ),
        evidence=_story_arc_evidence(project_path, arc_id),
        deterministic_prechecks={
            "target_chapter_count": candidate.target_chapter_count,
            "has_plan": bool(candidate.plan_markdown.strip()),
            "ownership_matches": candidate.arc_id == arc_id,
        },
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
            and existing.candidate_revision == evaluation_input.candidate_revision
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
    evaluation = _normalize_runtime_evaluation(evaluation_input, evaluation)
    if evaluation_path == evaluation_relative:
        write_activation_document(
            project_path,
            identity,
            result.activation_id,
            "evaluation.json",
            evaluation.model_dump(mode="json"),
        )
    _record_evaluation_chain(
        project_path,
        chain,
        run_result=result,
        evaluation_path=evaluation_path,
        evaluation_input=evaluation_input,
        evaluation=evaluation,
    )
    _emit_evaluation_completed(on_event, evaluation, evaluation_path)
    return StoryArcAgentResult(
        proposal=StoryArcPlanProposal(
            plan_markdown=candidate.plan_markdown,
            target_chapter_count=candidate.target_chapter_count,
        ),
        evaluation=evaluation,
        run_result=result,
        candidate_artifact_path=candidate_path,
        evaluation_input=evaluation_input,
        change_summary=candidate.change_summary,
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
        system_prompt=_chapter_agent_prompt(),
        messages=(ChatMessage(role="user", content=instruction),),
        policy=policy,
        initial_checkpoint_id=f"chapter:{chapter_id}:{expected_revision}",
        on_event=on_event,
        on_text_delta=on_text_delta,
        on_tool_event=on_tool_event,
    )
    activation = _resume_pending_semantic_repair(
        activation,
        candidate_kind="chapter",
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
        chain = _read_agent_repair_chain(
            project_path,
            identity,
            candidate_run_id=activation.candidate_run_id,
            candidate_kind="chapter",
            semantic_revision_limit=policy.semantic_revision_limit,
        )
        contract = _repair_contract_from_chain(chain, attempt.evaluation)
        if not runner.request_semantic_revision(
            activation,
            repair_contract=contract,
        ):
            break
        candidate_root = project_path / attempt.candidate_root
        activation = replace(
            activation,
            repair_contract=contract,
            allowed_tools=_repair_tools_for_contract(contract, "chapter"),
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
                        chain,
                        contract,
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


def bind_chapter_patch_evidence(
    project_path: Path,
    metadata: ProjectMetadata,
    *,
    chapter_id: str,
    expected_revision: int,
    on_event: AgentEventCallback | None = None,
) -> ChapterPatchEvidenceBindingResult:
    """Rebind exact patch evidence locally from provider-authored semantic intent."""
    identity = AgentIdentity(
        project_id=metadata.project_id,
        role="chapter",
        scope_id=chapter_id,
    )
    patch_payload = read_json(
        project_path / "chapters" / chapter_id / "candidate_state_patch.json",
        default=None,
    )
    if patch_payload is None:
        raise AgentCandidateError("Chapter candidate state patch is missing.")
    patch = CandidateStatePatch.model_validate(patch_payload)
    final_path = project_path / "chapters" / chapter_id / "final.md"
    final_text = final_path.read_text(encoding="utf-8-sig")
    operations = []
    for index, operation in enumerate(patch.operations):
        quote = materialize_semantic_evidence_quote(
            final_text,
            [
                operation.rationale,
                operation.target_id,
                json.dumps(operation.value, ensure_ascii=False),
            ],
        )
        if quote is None:
            raise AgentCandidateError(
                "Harness could not materialize evidence from an empty Chapter draft for "
                "state-patch operation "
                f"{index}."
            )
        evidence_file = f"chapters/{chapter_id}/final.md"
        operations.append(
            operation.model_copy(
                update={
                    "evidence": [
                        PatchEvidence(file=evidence_file, quote=quote)
                    ]
                }
            )
        )
    repaired = patch.model_copy(update={"operations": operations})
    resolved_run_id = (
        f"chapter-patch-{chapter_id}-{expected_revision + 1}-{uuid4().hex[:8]}"
    )
    activation_id = f"harness-bind-{uuid4().hex[:12]}"
    relative = (
        Path("chapters")
        / chapter_id
        / "state_patch_repairs"
        / f"{activation_id}.json"
    ).as_posix()
    commit_file_transaction(
        project_path,
        kind=f"harness-evidence-bind-{chapter_id}",
        files={
            relative: json.dumps(
                repaired.model_dump(mode="json"),
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        },
    )
    result = AgentRunResult(
        outcome="candidate",
        identity=identity,
        candidate_run_id=resolved_run_id,
        activation_id=activation_id,
        turns_used=0,
        terminal_result=ToolExecutionResult(
            status="ok",
            tool_name="harness_bind_state_patch_evidence",
            tool_call_id=f"harness:{activation_id}",
            content={"summary": "Harness rebound exact evidence from semantic intent."},
            checkpoint_id=f"chapter-patch:{chapter_id}:{expected_revision + 1}",
            terminal=True,
            artifact_paths=[relative],
        ),
        usage={},
    )
    if on_event is not None:
        on_event(
            {
                "kind": "harness_semantic_evidence_bound",
                "candidate_run_id": resolved_run_id,
                "activation_id": activation_id,
                "chapter_id": chapter_id,
                "operation_count": len(operations),
            }
        )
    return ChapterPatchEvidenceBindingResult(
        patch=repaired,
        run_result=result,
        candidate_artifact_path=relative,
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
    payload = _terminal_payload(
        project_path,
        result,
        ("submit_chapter_candidate", "submit_candidate_repair"),
    )
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
    chain = _read_agent_repair_chain(
        project_path,
        identity,
        candidate_run_id=result.candidate_run_id,
        candidate_kind="chapter",
        semantic_revision_limit=policy.semantic_revision_limit,
    )
    chained = _chain_evaluation_for_activation(
        project_path,
        chain,
        result.activation_id,
    )
    if chained is not None:
        cached_evaluation, evaluation_path = chained
        _emit_evaluation_completed(on_event, cached_evaluation, evaluation_path)
        return ChapterAgentResult(
            submission=submission,
            evaluation=cached_evaluation,
            verification=chapter_verification_from_evaluation(
                chapter_id,
                cached_evaluation,
            ),
            run_result=result,
            candidate_root=root.as_posix(),
            evaluation_input=None,
        )
    evaluation_input = _evaluation_input_for_chain(
        chain,
        identity=identity,
        checkpoint="chapter_candidate",
        candidate_artifact_id=manifest_path,
        candidate=ChapterCandidateSnapshot(
            plan=plan,
            draft=draft,
            observations=submission.observations.model_dump(mode="json"),
            state_patch=submission.state_patch.model_dump(mode="json"),
        ),
        evidence=_chapter_evidence(project_path, metadata),
        deterministic_prechecks={
            "plan_revision": submission.plan_revision,
            "draft_revision": submission.draft_revision,
            "draft_characters": len(draft),
            "has_observation_source": bool(submission.observations.based_on),
        },
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
            and existing.candidate_revision == evaluation_input.candidate_revision
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
    evaluation = _normalize_runtime_evaluation(evaluation_input, evaluation)
    write_activation_document(
        project_path,
        identity,
        result.activation_id,
        "evaluation.json",
        evaluation.model_dump(mode="json"),
    )
    _record_evaluation_chain(
        project_path,
        chain,
        run_result=result,
        evaluation_path=evaluation_path,
        evaluation_input=evaluation_input,
        evaluation=evaluation,
    )
    _emit_evaluation_completed(on_event, evaluation, evaluation_path)
    return ChapterAgentResult(
        submission=submission,
        evaluation=evaluation,
        verification=chapter_verification_from_evaluation(chapter_id, evaluation),
        run_result=result,
        candidate_root=root.as_posix(),
        evaluation_input=evaluation_input,
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
    precheck_issue_id = "issue-precheck-" + sha256(
        (evaluation.evaluation_id + "confirmed_decision_coverage").encode("utf-8")
    ).hexdigest()[:16]
    issue = EvaluationIssue(
        issue_id=precheck_issue_id,
        category="confirmed_decision_coverage",
        severity="blocking",
        candidate_locator="candidate.confirmed_decision_coverage",
        evidence_locator="book/setup.json#confirmed_decisions",
        explanation="The candidate does not cite coverage for: " + "; ".join(missing),
    )
    original = evaluation.result
    repair = "Add explicit candidate evidence for every confirmed user decision."
    revised = EvaluationResult(
        schema_version=2,
        outcome=(
            original.outcome if original.outcome != "pass" else "local_repair"
        ),
        contract_satisfied=False,
        summary=original.summary,
        issues=[*original.issues, issue],
        signals=original.signals,
        repair_brief=original.repair_brief or repair,
        upstream_blocker=original.upstream_blocker,
        rubric_checks=original.rubric_checks,
        prior_issue_checks=original.prior_issue_checks,
        new_issue_ids=[
            *original.new_issue_ids,
            precheck_issue_id,
        ],
        resolved_issue_ids=original.resolved_issue_ids,
        repair_scope=list(
            dict.fromkeys(
                [*original.repair_scope, "confirmed_decision_coverage"]
            )
        ),
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
            "evaluation_mode": evaluation.evaluation_mode,
            "logical_candidate_revision": evaluation.candidate_revision,
            "open_issue_count": len(evaluation.result.issues),
            "resolved_issue_count": len(evaluation.result.resolved_issue_ids),
            "new_issue_count": len(evaluation.result.new_issue_ids),
            "late_discovery_count": sum(
                issue.discovery == "late_discovery"
                for issue in evaluation.result.issues
                if issue.issue_id in evaluation.result.new_issue_ids
            ),
            "allowed_components": list(evaluation.result.repair_scope),
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


def _read_agent_repair_chain(
    project_path: Path,
    identity: AgentIdentity,
    *,
    candidate_run_id: str,
    candidate_kind: CandidateKind,
    semantic_revision_limit: int,
) -> RepairChain:
    return read_repair_chain(
        project_path,
        identity,
        candidate_run_id=candidate_run_id,
        candidate_kind=candidate_kind,
        semantic_revision_limit=semantic_revision_limit,
    )


def _resume_pending_semantic_repair(
    activation: AgentActivation,
    *,
    candidate_kind: CandidateKind,
) -> AgentActivation:
    chain = _read_agent_repair_chain(
        activation.project_path,
        activation.identity,
        candidate_run_id=activation.candidate_run_id,
        candidate_kind=candidate_kind,
        semantic_revision_limit=activation.policy.semantic_revision_limit,
    )
    contract = chain.pending_repair
    if contract is None:
        return activation
    if not chain.entries or chain.entries[-1].evaluation_id != contract.evaluation_id:
        raise AgentCandidateError("Pending repair is not attached to the chain head.")
    evaluation_payload = read_json(
        activation.project_path / chain.entries[-1].evaluation_path,
        default=None,
    )
    if evaluation_payload is None:
        raise AgentCandidateError("Pending repair evaluation is missing.")
    evaluation = EvaluationRecord.model_validate(evaluation_payload)
    candidate_payload = read_json(
        activation.project_path / contract.source_candidate_artifact_id,
        default=None,
    )
    if not isinstance(candidate_payload, dict):
        raise AgentCandidateError("Pending repair source candidate is missing.")
    label = {
        "book_direction": "Book Direction",
        "story_arc": "Story Arc",
        "chapter": "Chapter",
    }[candidate_kind]
    if candidate_kind == "chapter":
        candidate_root = Path(contract.source_candidate_artifact_id).parent
        candidate_payload = {
            "submission": candidate_payload,
            "plan": _read_candidate_text(
                activation.project_path,
                candidate_root / "plan.md",
            ),
            "draft": _read_candidate_text(
                activation.project_path,
                candidate_root / "draft.md",
            ),
        }
    return replace(
        activation,
        repair_contract=contract,
        allowed_tools=_repair_tools_for_contract(contract, candidate_kind),
        messages=(
            ChatMessage(
                role="user",
                content=_semantic_repair_prompt(
                    label,
                    candidate_payload,
                    evaluation,
                    chain,
                    contract,
                ),
            ),
        ),
    )


def _evaluation_input_for_chain(
    chain: RepairChain,
    *,
    identity: AgentIdentity,
    checkpoint: str,
    candidate_artifact_id: str,
    candidate: BookCandidateSnapshot
    | StoryArcCandidateSnapshot
    | ChapterCandidateSnapshot,
    evidence: list[EvaluationEvidence],
    deterministic_prechecks: dict[str, bool | int | float | str],
) -> EvaluationInput:
    fingerprints = component_fingerprints(candidate)
    if not chain.entries:
        return EvaluationInput(
            identity=identity,
            candidate_run_id=chain.candidate_run_id,
            checkpoint=checkpoint,
            candidate_artifact_id=candidate_artifact_id,
            candidate_revision=1,
            mode="initial",
            candidate=candidate,
            component_fingerprints=fingerprints,
            evidence=evidence,
            deterministic_prechecks=deterministic_prechecks,
            rubric=resolve_rubric(candidate.kind),
        )
    contract = chain.pending_repair
    if contract is None:
        raise AgentCandidateError(
            "Repair-chain head has no pending contract for the new candidate activation."
        )
    if contract.source_component_fingerprints != chain.entries[-1].component_fingerprints:
        raise AgentCandidateError("Repair contract source fingerprints are stale.")
    return EvaluationInput(
        identity=identity,
        candidate_run_id=chain.candidate_run_id,
        checkpoint=checkpoint,
        candidate_artifact_id=candidate_artifact_id,
        candidate_revision=contract.next_candidate_revision,
        mode="repair_verification",
        candidate=candidate,
        component_fingerprints=fingerprints,
        evidence=evidence,
        deterministic_prechecks=deterministic_prechecks,
        rubric=resolve_rubric(candidate.kind),
        review_history=chain.review_history,
        expected_repair=contract,
    )


def _chain_evaluation_for_activation(
    project_path: Path,
    chain: RepairChain,
    activation_id: str,
) -> tuple[EvaluationRecord, str] | None:
    for index, entry in enumerate(chain.entries):
        if entry.activation_id != activation_id:
            continue
        payload = read_json(project_path / entry.evaluation_path, default=None)
        if payload is None:
            chain.entries = chain.entries[:index]
            chain.review_history = chain.review_history[:index]
            chain.pending_repair = None
            chain.used_semantic_revisions = min(
                chain.used_semantic_revisions,
                max(index - 1, 0),
            )
            return None
        record = EvaluationRecord.model_validate(payload)
        if record.evaluation_id != entry.evaluation_id:
            raise AgentCandidateError("Repair chain evaluation identity is inconsistent.")
        return record, entry.evaluation_path
    return None


def _normalize_runtime_evaluation(
    evaluation_input: EvaluationInput,
    evaluation: EvaluationRecord,
) -> EvaluationRecord:
    result = evaluation.result
    issues: list[EvaluationIssue] = []
    assigned_ids: list[str] = []
    for index, issue in enumerate(result.issues):
        issue_id = issue.issue_id
        if issue_id is None:
            seed = json.dumps(
                {
                    "evaluation_id": evaluation.evaluation_id,
                    "index": index,
                    "category": issue.category,
                    "candidate_locator": issue.candidate_locator,
                    "evidence_locator": issue.evidence_locator,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            issue_id = "issue-" + sha256(seed.encode("utf-8")).hexdigest()[:20]
            assigned_ids.append(issue_id)
        issues.append(
            issue.model_copy(
                update={
                    "issue_id": issue_id,
                    "discovery": (
                        "late_discovery"
                        if evaluation_input.mode == "repair_verification"
                        else issue.discovery
                    ),
                }
            )
        )
    repair_scope = list(result.repair_scope)
    if result.outcome == "local_repair" and not repair_scope:
        repair_scope = list(evaluation_input.component_fingerprints)
    normalized = result.model_copy(
        update={
            "schema_version": 2,
            "issues": issues,
            "new_issue_ids": list(dict.fromkeys([*result.new_issue_ids, *assigned_ids])),
            "repair_scope": repair_scope,
        }
    )
    return evaluation.model_copy(
        update={
            "candidate_run_id": evaluation_input.candidate_run_id,
            "candidate_artifact_id": evaluation_input.candidate_artifact_id,
            "candidate_revision": evaluation_input.candidate_revision,
            "rubric_version": evaluation_input.rubric_version,
            "evaluation_mode": evaluation_input.mode,
            "result": normalized,
        }
    )


def _record_evaluation_chain(
    project_path: Path,
    chain: RepairChain,
    *,
    run_result: AgentRunResult,
    evaluation_path: str,
    evaluation_input: EvaluationInput,
    evaluation: EvaluationRecord,
) -> RepairChain:
    return append_repair_chain_evaluation(
        project_path,
        chain,
        activation_id=run_result.activation_id,
        evaluation_path=evaluation_path,
        evaluation_input=evaluation_input,
        evaluation=evaluation,
    )


def _repair_contract_from_chain(
    chain: RepairChain,
    evaluation: EvaluationRecord,
) -> RepairContract:
    if not chain.entries or chain.entries[-1].evaluation_id != evaluation.evaluation_id:
        raise AgentCandidateError("Cannot schedule repair away from the chain head.")
    if evaluation.result.outcome != "local_repair":
        raise AgentCandidateError("Only local_repair can create a repair contract.")
    open_issue_ids = [
        issue.issue_id for issue in evaluation.result.issues if issue.issue_id is not None
    ]
    if not open_issue_ids or not evaluation.result.repair_brief:
        raise AgentCandidateError("Local repair evaluation is missing its issue contract.")
    entry = chain.entries[-1]
    return RepairContract(
        evaluation_id=evaluation.evaluation_id,
        source_activation_id=entry.activation_id,
        source_candidate_artifact_id=entry.candidate_artifact_id,
        source_candidate_revision=entry.candidate_revision,
        next_candidate_revision=entry.candidate_revision + 1,
        open_issue_ids=open_issue_ids,
        repair_brief=evaluation.result.repair_brief,
        allowed_components=evaluation.result.repair_scope,
        source_component_fingerprints=entry.component_fingerprints,
        repair_workspace_id=(
            "repair-"
            + sha256(
                (
                    chain.candidate_run_id
                    + "\x1f"
                    + evaluation.evaluation_id
                    + "\x1f"
                    + entry.candidate_artifact_id
                ).encode("utf-8")
            ).hexdigest()[:24]
        ),
    )


def _repair_tools_for_contract(
    contract: RepairContract,
    candidate_kind: CandidateKind,
) -> tuple[str, ...]:
    allowed = set(contract.allowed_components)
    tools = ["get_loop_context", "open_candidate_repair"]
    if allowed & {"direction", "rolling_plan", "plan", "change_summary", "draft"}:
        tools.append("replace_candidate_text")
    if "target_chapter_count" in allowed:
        tools.append("set_story_arc_chapter_count")
    if allowed & {"constraints", "confirmed_decision_coverage", "recommended_titles"}:
        tools.extend(
            [
                "add_book_repair_item",
                "update_book_repair_item",
                "delete_candidate_repair_item",
            ]
        )
    if "observations" in allowed:
        tools.extend(
            [
                "add_chapter_observation_repair",
                "update_chapter_observation_repair",
                "delete_candidate_repair_item",
            ]
        )
    if "state_patch" in allowed:
        tools.extend(
            [
                "add_state_patch_operation_repair",
                "update_state_patch_operation_repair",
                "delete_candidate_repair_item",
            ]
        )
    tools.extend(["submit_candidate_repair", "report_blocker"])
    return tuple(dict.fromkeys(tools))


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
    chain: RepairChain,
    contract: RepairContract,
) -> str:
    return (
        "The Evaluator requested a scoped repair of the same uncommitted logical candidate. "
        "The Harness owns the source candidate, unchanged artifacts, identities, revisions, "
        "exact evidence, assembly, and the complete review ledger. Resolve every open issue "
        "semantically using only the "
        "artifact-level repair Tools that are exposed. Do not resubmit or restate unchanged "
        "candidate components or any identity, path, locator, quote, fingerprint, or revision. "
        "Call open_candidate_repair before structured updates; select an existing item by its "
        "current meaning, never by an opaque handle. For prose or plan problems, "
        "replace_candidate_text submits complete revised text. When the semantic changes are "
        "complete, call submit_candidate_repair "
        "with only a short summary. The Harness will merge the working copy and send the full "
        "candidate to an independent Evaluator. Do not rewrite committed prose or canon."
        + "\n\n"
        + json.dumps(
            {
                "candidate_kind": candidate_kind,
                "current_candidate": semantic_model_value(candidate_payload),
                "evaluation": _semantic_evaluation_view(evaluation.result),
                "complete_review_history": [
                    _semantic_evaluation_view(item.result)
                    for item in chain.review_history
                ],
            },
            ensure_ascii=False,
        )
    )


def _semantic_evaluation_view(result: EvaluationResult) -> dict[str, object]:
    return {
        "outcome": result.outcome,
        "contract_satisfied": result.contract_satisfied,
        "summary": result.summary,
        "issues": [
            {
                "category": item.category,
                "severity": item.severity,
                "explanation": item.explanation,
            }
            for item in result.issues
        ],
        "repair_brief": result.repair_brief,
    }


def _terminal_payload(
    project_path: Path,
    result: AgentRunResult,
    expected_tool: str | tuple[str, ...],
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
    expected_tools = (
        (expected_tool,) if isinstance(expected_tool, str) else expected_tool
    )
    if result.terminal_result.tool_name not in expected_tools:
        raise AgentCandidateError(
            "Agent stopped with "
            f"{result.terminal_result.tool_name}, expected one of {expected_tools}."
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
    review_candidate_revision: int,
    identity: AgentIdentity,
    candidate_run_id: str | None = None,
    chain: RepairChain | None = None,
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
    candidate = BookCandidateSnapshot(
        direction=synthesis.direction_markdown,
        constraints=synthesis.constraints.model_dump(mode="json"),
        confirmed_decision_coverage=[
            item.model_dump(mode="json")
            for item in synthesis.confirmed_decision_coverage
        ],
        recommended_titles=[
            item.model_dump(mode="json") for item in synthesis.recommended_titles
        ],
        rolling_plan=synthesis.rolling_plan_markdown,
    )
    prechecks: dict[str, bool | int | float | str] = {
        "has_direction": bool(synthesis.direction_markdown.strip()),
        "title_count": len(synthesis.recommended_titles),
        "confirmed_decision_count": len(state.confirmed_decisions),
        "coverage_count": len(synthesis.confirmed_decision_coverage),
        "direction_version": review_candidate_revision,
    }
    if chain is not None:
        return _evaluation_input_for_chain(
            chain,
            identity=identity,
            checkpoint="book_direction_candidate",
            candidate_artifact_id=candidate_path,
            candidate=candidate,
            evidence=evidence,
            deterministic_prechecks=prechecks,
        )
    return EvaluationInput(
        identity=identity,
        candidate_run_id=candidate_run_id,
        checkpoint="book_direction_candidate",
        candidate_artifact_id=candidate_path,
        candidate_revision=1,
        candidate=candidate,
        component_fingerprints=component_fingerprints(candidate),
        evidence=evidence,
        deterministic_prechecks=prechecks,
        rubric=resolve_rubric("book_direction"),
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
        "When the user explicitly delegates local creative choices, resolve those choices inside "
        "the stated constraints instead of turning each one into a clarification question. Ask "
        "only when missing human intent would materially change the Book contract and cannot be "
        "resolved by an expressed preference or delegation. The initial creative delegation "
        "remains authoritative across later suggestion clicks unless the user explicitly revokes "
        "it. A click on one recommended answer never authorizes a new chain of chapter-level "
        "questions. Injury choreography, exact evidence ordering, chapter placement, procedural "
        "wording, dialogue beats, and similar implementation details are local creative choices, "
        "not missing Book-level intent. "
        "Submit only newly confirmed decisions from the latest human message; the Harness "
        "preserves all earlier decisions and assigns every internal identity. Add a new "
        "confirmed decision only when the "
        "latest human message explicitly confirms it; review feedback and repair guidance are "
        "not new user-confirmed decisions. When the latest human message changes an earlier "
        "decision, describe its meaning in superseded_decisions.prior_meaning and provide the "
        "semantic replacement there. Do not copy exact stored wording and do not duplicate the "
        "replacement in newly_confirmed_decisions; the Harness resolves and updates the durable "
        "decision list. When clarification is needed, call "
        "submit_book_discussion_update with exactly one question "
        "and two or three actionable suggestions. After every substantive story decision has "
        "converged, if no title is selected, ask one concrete title-selection question and "
        "offer two or three meaningful title choices. For every title choice, set its "
        "formal_title to the plain title value; leave formal_title null on every ordinary "
        "answer. The Harness binds a clicked title suggestion without relying on the question "
        "wording, label formatting, or a later model repetition. Set newly_selected_title "
        "only when the latest human message explicitly chooses or supplies that title. Do not "
        "mark readiness=ready until a title is selected. When the direction and title "
        "are genuinely ready, submit readiness=ready with no question or suggestions. Never "
        "claim approval or commit state."
    )


def _book_direction_agent_prompt() -> str:
    return (
        "You are the Book Agent preparing one candidate for Harness evaluation. Use only exposed "
        "Tools. The Harness injects all confirmed decisions, formal-title authority, coverage, "
        "identity, and revision metadata. Do not copy or maintain them. Surface open decisions, "
        "produce two to four semantic comparison titles, and create a rolling plan. Call "
        "submit_book_direction_candidate with only the semantic candidate fields. "
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


def _book_revision_agent_prompt() -> str:
    return (
        "You are the Book Agent preparing a revision candidate for an already approved Book "
        "Direction. Use only exposed Tools. Treat committed prose and canon as immutable "
        "history. Revise only future or unfulfilled Book instructions needed to resolve the "
        "evidenced blocker, while preserving every unaffected approved decision. Return a "
        "complete replacement direction and rolling plan so the Harness can evaluate a single "
        "candidate. Provide two to four semantic comparison titles; Harness preserves the "
        "approved project title and all control metadata. Call submit_book_direction_candidate "
        "with semantic fields only. "
        "Never approve or "
        "commit the revision; even full-auto mode requires explicit user approval."
    )


def _story_arc_agent_prompt() -> str:
    return (
        "You are the persistent logical Story Arc Agent inside NovelPilot's deterministic "
        "Harness. Use only exposed Tools and never activate another Agent. Read bounded context "
        "as needed, plan only the current rolling arc, and preserve committed evidence. "
        "A Story Arc is a macro narrative unit ending at an approved turning point, not one "
        "drafting round, milestone, or next chapter. If the Book rolling contract lists several "
        "numbered chapter rounds that advance the same goal before that turning point, include "
        "those rounds in this arc and choose a target count that covers them. Use one chapter "
        "only when the approved Book contract explicitly defines a complete one-chapter arc. "
        "Resolve local creative choices yourself within the approved Book contract; no "
        "user-decision Tool is available. Human participation happens only when the submitted "
        "Story Arc plan is reviewed. "
        + "Submit one semantic candidate through submit_story_arc_candidate. Harness binds "
        "arc ownership, create/revise intent, and revisions. Choose a target chapter count from "
        "1 through 30. Do not "
        "approve or commit the plan. If an upper contract is truly impossible, report an "
        "evidence-complete proposal and let the Harness route it."
    )


def _chapter_agent_prompt() -> str:
    return (
        "You are the persistent logical Chapter Agent inside NovelPilot's deterministic Harness. "
        "Use only exposed Tools and never activate another Agent. Harness owns chapter identity, "
        "all revisions, paths, and component assembly. "
        "Resolve chapter-local creative choices yourself within the approved Book and Story Arc "
        "contracts; no user-decision Tool or chapter approval gate is available. "
        + "Read bounded context, call "
        "plan_chapter_candidate, write the complete visible prose through write_chapter_draft, "
        "and call inspect_chapter_consistency. Then write semantic observations "
        "with write_chapter_observations and the canon delta with write_chapter_state_patch. "
        "Observations are semantic candidates: submit them once, and let the Harness retain "
        "only those it can bind to draft evidence instead of repeatedly paraphrasing them. "
        "Describe each canon change by semantic change kind, entity kind/name, resulting state, "
        "and an evidence hint; never provide storage operations, field keys, JSON encodings, a "
        "target file, canonical ID, version, locator, or exact quote. Finally call "
        "submit_chapter_candidate with only a concise summary; Harness binds exact evidence and "
        "assembles the stored components. Never write final.md, never commit canon, and never "
        "claim semantic approval."
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
        Path("book/state.json"),
        Path("canon/characters.json"),
        Path("canon/relationships.json"),
        Path("canon/world_facts.json"),
        Path("canon/foreshadowing.json"),
    ]
    if metadata.active_arc_id is not None:
        paths.append(Path("arcs") / metadata.active_arc_id / "plan.md")
        paths.append(Path("arcs") / metadata.active_arc_id / "state.json")
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
