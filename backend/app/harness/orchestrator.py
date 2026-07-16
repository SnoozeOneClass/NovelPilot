import json
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from collections.abc import Callable
from typing import Any, Literal
from uuid import uuid4

from app.core.paths import resolve_artifact_path
from app.harness.agents.evaluator import evaluation_view_files
from app.harness.agents.events import project_agent_event
from app.harness.agents.loop_runners import (
    AgentControlCheckpoint,
    recover_completed_chapter_agent,
    recover_completed_story_arc_agent,
    run_book_revision_agent,
    run_chapter_agent,
    run_chapter_patch_evidence_repair_agent,
    run_story_arc_agent,
)
from app.harness.agents.models import AgentIdentity, EvaluationRecord
from app.harness.agents.persistence import (
    activation_relative,
    read_agent_state,
    save_agent_state,
)
from app.harness.agents.policy import ResolvedAgentPolicy, resolve_agent_policy
from app.harness.agents.public_stream import ChapterDraftStreamProjector
from app.harness.stream_progress import StreamProgressAccumulator
from app.llm.gateway import ChatChunk, ChatMessage, ChatRequest, ChatResult, call_llm
from app.llm.profiles import get_active_profile
from app.llm.redaction import redact_profile_secrets
from app.llm.retry import call_llm_with_transport_retries, is_retryable_provider_error
from app.schemas.artifacts import (
    ChapterVerification,
    ContextExclusion,
    ContextSnapshot,
    ContextSource,
)
from app.schemas.events import EventStatus, HarnessEvent, LoopLayer
from app.schemas.patches import CandidateStatePatch
from app.schemas.profiles import LlmProfile
from app.schemas.projects import ProjectMetadata, RunStatus
from app.storage import arcs as arc_storage
from app.storage import book_revisions as book_revision_storage
from app.storage.events import append_event, read_events
from app.storage.json_files import read_json, write_json
from app.storage.patches import PatchValidationError, commit_candidate_state_patch
from app.storage.projects import read_project_metadata, write_project_metadata
from app.storage.setup import read_setup_state
from app.storage.text_files import read_text_file, write_text_file
from app.storage.transactions import commit_file_transaction
from app.storage.run_state import (
    action_key_for_project,
    schedule_provider_wait,
    set_run_intent,
)

CANON_CONTEXT_FILES = (
    "canon/characters.json",
    "canon/relationships.json",
    "canon/world_facts.json",
    "canon/foreshadowing.json",
)


@dataclass(frozen=True)
class HarnessRunContext:
    project_path: Path
    run_id: str


class HarnessOrchestrator:
    """Coordinates durable book, story-arc, and chapter loop actions."""

    def __init__(self, context: HarnessRunContext) -> None:
        self.context = context

    def advance_to_next_checkpoint(self) -> None:
        metadata = read_project_metadata(self.context.project_path)
        setup_state = read_setup_state(self.context.project_path)
        if not setup_state.approved:
            metadata.run_status = "waiting_for_user"
            write_project_metadata(self.context.project_path, metadata)
            self._emit(
                metadata,
                kind="book_setup_required",
                loop_layer="book",
                atomic_action="continue_book_discussion",
                status="completed",
                routing_decision="pause",
                message="Book setup must be approved before the harness can continue.",
            )
            return

        pending_book_revision = book_revision_storage.read_pending_book_revision(
            self.context.project_path
        )
        if pending_book_revision is not None:
            metadata.run_status = "waiting_for_user"
            write_project_metadata(self.context.project_path, metadata)
            self._emit(
                metadata,
                kind="book_revision_approval_required",
                loop_layer="book",
                atomic_action="approve_book_revision",
                status="requested",
                artifact_path=pending_book_revision.candidate.direction_path,
                routing_decision="await_user_approval",
                message=(
                    "The evaluated Book revision still requires explicit user approval; "
                    "full-auto mode cannot bypass this gate."
                ),
                payload={
                    "revision_id": pending_book_revision.revision_id,
                    "base_book_version": pending_book_revision.base_book_version,
                },
            )
            return

        profile = get_active_profile()
        if profile is None:
            metadata.run_status = "waiting_for_user"
            write_project_metadata(self.context.project_path, metadata)
            self._emit(
                metadata,
                kind="llm_profile_required",
                loop_layer="system",
                atomic_action="select_llm_profile",
                status="completed",
                routing_decision="pause",
                message="An enabled LLM profile is required before generation can start.",
            )
            return

        metadata.active_profile_id = profile.id
        try:
            if self._process_pending_cross_loop_route(metadata):
                return

            if self._process_approved_book_revision(metadata, profile):
                return

            if self._process_pending_feedback(metadata):
                return

            if metadata.active_arc_id is None:
                metadata.active_arc_id = _next_arc_id(self.context.project_path)
                write_project_metadata(self.context.project_path, metadata)

            active_arc_path = (
                self.context.project_path / "arcs" / metadata.active_arc_id
            )
            if not (active_arc_path / "plan.md").exists() and not (
                active_arc_path / "state.json"
            ).exists():
                self._plan_initial_story_arc(metadata)
                return

            self._advance_chapter_loop(metadata)
        except Exception as exc:
            safe_error = redact_profile_secrets(str(exc), profile)
            provider_failure = is_retryable_provider_error(exc)
            if provider_failure:
                action_key = action_key_for_project(self.context.project_path)
                wait = schedule_provider_wait(
                    self.context.project_path,
                    action_key=action_key,
                    message=safe_error,
                )
                metadata.run_status = "waiting_for_provider"
                write_project_metadata(self.context.project_path, metadata)
                self._emit(
                    metadata,
                    kind="run_waiting_for_provider",
                    loop_layer="system",
                    atomic_action=action_key,
                    status="requested",
                    routing_decision="retry_automatically",
                    message=(
                        "Model provider connection was interrupted. NovelPilot preserved "
                        "the current candidate and will retry automatically."
                    ),
                    payload={
                        "category": "transport_provider",
                        "code": "provider_retry_scheduled",
                        "attempt": wait.attempt,
                        "next_wake_at": wait.next_wake_at.isoformat(),
                    },
                )
                return
            metadata.run_status = "failed"
            write_project_metadata(self.context.project_path, metadata)
            set_run_intent(self.context.project_path, desired_state="stopped")
            failure_category, failure_code = _non_retryable_failure_kind(safe_error)
            self._emit(
                metadata,
                kind="run_failed",
                loop_layer="system",
                atomic_action="advance_to_next_checkpoint",
                status="failed",
                routing_decision="pause",
                message=f"Harness run failed: {safe_error}",
                payload={
                    "category": failure_category,
                    "code": failure_code,
                },
            )

    def _advance_chapter_loop(self, metadata: ProjectMetadata) -> None:
        profile = get_active_profile()
        if profile is None:
            raise ValueError("Missing active profile.")

        if self._current_arc_requires_human_review(metadata):
            metadata.run_status = "waiting_for_user"
            write_project_metadata(self.context.project_path, metadata)
            self._emit(
                metadata,
                kind="story_arc_review_required",
                loop_layer="story_arc",
                atomic_action="review_current_arc",
                status="completed",
                artifact_path=f"arcs/{metadata.active_arc_id}/plan.md",
                routing_decision="pause",
                message="Participatory mode requires approval of the current story arc plan.",
            )
            return

        chapter_id = metadata.active_chapter_id or _next_chapter_id(self.context.project_path)
        metadata.active_chapter_id = chapter_id
        chapter_path = self.context.project_path / "chapters" / chapter_id
        chapter_path.mkdir(parents=True, exist_ok=True)
        write_project_metadata(self.context.project_path, metadata)

        if not (chapter_path / "context_snapshot.json").exists():
            self._write_context_snapshot(metadata, chapter_id, chapter_path)
            return
        if not (chapter_path / "verification.json").exists():
            self._run_chapter_agent(profile, metadata, chapter_id, chapter_path)
            return
        if not (chapter_path / "final.md").exists():
            self._write_final_chapter(metadata, chapter_id, chapter_path)
            return
        if (chapter_path / "state_patch_rejection.json").exists():
            self._repair_state_patch_evidence(
                profile,
                metadata,
                chapter_id,
                chapter_path,
            )
            return
        if not (chapter_path / "committed_state_patch.json").exists():
            self._commit_state_patch(metadata, chapter_id, chapter_path)
            return

        arc_state = arc_storage.record_chapter_committed(self.context.project_path, chapter_id)
        metadata.active_chapter_id = None
        if arc_state is not None and arc_state.status == "completed":
            metadata.active_arc_id = None
            message = f"{chapter_id} completed {arc_state.arc_id}; next checkpoint will plan a new arc."
            routing_decision = "plan_next_arc"
        else:
            message = f"{chapter_id} is complete; next checkpoint will start the next chapter."
            routing_decision = "continue"
        paused = self._set_status_after_checkpoint(metadata, "idle")
        write_project_metadata(self.context.project_path, metadata)
        self._emit(
            metadata,
            kind="safe_checkpoint_reached",
            loop_layer="chapter",
            atomic_action="chapter_complete",
            status="completed",
            routing_decision=routing_decision,
            message=message,
            payload={
                "chapter_id": chapter_id,
                "active_arc_id": metadata.active_arc_id,
                "completed_chapter_ids": (
                    arc_state.completed_chapter_ids if arc_state is not None else []
                ),
            },
        )
        if paused:
            self._emit_paused(metadata, f"Safe checkpoint reached after completing {chapter_id}.")

    def _plan_initial_story_arc(self, metadata: ProjectMetadata) -> None:
        profile = get_active_profile()
        if profile is None:
            raise ValueError("Missing active profile.")

        arc_id = metadata.active_arc_id or _next_arc_id(self.context.project_path)
        metadata.active_arc_id = arc_id
        write_project_metadata(self.context.project_path, metadata)
        arc_path = self.context.project_path / "arcs" / arc_id
        arc_path.mkdir(parents=True, exist_ok=True)

        self._emit(
            metadata,
            kind="atomic_action_started",
            loop_layer="story_arc",
            atomic_action="plan_current_arc",
            status="started",
            message="Planning the current rolling story arc.",
        )

        settings = _read_text(self.context.project_path / "book" / "settings.md")
        rolling_contract = _read_text(self.context.project_path / "book" / "outline.md")
        book_state = read_json(self.context.project_path / "book" / "state.json", default={})
        canon_summary = _read_canon_summary(self.context.project_path)
        feedback_block = self._feedback_prompt_block(
            {"revise_current_arc_plan", "escalate_to_book_loop"}
        )
        book_feedback = _read_text(self.context.project_path / "book" / "feedback.md")
        prompt = "\n\n".join(
            _without_empty(
                [
                    "Create the first rolling story arc plan for this novel.",
                    "Do not plan the full book. Plan only the current arc from committed state.",
                    "Submit the complete plan with the Story Arc candidate Tool. "
                    "The Markdown plan must include arc goal, conflicts, "
                    "chapter direction, pacing signal, foreshadowing movement, and stop "
                    "conditions. target_chapter_count must be an integer from 1 through 30 "
                    "chosen from the approved rolling contract and current pacing needs.",
                    f"Book settings:\n{settings}",
                    f"Approved rolling story arc contract:\n{rolling_contract}",
                    f"Book state:\n{book_state}",
                    f"Canon summary:\n{canon_summary}",
                    f"Book feedback memo:\n{book_feedback}",
                    feedback_block,
                ]
            )
        )
        policy = self._resolve_agent_policy(metadata, "story_arc", profile)
        stream_callback = self._agent_stream_callback(
            metadata,
            "story_arc",
            "plan_current_arc",
        )
        try:
            agent_result = recover_completed_story_arc_agent(
                self.context.project_path,
                metadata,
                policy,
                arc_id=arc_id,
                intent="create",
                expected_revision=0,
                on_event=self._agent_event_callback(
                    metadata,
                    "story_arc",
                    "plan_current_arc",
                ),
            )
            if agent_result is None:
                agent_result = run_story_arc_agent(
                    self.context.project_path,
                    metadata,
                    policy,
                    arc_id=arc_id,
                    intent="create",
                    expected_revision=0,
                    instruction=prompt,
                    candidate_run_id=self._story_arc_resume_candidate_run_id(arc_path),
                    on_event=self._agent_event_callback(
                        metadata,
                        "story_arc",
                        "plan_current_arc",
                    ),
                    on_text_delta=stream_callback,
                    on_tool_event=stream_callback,
                )
        except AgentControlCheckpoint as checkpoint:
            self._handle_agent_control_checkpoint(
                metadata,
                checkpoint,
                loop_layer="story_arc",
                action="plan_current_arc",
            )
            return
        if agent_result.evaluation.result.outcome != "pass":
            if self._handle_evaluation_control(
                metadata,
                agent_result.evaluation,
                loop_layer="story_arc",
                action="plan_current_arc",
                candidate_run_id=agent_result.run_result.candidate_run_id,
            ):
                return
            raise ValueError(
                "Story Arc candidate did not pass evaluation: "
                + agent_result.evaluation.result.summary
            )
        proposal = agent_result.proposal
        self._emit_model_output(
            metadata,
            "story_arc",
            "plan_current_arc",
            proposal.plan_markdown,
        )
        result = ChatResult(
            content="",
            model_snapshot=(
                agent_result.run_result.model_snapshot or policy.profile.model
            ),
            provider_snapshot=(
                agent_result.run_result.provider_snapshot or policy.profile.protocol
            ),
            usage=agent_result.run_result.usage,
        )
        review_root = Path("arcs") / arc_id / "reviews" / "review-0001"
        evaluation_files = evaluation_view_files(
            agent_result.evaluation,
            evaluation_path=(review_root / "evaluation.json").as_posix(),
            review_path=(review_root / "review.md").as_posix(),
            verification_path=(review_root / "verification.json").as_posix(),
        )
        arc_state = {
                "schema_version": 1,
                "version": 1,
                "arc_id": arc_id,
                "status": "planned",
                "profile_id": profile.id,
                "model_snapshot": result.model_snapshot,
                "provider_snapshot": result.provider_snapshot,
                "plan_path": f"arcs/{arc_id}/plan.md",
                "human_review": (
                    "awaiting_review"
                    if metadata.operation_mode == "participatory"
                    else "not_required"
                ),
                "approved_at": None,
                "recommended_target_chapter_count": proposal.target_chapter_count,
                "target_chapter_count": proposal.target_chapter_count,
                "completed_chapter_ids": [],
                "completed_at": None,
            }
        commit_file_transaction(
            self.context.project_path,
            kind=f"story-arc-candidate-{arc_id}-0001",
            files={
                f"arcs/{arc_id}/plan.md": proposal.plan_markdown.strip() + "\n",
                f"arcs/{arc_id}/state.json": (
                    json.dumps(arc_state, ensure_ascii=False, indent=2) + "\n"
                ),
                **evaluation_files,
            },
        )
        (arc_path / "book-upstream-resume.json").unlink(missing_ok=True)
        metadata.active_arc_id = arc_id
        status_after_checkpoint: RunStatus = (
            "waiting_for_user" if metadata.operation_mode == "participatory" else "idle"
        )
        paused = self._set_status_after_checkpoint(metadata, status_after_checkpoint)
        write_project_metadata(self.context.project_path, metadata)
        self._emit(
            metadata,
            kind="artifact_written",
            loop_layer="story_arc",
            atomic_action="plan_current_arc",
            status="completed",
            artifact_path=f"arcs/{arc_id}/plan.md",
            routing_decision=(
                "pause" if metadata.operation_mode == "participatory" else "continue"
            ),
            message=(
                "Story arc plan is ready for human review."
                if metadata.operation_mode == "participatory"
                else "Story arc plan is ready; next checkpoint is chapter planning."
            ),
            payload={
                "profile_id": profile.id,
                "model_snapshot": result.model_snapshot,
                "recommended_target_chapter_count": proposal.target_chapter_count,
            },
        )
        if paused:
            self._emit_paused(metadata, f"Safe checkpoint reached after planning {arc_id}.")

    def _write_context_snapshot(
        self,
        metadata: ProjectMetadata,
        chapter_id: str,
        chapter_path: Path,
    ) -> None:
        self._emit(
            metadata,
            kind="atomic_action_started",
            loop_layer="chapter",
            atomic_action="assemble_context",
            status="started",
            message=f"Assembling controlled context for {chapter_id}.",
        )
        arc_id = metadata.active_arc_id or "arc-001"
        snapshot = ContextSnapshot(
            chapter_id=chapter_id,
            created_at=datetime.now(UTC).isoformat(),
            sources=[
                ContextSource(
                    id="book-settings",
                    path="book/settings.md",
                    usage="direct",
                    included_fields=["full_text"],
                ),
                ContextSource(
                    id="book-rolling-contract",
                    path="book/outline.md",
                    usage="direct",
                    included_fields=["full_text"],
                ),
                ContextSource(
                    id="book-state",
                    path="book/state.json",
                    version=_state_version(self.context.project_path / "book" / "state.json"),
                    usage="direct",
                    included_fields=[
                        "book_direction_version",
                        "confirmed_decisions",
                        "must_preserve",
                        "must_avoid",
                        "creative_freedoms",
                        "open_decisions",
                        "current_strategy",
                    ],
                ),
                ContextSource(
                    id="book-constraints",
                    path="book/constraints.json",
                    usage="direct",
                    included_fields=[
                        "confirmed",
                        "must_preserve",
                        "must_avoid",
                        "creative_freedoms",
                        "open_decisions",
                    ],
                ),
                ContextSource(
                    id="current-arc-plan",
                    path=f"arcs/{arc_id}/plan.md",
                    usage="direct",
                    included_fields=["full_text"],
                ),
                ContextSource(
                    id="current-arc-state",
                    path=f"arcs/{arc_id}/state.json",
                    version=_state_version(
                        self.context.project_path / "arcs" / arc_id / "state.json"
                    ),
                    usage="direct",
                    included_fields=[
                        "version",
                        "status",
                        "target_chapter_count",
                        "completed_chapter_ids",
                    ],
                ),
                *self._canon_context_sources(),
                *self._prior_chapter_context_sources(chapter_id),
                *self._book_feedback_context_sources(),
                *self._feedback_context_sources(),
            ],
            excluded=_context_exclusions(chapter_id),
            assembly_rationale=(
                "Use approved book direction, the current rolling arc, committed canon versions, "
                "and summaries of prior committed chapters. Exclude current-chapter candidate "
                "materials and unwritten future arcs so the model sees only audited context for "
                "this checkpoint."
            ),
        )
        write_json(chapter_path / "context_snapshot.json", snapshot.model_dump(mode="json"))
        self._finish_artifact_step(
            metadata,
            kind="artifact_written",
            atomic_action="assemble_context",
            artifact_path=f"chapters/{chapter_id}/context_snapshot.json",
            message=f"Context snapshot written for {chapter_id}.",
        )

    def _process_pending_feedback(self, metadata: ProjectMetadata) -> bool:
        events = read_events(self.context.project_path)
        processed_ids = {
            event.payload.get("source_event_id")
            for event in events
            if event.kind == "feedback_processed"
        }
        pending = [
            event
            for event in events
            if event.kind == "user_feedback" and event.event_id not in processed_ids
        ]
        if not pending:
            return False

        feedback_event = pending[0]
        feedback = str(feedback_event.payload.get("feedback", "")).strip()
        routing_decision = _route_feedback(metadata, feedback)
        if routing_decision == "revise_current_arc_plan":
            profile = get_active_profile()
            if profile is None:
                raise ValueError("Missing active profile.")
            artifact_path = self._revise_current_arc_plan_from_feedback(
                profile,
                metadata,
                feedback,
            )
        elif routing_decision == "escalate_to_book_loop":
            profile = get_active_profile()
            if profile is None:
                raise ValueError("Missing active profile.")
            artifact_path = self._record_book_feedback(
                profile,
                metadata,
                feedback,
            )
        else:
            artifact_path = self._upsert_feedback_context_source(
                metadata,
                routing_decision,
                feedback,
            )
        self._emit_feedback_processed(
            metadata,
            source_event_id=feedback_event.event_id,
            feedback=feedback,
            routing_decision=routing_decision,
            artifact_path=artifact_path,
        )
        return True

    def _emit_feedback_processed(
        self,
        metadata: ProjectMetadata,
        *,
        source_event_id: str,
        feedback: str,
        routing_decision: str,
        artifact_path: str | None,
    ) -> None:
        self._emit(
            metadata,
            kind="feedback_processed",
            loop_layer=_loop_layer_for_feedback_route(routing_decision),
            atomic_action="process_user_feedback",
            status="completed",
            artifact_path=artifact_path,
            routing_decision=routing_decision,
            message=f"User feedback routed at safe checkpoint: {routing_decision}.",
            payload={
                "source_event_id": source_event_id,
                "feedback": feedback,
                "active_arc_id": metadata.active_arc_id,
                "active_chapter_id": metadata.active_chapter_id,
            },
        )

    def _revise_current_arc_plan_from_feedback(
        self,
        profile: LlmProfile,
        metadata: ProjectMetadata,
        feedback: str,
        *,
        source_label: str = "User Feedback",
    ) -> str | None:
        if metadata.active_arc_id is None:
            return None

        arc_id = metadata.active_arc_id
        arc_path = self.context.project_path / "arcs" / arc_id
        plan_path = arc_path / "plan.md"
        if not plan_path.exists():
            return None

        self._emit(
            metadata,
            kind="atomic_action_started",
            loop_layer="story_arc",
            atomic_action="revise_current_arc_plan",
            status="started",
            message=f"Revising {arc_id} from {source_label.lower()}.",
        )
        current_plan = _read_text(plan_path)
        prompt = "\n\n".join(
            _without_empty(
                [
                    "Revise the current rolling story arc plan using the user feedback.",
                    "Keep the plan current-arc-only. Preserve useful constraints, but update pacing, "
                    "focus, or chapter direction where the feedback requires it.",
                    "Submit the complete revised Markdown and target chapter count through the "
                    "Story Arc candidate Tool.",
                    f"Revision request source: {source_label}\n{feedback}",
                    f"Current arc plan:\n{current_plan}",
                    f"Book settings:\n{_read_text(self.context.project_path / 'book' / 'settings.md')}",
                    "Approved rolling story arc contract:\n"
                    + _read_text(self.context.project_path / "book" / "outline.md"),
                    f"Canon summary:\n{_read_canon_summary(self.context.project_path)}",
                ]
            )
        )
        state_payload = read_json(arc_path / "state.json", default={})
        expected_revision = (
            state_payload.get("version", 1)
            if isinstance(state_payload, dict)
            and isinstance(state_payload.get("version", 1), int)
            else 1
        )
        policy = self._resolve_agent_policy(metadata, "story_arc", profile)
        stream_callback = self._agent_stream_callback(
            metadata,
            "story_arc",
            "revise_current_arc_plan",
        )
        try:
            agent_result = recover_completed_story_arc_agent(
                self.context.project_path,
                metadata,
                policy,
                arc_id=arc_id,
                intent="revise",
                expected_revision=expected_revision,
                on_event=self._agent_event_callback(
                    metadata,
                    "story_arc",
                    "revise_current_arc_plan",
                ),
            )
            if agent_result is None:
                agent_result = run_story_arc_agent(
                    self.context.project_path,
                    metadata,
                    policy,
                    arc_id=arc_id,
                    intent="revise",
                    expected_revision=expected_revision,
                    instruction=prompt,
                    candidate_run_id=self._story_arc_resume_candidate_run_id(arc_path),
                    on_event=self._agent_event_callback(
                        metadata,
                        "story_arc",
                        "revise_current_arc_plan",
                    ),
                    on_text_delta=stream_callback,
                    on_tool_event=stream_callback,
                )
        except AgentControlCheckpoint as checkpoint:
            self._handle_agent_control_checkpoint(
                metadata,
                checkpoint,
                loop_layer="story_arc",
                action="revise_current_arc_plan",
            )
            return None
        if agent_result.evaluation.result.outcome != "pass":
            if self._handle_evaluation_control(
                metadata,
                agent_result.evaluation,
                loop_layer="story_arc",
                action="revise_current_arc_plan",
                candidate_run_id=agent_result.run_result.candidate_run_id,
            ):
                return None
            raise ValueError(
                "Revised Story Arc candidate did not pass evaluation: "
                + agent_result.evaluation.result.summary
            )
        proposal = agent_result.proposal
        self._emit_model_output(
            metadata,
            "story_arc",
            "revise_current_arc_plan",
            proposal.plan_markdown,
        )
        result = ChatResult(
            content="",
            model_snapshot=(
                agent_result.run_result.model_snapshot or policy.profile.model
            ),
            provider_snapshot=(
                agent_result.run_result.provider_snapshot or policy.profile.protocol
            ),
            usage=agent_result.run_result.usage,
        )
        review_root = (
            Path("arcs")
            / arc_id
            / "reviews"
            / f"review-{expected_revision + 1:04d}"
        )
        evaluation_files = evaluation_view_files(
            agent_result.evaluation,
            evaluation_path=(review_root / "evaluation.json").as_posix(),
            review_path=(review_root / "review.md").as_posix(),
            verification_path=(review_root / "verification.json").as_posix(),
        )
        revised_plan = proposal.plan_markdown.strip() + "\n"
        revision_document = (
            "\n\n".join(
                [
                    "# Arc Revision",
                    f"## Revision Source\n{source_label}",
                    f"## Revision Request\n{feedback}",
                    f"## Revised Plan\n{proposal.plan_markdown.strip()}",
                ]
            )
            + "\n"
        )
        arc_state = self._arc_revision_state_payload(
            metadata,
            result,
            target_chapter_count=proposal.target_chapter_count,
        )
        commit_file_transaction(
            self.context.project_path,
            kind=f"story-arc-revision-{arc_id}-{expected_revision + 1:04d}",
            files={
                f"arcs/{arc_id}/plan.md": revised_plan,
                f"arcs/{arc_id}/revision.md": revision_document,
                f"arcs/{arc_id}/state.json": (
                    json.dumps(arc_state, ensure_ascii=False, indent=2) + "\n"
                ),
                **evaluation_files,
            },
        )
        (arc_path / "book-upstream-resume.json").unlink(missing_ok=True)
        self._finish_feedback_artifact_step(
            metadata,
            loop_layer="story_arc",
            atomic_action="revise_current_arc_plan",
            artifact_path=f"arcs/{arc_id}/plan.md",
            message=f"{arc_id} plan revised from {source_label.lower()}.",
            routing_decision=(
                "pause" if metadata.operation_mode == "participatory" else "continue"
            ),
            payload=_llm_usage_payload(
                profile,
                result,
                {"revision_path": f"arcs/{arc_id}/revision.md"},
            ),
        )
        return f"arcs/{arc_id}/plan.md"

    def _record_book_feedback(
        self,
        profile: LlmProfile,
        metadata: ProjectMetadata,
        feedback: str,
    ) -> str:
        self._emit(
            metadata,
            kind="atomic_action_started",
            loop_layer="book",
            atomic_action="record_book_feedback",
            status="started",
            message="Recording book-level user feedback.",
        )
        existing_feedback = _read_text(self.context.project_path / "book" / "feedback.md")
        result = self._call_feedback_llm_action(
            profile,
            metadata,
            loop_layer="book",
            action="record_book_feedback",
            system=(
                "You are Novelpilot's book loop. Turn user feedback into a concise, visible "
                "long-term direction memo. Do not overwrite approved settings."
            ),
            user="\n\n".join(
                _without_empty(
                    [
                        "Summarize how this feedback should influence future story arcs and "
                        "chapters. Keep it as guidance, not a direct settings rewrite.",
                        f"User feedback:\n{feedback}",
                        f"Approved book settings:\n{_read_text(self.context.project_path / 'book' / 'settings.md')}",
                        f"Existing book feedback memo:\n{existing_feedback}",
                    ]
                )
            ),
        )
        feedback_path = self.context.project_path / "book" / "feedback.md"
        prior = existing_feedback.rstrip()
        entry = "\n\n".join(
            [
                f"## {datetime.now(UTC).isoformat()}",
                f"User feedback: {feedback}",
                result.content.strip(),
            ]
        )
        write_text_file(
            feedback_path,
            f"{prior}\n\n{entry}\n".lstrip() if prior else f"# Book Feedback\n\n{entry}\n",
        )
        self._bump_book_feedback_state()
        self._finish_feedback_artifact_step(
            metadata,
            loop_layer="book",
            atomic_action="record_book_feedback",
            artifact_path="book/feedback.md",
            message="Book-level feedback memo recorded.",
            routing_decision="continue",
            payload=_llm_usage_payload(profile, result),
        )
        return "book/feedback.md"

    def _upsert_feedback_context_source(
        self,
        metadata: ProjectMetadata,
        routing_decision: str,
        feedback: str,
    ) -> str | None:
        if metadata.active_chapter_id is None:
            return None

        snapshot_path = (
            self.context.project_path
            / "chapters"
            / metadata.active_chapter_id
            / "context_snapshot.json"
        )
        if not snapshot_path.exists():
            return None

        snapshot = read_json(snapshot_path, default={})
        existing_sources = snapshot.get("sources", [])
        sources = existing_sources if isinstance(existing_sources, list) else []
        sources = [
            source
            for source in sources
            if isinstance(source, dict) and source.get("id") != "processed-user-feedback"
        ]
        processed_feedback = [
            (event.routing_decision, str(event.payload.get("feedback", "")))
            for event in read_events(self.context.project_path)
            if event.kind == "feedback_processed"
        ]
        processed_feedback.append((routing_decision, feedback))
        summary = "; ".join(f"{route}: {text}" for route, text in processed_feedback[-5:])
        sources.append(
            ContextSource(
                id="processed-user-feedback",
                path="events.jsonl",
                usage="summary",
                summary=summary,
            ).model_dump(mode="json")
        )
        snapshot["sources"] = sources
        rationale = str(snapshot.get("assembly_rationale", "")).strip()
        overlay_note = (
            "Processed user feedback may be overlaid after initial context assembly at safe "
            "checkpoints."
        )
        snapshot["assembly_rationale"] = (
            f"{rationale} {overlay_note}".strip() if overlay_note not in rationale else rationale
        )
        write_json(snapshot_path, snapshot)
        return f"chapters/{metadata.active_chapter_id}/context_snapshot.json"

    def _current_arc_requires_human_review(self, metadata: ProjectMetadata) -> bool:
        if metadata.active_arc_id is None:
            return False
        arc_state = arc_storage.read_current_arc_state(self.context.project_path)
        if arc_state is None:
            return metadata.operation_mode == "participatory"
        if arc_state.human_review == "awaiting_review":
            return True
        return (
            metadata.operation_mode == "participatory"
            and arc_state.human_review != "approved"
        )

    def _feedback_context_sources(self) -> list[ContextSource]:
        processed_feedback = [
            event
            for event in read_events(self.context.project_path)
            if event.kind == "feedback_processed"
        ]
        if not processed_feedback:
            return []
        summary = "; ".join(
            f"{event.routing_decision}: {event.payload.get('feedback', '')}"
            for event in processed_feedback[-5:]
        )
        return [
            ContextSource(
                id="processed-user-feedback",
                path="events.jsonl",
                usage="summary",
                summary=summary,
            )
        ]

    def _book_feedback_context_sources(self) -> list[ContextSource]:
        feedback_text = _read_text(self.context.project_path / "book" / "feedback.md").strip()
        if not feedback_text:
            return []
        return [
            ContextSource(
                id="book-feedback",
                path="book/feedback.md",
                usage="summary",
                summary=feedback_text[-800:],
            )
        ]

    def _canon_context_sources(self) -> list[ContextSource]:
        sources: list[ContextSource] = []
        for relative_path in CANON_CONTEXT_FILES:
            payload = read_json(self.context.project_path / relative_path, default={})
            state = payload if isinstance(payload, dict) else {}
            items = state.get("items")
            item_count = len(items) if isinstance(items, dict) else 0
            version = state.get("version")
            source_id = (
                "canon-"
                + relative_path.removeprefix("canon/").removesuffix(".json").replace("_", "-")
            )
            sources.append(
                ContextSource(
                    id=source_id,
                    path=relative_path,
                    version=version if isinstance(version, int) else None,
                    usage="summary",
                    included_fields=["version", "item_count"],
                    summary=f"{relative_path} has {item_count} committed item(s).",
                )
            )
        return sources

    def _prior_chapter_context_sources(self, chapter_id: str) -> list[ContextSource]:
        current_number = _chapter_number(chapter_id)
        if current_number is None:
            return []
        chapters_path = self.context.project_path / "chapters"
        if not chapters_path.exists():
            return []

        summaries: list[str] = []
        for chapter_path in sorted(chapters_path.iterdir(), key=lambda path: path.name):
            if not chapter_path.is_dir():
                continue
            prior_number = _chapter_number(chapter_path.name)
            if prior_number is None or prior_number >= current_number:
                continue
            final_path = chapter_path / "final.md"
            if not final_path.exists():
                continue
            final_text = read_text_file(final_path)
            heading = _markdown_heading(final_text)
            summary = heading if heading else "committed final without Markdown heading"
            summaries.append(f"{chapter_path.name} ({len(final_text)} chars): {summary}")

        if not summaries:
            return []
        return [
            ContextSource(
                id="prior-committed-chapters",
                path="chapters/*/final.md",
                usage="summary",
                included_fields=["chapter_id", "final_path", "heading", "character_count"],
                summary="; ".join(summaries[-8:]),
            )
        ]

    def _assembled_context_block(self, snapshot_path: Path) -> str:
        payload = read_json(snapshot_path, default={})
        try:
            snapshot = ContextSnapshot.model_validate(payload)
        except ValueError:
            return "Context snapshot could not be loaded; use other provided chapter inputs only."

        lines = [
            f"Chapter: {snapshot.chapter_id}",
            f"Assembly rationale: {snapshot.assembly_rationale}",
            "Included sources:",
        ]
        for source in snapshot.sources:
            version = f", version={source.version}" if source.version is not None else ""
            lines.append(f"- {source.id} [{source.usage}] path={source.path}{version}")
            if source.usage == "direct":
                lines.append(_indent_block(self._direct_context_source_content(source)))
            elif source.summary:
                lines.append(_indent_block(f"Summary: {source.summary}"))

        if snapshot.excluded:
            lines.append("Excluded sources:")
            for exclusion in snapshot.excluded:
                lines.append(f"- {exclusion.source}: {exclusion.reason}")
        return "\n".join(lines)

    def _direct_context_source_content(self, source: ContextSource) -> str:
        try:
            path = resolve_artifact_path(self.context.project_path, source.path)
        except ValueError as exc:
            return f"[invalid context source path: {exc}]"
        if not path.exists():
            return "[missing context source]"
        if source.included_fields == ["full_text"]:
            return read_text_file(path).strip() or "[empty context source]"

        payload = read_json(path, default=None)
        if isinstance(payload, dict):
            selected = {
                field: payload[field]
                for field in source.included_fields
                if field in payload
            }
            if selected:
                return json.dumps(selected, ensure_ascii=False, indent=2)
            return "[no selected fields present]"
        return read_text_file(path).strip() or "[empty context source]"

    def _feedback_prompt_block(self, routing_decisions: set[str]) -> str:
        processed_feedback = [
            event
            for event in read_events(self.context.project_path)
            if event.kind == "feedback_processed" and event.routing_decision in routing_decisions
        ]
        if not processed_feedback:
            return ""

        lines = [
            "- "
            + str(event.payload.get("feedback", "")).strip()
            + f" [route: {event.routing_decision}]"
            for event in processed_feedback[-5:]
            if str(event.payload.get("feedback", "")).strip()
        ]
        if not lines:
            return ""
        return "\n".join(["User checkpoint feedback to apply after the last atomic action:", *lines])

    def _arc_revision_state_payload(
        self,
        metadata: ProjectMetadata,
        result: ChatResult,
        *,
        target_chapter_count: int,
    ) -> dict[str, object]:
        if metadata.active_arc_id is None:
            raise ValueError("Cannot revise an absent Story Arc.")
        state_path = self.context.project_path / "arcs" / metadata.active_arc_id / "state.json"
        payload = read_json(state_path, default={})
        state = payload if isinstance(payload, dict) else {}
        version = state.get("version")
        state["schema_version"] = 1
        state["version"] = (version if isinstance(version, int) else 1) + 1
        state["arc_id"] = metadata.active_arc_id
        state["status"] = "revised"
        state["plan_path"] = f"arcs/{metadata.active_arc_id}/plan.md"
        state["revision_path"] = f"arcs/{metadata.active_arc_id}/revision.md"
        state["model_snapshot"] = result.model_snapshot
        state["provider_snapshot"] = result.provider_snapshot
        state["recommended_target_chapter_count"] = target_chapter_count
        state["target_chapter_count"] = target_chapter_count
        state.setdefault("completed_chapter_ids", [])
        state.setdefault("completed_at", None)
        if metadata.operation_mode == "participatory":
            state["human_review"] = "awaiting_review"
            state["approved_at"] = None
        elif state.get("human_review") == "awaiting_review":
            # A mode change cannot silently clear a gate that was already presented.
            state["approved_at"] = None
        else:
            state["human_review"] = "not_required"
            state["approved_at"] = None
        return state

    def _run_chapter_agent(
        self,
        profile: LlmProfile,
        metadata: ProjectMetadata,
        chapter_id: str,
        chapter_path: Path,
    ) -> None:
        self._emit_started(
            metadata,
            "run_chapter_agent",
            f"Running bounded Chapter Agent for {chapter_id}.",
        )
        policy = self._resolve_agent_policy(metadata, "chapter", profile)
        instruction = "\n\n".join(
            [
                f"Create the complete candidate transaction for {chapter_id}.",
                "The draft should be visible chapter prose, while observations and state patch "
                "remain candidate-only until Harness promotion.",
                "Assembled context snapshot:\n"
                + self._assembled_context_block(chapter_path / "context_snapshot.json"),
                "Current Story Arc plan:\n"
                + _read_text(
                    self.context.project_path
                    / "arcs"
                    / (metadata.active_arc_id or "arc-001")
                    / "plan.md"
                ),
                self._feedback_prompt_block(
                    {
                        "apply_to_current_chapter_context",
                        "revise_current_arc_plan",
                        "escalate_to_book_loop",
                    }
                ),
            ]
        )
        stream_callback = self._agent_stream_callback(
            metadata,
            "chapter",
            "run_chapter_agent",
        )
        public_stream = ChapterDraftStreamProjector(
            chapter_id=chapter_id,
            project_path=self.context.project_path,
            emit=self._chapter_draft_event_callback(metadata),
        )

        def on_tool_event(chunk: ChatChunk) -> None:
            stream_callback(chunk)
            public_stream.observe(chunk)

        agent_event_callback = self._agent_event_callback(
            metadata,
            "chapter",
            "run_chapter_agent",
        )

        def on_agent_event(payload: dict[str, Any]) -> None:
            public_stream.observe_agent_event(payload)
            agent_event_callback(payload)

        resume_payload = read_json(chapter_path / "upstream-resume.json", default={})
        resume_candidate_run_id = (
            resume_payload.get("candidate_run_id")
            if isinstance(resume_payload, dict)
            and isinstance(resume_payload.get("candidate_run_id"), str)
            else None
        )
        if resume_candidate_run_id is None:
            prior_state = read_agent_state(
                self.context.project_path,
                AgentIdentity(
                    project_id=metadata.project_id,
                    role="chapter",
                    scope_id=chapter_id,
                ),
            )
            if prior_state.lifecycle == "failed":
                resume_candidate_run_id = prior_state.candidate_run_id
        try:
            agent_result = recover_completed_chapter_agent(
                self.context.project_path,
                metadata,
                policy,
                chapter_id=chapter_id,
                on_event=on_agent_event,
            )
            if agent_result is None:
                agent_result = run_chapter_agent(
                    self.context.project_path,
                    metadata,
                    policy,
                    chapter_id=chapter_id,
                    expected_revision=0,
                    instruction=instruction,
                    candidate_run_id=resume_candidate_run_id,
                    on_event=on_agent_event,
                    on_text_delta=stream_callback,
                    on_tool_event=on_tool_event,
                )
        except AgentControlCheckpoint as checkpoint:
            public_stream.discard_open("chapter_agent_control_checkpoint")
            self._handle_agent_control_checkpoint(
                metadata,
                checkpoint,
                loop_layer="chapter",
                action="run_chapter_agent",
            )
            return
        except Exception:
            public_stream.discard_open("chapter_agent_failed")
            raise
        (chapter_path / "upstream-resume.json").unlink(missing_ok=True)
        root = self.context.project_path / agent_result.candidate_root
        plan = _read_text(root / "plan.md")
        draft = _read_text(root / "draft.md")
        if not plan.strip() or not draft.strip():
            raise ValueError("Chapter Agent candidate is missing plan or draft content.")

        draft_path = f"chapters/{chapter_id}/draft.md"
        final_path = f"chapters/{chapter_id}/final.md"
        observations_path = f"chapters/{chapter_id}/observations.json"
        observations = agent_result.submission.observations.model_copy(
            update={"based_on": draft_path}
        )
        operations = [
            operation.model_copy(
                update={
                    "evidence": [
                        item.model_copy(update={"file": final_path})
                        for item in operation.evidence
                    ]
                }
            )
            for operation in agent_result.submission.state_patch.operations
        ]
        patch = agent_result.submission.state_patch.model_copy(
            update={
                "based_on": {
                    "chapter_final": final_path,
                    "observations": observations_path,
                },
                "operations": operations,
            }
        )
        candidate_run_id = agent_result.run_result.candidate_run_id
        if (
            agent_result.evaluation.candidate_run_id is not None
            and agent_result.evaluation.candidate_run_id != candidate_run_id
        ):
            raise ValueError(
                "Chapter candidate and evaluation belong to different candidate runs."
            )
        agent_state = read_agent_state(
            self.context.project_path,
            AgentIdentity(
                project_id=metadata.project_id,
                role="chapter",
                scope_id=chapter_id,
            ),
        )
        semantic_revisions = (
            agent_state.budgets.used_semantic_revisions
            if agent_state.candidate_run_id == candidate_run_id
            and agent_state.budgets is not None
            else 0
        )
        chain_revision = semantic_revisions + 1
        run_segment = _candidate_run_segment(candidate_run_id)
        revision_root = (
            f"chapters/{chapter_id}/runs/{run_segment}/r/{chain_revision:04d}"
        )
        revision_evaluation_path = f"{revision_root}/evaluation.json"
        revision_review_path = f"{revision_root}/review.md"
        revision_verification_path = f"{revision_root}/verification.json"
        candidate_document = {
            "schema_version": 1,
            "chapter_id": chapter_id,
            "candidate_run_id": candidate_run_id,
            "activation_id": agent_result.run_result.activation_id,
            "chain_revision": chain_revision,
            "source_candidate_root": agent_result.candidate_root,
            "candidate_revision": agent_result.submission.candidate_revision,
            "plan_revision": agent_result.submission.plan_revision,
            "draft_revision": agent_result.submission.draft_revision,
            "evaluation_id": agent_result.evaluation.evaluation_id,
            "evaluation_input_fingerprint": agent_result.evaluation.input_fingerprint,
            "revision_artifacts": {
                "candidate": f"{revision_root}/candidate.json",
                "evaluation": revision_evaluation_path,
                "review": revision_review_path,
                "verification": revision_verification_path,
            },
            "promotable": agent_result.verification.commit_allowed,
        }
        plan_document = plan.rstrip() + "\n"
        draft_document = draft.rstrip() + "\n"
        observations_document = json.dumps(
            observations.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
        ) + "\n"
        patch_document = json.dumps(
            patch.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
        ) + "\n"
        candidate_json = json.dumps(
            candidate_document,
            ensure_ascii=False,
            indent=2,
        ) + "\n"
        verification_payload = agent_result.verification.model_dump(mode="json")
        evaluation_files = {
            **evaluation_view_files(
                agent_result.evaluation,
                evaluation_path=revision_evaluation_path,
                review_path=revision_review_path,
                verification_path=revision_verification_path,
                verification_payload=verification_payload,
            ),
            **evaluation_view_files(
                agent_result.evaluation,
                evaluation_path=f"chapters/{chapter_id}/evaluation.json",
                review_path=f"chapters/{chapter_id}/review.md",
                verification_path=f"chapters/{chapter_id}/verification.json",
                verification_payload=verification_payload,
            ),
        }
        commit_file_transaction(
            self.context.project_path,
            kind=f"chapter-agent-candidate-{chapter_id}",
            files={
                f"chapters/{chapter_id}/goal.md": plan_document,
                draft_path: draft_document,
                observations_path: observations_document,
                f"chapters/{chapter_id}/candidate_state_patch.json": patch_document,
                f"chapters/{chapter_id}/agent_candidate.json": candidate_json,
                f"{revision_root}/goal.md": plan_document,
                f"{revision_root}/draft.md": draft_document,
                f"{revision_root}/observations.json": observations_document,
                f"{revision_root}/candidate_state_patch.json": patch_document,
                f"{revision_root}/candidate.json": candidate_json,
                **evaluation_files,
            },
        )
        self._finish_artifact_step(
            metadata,
            kind="verification_completed",
            atomic_action="run_chapter_agent",
            artifact_path=f"chapters/{chapter_id}/verification.json",
            message=f"Chapter Agent candidate evaluated for {chapter_id}.",
            routing_decision=agent_result.verification.routing_decision,
            payload={
                "profile_id": policy.profile.id,
                "model_snapshot": agent_result.run_result.model_snapshot,
                "evaluation_id": agent_result.evaluation.evaluation_id,
                "candidate_root": agent_result.candidate_root,
            },
        )
        self._handle_evaluation_control(
            metadata,
            agent_result.evaluation,
            loop_layer="chapter",
            action="run_chapter_agent",
            candidate_run_id=agent_result.run_result.candidate_run_id,
        )

    def _agent_stream_callback(
        self,
        metadata: ProjectMetadata,
        loop_layer: LoopLayer,
        action: str,
    ) -> Callable[[ChatChunk], None]:
        progress = StreamProgressAccumulator()

        def on_delta(chunk: ChatChunk) -> None:
            received_characters = progress.observe(chunk)
            if received_characters is None:
                return
            self._emit_model_stream_progress(
                metadata,
                loop_layer,
                action,
                received_characters,
            )

        return on_delta

    def _agent_event_callback(
        self,
        metadata: ProjectMetadata,
        loop_layer: LoopLayer,
        action: str,
    ) -> Callable[[dict[str, Any]], None]:
        def on_event(payload: dict[str, Any]) -> None:
            projected = project_agent_event(payload)
            if projected is None:
                return
            self._emit(
                metadata,
                kind=projected.kind,
                loop_layer=loop_layer,
                atomic_action=action,
                status=projected.status,
                artifact_path=projected.artifact_path,
                routing_decision=projected.routing_decision,
                message=projected.message,
                payload=projected.payload,
            )

        return on_event

    def _chapter_draft_event_callback(
        self,
        metadata: ProjectMetadata,
    ) -> Callable[[str, dict[str, object]], None]:
        messages = {
            "chapter_draft_stream_started": "Chapter draft streaming started.",
            "chapter_draft_delta": "Chapter draft prose received.",
            "chapter_draft_stream_committed": (
                "Chapter draft stream reconciled with its candidate artifact."
            ),
            "chapter_draft_stream_discarded": (
                "Chapter draft stream was discarded after a rejected generation step."
            ),
        }

        def on_event(kind: str, payload: dict[str, object]) -> None:
            status: EventStatus = (
                "failed" if kind == "chapter_draft_stream_discarded" else
                "completed" if kind == "chapter_draft_stream_committed" else
                "delta" if kind == "chapter_draft_delta" else
                "started"
            )
            artifact_path = payload.get("artifact_path")
            self._emit(
                metadata,
                kind=kind,
                loop_layer="chapter",
                atomic_action="run_chapter_agent",
                status=status,
                artifact_path=(artifact_path if isinstance(artifact_path, str) else None),
                message=messages[kind],
                payload=payload,
            )

        return on_event

    def _handle_agent_control_checkpoint(
        self,
        metadata: ProjectMetadata,
        checkpoint: AgentControlCheckpoint,
        *,
        loop_layer: LoopLayer,
        action: str,
    ) -> None:
        payload = {
            key: value
            for key, value in checkpoint.payload.items()
            if key
            in {
                "checkpoint_id",
                "candidate_run_id",
                "kind",
                "summary",
                "evidence",
                "target_owner",
                "contract_field",
                "contract_revision",
                "committed_evidence_locator",
                "impossibility_reason",
                "routing_status",
                "question",
                "suggestions",
                "context",
            }
        }
        payload.setdefault("candidate_run_id", checkpoint.run_result.candidate_run_id)
        outcome = checkpoint.run_result.outcome
        blocker_kind = payload.get("kind")
        if outcome == "waiting_user":
            kind = "agent_decision_checkpoint_rejected"
            routing_decision = "stop"
            message = (
                "This autonomous Loop cannot open an extra user-decision checkpoint."
            )
        elif blocker_kind == "cross_loop":
            kind = "cross_loop_proposal_recorded"
            target_owner = payload.get("target_owner")
            routing_decision = (
                f"propose_to_{target_owner}"
                if isinstance(target_owner, str)
                else "await_harness_routing"
            )
            message = (
                "Cross-Loop blocker proposal recorded; only the Harness may route it."
            )
        else:
            kind = "agent_blocker_recorded"
            routing_decision = "pause"
            message = "Loop Agent blocker recorded at a durable checkpoint."
        self._emit(
            metadata,
            kind=kind,
            loop_layer=loop_layer,
            atomic_action=action,
            status="requested",
            artifact_path=checkpoint.artifact_path,
            routing_decision=routing_decision,
            message=message,
            payload=payload,
        )
        if blocker_kind == "cross_loop" and self._route_cross_loop_proposal(
            metadata,
            loop_layer=loop_layer,
            action=action,
            proposal=payload,
            source_artifact=checkpoint.artifact_path,
        ):
            return
        metadata.run_status = "failed"
        write_project_metadata(self.context.project_path, metadata)
        set_run_intent(self.context.project_path, desired_state="stopped")

    def _handle_evaluation_control(
        self,
        metadata: ProjectMetadata,
        evaluation: EvaluationRecord,
        *,
        loop_layer: LoopLayer,
        action: str,
        candidate_run_id: str | None = None,
    ) -> bool:
        result = evaluation.result
        if result.outcome == "pass":
            return False
        if result.outcome == "cross_loop_escalation":
            kind = "cross_loop_proposal_recorded"
            owner = (
                result.upstream_blocker.owner
                if result.upstream_blocker is not None
                else None
            )
            routing_decision = (
                f"propose_to_{owner}" if owner is not None else "await_harness_routing"
            )
            message = (
                "Evaluator cross-Loop proposal recorded; only the Harness may route it."
            )
        elif result.outcome == "needs_user":
            if loop_layer == "chapter":
                kind = "agent_semantic_revision_exhausted"
                routing_decision = "retry_candidate"
                message = (
                    "Chapter evaluation could not resolve the candidate automatically; "
                    "the user may start a fresh bounded revision without answering a "
                    "creative question."
                )
            else:
                kind = "agent_semantic_revision_exhausted"
                routing_decision = "retry_candidate"
                message = (
                    "Evaluation could not resolve the candidate automatically; a user "
                    "may start a fresh bounded candidate revision without answering an "
                    "extra creative question."
                )
        else:
            kind = "agent_semantic_revision_exhausted"
            routing_decision = "pause"
            message = "Candidate remains blocked after bounded semantic revision."
        blocker_payload = (
            result.upstream_blocker.model_dump(mode="json")
            if result.upstream_blocker is not None
            else None
        )
        self._emit(
            metadata,
            kind=kind,
            loop_layer=loop_layer,
            atomic_action=action,
            status="requested",
            artifact_path=evaluation.candidate_artifact_id,
            routing_decision=routing_decision,
            message=message,
            payload={
                "evaluation_id": evaluation.evaluation_id,
                "candidate_revision": evaluation.candidate_revision,
                "outcome": result.outcome,
                "summary": result.summary,
                "upstream_blocker": blocker_payload,
            },
        )
        if (
            result.outcome == "cross_loop_escalation"
            and blocker_payload is not None
            and self._route_cross_loop_proposal(
                metadata,
                loop_layer=loop_layer,
                action=action,
                proposal={
                    **blocker_payload,
                    "summary": result.summary,
                    "evaluation_id": evaluation.evaluation_id,
                    "candidate_revision": evaluation.candidate_revision,
                    "candidate_run_id": candidate_run_id,
                },
                source_artifact=evaluation.candidate_artifact_id,
            )
        ):
            return True
        if loop_layer == "chapter":
            metadata.run_status = "waiting_for_user"
        else:
            metadata.run_status = "failed"
            set_run_intent(self.context.project_path, desired_state="stopped")
        write_project_metadata(self.context.project_path, metadata)
        return True

    def _process_approved_book_revision(
        self,
        metadata: ProjectMetadata,
        profile: LlmProfile,
    ) -> bool:
        revision = (
            book_revision_storage.read_approved_book_revision_with_pending_downstream(
                self.context.project_path
            )
        )
        if revision is None:
            return False

        if metadata.active_arc_id is None:
            book_revision_storage.mark_book_revision_downstream_completed(
                self.context.project_path,
                revision.revision_id,
                artifact_paths=[],
            )
            self._emit(
                metadata,
                kind="book_revision_downstream_completed",
                loop_layer="book",
                atomic_action="apply_approved_book_revision",
                status="completed",
                artifact_path=f"book/revisions/{revision.revision_id}/state.json",
                routing_decision="plan_story_arc",
                message="Approved Book revision is active; no existing Story Arc required repair.",
                payload={"revision_id": revision.revision_id},
            )
            metadata.run_status = "idle"
            write_project_metadata(self.context.project_path, metadata)
            return True

        revision_request = json.dumps(
            {
                "book_revision_id": revision.revision_id,
                "base_book_version": revision.base_book_version,
                "approved_book_version": revision.target_book_version,
                "source_loop": revision.source_loop,
                "source_artifact": revision.source_artifact,
                "contract_field": revision.contract_field,
                "committed_evidence_locator": revision.committed_evidence_locator,
                "impossibility_reason": revision.impossibility_reason,
                "instruction": (
                    "Preserve completed chapters as immutable history. Revise only the "
                    "unfulfilled remainder of the active Story Arc against the newly approved "
                    "Book contract."
                ),
            },
            ensure_ascii=False,
            indent=2,
        )
        revised_path = self._revise_current_arc_plan_from_feedback(
            profile,
            metadata,
            revision_request,
            source_label="approved Book revision",
        )
        if revised_path is None:
            return True

        artifact_paths = [revised_path]
        if revision.source_loop == "chapter" and metadata.active_chapter_id is not None:
            route_path, invalidated = (
                self._invalidate_uncommitted_chapter_after_arc_revision(
                    metadata,
                    route_id=revision.route_id,
                    source_artifact=revision.source_artifact,
                    revised_arc_path=revised_path,
                    proposal={
                        "candidate_run_id": revision.source_candidate_run_id,
                        "summary": revision.summary,
                        "contract_field": revision.contract_field,
                        "contract_revision": revision.base_book_version,
                        "committed_evidence_locator": (
                            revision.committed_evidence_locator
                        ),
                        "impossibility_reason": revision.impossibility_reason,
                    },
                )
            )
            artifact_paths.extend([route_path, *invalidated])

        book_revision_storage.mark_book_revision_downstream_completed(
            self.context.project_path,
            revision.revision_id,
            artifact_paths=artifact_paths,
        )
        self._emit(
            metadata,
            kind="book_revision_downstream_completed",
            loop_layer="book",
            atomic_action="apply_approved_book_revision",
            status="completed",
            artifact_path=f"book/revisions/{revision.revision_id}/state.json",
            routing_decision=(
                "pause_for_arc_review"
                if metadata.operation_mode == "participatory"
                else "resume_lower_loop"
            ),
            message=(
                "Approved Book revision was propagated only into uncommitted downstream work."
            ),
            payload={
                "revision_id": revision.revision_id,
                "artifact_paths": artifact_paths,
            },
        )
        return True

    def _route_cross_loop_proposal(
        self,
        metadata: ProjectMetadata,
        *,
        loop_layer: LoopLayer,
        action: str,
        proposal: dict[str, object],
        source_artifact: str,
        route_id: str | None = None,
    ) -> bool:
        """Validate and execute a Harness-owned upper route.

        Lower Agents and the Evaluator can only submit proposals. This method is the
        Harness authority boundary that may activate the owning upper Agent.
        """

        target_owner = proposal.get("target_owner", proposal.get("owner"))
        if target_owner == "book":
            return self._route_book_revision_proposal(
                metadata,
                loop_layer=loop_layer,
                action=action,
                proposal=proposal,
                source_artifact=source_artifact,
                route_id=route_id,
            )

        rejection = self._story_arc_route_rejection(metadata, loop_layer, proposal)
        if rejection is not None:
            self._emit(
                metadata,
                kind="cross_loop_route_rejected",
                loop_layer=loop_layer,
                atomic_action=action,
                status="failed",
                artifact_path=source_artifact,
                routing_decision="pause",
                message="Harness rejected automatic cross-Loop routing.",
                payload={"reason": rejection},
            )
            return False

        profile = get_active_profile()
        if profile is None:
            self._emit(
                metadata,
                kind="cross_loop_route_rejected",
                loop_layer=loop_layer,
                atomic_action=action,
                status="failed",
                artifact_path=source_artifact,
                routing_decision="pause",
                message="Harness could not route the proposal without an active profile.",
                payload={"reason": "missing_active_profile"},
            )
            return False

        route_id = route_id or f"route-{uuid4().hex}"
        write_json(
            self.context.project_path
            / "book"
            / "harness"
            / "pending-cross-loop-route.json",
            {
                "schema_version": 1,
                "route_id": route_id,
                "loop_layer": loop_layer,
                "action": action,
                "proposal": proposal,
                "source_artifact": source_artifact,
            },
        )
        self._emit(
            metadata,
            kind="cross_loop_route_accepted",
            loop_layer=loop_layer,
            atomic_action=action,
            status="started",
            artifact_path=source_artifact,
            routing_decision="activate_story_arc",
            message="Harness accepted the evidenced blocker and activated Story Arc Agent.",
            payload={
                "route_id": route_id,
                "target_owner": "story_arc",
                "contract_field": proposal.get("contract_field"),
                "contract_revision": proposal.get("contract_revision"),
            },
        )
        self._mark_lower_agent_blocked(
            metadata,
            {**proposal, "source_artifact": source_artifact},
            loop_layer=loop_layer,
        )
        revision_request = json.dumps(
            {
                "route_id": route_id,
                "lower_loop": loop_layer,
                "source_artifact": source_artifact,
                "summary": proposal.get("summary"),
                "contract_field": proposal.get("contract_field"),
                "contract_revision": proposal.get("contract_revision"),
                "committed_evidence_locator": proposal.get(
                    "committed_evidence_locator"
                ),
                "impossibility_reason": proposal.get("impossibility_reason"),
            },
            ensure_ascii=False,
            indent=2,
        )
        revised_path = self._revise_current_arc_plan_from_feedback(
            profile,
            metadata,
            revision_request,
            source_label="Harness-routed Chapter blocker",
        )
        if revised_path is None:
            self._emit(
                metadata,
                kind="cross_loop_route_deferred",
                loop_layer="story_arc",
                atomic_action="revise_current_arc_plan",
                status="requested",
                artifact_path=source_artifact,
                routing_decision="pause",
                message="The routed Story Arc revision reached another durable wait.",
                payload={"route_id": route_id},
            )
            return True

        route_path, invalidated = self._invalidate_uncommitted_chapter_after_arc_revision(
            metadata,
            route_id=route_id,
            source_artifact=source_artifact,
            revised_arc_path=revised_path,
            proposal=proposal,
        )
        (
            self.context.project_path
            / "book"
            / "harness"
            / "pending-cross-loop-route.json"
        ).unlink(missing_ok=True)
        self._emit(
            metadata,
            kind="cross_loop_route_completed",
            loop_layer="story_arc",
            atomic_action="revise_current_arc_plan",
            status="completed",
            artifact_path=route_path,
            routing_decision=(
                "pause_for_arc_review"
                if metadata.operation_mode == "participatory"
                else "retry_chapter"
            ),
            message="Story Arc revision passed; only uncommitted Chapter derivatives were invalidated.",
            payload={
                "route_id": route_id,
                "revised_arc_path": revised_path,
                "invalidated_paths": invalidated,
            },
        )
        return True

    def _route_book_revision_proposal(
        self,
        metadata: ProjectMetadata,
        *,
        loop_layer: LoopLayer,
        action: str,
        proposal: dict[str, object],
        source_artifact: str,
        route_id: str | None = None,
    ) -> bool:
        rejection = self._book_route_rejection(metadata, loop_layer, proposal)
        if rejection is not None:
            self._emit(
                metadata,
                kind="cross_loop_route_rejected",
                loop_layer=loop_layer,
                atomic_action=action,
                status="failed",
                artifact_path=source_artifact,
                routing_decision="pause",
                message="Harness rejected the proposed Book revision route.",
                payload={"reason": rejection},
            )
            return False

        profile = get_active_profile()
        if profile is None:
            self._emit(
                metadata,
                kind="cross_loop_route_rejected",
                loop_layer=loop_layer,
                atomic_action=action,
                status="failed",
                artifact_path=source_artifact,
                routing_decision="pause",
                message="Harness could not route the proposal without an active profile.",
                payload={"reason": "missing_active_profile"},
            )
            return False

        book_state = read_json(
            self.context.project_path / "book" / "state.json",
            default={},
        )
        assert isinstance(book_state, dict)
        base_book_version = int(book_state["version"])
        target_direction_version = int(book_state.get("book_direction_version", 1)) + 1
        route_id = route_id or f"route-{uuid4().hex}"
        write_json(
            self.context.project_path
            / "book"
            / "harness"
            / "pending-cross-loop-route.json",
            {
                "schema_version": 1,
                "route_id": route_id,
                "loop_layer": loop_layer,
                "action": action,
                "proposal": proposal,
                "source_artifact": source_artifact,
            },
        )
        self._emit(
            metadata,
            kind="cross_loop_route_accepted",
            loop_layer=loop_layer,
            atomic_action=action,
            status="started",
            artifact_path=source_artifact,
            routing_decision="activate_book",
            message="Harness accepted the evidenced blocker and activated Book Agent.",
            payload={
                "route_id": route_id,
                "target_owner": "book",
                "contract_field": proposal.get("contract_field"),
                "contract_revision": base_book_version,
            },
        )
        self._mark_lower_agent_blocked(
            metadata,
            {**proposal, "source_artifact": source_artifact},
            loop_layer=loop_layer,
        )

        setup_state = read_setup_state(self.context.project_path).model_copy(deep=True)
        setup_state.revision = base_book_version
        setup_state.candidate_revision_counter = target_direction_version - 1
        setup_state.direction_draft = _read_text(
            self.context.project_path / "book" / "direction.md"
        )
        confirmed = book_state.get("confirmed_decisions", [])
        if isinstance(confirmed, list):
            setup_state.confirmed_decisions = [
                str(item) for item in confirmed if isinstance(item, str)
            ]
        setup_state.unresolved_questions = []
        setup_state.contradictions = [
            str(proposal["impossibility_reason"]),
        ]
        revision_request = {
            "route_id": route_id,
            "source_loop": loop_layer,
            "source_artifact": source_artifact,
            "summary": proposal.get("summary"),
            "contract_field": proposal.get("contract_field"),
            "contract_revision": proposal.get("contract_revision"),
            "committed_evidence_locator": proposal.get(
                "committed_evidence_locator"
            ),
            "impossibility_reason": proposal.get("impossibility_reason"),
            "immutable_history_rule": (
                "Committed prose and canon cannot be rewritten; revise only future or "
                "unfulfilled Book instructions."
            ),
        }
        policy = self._resolve_agent_policy(metadata, "book", profile)
        stream_callback = self._agent_stream_callback(
            metadata,
            "book",
            "revise_book_direction",
        )
        try:
            synthesis, evaluation, review = run_book_revision_agent(
                self.context.project_path,
                metadata,
                setup_state,
                policy,
                target_direction_version=target_direction_version,
                revision_request=revision_request,
                candidate_run_id=self._book_revision_resume_candidate_run_id(
                    metadata,
                    target_direction_version,
                ),
                on_event=self._agent_event_callback(
                    metadata,
                    "book",
                    "revise_book_direction",
                ),
                on_text_delta=stream_callback,
                on_tool_event=stream_callback,
            )
        except AgentControlCheckpoint as checkpoint:
            self._handle_agent_control_checkpoint(
                metadata,
                checkpoint,
                loop_layer="book",
                action="revise_book_direction",
            )
            return True
        if evaluation.result.outcome != "pass":
            self._handle_evaluation_control(
                metadata,
                evaluation,
                loop_layer="book",
                action="revise_book_direction",
            )
            return True

        source_loop: Literal["story_arc", "chapter"] = (
            "chapter" if loop_layer == "chapter" else "story_arc"
        )
        revision = book_revision_storage.save_book_revision_candidate(
            self.context.project_path,
            route_id=route_id,
            base_book_version=base_book_version,
            source_loop=source_loop,
            source_artifact=source_artifact,
            source_candidate_run_id=(
                str(proposal["candidate_run_id"])
                if isinstance(proposal.get("candidate_run_id"), str)
                else None
            ),
            summary=str(proposal.get("summary", "Book contract revision required.")),
            contract_field=str(proposal["contract_field"]),
            committed_evidence_locator=str(proposal["committed_evidence_locator"]),
            impossibility_reason=str(proposal["impossibility_reason"]),
            synthesis=synthesis,
            evaluation=evaluation,
            review=review,
            profile_id=policy.profile.id,
        )
        (
            self.context.project_path
            / "book"
            / "harness"
            / "pending-cross-loop-route.json"
        ).unlink(missing_ok=True)
        if source_loop == "story_arc" and revision.source_candidate_run_id is not None:
            scope_id = self._story_arc_scope_id(metadata, source_artifact)
            if scope_id is not None:
                write_json(
                    self.context.project_path
                    / "arcs"
                    / scope_id
                    / "book-upstream-resume.json",
                    {
                        "schema_version": 1,
                        "route_id": route_id,
                        "candidate_run_id": revision.source_candidate_run_id,
                        "book_revision_id": revision.revision_id,
                    },
                )
        metadata.run_status = "waiting_for_user"
        write_project_metadata(self.context.project_path, metadata)
        self._emit(
            metadata,
            kind="book_revision_approval_required",
            loop_layer="book",
            atomic_action="revise_book_direction",
            status="requested",
            artifact_path=revision.candidate.direction_path,
            routing_decision="await_user_approval",
            message=(
                "Book revision passed evaluation and requires explicit user approval, "
                "including in full-auto mode."
            ),
            payload={
                "revision_id": revision.revision_id,
                "base_book_version": revision.base_book_version,
                "candidate_revision": revision.candidate.revision,
                "evidence_paths": [
                    revision.candidate.direction_path,
                    revision.review_path,
                    revision.verification_path,
                    f"book/revisions/{revision.revision_id}/state.json",
                ],
            },
        )
        return True

    def _process_pending_cross_loop_route(
        self,
        metadata: ProjectMetadata,
    ) -> bool:
        path = (
            self.context.project_path
            / "book"
            / "harness"
            / "pending-cross-loop-route.json"
        )
        payload = read_json(path, default=None)
        if payload is None:
            return False
        if not isinstance(payload, dict):
            raise ValueError("Pending cross-Loop route is not a valid document.")
        loop_layer = payload.get("loop_layer")
        action = payload.get("action")
        proposal = payload.get("proposal")
        source_artifact = payload.get("source_artifact")
        route_id = payload.get("route_id")
        if (
            loop_layer not in {"story_arc", "chapter"}
            or not isinstance(action, str)
            or not isinstance(proposal, dict)
            or not isinstance(source_artifact, str)
            or not isinstance(route_id, str)
        ):
            raise ValueError("Pending cross-Loop route is incomplete.")
        target_owner = proposal.get("target_owner", proposal.get("owner"))
        if target_owner == "book":
            saved_revision = book_revision_storage.read_pending_book_revision(
                self.context.project_path
            )
            if saved_revision is not None and saved_revision.route_id == route_id:
                path.unlink(missing_ok=True)
                metadata.run_status = "waiting_for_user"
                write_project_metadata(self.context.project_path, metadata)
                self._emit(
                    metadata,
                    kind="cross_loop_route_recovered",
                    loop_layer="book",
                    atomic_action="revise_book_direction",
                    status="completed",
                    artifact_path=saved_revision.candidate.direction_path,
                    routing_decision="await_user_approval",
                    message=(
                        "Recovered the already-saved Book revision route without "
                        "rerunning either Agent."
                    ),
                    payload={
                        "route_id": route_id,
                        "revision_id": saved_revision.revision_id,
                    },
                )
                return True
            routed = self._route_book_revision_proposal(
                metadata,
                loop_layer=loop_layer,
                action=action,
                proposal=proposal,
                source_artifact=source_artifact,
                route_id=route_id,
            )
        else:
            if self._recover_completed_story_arc_route(
                metadata,
                path=path,
                route_id=route_id,
                proposal=proposal,
                source_artifact=source_artifact,
            ):
                return True
            routed = self._route_cross_loop_proposal(
                metadata,
                loop_layer=loop_layer,
                action=action,
                proposal=proposal,
                source_artifact=source_artifact,
                route_id=route_id,
            )
        if not routed:
            archive_path = (
                self.context.project_path
                / "book"
                / "harness"
                / "cross-loop-routes"
                / f"{route_id}-rejected.json"
            )
            write_json(
                archive_path,
                {
                    **payload,
                    "status": "rejected_on_replay",
                    "archived_at": datetime.now(UTC).isoformat(),
                },
            )
            path.unlink(missing_ok=True)
            metadata.run_status = "failed"
            write_project_metadata(self.context.project_path, metadata)
            set_run_intent(self.context.project_path, desired_state="stopped")
        return True

    def _recover_completed_story_arc_route(
        self,
        metadata: ProjectMetadata,
        *,
        path: Path,
        route_id: str,
        proposal: dict[str, object],
        source_artifact: str,
    ) -> bool:
        chapter_id = metadata.active_chapter_id
        arc_id = metadata.active_arc_id
        if chapter_id is None or arc_id is None:
            return False
        route_relative = (
            Path("chapters")
            / chapter_id
            / "upstream-routes"
            / f"{route_id}.json"
        )
        route_path = self.context.project_path / route_relative
        invalidated: list[str]
        if route_path.is_file():
            route_payload = read_json(route_path, default={})
            raw_invalidated = (
                route_payload.get("invalidated_paths", [])
                if isinstance(route_payload, dict)
                else []
            )
            invalidated = [
                item for item in raw_invalidated if isinstance(item, str)
            ]
        else:
            contract_revision = proposal.get("contract_revision")
            state_payload = read_json(
                self.context.project_path / "arcs" / arc_id / "state.json",
                default={},
            )
            current_revision = (
                state_payload.get("version")
                if isinstance(state_payload, dict)
                else None
            )
            revision_text = _read_text(
                self.context.project_path / "arcs" / arc_id / "revision.md"
            )
            if (
                not isinstance(contract_revision, int)
                or current_revision != contract_revision + 1
                or route_id not in revision_text
            ):
                return False
            route_relative_value, invalidated = (
                self._invalidate_uncommitted_chapter_after_arc_revision(
                    metadata,
                    route_id=route_id,
                    source_artifact=source_artifact,
                    revised_arc_path=f"arcs/{arc_id}/plan.md",
                    proposal=proposal,
                )
            )
            route_relative = Path(route_relative_value)

        path.unlink(missing_ok=True)
        self._emit(
            metadata,
            kind="cross_loop_route_recovered",
            loop_layer="story_arc",
            atomic_action="revise_current_arc_plan",
            status="completed",
            artifact_path=route_relative.as_posix(),
            routing_decision=(
                "pause_for_arc_review"
                if metadata.operation_mode == "participatory"
                else "retry_chapter"
            ),
            message=(
                "Recovered the completed Story Arc route and finished only its "
                "uncommitted Chapter cleanup."
            ),
            payload={
                "route_id": route_id,
                "invalidated_paths": invalidated,
            },
        )
        return True

    def _mark_lower_agent_blocked(
        self,
        metadata: ProjectMetadata,
        proposal: dict[str, object],
        *,
        loop_layer: LoopLayer,
    ) -> None:
        candidate_run_id = proposal.get("candidate_run_id")
        if not isinstance(candidate_run_id, str):
            return
        if loop_layer == "chapter":
            scope_id = metadata.active_chapter_id
        elif loop_layer == "story_arc":
            source_artifact = proposal.get("source_artifact")
            scope_id = self._story_arc_scope_id(
                metadata,
                source_artifact if isinstance(source_artifact, str) else "",
            )
        else:
            return
        if scope_id is None:
            return
        identity = AgentIdentity(
            project_id=metadata.project_id,
            role=loop_layer,
            scope_id=scope_id,
        )
        state = read_agent_state(self.context.project_path, identity)
        if state.candidate_run_id != candidate_run_id:
            return
        state.lifecycle = "blocked"
        target_owner = proposal.get("target_owner", proposal.get("owner"))
        state.phase = f"waiting_for_{target_owner}_revision"
        state.summary = f"Harness routed an evidenced blocker to {target_owner} Agent."
        save_agent_state(self.context.project_path, state)

    @staticmethod
    def _story_arc_scope_id(
        metadata: ProjectMetadata,
        source_artifact: str,
    ) -> str | None:
        if metadata.active_arc_id is not None:
            return metadata.active_arc_id
        parts = Path(source_artifact).parts
        if len(parts) >= 2 and parts[0] == "arcs":
            return parts[1]
        return None

    def _story_arc_resume_candidate_run_id(self, arc_path: Path) -> str | None:
        payload = read_json(arc_path / "book-upstream-resume.json", default={})
        if isinstance(payload, dict):
            candidate_run_id = payload.get("candidate_run_id")
            if isinstance(candidate_run_id, str):
                return candidate_run_id
        metadata = read_project_metadata(self.context.project_path)
        state = read_agent_state(
            self.context.project_path,
            AgentIdentity(
                project_id=metadata.project_id,
                role="story_arc",
                scope_id=arc_path.name,
            ),
        )
        return state.candidate_run_id if state.lifecycle == "failed" else None

    def _book_revision_resume_candidate_run_id(
        self,
        metadata: ProjectMetadata,
        target_direction_version: int,
    ) -> str | None:
        identity = AgentIdentity(project_id=metadata.project_id, role="book")
        state = read_agent_state(self.context.project_path, identity)
        if state.candidate_run_id is None:
            return None
        if state.lifecycle == "failed":
            return state.candidate_run_id
        if state.lifecycle != "completed" or state.activation_id is None:
            return None
        candidate_path = (
            activation_relative(identity, state.activation_id)
            / "c"
            / "book-direction.json"
        )
        payload = read_json(self.context.project_path / candidate_path, default={})
        if (
            isinstance(payload, dict)
            and payload.get("candidate_revision") == target_direction_version
        ):
            return state.candidate_run_id
        return None

    def _book_route_rejection(
        self,
        metadata: ProjectMetadata,
        loop_layer: LoopLayer,
        proposal: dict[str, object],
    ) -> str | None:
        if loop_layer not in {"story_arc", "chapter"}:
            return "book_route_requires_lower_loop_source"
        if proposal.get("target_owner", proposal.get("owner")) != "book":
            return "book_route_requires_book_owner"
        if not isinstance(proposal.get("candidate_run_id"), str):
            return "book_route_requires_source_candidate_run"
        if book_revision_storage.read_pending_book_revision(self.context.project_path):
            return "book_revision_already_awaiting_approval"

        book_state = read_json(
            self.context.project_path / "book" / "state.json",
            default={},
        )
        current_revision = (
            book_state.get("version") if isinstance(book_state, dict) else None
        )
        if not isinstance(current_revision, int):
            return "missing_book_contract_revision"
        if proposal.get("contract_revision") != current_revision:
            return "stale_or_unknown_book_revision"

        locator = proposal.get("committed_evidence_locator")
        allowed_locators = {
            "book/direction.md",
            "book/settings.md",
            "book/outline.md",
            "book/constraints.json",
            "book/state.json",
        }
        if locator not in allowed_locators:
            return "book_route_requires_approved_book_evidence"
        assert isinstance(locator, str)
        try:
            evidence_path = resolve_artifact_path(self.context.project_path, locator)
        except ValueError:
            return "invalid_book_evidence_path"
        if not evidence_path.is_file():
            return "missing_book_evidence"
        if not isinstance(proposal.get("contract_field"), str) or not isinstance(
            proposal.get("impossibility_reason"), str
        ):
            return "incomplete_book_contract_evidence"

        if loop_layer == "chapter" and metadata.active_chapter_id is not None:
            chapter_path = (
                self.context.project_path / "chapters" / metadata.active_chapter_id
            )
            if (chapter_path / "final.md").exists() or (
                chapter_path / "committed_state_patch.json"
            ).exists():
                return "committed_chapter_work_cannot_be_invalidated"
        return None

    def _story_arc_route_rejection(
        self,
        metadata: ProjectMetadata,
        loop_layer: LoopLayer,
        proposal: dict[str, object],
    ) -> str | None:
        if loop_layer != "chapter" or proposal.get("target_owner", proposal.get("owner")) != "story_arc":
            return "automatic_route_not_supported_for_loop_pair"
        if metadata.active_arc_id is None or metadata.active_chapter_id is None:
            return "missing_active_arc_or_chapter"

        chapter_path = self.context.project_path / "chapters" / metadata.active_chapter_id
        if (chapter_path / "final.md").exists() or (
            chapter_path / "committed_state_patch.json"
        ).exists():
            return "committed_chapter_work_cannot_be_invalidated"

        state_payload = read_json(
            self.context.project_path / "arcs" / metadata.active_arc_id / "state.json",
            default={},
        )
        current_revision = (
            state_payload.get("version") if isinstance(state_payload, dict) else None
        )
        if proposal.get("contract_revision") != current_revision:
            return "stale_or_unknown_story_arc_revision"

        locator = proposal.get("committed_evidence_locator")
        expected_locator = f"arcs/{metadata.active_arc_id}/plan.md"
        if locator != expected_locator:
            return "story_arc_route_requires_current_plan_evidence"
        try:
            evidence_path = resolve_artifact_path(
                self.context.project_path,
                expected_locator,
            )
        except ValueError:
            return "invalid_story_arc_evidence_path"
        if not evidence_path.is_file():
            return "missing_story_arc_evidence"
        if not isinstance(proposal.get("contract_field"), str) or not isinstance(
            proposal.get("impossibility_reason"), str
        ):
            return "incomplete_story_arc_contract_evidence"
        return None

    def _invalidate_uncommitted_chapter_after_arc_revision(
        self,
        metadata: ProjectMetadata,
        *,
        route_id: str,
        source_artifact: str,
        revised_arc_path: str,
        proposal: dict[str, object],
    ) -> tuple[str, list[str]]:
        chapter_id = metadata.active_chapter_id
        if chapter_id is None:
            raise ValueError("Cannot invalidate Chapter derivatives without an active chapter.")
        chapter_path = self.context.project_path / "chapters" / chapter_id
        if (chapter_path / "final.md").exists() or (
            chapter_path / "committed_state_patch.json"
        ).exists():
            raise ValueError("Harness cannot invalidate committed Chapter artifacts.")

        relative_names = [
            "context_snapshot.json",
            "goal.md",
            "draft.md",
            "observations.json",
            "candidate_state_patch.json",
            "agent_candidate.json",
            "evaluation.json",
            "review.md",
            "verification.json",
        ]
        invalidated = [
            f"chapters/{chapter_id}/{name}"
            for name in relative_names
            if (chapter_path / name).is_file()
        ]
        route_relative = (
            Path("chapters") / chapter_id / "upstream-routes" / f"{route_id}.json"
        )
        write_json(
            self.context.project_path / route_relative,
            {
                "schema_version": 1,
                "route_id": route_id,
                "source_artifact": source_artifact,
                "revised_arc_path": revised_arc_path,
                "proposal": proposal,
                "invalidated_paths": invalidated,
                "committed_artifacts_touched": False,
                "created_at": datetime.now(UTC).isoformat(),
            },
        )
        for name in relative_names:
            (chapter_path / name).unlink(missing_ok=True)
        candidate_run_id = proposal.get("candidate_run_id")
        if isinstance(candidate_run_id, str) and candidate_run_id:
            write_json(
                chapter_path / "upstream-resume.json",
                {
                    "schema_version": 1,
                    "route_id": route_id,
                    "candidate_run_id": candidate_run_id,
                    "revised_arc_path": revised_arc_path,
                },
            )
        return route_relative.as_posix(), invalidated

    @staticmethod
    def _resolve_agent_policy(
        metadata: ProjectMetadata,
        role: Literal["book", "story_arc", "chapter"],
        active_profile: LlmProfile | None,
    ) -> ResolvedAgentPolicy:
        try:
            return resolve_agent_policy(metadata, role)
        except ValueError:
            override_id = (
                metadata.agent_policy.book_profile_id
                if role == "book"
                else (
                    metadata.agent_policy.story_arc_profile_id
                    if role == "story_arc"
                    else metadata.agent_policy.chapter_profile_id
                )
            )
            if (
                active_profile is None
                or override_id is not None
                or metadata.agent_policy.evaluator_profile_id is not None
            ):
                raise
            return ResolvedAgentPolicy(
                role=role,
                profile=active_profile,
                evaluator_profile=active_profile,
                max_turns=(
                    metadata.agent_policy.book_max_turns
                    if role == "book"
                    else (
                        metadata.agent_policy.story_arc_max_turns
                        if role == "story_arc"
                        else metadata.agent_policy.chapter_max_turns
                    )
                ),
                tool_schema_repair_limit=metadata.agent_policy.tool_schema_repair_limit,
                semantic_revision_limit=metadata.agent_policy.semantic_revision_limit,
                transport_retry_limit=metadata.agent_policy.transport_retry_limit,
            )

    def _bump_book_feedback_state(self) -> None:
        state_path = self.context.project_path / "book" / "state.json"
        payload = read_json(state_path, default={})
        state = payload if isinstance(payload, dict) else {}
        version = state.get("version")
        state["schema_version"] = 1
        state["version"] = (version if isinstance(version, int) else 1) + 1
        state["feedback_path"] = "book/feedback.md"
        state["feedback_updated_at"] = datetime.now(UTC).isoformat()
        write_json(state_path, state)

    def _write_final_chapter(
        self,
        metadata: ProjectMetadata,
        chapter_id: str,
        chapter_path: Path,
    ) -> None:
        verification = ChapterVerification.model_validate(
            read_json(chapter_path / "verification.json")
        )
        if not verification.commit_allowed:
            metadata.run_status = "waiting_for_user"
            write_project_metadata(self.context.project_path, metadata)
            self._emit(
                metadata,
                kind="routing_decision",
                loop_layer="chapter",
                atomic_action="write_final_chapter",
                status="completed",
                routing_decision=verification.routing_decision,
                message=f"{chapter_id} cannot be finalized until verification passes.",
            )
            return

        write_text_file(
            chapter_path / "final.md",
            _read_text(chapter_path / "draft.md").rstrip() + "\n",
        )
        self._finish_artifact_step(
            metadata,
            kind="artifact_written",
            atomic_action="write_final_chapter",
            artifact_path=f"chapters/{chapter_id}/final.md",
            message=f"Final chapter prose committed for {chapter_id}.",
            routing_decision="commit",
        )

    def _commit_state_patch(
        self,
        metadata: ProjectMetadata,
        chapter_id: str,
        chapter_path: Path,
    ) -> None:
        self._emit_started(
            metadata,
            "commit_state_patch",
            f"Validating candidate state patch for {chapter_id}.",
        )
        patch = CandidateStatePatch.model_validate(
            read_json(chapter_path / "candidate_state_patch.json")
        )
        try:
            committed = commit_candidate_state_patch(
                self.context.project_path,
                patch,
                chapter_path / "committed_state_patch.json",
            )
        except PatchValidationError as exc:
            repair_state = read_json(
                chapter_path / "state_patch_repair_state.json",
                default={},
            )
            attempts = (
                repair_state.get("attempts", 0)
                if isinstance(repair_state, dict)
                else 0
            )
            repair_limit = metadata.agent_policy.semantic_revision_limit
            auto_repair = (
                attempts < repair_limit
                and _is_evidence_quote_repairable(exc.result.reasons)
            )
            metadata.run_status = "running" if auto_repair else "waiting_for_user"
            write_project_metadata(self.context.project_path, metadata)
            write_json(
                chapter_path / "state_patch_rejection.json",
                exc.result.model_dump(mode="json", by_alias=True),
            )
            self._emit(
                metadata,
                kind="state_patch_rejected",
                loop_layer="chapter",
                atomic_action="commit_state_patch",
                status="failed",
                artifact_path=f"chapters/{chapter_id}/state_patch_rejection.json",
                routing_decision=(
                    "repair_current_candidate" if auto_repair else "pause"
                ),
                message=f"Candidate state patch rejected for {chapter_id}.",
                payload={
                    "reasons": exc.result.reasons,
                    "repair_attempts": attempts,
                    "repair_limit": repair_limit,
                },
            )
            return

        self._finish_artifact_step(
            metadata,
            kind="state_patch_committed",
            atomic_action="commit_state_patch",
            artifact_path=f"chapters/{chapter_id}/committed_state_patch.json",
            message=f"State patch committed for {chapter_id}.",
            routing_decision="continue",
            payload={"operation_count": len(committed.operations)},
        )

    def _repair_state_patch_evidence(
        self,
        profile: LlmProfile,
        metadata: ProjectMetadata,
        chapter_id: str,
        chapter_path: Path,
    ) -> None:
        state_path = chapter_path / "state_patch_repair_state.json"
        state_payload = read_json(state_path, default={})
        state = state_payload if isinstance(state_payload, dict) else {}
        attempts = state.get("attempts", 0)
        attempts = attempts if isinstance(attempts, int) else 0
        rejection_payload = read_json(
            chapter_path / "state_patch_rejection.json",
            default={},
        )
        reasons = (
            rejection_payload.get("reasons", [])
            if isinstance(rejection_payload, dict)
            else []
        )
        repair_limit = metadata.agent_policy.semantic_revision_limit
        if (
            attempts >= repair_limit
            or not _is_evidence_quote_repairable(reasons)
        ):
            metadata.run_status = "waiting_for_user"
            write_project_metadata(self.context.project_path, metadata)
            return

        self._emit_started(
            metadata,
            "repair_state_patch_evidence",
            f"Repairing rejected state-patch evidence for {chapter_id}.",
        )
        policy = self._resolve_agent_policy(metadata, "chapter", profile)
        instruction = json.dumps(
            {
                "chapter_id": chapter_id,
                "final_markdown": _read_text(chapter_path / "final.md"),
                "candidate_state_patch": read_json(
                    chapter_path / "candidate_state_patch.json"
                ),
                "rejection_reasons": reasons,
            },
            ensure_ascii=False,
        )
        stream_callback = self._agent_stream_callback(
            metadata,
            "chapter",
            "repair_state_patch_evidence",
        )
        try:
            repair = run_chapter_patch_evidence_repair_agent(
                self.context.project_path,
                metadata,
                policy,
                chapter_id=chapter_id,
                expected_revision=attempts,
                instruction=instruction,
                on_event=self._agent_event_callback(
                    metadata,
                    "chapter",
                    "repair_state_patch_evidence",
                ),
                on_text_delta=stream_callback,
                on_tool_event=stream_callback,
            )
        except AgentControlCheckpoint as checkpoint:
            self._handle_agent_control_checkpoint(
                metadata,
                checkpoint,
                loop_layer="chapter",
                action="repair_state_patch_evidence",
            )
            return

        attempt_number = attempts + 1
        state = {
            "schema_version": 1,
            "attempts": attempt_number,
            "limit": repair_limit,
            "last_candidate_artifact": repair.candidate_artifact_path,
            "updated_at": datetime.now(UTC).isoformat(),
        }
        patch_document = json.dumps(
            repair.patch.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
        ) + "\n"
        commit_file_transaction(
            self.context.project_path,
            kind=f"chapter-state-patch-repair-{chapter_id}-{attempt_number}",
            files={
                f"chapters/{chapter_id}/candidate_state_patch.json": patch_document,
                f"chapters/{chapter_id}/state_patch_repair_state.json": (
                    json.dumps(state, ensure_ascii=False, indent=2) + "\n"
                ),
                (
                    f"chapters/{chapter_id}/state_patch_repairs/"
                    f"attempt-{attempt_number:03d}/rejection.json"
                ): json.dumps(rejection_payload, ensure_ascii=False, indent=2) + "\n",
                (
                    f"chapters/{chapter_id}/state_patch_repairs/"
                    f"attempt-{attempt_number:03d}/candidate_state_patch.json"
                ): patch_document,
            },
        )
        (chapter_path / "state_patch_rejection.json").unlink(missing_ok=True)
        metadata.run_status = "running"
        write_project_metadata(self.context.project_path, metadata)
        self._emit(
            metadata,
            kind="state_patch_evidence_repaired",
            loop_layer="chapter",
            atomic_action="repair_state_patch_evidence",
            status="completed",
            artifact_path=f"chapters/{chapter_id}/candidate_state_patch.json",
            routing_decision="retry_commit",
            message=f"Rejected state-patch evidence repaired for {chapter_id}.",
            payload={
                "repair_attempt": attempt_number,
                "repair_limit": repair_limit,
                "candidate_artifact": repair.candidate_artifact_path,
            },
        )

    def _call_feedback_llm_action(
        self,
        profile: LlmProfile,
        metadata: ProjectMetadata,
        *,
        loop_layer: LoopLayer,
        action: str,
        system: str,
        user: str,
    ) -> ChatResult:
        emitted_delta = False

        def on_text_delta(chunk: ChatChunk) -> None:
            nonlocal emitted_delta
            emitted_delta = True
            self._emit_model_output_delta(metadata, loop_layer, action, chunk.text_delta)

        def on_transport_retry(retry: int, limit: int, exc: Exception) -> None:
            self._emit(
                metadata,
                kind="llm_transport_retry",
                loop_layer=loop_layer,
                atomic_action=action,
                status="requested",
                routing_decision="retry_provider_call",
                message="Provider call scheduled an automatic transport retry.",
                payload={
                    "retry": retry,
                    "limit": limit,
                    "error": redact_profile_secrets(str(exc), profile),
                },
            )

        result = call_llm_with_transport_retries(
            profile,
            ChatRequest(
                profile_id=profile.id,
                messages=[
                    ChatMessage(role="system", content=system),
                    ChatMessage(role="user", content=user),
                ],
                metadata={
                    "loop_layer": loop_layer,
                    "atomic_action": action,
                    "project_id": metadata.project_id,
                    "on_text_delta": on_text_delta,
                },
            ),
            retry_limit=metadata.agent_policy.transport_retry_limit,
            llm_call=call_llm,
            on_retry=on_transport_retry,
        )
        if not emitted_delta:
            self._emit_model_output(metadata, loop_layer, action, result.content)
        return result

    def _emit_model_output(
        self,
        metadata: ProjectMetadata,
        loop_layer: LoopLayer,
        atomic_action: str,
        content: str,
    ) -> None:
        for chunk in _text_chunks(content):
            self._emit_model_output_delta(metadata, loop_layer, atomic_action, chunk)

    def _emit_model_output_delta(
        self,
        metadata: ProjectMetadata,
        loop_layer: LoopLayer,
        atomic_action: str,
        text_delta: str,
    ) -> None:
        if not text_delta:
            return
        self._emit(
            metadata,
            kind="llm_output_delta",
            loop_layer=loop_layer,
            atomic_action=atomic_action,
            status="delta",
            message="Model visible output.",
            payload={"text_delta": text_delta},
        )

    def _emit_model_stream_progress(
        self,
        metadata: ProjectMetadata,
        loop_layer: LoopLayer,
        atomic_action: str,
        received_characters: int,
    ) -> None:
        self._emit(
            metadata,
            kind="llm_stream_progress",
            loop_layer=loop_layer,
            atomic_action=atomic_action,
            status="delta",
            message="Model response is streaming.",
            payload={"received_characters": received_characters},
        )

    def _emit_started(self, metadata: ProjectMetadata, action: str, message: str) -> None:
        self._emit(
            metadata,
            kind="atomic_action_started",
            loop_layer="chapter",
            atomic_action=action,
            status="started",
            message=message,
        )

    def _finish_artifact_step(
        self,
        metadata: ProjectMetadata,
        *,
        kind: str,
        atomic_action: str,
        artifact_path: str,
        message: str,
        routing_decision: str = "continue",
        payload: dict[str, object] | None = None,
    ) -> None:
        paused = self._set_status_after_checkpoint(metadata, "idle")
        write_project_metadata(self.context.project_path, metadata)
        self._emit(
            metadata,
            kind=kind,
            loop_layer="chapter",
            atomic_action=atomic_action,
            status="completed",
            artifact_path=artifact_path,
            routing_decision=routing_decision,
            message=message,
            payload=payload,
        )
        if paused:
            self._emit_paused(metadata, f"Safe checkpoint reached after {atomic_action}.")

    def _finish_feedback_artifact_step(
        self,
        metadata: ProjectMetadata,
        *,
        loop_layer: LoopLayer,
        atomic_action: str,
        artifact_path: str,
        message: str,
        routing_decision: str,
        payload: dict[str, object] | None = None,
    ) -> None:
        default_status: RunStatus = "waiting_for_user" if routing_decision == "pause" else "idle"
        paused = self._set_status_after_checkpoint(metadata, default_status)
        write_project_metadata(self.context.project_path, metadata)
        self._emit(
            metadata,
            kind="feedback_artifact_written",
            loop_layer=loop_layer,
            atomic_action=atomic_action,
            status="completed",
            artifact_path=artifact_path,
            routing_decision=routing_decision,
            message=message,
            payload=payload,
        )
        if paused:
            self._emit_paused(metadata, f"Safe checkpoint reached after {atomic_action}.")

    def _set_status_after_checkpoint(
        self,
        metadata: ProjectMetadata,
        default_status: RunStatus,
    ) -> bool:
        latest_metadata = read_project_metadata(self.context.project_path)
        pause_requested = latest_metadata.run_status == "pause_requested"
        metadata.run_status = "paused" if pause_requested else default_status
        return pause_requested

    def _emit_paused(self, metadata: ProjectMetadata, message: str) -> None:
        self._emit(
            metadata,
            kind="run_paused",
            loop_layer="system",
            atomic_action="safe_checkpoint",
            status="completed",
            routing_decision="pause",
            message=message,
        )

    def _emit(
        self,
        metadata: ProjectMetadata,
        *,
        kind: str,
        loop_layer: LoopLayer,
        atomic_action: str | None,
        status: EventStatus,
        message: str,
        artifact_path: str | None = None,
        routing_decision: str | None = None,
        payload: dict[str, object] | None = None,
    ) -> None:
        append_event(
            self.context.project_path,
            HarnessEvent(
                project_id=metadata.project_id,
                run_id=self.context.run_id,
                kind=kind,
                loop_layer=loop_layer,
                atomic_action=atomic_action,
                status=status,
                artifact_path=artifact_path,
                routing_decision=routing_decision,
                message=message,
                payload=payload or {},
            ),
        )


def _read_text(path: Path) -> str:
    if not path.exists():
        return ""
    return read_text_file(path).strip()


def _markdown_heading(text: str) -> str | None:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            heading = stripped.lstrip("#").strip()
            return heading[:120] if heading else None
    return None


def _indent_block(text: str) -> str:
    return "\n".join(f"  {line}" if line else "" for line in text.splitlines())


def _read_canon_summary(project_path: Path) -> dict[str, object]:
    summary: dict[str, object] = {}
    for name in ["characters", "relationships", "world_facts", "foreshadowing"]:
        summary[name] = read_json(project_path / "canon" / f"{name}.json", default={})
    return summary


def _state_version(path: Path) -> int | None:
    data = read_json(path, default=None)
    if not isinstance(data, dict):
        return None
    version = data.get("version")
    return version if isinstance(version, int) else None


def _context_exclusions(chapter_id: str) -> list[ContextExclusion]:
    return [
        ContextExclusion(
            source=f"chapters/{chapter_id}/draft.md",
            reason="Current chapter draft is candidate prose and is excluded until verification passes.",
        ),
        ContextExclusion(
            source=f"chapters/{chapter_id}/observations.json",
            reason=(
                "Candidate observations are excluded from committed-state context until the "
                "chapter and state patch are verified."
            ),
        ),
        ContextExclusion(
            source=f"chapters/{chapter_id}/candidate_state_patch.json",
            reason="Candidate state patches are excluded until harness validation commits them.",
        ),
        ContextExclusion(
            source="future-story-arcs",
            reason="Rolling planning excludes unwritten future arcs; only the current arc is injected.",
        ),
    ]


def _chapter_number(chapter_id: str) -> int | None:
    if not chapter_id.startswith("chapter-"):
        return None
    try:
        return int(chapter_id.removeprefix("chapter-"))
    except ValueError:
        return None


def _candidate_run_segment(candidate_run_id: str) -> str:
    return sha256(candidate_run_id.encode("utf-8")).hexdigest()[:12]


def _next_chapter_id(project_path: Path) -> str:
    chapters_path = project_path / "chapters"
    chapters_path.mkdir(parents=True, exist_ok=True)
    existing_numbers: list[int] = []
    for entry in chapters_path.iterdir():
        if not entry.is_dir():
            continue
        chapter_number = _chapter_number(entry.name)
        if chapter_number is not None:
            existing_numbers.append(chapter_number)
    next_number = max(existing_numbers, default=0) + 1
    return f"chapter-{next_number:03d}"


def _next_arc_id(project_path: Path) -> str:
    arcs_path = project_path / "arcs"
    arcs_path.mkdir(parents=True, exist_ok=True)
    existing_numbers: list[int] = []
    reusable_ids: list[str] = []
    for entry in arcs_path.iterdir():
        if not entry.is_dir() or not entry.name.startswith("arc-"):
            continue
        try:
            existing_numbers.append(int(entry.name.removeprefix("arc-")))
        except ValueError:
            continue
        if not (entry / "plan.md").exists() and not (entry / "state.json").exists():
            reusable_ids.append(entry.name)
    if reusable_ids:
        return sorted(reusable_ids)[0]
    next_number = max(existing_numbers, default=0) + 1
    return f"arc-{next_number:03d}"


def _is_evidence_quote_repairable(reasons: object) -> bool:
    return (
        isinstance(reasons, list)
        and bool(reasons)
        and all(
            isinstance(reason, str)
            and reason.startswith("Operation ")
            and " evidence " in reason
            and " quote " in reason
            for reason in reasons
        )
    )


def _text_chunks(text: str, chunk_size: int = 1000) -> list[str]:
    if not text:
        return []
    return [text[index : index + chunk_size] for index in range(0, len(text), chunk_size)]


def _without_empty(parts: list[str]) -> list[str]:
    return [part for part in parts if part.strip()]


def _llm_usage_payload(
    profile: LlmProfile,
    result: ChatResult,
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "profile_id": profile.id,
        "model_snapshot": result.model_snapshot,
    }
    if extra is not None:
        payload.update(extra)
    return payload


def _route_feedback(metadata: ProjectMetadata, feedback: str) -> str:
    normalized = feedback.lower()
    if any(keyword in normalized for keyword in ["book", "setting", "ending", "genre"]):
        return "escalate_to_book_loop"
    if any(keyword in normalized for keyword in ["arc", "plan", "pacing"]):
        return "revise_current_arc_plan"
    if metadata.active_chapter_id is not None:
        return "apply_to_current_chapter_context"
    if metadata.active_arc_id is not None:
        return "revise_current_arc_plan"
    return "escalate_to_book_loop"


def _loop_layer_for_feedback_route(routing_decision: str) -> LoopLayer:
    if routing_decision == "apply_to_current_chapter_context":
        return "chapter"
    if routing_decision == "revise_current_arc_plan":
        return "story_arc"
    return "book"


def _non_retryable_failure_kind(message: str) -> tuple[str, str]:
    lowered = message.casefold()
    if any(
        marker in lowered
        for marker in (
            "auth_unavailable",
            "no auth available",
            "invalid api key",
            "invalid_api_key",
            "unauthorized",
            "forbidden",
        )
    ):
        return "provider_auth", "provider_auth_configuration_required"
    if any(
        marker in lowered
        for marker in ("unsupported", "does not support", "capability")
    ):
        return "unsupported_capability", "provider_capability_required"
    return "harness_failure", "run_failed"
