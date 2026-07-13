import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from app.core.paths import resolve_artifact_path
from app.llm.gateway import ChatChunk, ChatMessage, ChatRequest, ChatResult, call_llm
from app.llm.profiles import get_active_profile
from app.llm.redaction import redact_profile_secrets
from app.schemas.artifacts import (
    CandidateObservations,
    ChapterVerification,
    ContextExclusion,
    ContextSnapshot,
    ContextSource,
    VerificationSignal,
)
from app.schemas.events import EventStatus, HarnessEvent, LoopLayer
from app.schemas.arcs import StoryArcPlanProposal
from app.schemas.patches import CandidateStatePatch, PatchValidationResult
from app.schemas.profiles import LlmProfile
from app.schemas.projects import ProjectMetadata, RunStatus
from app.storage import arcs as arc_storage
from app.storage.events import append_event, read_events
from app.storage.json_files import read_json, write_json
from app.storage.patches import PatchValidationError, commit_candidate_state_patch
from app.storage.projects import read_project_metadata, write_project_metadata
from app.storage.setup import read_setup_state
from app.storage.text_files import read_text_file, write_text_file

ChapterRoutingDecision = Literal[
    "commit",
    "revise",
    "rewrite",
    "pause",
    "escalate_to_arc",
    "escalate_to_book",
]
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
            if self._process_pending_feedback(metadata):
                return

            if metadata.active_arc_id is None:
                self._plan_initial_story_arc(metadata)
                return

            self._advance_chapter_loop(metadata)
        except Exception as exc:
            metadata.run_status = "failed"
            write_project_metadata(self.context.project_path, metadata)
            safe_error = redact_profile_secrets(str(exc), profile)
            self._emit(
                metadata,
                kind="run_failed",
                loop_layer="system",
                atomic_action="advance_to_next_checkpoint",
                status="failed",
                routing_decision="pause",
                message=f"Harness run failed: {safe_error}",
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
        if not (chapter_path / "goal.md").exists():
            self._generate_chapter_goal(profile, metadata, chapter_id, chapter_path)
            return
        if not (chapter_path / "draft.md").exists():
            self._draft_chapter(profile, metadata, chapter_id, chapter_path)
            return
        if not (chapter_path / "observations.json").exists():
            self._extract_observations(profile, metadata, chapter_id, chapter_path)
            return
        if not (chapter_path / "review.md").exists():
            self._review_chapter(profile, metadata, chapter_id, chapter_path)
            return
        if not (chapter_path / "verification.json").exists():
            self._verify_chapter(profile, metadata, chapter_id, chapter_path)
            return
        if not (chapter_path / "final.md").exists():
            self._write_final_chapter(metadata, chapter_id, chapter_path)
            return
        if not (chapter_path / "candidate_state_patch.json").exists():
            self._generate_candidate_state_patch(profile, metadata, chapter_id, chapter_path)
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

        arc_id = _next_arc_id(self.context.project_path)
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
                    "Return one JSON object with plan_markdown and target_chapter_count. "
                    "plan_markdown must be a concise Markdown plan with arc goal, conflicts, "
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
        result, proposal = self._call_story_arc_plan_action(
            profile,
            metadata,
            action="plan_current_arc",
            system=(
                "You are Novelpilot's story arc loop. Produce visible planning output only, "
                "not private chain-of-thought. Return the requested JSON object only."
            ),
            user=prompt,
            arc_id=arc_id,
        )

        plan_path = arc_path / "plan.md"
        write_text_file(plan_path, proposal.plan_markdown.strip() + "\n")
        write_json(
            arc_path / "state.json",
            {
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
            },
        )
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
            message=f"Revising {arc_id} from user feedback.",
        )
        current_plan = _read_text(plan_path)
        prompt = "\n\n".join(
            _without_empty(
                [
                    "Revise the current rolling story arc plan using the user feedback.",
                    "Keep the plan current-arc-only. Preserve useful constraints, but update pacing, "
                    "focus, or chapter direction where the feedback requires it.",
                    "Return one JSON object with the complete revised Markdown in "
                    "plan_markdown and a revised target_chapter_count integer from 1 through 30.",
                    f"User feedback:\n{feedback}",
                    f"Current arc plan:\n{current_plan}",
                    f"Book settings:\n{_read_text(self.context.project_path / 'book' / 'settings.md')}",
                    "Approved rolling story arc contract:\n"
                    + _read_text(self.context.project_path / "book" / "outline.md"),
                    f"Canon summary:\n{_read_canon_summary(self.context.project_path)}",
                ]
            )
        )
        result, proposal = self._call_story_arc_plan_action(
            profile,
            metadata,
            action="revise_current_arc_plan",
            system=(
                "You are Novelpilot's story arc loop. Revise visible planning artifacts only; "
                "do not reveal private chain-of-thought."
            ),
            user=prompt,
            arc_id=arc_id,
        )
        revised_plan = proposal.plan_markdown.strip() + "\n"
        write_text_file(plan_path, revised_plan)
        write_text_file(
            arc_path / "revision.md",
            "\n\n".join(
                [
                    "# Arc Revision",
                    f"## User Feedback\n{feedback}",
                    f"## Revised Plan\n{proposal.plan_markdown.strip()}",
                ]
            )
            + "\n",
        )
        self._update_arc_revision_state(
            metadata,
            result,
            target_chapter_count=proposal.target_chapter_count,
        )
        self._finish_feedback_artifact_step(
            metadata,
            loop_layer="story_arc",
            atomic_action="revise_current_arc_plan",
            artifact_path=f"arcs/{arc_id}/plan.md",
            message=f"{arc_id} plan revised from user feedback.",
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

    def _update_arc_revision_state(
        self,
        metadata: ProjectMetadata,
        result: ChatResult,
        *,
        target_chapter_count: int,
    ) -> None:
        if metadata.active_arc_id is None:
            return
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
        write_json(state_path, state)

    def _call_story_arc_plan_action(
        self,
        profile: LlmProfile,
        metadata: ProjectMetadata,
        *,
        action: str,
        system: str,
        user: str,
        arc_id: str,
    ) -> tuple[ChatResult, StoryArcPlanProposal]:
        received_characters = 0

        def on_text_delta(chunk: ChatChunk) -> None:
            nonlocal received_characters
            received_characters += len(chunk.text_delta)
            self._emit_model_stream_progress(
                metadata,
                "story_arc",
                action,
                received_characters,
            )

        result = call_llm(
            profile,
            ChatRequest(
                profile_id=profile.id,
                messages=[
                    ChatMessage(role="system", content=system),
                    ChatMessage(role="user", content=user),
                ],
                metadata={
                    "loop_layer": "story_arc",
                    "atomic_action": action,
                    "project_id": metadata.project_id,
                    "arc_id": arc_id,
                    "on_text_delta": on_text_delta,
                },
            ),
        )
        proposal = _story_arc_plan_from_llm(result.content)
        self._emit_model_output(metadata, "story_arc", action, proposal.plan_markdown)
        return result, proposal

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

    def _generate_chapter_goal(
        self,
        profile: LlmProfile,
        metadata: ProjectMetadata,
        chapter_id: str,
        chapter_path: Path,
    ) -> None:
        self._emit_started(metadata, "generate_chapter_goal", f"Generating goal for {chapter_id}.")
        result = self._call_text_action(
            profile,
            metadata,
            action="generate_chapter_goal",
            system="You are Novelpilot's chapter loop. Produce concise visible planning output.",
            user="\n\n".join(
                [
                    f"Write the chapter goal and contract for {chapter_id}.",
                    "Include required scene movement, character pressure, continuity constraints, "
                    "and verification criteria.",
                    "Use the assembled context below. Treat excluded sources as unavailable.",
                    "Assembled context:\n"
                    + self._assembled_context_block(chapter_path / "context_snapshot.json"),
                    f"Arc plan:\n{_read_text(self.context.project_path / 'arcs' / (metadata.active_arc_id or 'arc-001') / 'plan.md')}",
                ]
            ),
        )
        write_text_file(chapter_path / "goal.md", result.content.strip() + "\n")
        self._finish_artifact_step(
            metadata,
            kind="artifact_written",
            atomic_action="generate_chapter_goal",
            artifact_path=f"chapters/{chapter_id}/goal.md",
            message=f"Chapter goal written for {chapter_id}.",
            payload=_llm_usage_payload(profile, result),
        )

    def _draft_chapter(
        self,
        profile: LlmProfile,
        metadata: ProjectMetadata,
        chapter_id: str,
        chapter_path: Path,
    ) -> None:
        self._emit_started(metadata, "draft_chapter", f"Drafting {chapter_id}.")
        result = self._call_text_action(
            profile,
            metadata,
            action="draft_chapter",
            system=(
                "You are Novelpilot's drafting action. Write only visible chapter prose, "
                "not analysis."
            ),
            user="\n\n".join(
                [
                    f"Draft {chapter_id} according to the chapter contract.",
                    f"Goal:\n{_read_text(chapter_path / 'goal.md')}",
                    f"Book settings:\n{_read_text(self.context.project_path / 'book' / 'settings.md')}",
                ]
            ),
        )
        write_text_file(chapter_path / "draft.md", result.content.strip() + "\n")
        self._finish_artifact_step(
            metadata,
            kind="artifact_written",
            atomic_action="draft_chapter",
            artifact_path=f"chapters/{chapter_id}/draft.md",
            message=f"Candidate draft written for {chapter_id}.",
            payload=_llm_usage_payload(profile, result),
        )

    def _extract_observations(
        self,
        profile: LlmProfile,
        metadata: ProjectMetadata,
        chapter_id: str,
        chapter_path: Path,
    ) -> None:
        self._emit_started(
            metadata,
            "extract_candidate_observations",
            f"Extracting candidate observations for {chapter_id}.",
        )
        result = self._call_text_action(
            profile,
            metadata,
            action="extract_candidate_observations",
            system=(
                "Extract candidate observations as JSON only. These are not canon and must "
                "not be treated as committed state."
            ),
            user="\n\n".join(
                [
                    "Return JSON matching this shape: "
                    '{"schema_version":1,"status":"candidate","based_on":"chapters/.../draft.md",'
                    '"events":[],"character_changes":[],"relationship_changes":[],'
                    '"world_fact_candidates":[],"foreshadowing_candidates":[],'
                    '"requires_commit":true}',
                    f"Draft:\n{_read_text(chapter_path / 'draft.md')}",
                ]
            ),
        )
        payload = _parse_json_object(result.content) or {}
        payload["based_on"] = f"chapters/{chapter_id}/draft.md"
        try:
            observations = CandidateObservations.model_validate(payload)
        except ValueError:
            observations = CandidateObservations(
                based_on=f"chapters/{chapter_id}/draft.md",
                events=[{"raw_observation": result.content.strip()}],
            )
        write_json(chapter_path / "observations.json", observations.model_dump(mode="json"))
        self._finish_artifact_step(
            metadata,
            kind="artifact_written",
            atomic_action="extract_candidate_observations",
            artifact_path=f"chapters/{chapter_id}/observations.json",
            message=f"Candidate observations written for {chapter_id}.",
            payload=_llm_usage_payload(profile, result),
        )

    def _review_chapter(
        self,
        profile: LlmProfile,
        metadata: ProjectMetadata,
        chapter_id: str,
        chapter_path: Path,
    ) -> None:
        self._emit_started(metadata, "semantic_review", f"Reviewing {chapter_id}.")
        result = self._call_text_action(
            profile,
            metadata,
            action="semantic_review",
            system=(
                "You are Novelpilot's semantic review action. Produce a readable review with "
                "issues, evidence, literary signals, and revision suggestions."
            ),
            user="\n\n".join(
                [
                    f"Review {chapter_id} against its goal.",
                    f"Goal:\n{_read_text(chapter_path / 'goal.md')}",
                    f"Draft:\n{_read_text(chapter_path / 'draft.md')}",
                    f"Candidate observations:\n{_read_text(chapter_path / 'observations.json')}",
                ]
            ),
        )
        write_text_file(chapter_path / "review.md", result.content.strip() + "\n")
        self._finish_artifact_step(
            metadata,
            kind="artifact_written",
            atomic_action="semantic_review",
            artifact_path=f"chapters/{chapter_id}/review.md",
            message=f"Semantic review written for {chapter_id}.",
            payload=_llm_usage_payload(profile, result),
        )

    def _verify_chapter(
        self,
        profile: LlmProfile,
        metadata: ProjectMetadata,
        chapter_id: str,
        chapter_path: Path,
    ) -> None:
        self._emit_started(metadata, "verify_chapter", f"Verifying {chapter_id}.")
        result = self._call_text_action(
            profile,
            metadata,
            action="verify_chapter",
            system=(
                "You are Novelpilot's chapter verifier. Return JSON only. "
                "Judge whether the candidate chapter satisfies its goal and can be committed."
            ),
            user="\n\n".join(
                [
                    "Return JSON with keys: goal_satisfied, commit_allowed, routing_decision, "
                    "signals, reasons.",
                    "routing_decision must be one of: commit, revise, rewrite, pause, "
                    "escalate_to_arc, escalate_to_book.",
                    "signals must be objects with name, status, evidence. "
                    "status must be passed, failed, or warning.",
                    f"Chapter id: {chapter_id}",
                    f"Goal:\n{_read_text(chapter_path / 'goal.md')}",
                    f"Draft:\n{_read_text(chapter_path / 'draft.md')}",
                    f"Candidate observations:\n{_read_text(chapter_path / 'observations.json')}",
                    f"Semantic review:\n{_read_text(chapter_path / 'review.md')}",
                ]
            ),
        )
        verification = _chapter_verification_from_llm(
            chapter_id,
            result.content,
        )
        write_json(chapter_path / "verification.json", verification.model_dump(mode="json"))
        self._finish_artifact_step(
            metadata,
            kind="verification_completed",
            atomic_action="verify_chapter",
            artifact_path=f"chapters/{chapter_id}/verification.json",
            message=f"Verification completed for {chapter_id}.",
            routing_decision=verification.routing_decision,
            payload=_llm_usage_payload(profile, result),
        )

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

    def _generate_candidate_state_patch(
        self,
        profile: LlmProfile,
        metadata: ProjectMetadata,
        chapter_id: str,
        chapter_path: Path,
    ) -> None:
        self._emit_started(
            metadata,
            "generate_candidate_state_patch",
            f"Generating candidate state patch for {chapter_id}.",
        )
        canon_summary = _read_canon_summary(self.context.project_path)
        result = self._call_text_action(
            profile,
            metadata,
            action="generate_candidate_state_patch",
            system=(
                "Generate candidate_state_patch.json as JSON only. The patch is candidate "
                "material; the harness will validate it before canon changes."
            ),
            user="\n\n".join(
                [
                    "Allowed target_file values: canon/characters.json, "
                    "canon/relationships.json, canon/world_facts.json, canon/foreshadowing.json.",
                    "Each operation needs op, target_file, target_id, expected_version, value, "
                    "evidence, and rationale. Evidence quotes from final.md must be exact substrings.",
                    f"Final chapter path: chapters/{chapter_id}/final.md",
                    f"Final chapter:\n{_read_text(chapter_path / 'final.md')}",
                    f"Candidate observations:\n{_read_text(chapter_path / 'observations.json')}",
                    f"Current canon:\n{canon_summary}",
                ]
            ),
        )
        based_on = {
            "chapter_final": f"chapters/{chapter_id}/final.md",
            "observations": f"chapters/{chapter_id}/observations.json",
        }
        payload = _parse_json_object(result.content)
        if payload is None:
            self._reject_candidate_state_patch_generation(
                metadata,
                chapter_id,
                chapter_path,
                ["State patch generator output could not be parsed as JSON."],
                payload=_llm_usage_payload(profile, result),
            )
            return

        payload["based_on"] = based_on
        try:
            patch = CandidateStatePatch.model_validate(payload)
        except ValueError as exc:
            self._reject_candidate_state_patch_generation(
                metadata,
                chapter_id,
                chapter_path,
                ["State patch generator output failed schema validation: " + _error_summary(exc)],
                payload=_llm_usage_payload(profile, result),
            )
            return
        write_json(chapter_path / "candidate_state_patch.json", patch.model_dump(mode="json"))
        self._finish_artifact_step(
            metadata,
            kind="state_patch_candidate_created",
            atomic_action="generate_candidate_state_patch",
            artifact_path=f"chapters/{chapter_id}/candidate_state_patch.json",
            message=f"Candidate state patch written for {chapter_id}.",
            payload=_llm_usage_payload(profile, result),
        )

    def _reject_candidate_state_patch_generation(
        self,
        metadata: ProjectMetadata,
        chapter_id: str,
        chapter_path: Path,
        reasons: list[str],
        *,
        payload: dict[str, object] | None = None,
    ) -> None:
        metadata.run_status = "waiting_for_user"
        write_project_metadata(self.context.project_path, metadata)
        write_json(
            chapter_path / "state_patch_rejection.json",
            PatchValidationResult(
                schema="failed",
                versions="passed",
                evidence="passed",
                conflicts="passed",
                reasons=reasons,
            ).model_dump(mode="json", by_alias=True),
        )
        self._emit(
            metadata,
            kind="state_patch_rejected",
            loop_layer="chapter",
            atomic_action="generate_candidate_state_patch",
            status="failed",
            artifact_path=f"chapters/{chapter_id}/state_patch_rejection.json",
            routing_decision="pause",
            message=f"Candidate state patch generation failed for {chapter_id}.",
            payload={**(payload or {}), "reasons": reasons},
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
            metadata.run_status = "waiting_for_user"
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
                routing_decision="pause",
                message=f"Candidate state patch rejected for {chapter_id}.",
                payload={"reasons": exc.result.reasons},
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

    def _call_text_action(
        self,
        profile: LlmProfile,
        metadata: ProjectMetadata,
        *,
        action: str,
        system: str,
        user: str,
    ) -> ChatResult:
        emitted_delta = False
        feedback_block = self._feedback_prompt_block(
            {
                "apply_to_current_chapter_context",
                "revise_current_arc_plan",
                "escalate_to_book_loop",
            }
        )
        effective_user = "\n\n".join(_without_empty([user, feedback_block]))

        def on_text_delta(chunk: ChatChunk) -> None:
            nonlocal emitted_delta
            emitted_delta = True
            self._emit_model_output_delta(metadata, "chapter", action, chunk.text_delta)

        result = call_llm(
            profile,
            ChatRequest(
                profile_id=profile.id,
                messages=[
                    ChatMessage(role="system", content=system),
                    ChatMessage(role="user", content=effective_user),
                ],
                metadata={
                    "loop_layer": "chapter",
                    "atomic_action": action,
                    "project_id": metadata.project_id,
                    "on_text_delta": on_text_delta,
                },
            ),
        )
        if not emitted_delta:
            self._emit_model_output(metadata, "chapter", action, result.content)
        return result

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

        result = call_llm(
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
    for entry in arcs_path.iterdir():
        if not entry.is_dir() or not entry.name.startswith("arc-"):
            continue
        try:
            existing_numbers.append(int(entry.name.removeprefix("arc-")))
        except ValueError:
            continue
    next_number = max(existing_numbers, default=0) + 1
    return f"arc-{next_number:03d}"


def _parse_json_object(text: str) -> dict[str, object] | None:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.startswith("json"):
            stripped = stripped[4:].strip()
    decoder = json.JSONDecoder()
    for index, char in enumerate(stripped):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(stripped[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _story_arc_plan_from_llm(content: str) -> StoryArcPlanProposal:
    payload = _parse_json_object(content)
    if payload is None:
        raise ValueError("Story arc planner output could not be parsed as JSON.")
    try:
        proposal = StoryArcPlanProposal.model_validate(payload)
    except ValueError as exc:
        raise ValueError("Story arc planner output did not match the required schema.") from exc
    plan_markdown = proposal.plan_markdown.strip()
    if not plan_markdown:
        raise ValueError("Story arc planner returned an empty Markdown plan.")
    return proposal.model_copy(update={"plan_markdown": plan_markdown})


def _text_chunks(text: str, chunk_size: int = 1000) -> list[str]:
    if not text:
        return []
    return [text[index : index + chunk_size] for index in range(0, len(text), chunk_size)]


def _without_empty(parts: list[str]) -> list[str]:
    return [part for part in parts if part.strip()]


def _chapter_verification_from_llm(
    chapter_id: str,
    content: str,
) -> ChapterVerification:
    payload = _parse_json_object(content)
    if payload is None:
        return _failed_chapter_verification(
            chapter_id,
            "Verifier output could not be parsed as JSON; chapter must be retried.",
        )

    signals = _verification_signals_from_payload(payload.get("signals"))
    if not signals:
        signals = [
            VerificationSignal(
                name="semantic_verifier_payload",
                status="warning",
                evidence="Verifier returned no structured signals.",
            )
        ]

    goal_satisfied = _bool_value(payload.get("goal_satisfied"), False)
    commit_allowed = goal_satisfied and _bool_value(payload.get("commit_allowed"), False)
    routing_decision = _routing_decision_value(
        payload.get("routing_decision"),
        "commit" if commit_allowed else "rewrite",
    )
    raw_reasons = payload.get("reasons")
    reasons = (
        [reason for reason in raw_reasons if isinstance(reason, str) and reason.strip()]
        if isinstance(raw_reasons, list)
        else []
    )
    if not isinstance(payload.get("goal_satisfied"), bool):
        reasons.append("Verifier output is missing boolean goal_satisfied.")
    if not isinstance(payload.get("commit_allowed"), bool):
        reasons.append("Verifier output is missing boolean commit_allowed.")
    if not commit_allowed and routing_decision == "commit":
        routing_decision = "rewrite"
        reasons.append(
            "Verifier cannot route to commit when goal_satisfied or commit_allowed is false."
        )

    try:
        return ChapterVerification(
            chapter_id=chapter_id,
            goal_satisfied=goal_satisfied,
            commit_allowed=commit_allowed,
            routing_decision=routing_decision,
            signals=[
                *signals,
                VerificationSignal(
                    name="candidate_boundary",
                    status="passed",
                    evidence="observations.json remains candidate-only and has not updated canon.",
                ),
            ],
            reasons=reasons,
        )
    except ValueError as exc:
        return _failed_chapter_verification(
            chapter_id,
            "Verifier output failed schema validation: " + _error_summary(exc),
        )


def _failed_chapter_verification(chapter_id: str, reason: str) -> ChapterVerification:
    return ChapterVerification(
        chapter_id=chapter_id,
        goal_satisfied=False,
        commit_allowed=False,
        routing_decision="rewrite",
        signals=[
            VerificationSignal(
                name="semantic_verifier_payload",
                status="failed",
                evidence=reason,
            ),
            VerificationSignal(
                name="candidate_boundary",
                status="passed",
                evidence="observations.json remains candidate-only and has not updated canon.",
            ),
        ],
        reasons=[reason],
    )


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


def _verification_signals_from_payload(value: Any) -> list[VerificationSignal]:
    if not isinstance(value, list):
        return []

    signals: list[VerificationSignal] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        status = item.get("status")
        if not isinstance(name, str) or status not in {"passed", "failed", "warning"}:
            continue
        evidence = item.get("evidence")
        signals.append(
            VerificationSignal(
                name=name,
                status=status,
                evidence=evidence if isinstance(evidence, str) else None,
            )
        )
    return signals


def _bool_value(value: Any, fallback: bool) -> bool:
    return value if isinstance(value, bool) else fallback


def _error_summary(exc: ValueError) -> str:
    return str(exc).splitlines()[0]


def _routing_decision_value(
    value: Any,
    fallback: ChapterRoutingDecision,
) -> ChapterRoutingDecision:
    allowed: set[ChapterRoutingDecision] = {
        "commit",
        "revise",
        "rewrite",
        "pause",
        "escalate_to_arc",
        "escalate_to_book",
    }
    return value if isinstance(value, str) and value in allowed else fallback


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
