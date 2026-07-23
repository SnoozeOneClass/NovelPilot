from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.agents.contracts import BookProgressAssessment
from app.db.uow import StoreSession
from app.domain.commands import (
    CommandEffect,
    CommandEnvelope,
    CommandExecution,
    CommandPreconditionError,
    EventDraft,
)
from app.store.arcs import ArcRecord, ArcWorkspaceRecord
from app.store.books import BookBaselineRecord, BookRecord, BookWorkspaceRecord
from app.store.command_bus import CommandBus
from app.store.completion import (
    BookCompletionRecord,
    TerminalArcRecord,
    TerminalChapterRecord,
)
from app.store.content import PreparedContent, prepare_canonical_json
from app.store.execution import SuccessfulTaskRecord
from app.store.runs import GenerationRunRecord


class ApplyBookProgressRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    book_id: str
    task_id: str
    attempt_id: str
    expected_book_baseline_id: str
    expected_canon_baseline_id: str
    expected_book_workspace_lock_version: int = Field(ge=1)
    terminal_arc_id: str
    terminal_arc_baseline_id: str
    terminal_chapter_id: str
    terminal_chapter_baseline_id: str

    @field_validator(
        "project_id",
        "book_id",
        "task_id",
        "attempt_id",
        "expected_book_baseline_id",
        "expected_canon_baseline_id",
        "terminal_arc_id",
        "terminal_arc_baseline_id",
        "terminal_chapter_id",
        "terminal_chapter_baseline_id",
    )
    @classmethod
    def _identity_non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Book progress identities must be non-blank.")
        return value


class ApplyBookProgressResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    book_id: str
    decision: Literal["continue", "plan_final_arc", "complete", "needs_user"]
    action: Literal["created_regular_arc", "created_final_arc", "completed", "await_user"]
    arc_id: str | None = None
    completion_id: str | None = None

    @model_validator(mode="after")
    def _action_identity(self) -> ApplyBookProgressResult:
        creates_arc = self.action in {"created_regular_arc", "created_final_arc"}
        if creates_arc != (self.arc_id is not None):
            raise ValueError("Only an Arc creation action carries arc_id.")
        if (self.action == "completed") != (self.completion_id is not None):
            raise ValueError("Only a completion action carries completion_id.")
        return self


class ReopenBookRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    book_id: str
    expected_completion_id: str


class ReopenBookResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    book_id: str
    previous_completion_id: str
    generation_run_id: str
    run_number: int = Field(ge=2)


@dataclass(frozen=True, slots=True)
class _BoundarySnapshot:
    task: SuccessfulTaskRecord
    book: BookRecord
    baseline: BookBaselineRecord
    workspace: BookWorkspaceRecord
    terminal_arc: TerminalArcRecord
    terminal_chapter: TerminalChapterRecord
    committed_chapter_count: int


class CompletionCommandService:
    def __init__(
        self,
        command_bus: CommandBus,
        *,
        id_factory: Callable[[], str] | None = None,
        now_ms: Callable[[], int] | None = None,
    ) -> None:
        self._command_bus = command_bus
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex)
        self._now_ms = now_ms or (lambda: time.time_ns() // 1_000_000)

    async def reopen_book(
        self,
        request: ReopenBookRequest,
        *,
        idempotency_key: str,
    ) -> CommandExecution[ReopenBookResult]:
        timestamp = self._now_ms()
        run_id = self._id_factory()
        envelope = CommandEnvelope.for_request(
            project_id=request.project_id,
            idempotency_key=idempotency_key,
            command_kind="reopen_book",
            request_schema="reopen_book.request.v1",
            request_payload=request,
            actor="user",
            command_id=self._id_factory(),
            created_at_ms=timestamp,
        )

        async def handler(session: StoreSession) -> CommandEffect[ReopenBookResult]:
            project = await session.projects.get(request.project_id)
            book = await session.books.get_for_project(request.project_id)
            if (
                project is None
                or project.lifecycle_status != "completed"
                or book is None
                or book.id != request.book_id
                or book.lifecycle_status != "completed"
                or book.current_completion_id != request.expected_completion_id
                or await session.runs.get_open_for_project(request.project_id) is not None
            ):
                raise CommandPreconditionError("Completed Book reopen facts are stale.")
            run_number = await session.runs.next_run_number(project_id=request.project_id)
            if run_number < 2:
                raise CommandPreconditionError("A reopened Book requires a prior Run.")
            if not await session.books.reopen(
                project_id=request.project_id,
                book_id=request.book_id,
                expected_completion_id=request.expected_completion_id,
                updated_at_ms=timestamp,
            ):
                raise CommandPreconditionError("Book completion pointer could not be cleared.")
            if not await session.projects.set_lifecycle_status(
                project_id=request.project_id,
                expected_status="completed",
                new_status="active",
                updated_at_ms=timestamp,
            ):
                raise CommandPreconditionError("Project could not be reopened.")
            await session.runs.insert(
                GenerationRunRecord(
                    id=run_id,
                    project_id=request.project_id,
                    run_number=run_number,
                    status="waiting_for_user",
                    desired_state="running",
                    lock_version=1,
                    wait_reason_code="reopen_direction_required",
                    blocking_task_id=None,
                    failure_code=None,
                    failure_ref_id=None,
                    created_at_ms=timestamp,
                    started_at_ms=None,
                    updated_at_ms=timestamp,
                    finished_at_ms=None,
                )
            )
            result = ReopenBookResult(
                project_id=request.project_id,
                book_id=request.book_id,
                previous_completion_id=request.expected_completion_id,
                generation_run_id=run_id,
                run_number=run_number,
            )
            return CommandEffect(
                result=result,
                events=(
                    EventDraft(
                        event_type="book.reopened",
                        aggregate_type="book",
                        aggregate_id=request.book_id,
                        payload={
                            "previous_completion_id": request.expected_completion_id,
                            "generation_run_id": run_id,
                            "run_number": run_number,
                        },
                    ),
                ),
            )

        return await self._command_bus.execute(
            envelope=envelope,
            result_type=ReopenBookResult,
            handler=handler,
        )

    async def apply_assessment(
        self,
        request: ApplyBookProgressRequest,
        *,
        idempotency_key: str,
    ) -> CommandExecution[ApplyBookProgressResult]:
        timestamp = self._now_ms()
        completion_id = self._id_factory()
        gate_manifest_ref_id = self._id_factory()
        arc_id = self._id_factory()
        arc_workspace_id = self._id_factory()
        async with self._command_bus.read_unit_of_work() as session:
            task = await session.execution.get_successful_task(
                project_id=request.project_id,
                task_id=request.task_id,
                attempt_id=request.attempt_id,
            )
            if task is None:
                raise CommandPreconditionError("Book assessment task has no successful result.")
            assessment = BookProgressAssessment.model_validate_json(
                (
                    await session.content.get_packed(
                        project_id=request.project_id,
                        ref_id=task.result_ref_id,
                    )
                ).unpack_and_verify()
            )
            snapshot = await self._read_boundary(session, request=request, task=task)
        manifest = {
            "schema": "book-completion-gate-manifest-v1",
            "project_id": request.project_id,
            "book_id": request.book_id,
            "book_baseline_id": snapshot.baseline.id,
            "canon_baseline_id": request.expected_canon_baseline_id,
            "terminal_arc_id": snapshot.terminal_arc.arc_id,
            "terminal_arc_baseline_id": snapshot.terminal_arc.arc_baseline_id,
            "terminal_arc_purpose": snapshot.terminal_arc.purpose,
            "terminal_chapter_id": snapshot.terminal_chapter.chapter_id,
            "terminal_chapter_baseline_id": snapshot.terminal_chapter.chapter_baseline_id,
            "committed_chapter_count": snapshot.committed_chapter_count,
            "minimum_chapter_count": snapshot.baseline.minimum_chapter_count,
            "maximum_chapter_count": snapshot.baseline.maximum_chapter_count,
            "source_task_id": task.task_id,
            "decision": assessment.decision,
        }
        prepared_manifest = prepare_canonical_json(manifest)
        envelope = CommandEnvelope.for_request(
            project_id=request.project_id,
            idempotency_key=idempotency_key,
            command_kind="apply_book_progress_assessment",
            request_schema="apply_book_progress_assessment.request.v1",
            request_payload=request,
            actor="engine",
            command_id=self._id_factory(),
            run_id=task.run_id,
            source_task_id=task.task_id,
            created_at_ms=timestamp,
        )

        async def handler(session: StoreSession) -> CommandEffect[ApplyBookProgressResult]:
            current_task = await session.execution.get_successful_task(
                project_id=request.project_id,
                task_id=request.task_id,
                attempt_id=request.attempt_id,
            )
            if current_task != task:
                raise CommandPreconditionError("Book assessment task changed concurrently.")
            current = await self._read_boundary(session, request=request, task=task)
            if current != snapshot:
                raise CommandPreconditionError("Book completion boundary changed concurrently.")
            if assessment.decision == "needs_user":
                if not await session.execution.mark_delivery_applied(
                    project_id=request.project_id,
                    task_id=task.task_id,
                    attempt_id=task.attempt_id,
                    command_id=envelope.command_id,
                    updated_at_ms=timestamp,
                ):
                    raise CommandPreconditionError("Book assessment delivery changed concurrently.")
                run = await session.runs.get(
                    project_id=request.project_id,
                    run_id=task.run_id,
                )
                if run is not None and run.status == "running" and not await session.runs.wait_for_user(
                    run_id=run.id,
                    reason_code="book_completion_needs_user",
                    now_ms=timestamp,
                ):
                    raise CommandPreconditionError("Run could not wait for Book completion input.")
                result = ApplyBookProgressResult(
                    project_id=request.project_id,
                    book_id=request.book_id,
                    decision=assessment.decision,
                    action="await_user",
                )
                return CommandEffect(
                    result=result,
                    events=(self._assessment_event(request, assessment, "await_user"),),
                )

            await self._require_safe_boundary(session, snapshot=snapshot)
            events: tuple[EventDraft, ...]
            if assessment.decision in {"continue", "plan_final_arc"}:
                result, arc_event = await self._create_next_arc(
                    session,
                    request=request,
                    snapshot=snapshot,
                    assessment=assessment,
                    arc_id=arc_id,
                    workspace_id=arc_workspace_id,
                    timestamp=timestamp,
                )
                events = (
                    self._assessment_event(request, assessment, result.action),
                    arc_event,
                )
            else:
                result = await self._commit_completion(
                    session,
                    request=request,
                    snapshot=snapshot,
                    task=task,
                    completion_id=completion_id,
                    gate_manifest_ref_id=gate_manifest_ref_id,
                    prepared_manifest=prepared_manifest,
                    timestamp=timestamp,
                )
                events = (
                    self._assessment_event(request, assessment, "completed"),
                    EventDraft(
                        event_type="book.completed",
                        aggregate_type="book",
                        aggregate_id=request.book_id,
                        payload={
                            "completion_id": completion_id,
                            "committed_chapter_count": snapshot.committed_chapter_count,
                        },
                    ),
                    EventDraft(
                        event_type="run.completed",
                        aggregate_type="run",
                        aggregate_id=task.run_id,
                        payload={"completion_id": completion_id},
                    ),
                )
            if not await session.execution.mark_delivery_applied(
                project_id=request.project_id,
                task_id=task.task_id,
                attempt_id=task.attempt_id,
                command_id=envelope.command_id,
                updated_at_ms=timestamp,
            ):
                raise CommandPreconditionError("Book assessment delivery changed concurrently.")
            return CommandEffect(result=result, events=events)

        return await self._command_bus.execute(
            envelope=envelope,
            result_type=ApplyBookProgressResult,
            handler=handler,
        )

    @staticmethod
    async def _read_boundary(
        session: StoreSession,
        *,
        request: ApplyBookProgressRequest,
        task: SuccessfulTaskRecord,
    ) -> _BoundarySnapshot:
        project = await session.projects.get(request.project_id)
        book = await session.books.get_for_project(request.project_id)
        workspace = await session.books.get_workspace(
            project_id=request.project_id,
            book_id=request.book_id,
        )
        baseline = await session.books.get_baseline(
            project_id=request.project_id,
            book_id=request.book_id,
            baseline_id=request.expected_book_baseline_id,
        )
        terminal_arc = await session.completion.get_terminal_arc(
            project_id=request.project_id,
            book_id=request.book_id,
        )
        terminal_chapter = (
            None
            if terminal_arc is None
            else await session.completion.get_terminal_chapter(
                project_id=request.project_id,
                book_id=request.book_id,
                arc_id=terminal_arc.arc_id,
            )
        )
        if (
            project is None
            or project.lifecycle_status != "active"
            or project.current_canon_baseline_id != request.expected_canon_baseline_id
            or book is None
            or book.id != request.book_id
            or book.lifecycle_status != "active"
            or book.current_baseline_id != request.expected_book_baseline_id
            or book.current_completion_id is not None
            or baseline is None
            or workspace is None
            or workspace.lock_version != request.expected_book_workspace_lock_version
            or workspace.state != "idle"
            or workspace.base_book_baseline_id != baseline.id
            or terminal_arc is None
            or terminal_arc.lifecycle_status != "completed"
            or terminal_arc.arc_id != request.terminal_arc_id
            or terminal_arc.arc_baseline_id != request.terminal_arc_baseline_id
            or terminal_chapter is None
            or terminal_chapter.chapter_id != request.terminal_chapter_id
            or terminal_chapter.chapter_baseline_id
            != request.terminal_chapter_baseline_id
            or task.delivery_state != "pending"
            or task.role != "book_strategist"
            or task.task_kind != "book.assess_progress_or_completion"
            or task.scope_layer != "book"
            or task.book_id != request.book_id
            or task.arc_id is not None
            or task.chapter_id is not None
            or task.workspace_lock_version != workspace.lock_version
            or task.book_baseline_id != baseline.id
            or task.arc_baseline_id is not None
            or task.chapter_baseline_id is not None
            or task.canon_baseline_id != request.expected_canon_baseline_id
        ):
            raise CommandPreconditionError("Book assessment facts are stale or mismatched.")
        count = await session.completion.count_committed_chapters(book_id=request.book_id)
        return _BoundarySnapshot(
            task=task,
            book=book,
            baseline=baseline,
            workspace=workspace,
            terminal_arc=terminal_arc,
            terminal_chapter=terminal_chapter,
            committed_chapter_count=count,
        )

    @staticmethod
    async def _require_safe_boundary(
        session: StoreSession,
        *,
        snapshot: _BoundarySnapshot,
    ) -> None:
        if await session.completion.has_lifecycle_blocker(
            project_id=snapshot.book.project_id,
            book_id=snapshot.book.id,
            source_task_id=snapshot.task.task_id,
        ):
            raise CommandPreconditionError("Book boundary has unfinished lifecycle work.")
        if await session.feedback.has_unapplied(project_id=snapshot.book.project_id):
            raise CommandPreconditionError("Book boundary has unapplied user feedback.")
        if await session.changes.has_open(project_id=snapshot.book.project_id):
            raise CommandPreconditionError("Book boundary has an open change request.")

    @staticmethod
    async def _create_next_arc(
        session: StoreSession,
        *,
        request: ApplyBookProgressRequest,
        snapshot: _BoundarySnapshot,
        assessment: BookProgressAssessment,
        arc_id: str,
        workspace_id: str,
        timestamp: int,
    ) -> tuple[ApplyBookProgressResult, EventDraft]:
        if snapshot.terminal_arc.purpose == "final":
            raise CommandPreconditionError("A completed final Arc cannot be followed by another Arc.")
        if snapshot.committed_chapter_count >= snapshot.baseline.maximum_chapter_count:
            raise CommandPreconditionError("Book maximum Chapter count forbids another Arc.")
        purpose: Literal["regular", "final"] = (
            "final" if assessment.decision == "plan_final_arc" else "regular"
        )
        ordinal = await session.arcs.next_ordinal(book_id=request.book_id)
        await session.arcs.insert(
            ArcRecord(
                id=arc_id,
                project_id=request.project_id,
                book_id=request.book_id,
                ordinal=ordinal,
                purpose=purpose,
                lifecycle_status="planning",
                current_baseline_id=None,
                created_at_ms=timestamp,
                updated_at_ms=timestamp,
                completed_at_ms=None,
            )
        )
        await session.arcs.insert_workspace(
            ArcWorkspaceRecord(
                id=workspace_id,
                project_id=request.project_id,
                book_id=request.book_id,
                arc_id=arc_id,
                state="active",
                lock_version=1,
                base_arc_baseline_id=None,
                book_baseline_id=snapshot.baseline.id,
                canon_baseline_id=request.expected_canon_baseline_id,
                prior_arc_id=snapshot.terminal_arc.arc_id,
                prior_arc_baseline_id=snapshot.terminal_arc.arc_baseline_id,
                plan_ref_id=None,
                recommended_target_chapter_count=None,
                repair_policy_id="semantic-repair-v1",
                semantic_repair_count=0,
                semantic_repair_limit=5,
                stale_reason_code=None,
                stale_at_ms=None,
                created_at_ms=timestamp,
                updated_at_ms=timestamp,
            )
        )
        action: Literal["created_regular_arc", "created_final_arc"] = (
            "created_final_arc" if purpose == "final" else "created_regular_arc"
        )
        return (
            ApplyBookProgressResult(
                project_id=request.project_id,
                book_id=request.book_id,
                decision=assessment.decision,
                action=action,
                arc_id=arc_id,
            ),
            EventDraft(
                event_type="arc.created",
                aggregate_type="arc",
                aggregate_id=arc_id,
                payload={
                    "book_id": request.book_id,
                    "ordinal": ordinal,
                    "purpose": purpose,
                    "source_task_id": snapshot.task.task_id,
                },
            ),
        )

    @staticmethod
    async def _commit_completion(
        session: StoreSession,
        *,
        request: ApplyBookProgressRequest,
        snapshot: _BoundarySnapshot,
        task: SuccessfulTaskRecord,
        completion_id: str,
        gate_manifest_ref_id: str,
        prepared_manifest: PreparedContent,
        timestamp: int,
    ) -> ApplyBookProgressResult:
        if not (
            snapshot.baseline.minimum_chapter_count
            <= snapshot.committed_chapter_count
            <= snapshot.baseline.maximum_chapter_count
        ):
            raise CommandPreconditionError(
                "Committed Chapter count does not satisfy the Book completion contract."
            )
        latest = await session.completion.get_latest_identity(book_id=request.book_id)
        completion_version = await session.completion.next_version(book_id=request.book_id)
        parent_completion_id = None if latest is None else latest[0]
        expected_version = 1 if latest is None else latest[1] + 1
        if completion_version != expected_version:
            raise CommandPreconditionError("Book completion version is not contiguous.")
        gate_manifest_ref = await session.content.put(
            project_id=request.project_id,
            prepared=prepared_manifest,
            semantic_kind="book.completion_gate_manifest",
            media_type="application/json",
            schema_id="book-completion-gate-manifest",
            schema_version=1,
            ref_id=gate_manifest_ref_id,
            created_at_ms=timestamp,
        )
        await session.completion.insert(
            BookCompletionRecord(
                id=completion_id,
                project_id=request.project_id,
                book_id=request.book_id,
                completion_version=completion_version,
                parent_completion_id=parent_completion_id,
                book_baseline_id=snapshot.baseline.id,
                terminal_arc_id=snapshot.terminal_arc.arc_id,
                terminal_arc_baseline_id=snapshot.terminal_arc.arc_baseline_id,
                terminal_chapter_id=snapshot.terminal_chapter.chapter_id,
                terminal_chapter_baseline_id=(
                    snapshot.terminal_chapter.chapter_baseline_id
                ),
                canon_baseline_id=request.expected_canon_baseline_id,
                committed_chapter_count=snapshot.committed_chapter_count,
                source_task_id=task.task_id,
                completion_decision_ref_id=task.result_ref_id,
                gate_manifest_ref_id=gate_manifest_ref.id,
                created_at_ms=timestamp,
            )
        )
        if not await session.books.commit_completion(
            project_id=request.project_id,
            book_id=request.book_id,
            expected_baseline_id=snapshot.baseline.id,
            completion_id=completion_id,
            updated_at_ms=timestamp,
        ):
            raise CommandPreconditionError("Book completion pointer CAS failed.")
        if not await session.projects.set_lifecycle_status(
            project_id=request.project_id,
            expected_status="active",
            new_status="completed",
            updated_at_ms=timestamp,
        ):
            raise CommandPreconditionError("Project completion status CAS failed.")
        if not await session.runs.complete(run_id=task.run_id, now_ms=timestamp):
            raise CommandPreconditionError("Generation Run could not enter completed state.")
        return ApplyBookProgressResult(
            project_id=request.project_id,
            book_id=request.book_id,
            decision="complete",
            action="completed",
            completion_id=completion_id,
        )

    @staticmethod
    def _assessment_event(
        request: ApplyBookProgressRequest,
        assessment: BookProgressAssessment,
        action: str,
    ) -> EventDraft:
        return EventDraft(
            event_type="book.progress_assessed",
            aggregate_type="book",
            aggregate_id=request.book_id,
            payload={
                "decision": assessment.decision,
                "action": action,
                "rationale": assessment.rationale,
                "unresolved_requirements": assessment.unresolved_requirements,
            },
        )
