from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol, cast

from sqlalchemy.ext.asyncio import AsyncEngine

from app.agents.binding import ProfileCredential
from app.agents.contracts import AgentRole
from app.agents.executor import AgentExecutionResult, AgentExecutor
from app.agents.registry import DEFAULT_TASK_REGISTRY, TaskRegistry
from app.db.uow import UnitOfWork
from app.domain.arc.commands import ArcCommandService
from app.domain.arc.contracts import (
    ApplyArcTaskRequest,
    CommitArcAutoRequest,
    CreateStoryArcRequest,
    RecordArcReviewRequest,
    RebaseStaleArcRequest,
    SubmitArcRequest,
)
from app.domain.book.commands import BookCommandService
from app.domain.book.contracts import (
    ApplyBookCandidateTaskRequest,
    ApplyBookDiscussionTaskRequest,
    BookDiscussionState,
    RecordBookReviewRequest,
    SubmitBookRequest,
)
from app.domain.change_requests import ActivateChangeRequest, ChangeRequestCommandService
from app.domain.chapter.commands import ChapterCommandService
from app.domain.chapter.contracts import (
    ApplyChapterTaskRequest,
    CommitChapterRequest,
    CreateChapterRequest,
    RecordChapterReviewRequest,
    RebaseStaleChapterRequest,
    SubmitChapterRequest,
)
from app.domain.completion import ApplyBookProgressRequest, CompletionCommandService
from app.profiles import ProfileCatalog
from app.runtime.context import HarnessContextBuilder
from app.store.agent_tasks import AgentTaskStore
from app.store.command_bus import CommandBus
from app.store.content import prepare_canonical_json
from app.store.execution import ActionableTaskRecord
from app.store.runs import GenerationRunRecord


class HarnessInvariantError(RuntimeError):
    """Authoritative facts do not describe one legal next Domain Harness action."""


class TaskExecutor(Protocol):
    async def execute(
        self,
        *,
        project_id: str,
        task_id: str,
        attempt_id: str,
        owner_instance_id: str,
        lease_token: str,
        credential: ProfileCredential,
    ) -> AgentExecutionResult: ...


@dataclass(frozen=True, slots=True)
class _TaskInstruction:
    role: AgentRole
    task_kind: str
    book_id: str
    workspace_lock_version: int
    book_baseline_id: str | None
    arc_id: str | None = None
    arc_baseline_id: str | None = None
    chapter_id: str | None = None
    chapter_baseline_id: str | None = None


@dataclass(frozen=True, slots=True)
class _CommandInstruction:
    kind: Literal[
        "activate_change",
        "create_arc",
        "create_chapter",
        "rebase_arc",
        "rebase_chapter",
        "submit_book",
        "submit_arc",
        "submit_chapter",
        "commit_arc_auto",
        "commit_chapter",
    ]
    request: object
    idempotency_key: str


type _Instruction = _TaskInstruction | _CommandInstruction | None


_SEMANTIC_GOALS: dict[str, str] = {
    "book.discuss": "Advance one concrete creator decision while preserving the full discussion record.",
    "book.synthesize": "Synthesize the approved discussion into a coherent whole-book baseline candidate.",
    "book.revise": "Revise the whole-book candidate only for the active formal Book change.",
    "book.repair": "Repair only the evaluator-authorized Book components.",
    "book.assess_progress_or_completion": (
        "At a completed Story Arc boundary, decide whether to continue, plan the final Arc, "
        "complete the Book, or request user input."
    ),
    "arc.plan": "Plan the next rolling Story Arc under the approved Book and current Canon.",
    "arc.revise": "Revise the current Story Arc only for its active formal change.",
    "arc.repair": "Repair only the evaluator-authorized Story Arc components.",
    "chapter.plan": "Plan the next Chapter under the approved Book, Arc, and current Canon.",
    "chapter.revise.plan": "Revise the committed Chapter plan only within the active change.",
    "chapter.draft": "Write the complete Chapter prose from the frozen Chapter plan.",
    "chapter.revise.draft": "Write the complete revised Chapter prose from the revised plan.",
    "chapter.observe": "Observe the Chapter prose and propose evidence-bound Canon changes.",
    "chapter.revise.observe": "Re-observe revised Chapter prose and propose evidence-bound Canon changes.",
    "chapter.repair.prose": "Repair the complete Chapter prose only within the authorized scope.",
    "chapter.repair.observation": (
        "Repair the Chapter observations and Canon proposals only within the authorized scope."
    ),
    "evaluate.book": "Independently evaluate the whole-book candidate against the Book rubric.",
    "verify_repair.book": "Verify the repaired Book candidate against the prior findings.",
    "evaluate.arc": "Independently evaluate the Story Arc candidate against upstream facts.",
    "verify_repair.arc": "Verify the repaired Story Arc candidate against the prior findings.",
    "evaluate.chapter": "Independently evaluate the complete Chapter candidate and Canon proposal.",
    "verify_repair.chapter": "Verify the repaired Chapter candidate against the prior findings.",
}


class DomainRunDriver:
    """Execute exactly one durable Harness action, then return control to Run Engine."""

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        profile_catalog: ProfileCatalog,
        registry: TaskRegistry = DEFAULT_TASK_REGISTRY,
        executor: TaskExecutor | None = None,
        owner_instance_id: str | None = None,
        now_ms: Callable[[], int] | None = None,
    ) -> None:
        self._engine = engine
        self._profiles = profile_catalog
        self._registry = registry
        self._context = HarnessContextBuilder(engine)
        self._tasks = AgentTaskStore(engine)
        self._executor = executor or AgentExecutor(engine, registry=registry)
        self._owner_instance_id = owner_instance_id or f"domain-driver-{uuid.uuid4().hex}"
        self._now_ms = now_ms or (lambda: time.time_ns() // 1_000_000)
        bus = CommandBus(engine)
        self._books = BookCommandService(bus)
        self._arcs = ArcCommandService(bus)
        self._chapters = ChapterCommandService(bus)
        self._changes = ChangeRequestCommandService(bus)
        self._completion = CompletionCommandService(bus)

    async def drive_one(self, run: GenerationRunRecord) -> None:
        async with UnitOfWork(self._engine) as store:
            actionable = await store.execution.find_actionable_for_run(run_id=run.id)
        if actionable is not None:
            if actionable.task_status == "queued":
                await self._execute_task(actionable)
            else:
                await self._deliver_task(actionable)
            return

        instruction = await self._decide_next(run)
        if isinstance(instruction, _TaskInstruction):
            await self._freeze_task(run, instruction)
        elif isinstance(instruction, _CommandInstruction):
            await self._apply_command(instruction)

    async def _execute_task(self, task: ActionableTaskRecord) -> None:
        resolved = self._profiles.resolve(task.profile_id)
        await self._executor.execute(
            project_id=task.project_id,
            task_id=task.task_id,
            attempt_id=task.attempt_id,
            owner_instance_id=self._owner_instance_id,
            lease_token=uuid.uuid4().hex,
            credential=resolved.credential,
        )

    async def _freeze_task(
        self,
        run: GenerationRunRecord,
        instruction: _TaskInstruction,
    ) -> None:
        async with UnitOfWork(self._engine) as store:
            project = await store.projects.get(run.project_id)
        if project is None:
            raise HarnessInvariantError("Runnable project no longer exists.")
        profile_id = self._profile_id(project, instruction.role)
        if profile_id is None:
            raise HarnessInvariantError(
                f"No Profile is selected for role {instruction.role!r}."
            )
        profile = self._profiles.resolve(profile_id).snapshot
        semantic_goal = _SEMANTIC_GOALS[instruction.task_kind]
        context = await self._context.build(
            task_kind=instruction.task_kind,
            project_id=run.project_id,
            book_id=instruction.book_id,
            arc_id=instruction.arc_id,
            chapter_id=instruction.chapter_id,
            semantic_goal=semantic_goal,
        )
        context_fingerprint = prepare_canonical_json(context.manifest).sha256
        scope_id = instruction.chapter_id or instruction.arc_id or instruction.book_id
        task_key = ":".join(
            [
                run.id,
                instruction.task_kind,
                scope_id,
                str(instruction.workspace_lock_version),
                instruction.book_baseline_id or "none",
                instruction.arc_baseline_id or "none",
                instruction.chapter_baseline_id or "none",
                project.current_canon_baseline_id,
                context_fingerprint,
            ]
        )
        task_id = uuid.uuid4().hex
        plan = self._registry.freeze_plan(
            task_id=task_id,
            project_id=run.project_id,
            run_id=run.id,
            task_key=task_key,
            action_key=f"{instruction.task_kind}:{scope_id}",
            role=instruction.role,
            task_kind=instruction.task_kind,
            contract_version=1,
            book_id=instruction.book_id,
            canon_baseline_id=project.current_canon_baseline_id,
            semantic_goal=semantic_goal,
            prompt=context.prompt,
            context_manifest=context.manifest,
            profile_snapshot=profile,
            arc_id=instruction.arc_id,
            chapter_id=instruction.chapter_id,
            workspace_lock_version=instruction.workspace_lock_version,
            book_baseline_id=instruction.book_baseline_id,
            arc_baseline_id=instruction.arc_baseline_id,
            chapter_baseline_id=instruction.chapter_baseline_id,
        )
        await self._tasks.create_initial(
            plan=plan,
            attempt_id=uuid.uuid4().hex,
            created_at_ms=self._now_ms(),
        )

    @staticmethod
    def _profile_id(project: object, role: AgentRole) -> str | None:
        if role == "book_strategist":
            return cast(str | None, getattr(project, "book_profile_id")) or cast(
                str | None, getattr(project, "default_profile_id")
            )
        if role == "arc_planner":
            return cast(str | None, getattr(project, "arc_profile_id")) or cast(
                str | None, getattr(project, "default_profile_id")
            )
        if role == "chapter_writer":
            return cast(str | None, getattr(project, "chapter_profile_id")) or cast(
                str | None, getattr(project, "default_profile_id")
            )
        return cast(str | None, getattr(project, "evaluator_profile_id")) or cast(
            str | None, getattr(project, "default_profile_id")
        )

    async def _deliver_task(self, task: ActionableTaskRecord) -> None:
        key = f"engine:deliver:{task.task_id}:{task.attempt_id}"
        lock_version = task.workspace_lock_version
        if lock_version is None:
            raise HarnessInvariantError("Deliverable Agent task has no workspace lock version.")
        if task.task_kind == "book.discuss":
            await self._books.apply_discussion_result(
                ApplyBookDiscussionTaskRequest(
                    project_id=task.project_id,
                    book_id=task.book_id,
                    task_id=task.task_id,
                    attempt_id=task.attempt_id,
                    expected_workspace_lock_version=lock_version,
                ),
                idempotency_key=key,
            )
            return
        if task.task_kind in {"book.synthesize", "book.revise", "book.repair"}:
            await self._books.apply_candidate_result(
                ApplyBookCandidateTaskRequest(
                    project_id=task.project_id,
                    book_id=task.book_id,
                    task_id=task.task_id,
                    attempt_id=task.attempt_id,
                    expected_workspace_lock_version=lock_version,
                ),
                idempotency_key=key,
            )
            return
        if task.task_kind in {"evaluate.book", "verify_repair.book"}:
            async with UnitOfWork(self._engine) as store:
                book_submission = await store.books.find_pending_submission(
                    project_id=task.project_id,
                    book_id=task.book_id,
                )
            if book_submission is None:
                raise HarnessInvariantError("Book evaluator result has no pending submission.")
            await self._books.record_review(
                RecordBookReviewRequest(
                    project_id=task.project_id,
                    book_id=task.book_id,
                    submission_id=book_submission.id,
                    evaluator_task_id=task.task_id,
                    evaluator_attempt_id=task.attempt_id,
                    rubric_id="book-rubric-v1",
                    rubric_version=1,
                    deterministic_precheck={"passed": True, "manifest": "book-submission-v1"},
                ),
                idempotency_key=key,
            )
            return
        if task.task_kind in {"arc.plan", "arc.revise", "arc.repair"}:
            if task.arc_id is None:
                raise HarnessInvariantError("Story Arc task has no arc_id.")
            await self._arcs.apply_task_result(
                ApplyArcTaskRequest(
                    project_id=task.project_id,
                    book_id=task.book_id,
                    arc_id=task.arc_id,
                    task_id=task.task_id,
                    attempt_id=task.attempt_id,
                    expected_workspace_lock_version=lock_version,
                ),
                idempotency_key=key,
            )
            return
        if task.task_kind in {"evaluate.arc", "verify_repair.arc"}:
            if task.arc_id is None:
                raise HarnessInvariantError("Story Arc evaluator task has no arc_id.")
            async with UnitOfWork(self._engine) as store:
                arc_submission = await store.arcs.find_pending_submission(
                    project_id=task.project_id,
                    arc_id=task.arc_id,
                )
            if arc_submission is None:
                raise HarnessInvariantError("Story Arc evaluator result has no submission.")
            await self._arcs.record_review(
                RecordArcReviewRequest(
                    project_id=task.project_id,
                    book_id=task.book_id,
                    arc_id=task.arc_id,
                    submission_id=arc_submission.id,
                    evaluator_task_id=task.task_id,
                    evaluator_attempt_id=task.attempt_id,
                    rubric_id="arc-rubric-v1",
                    rubric_version=1,
                    deterministic_precheck={"passed": True, "manifest": "arc-submission-v1"},
                ),
                idempotency_key=key,
            )
            return
        if task.task_kind in {
            "chapter.plan",
            "chapter.revise.plan",
            "chapter.draft",
            "chapter.revise.draft",
            "chapter.observe",
            "chapter.revise.observe",
            "chapter.repair.prose",
            "chapter.repair.observation",
        }:
            await self._deliver_chapter_component(task, lock_version=lock_version, key=key)
            return
        if task.task_kind in {"evaluate.chapter", "verify_repair.chapter"}:
            if task.chapter_id is None:
                raise HarnessInvariantError("Chapter evaluator task has no chapter_id.")
            async with UnitOfWork(self._engine) as store:
                chapter_submission = await store.chapters.find_pending_submission(
                    project_id=task.project_id,
                    chapter_id=task.chapter_id,
                )
            if chapter_submission is None:
                raise HarnessInvariantError("Chapter evaluator result has no submission.")
            await self._chapters.record_review(
                RecordChapterReviewRequest(
                    project_id=task.project_id,
                    chapter_id=task.chapter_id,
                    submission_id=chapter_submission.id,
                    evaluator_task_id=task.task_id,
                    evaluator_attempt_id=task.attempt_id,
                    rubric_id="chapter-rubric-v1",
                    rubric_version=1,
                    deterministic_precheck={
                        "passed": True,
                        "manifest": "chapter-submission-v1",
                    },
                ),
                idempotency_key=key,
            )
            return
        if task.task_kind == "book.assess_progress_or_completion":
            await self._deliver_completion_assessment(task, key=key)
            return
        raise HarnessInvariantError(f"No delivery command for task kind {task.task_kind!r}.")

    async def _deliver_chapter_component(
        self,
        task: ActionableTaskRecord,
        *,
        lock_version: int,
        key: str,
    ) -> None:
        if task.chapter_id is None:
            raise HarnessInvariantError("Chapter component task has no chapter_id.")
        request = ApplyChapterTaskRequest(
            project_id=task.project_id,
            chapter_id=task.chapter_id,
            task_id=task.task_id,
            attempt_id=task.attempt_id,
            expected_workspace_lock_version=lock_version,
        )
        methods = {
            "chapter.plan": self._chapters.apply_plan_result,
            "chapter.revise.plan": self._chapters.apply_revision_plan_result,
            "chapter.draft": self._chapters.apply_draft_result,
            "chapter.revise.draft": self._chapters.apply_revision_draft_result,
            "chapter.observe": self._chapters.apply_observation_result,
            "chapter.revise.observe": self._chapters.apply_revision_observation_result,
            "chapter.repair.prose": self._chapters.apply_repair_result,
            "chapter.repair.observation": self._chapters.apply_repair_result,
        }
        await methods[task.task_kind](request, idempotency_key=key)

    async def _deliver_completion_assessment(
        self,
        task: ActionableTaskRecord,
        *,
        key: str,
    ) -> None:
        if task.book_baseline_id is None or task.workspace_lock_version is None:
            raise HarnessInvariantError("Completion assessment lacks frozen Book dependencies.")
        async with UnitOfWork(self._engine) as store:
            project = await store.projects.get(task.project_id)
            terminal_arc = await store.completion.get_terminal_arc(
                project_id=task.project_id,
                book_id=task.book_id,
            )
            if terminal_arc is None:
                raise HarnessInvariantError("Completion assessment has no terminal Story Arc.")
            terminal_chapter = await store.completion.get_terminal_chapter(
                project_id=task.project_id,
                book_id=task.book_id,
                arc_id=terminal_arc.arc_id,
            )
        if project is None or terminal_chapter is None:
            raise HarnessInvariantError("Completion assessment boundary is incomplete.")
        await self._completion.apply_assessment(
            ApplyBookProgressRequest(
                project_id=task.project_id,
                book_id=task.book_id,
                task_id=task.task_id,
                attempt_id=task.attempt_id,
                expected_book_baseline_id=task.book_baseline_id,
                expected_canon_baseline_id=project.current_canon_baseline_id,
                expected_book_workspace_lock_version=task.workspace_lock_version,
                terminal_arc_id=terminal_arc.arc_id,
                terminal_arc_baseline_id=terminal_arc.arc_baseline_id,
                terminal_chapter_id=terminal_chapter.chapter_id,
                terminal_chapter_baseline_id=terminal_chapter.chapter_baseline_id,
            ),
            idempotency_key=key,
        )

    async def _decide_next(self, run: GenerationRunRecord) -> _Instruction:
        async with UnitOfWork(self._engine) as store:
            project = await store.projects.get(run.project_id)
            book = await store.books.get_for_project(run.project_id)
            if project is None or book is None:
                raise HarnessInvariantError("Runnable Run has no Project/Book aggregate.")
            if project.lifecycle_status == "completed" and book.lifecycle_status == "completed":
                return None
            if project.lifecycle_status != "active" or book.current_completion_id is not None:
                raise HarnessInvariantError("Runnable Run points at a non-active Project.")
            book_workspace = await store.books.get_workspace(
                project_id=project.id,
                book_id=book.id,
            )
            if book_workspace is None:
                raise HarnessInvariantError("Book workspace is missing.")

            open_changes = await store.changes.list_open(project_id=project.id)
            for change in open_changes:
                if change.request_kind == "chapter_to_arc":
                    target = await store.arcs.get_workspace(
                        project_id=project.id,
                        arc_id=change.target_id,
                    )
                    if target is None:
                        raise HarnessInvariantError("Open Chapter-to-Arc request lost its target.")
                    activated = (
                        target.state == "active"
                        and target.base_arc_baseline_id == change.target_baseline_id
                    )
                    expected_lock = target.lock_version
                else:
                    activated = (
                        book_workspace.state == "active"
                        and book_workspace.base_book_baseline_id == change.target_baseline_id
                    )
                    expected_lock = book_workspace.lock_version
                if not activated:
                    return _CommandInstruction(
                        kind="activate_change",
                        request=ActivateChangeRequest(
                            project_id=project.id,
                            change_request_id=change.id,
                            request_kind=change.request_kind,
                            expected_target_baseline_id=change.target_baseline_id,
                            expected_workspace_lock_version=expected_lock,
                        ),
                        idempotency_key=f"engine:activate-change:{change.id}",
                    )

            book_instruction = await self._decide_book(
                store=store,
                run=run,
                project=project,
                book=book,
                workspace=book_workspace,
                has_open_book_change=any(
                    change.request_kind in {"chapter_to_book", "arc_to_book"}
                    for change in open_changes
                ),
            )
            if book_instruction is not None:
                return book_instruction
            return await self._decide_arc(
                store=store,
                run=run,
                project=project,
                book=book,
                book_workspace=book_workspace,
            )

    async def _decide_book(
        self,
        *,
        store: object,
        run: GenerationRunRecord,
        project: object,
        book: object,
        workspace: object,
        has_open_book_change: bool,
    ) -> _Instruction:
        # StoreSession is intentionally kept behind a structural local variable so this
        # runtime layer does not expose it in its public protocol.
        session = store
        books = getattr(session, "books")
        content = getattr(session, "content")
        execution = getattr(session, "execution")
        project_id = cast(str, getattr(project, "id"))
        book_id = cast(str, getattr(book, "id"))
        pending = await books.find_pending_submission(project_id=project_id, book_id=book_id)
        latest_review = await books.get_latest_review(project_id=project_id, book_id=book_id)
        lock_version = cast(int, getattr(workspace, "lock_version"))
        current_baseline = cast(str | None, getattr(book, "current_baseline_id"))
        if pending is not None:
            if latest_review is None or latest_review.submission_id != pending.id:
                task_kind = (
                    "verify_repair.book"
                    if cast(int, getattr(workspace, "semantic_repair_count")) > 0
                    else "evaluate.book"
                )
                return _TaskInstruction(
                    role="evaluator",
                    task_kind=task_kind,
                    book_id=book_id,
                    workspace_lock_version=lock_version,
                    book_baseline_id=pending.base_book_baseline_id,
                )
            raise HarnessInvariantError("Reviewed Book submission remained on a runnable Run.")

        state_name = cast(str, getattr(workspace, "state"))
        if state_name in {"blocked_by_user", "blocked_by_upstream", "stale"}:
            raise HarnessInvariantError("Blocked Book workspace remained on a runnable Run.")
        if state_name == "idle":
            return None
        discussion = BookDiscussionState.model_validate_json(
            (
                await content.get_packed(
                    project_id=project_id,
                    ref_id=cast(str, getattr(workspace, "discussion_state_ref_id")),
                )
            ).unpack_and_verify()
        )
        if discussion.readiness_status == "awaiting_agent":
            return _TaskInstruction(
                role="book_strategist",
                task_kind="book.discuss",
                book_id=book_id,
                workspace_lock_version=lock_version,
                book_baseline_id=current_baseline,
            )
        if discussion.readiness_status == "continue":
            raise HarnessInvariantError("Book discussion awaiting user input remained runnable.")

        candidate_ready = all(
            getattr(workspace, field) is not None
            for field in (
                "candidate_constraints_ref_id",
                "candidate_titles_ref_id",
                "candidate_rolling_plan_ref_id",
                "candidate_completion_contract_ref_id",
            )
        )
        if latest_review is not None and latest_review.decision == "local_repair":
            return _TaskInstruction(
                role="book_strategist",
                task_kind="book.repair",
                book_id=book_id,
                workspace_lock_version=lock_version,
                book_baseline_id=current_baseline,
            )
        if current_baseline is None and not candidate_ready:
            return _TaskInstruction(
                role="book_strategist",
                task_kind="book.synthesize",
                book_id=book_id,
                workspace_lock_version=lock_version,
                book_baseline_id=None,
            )
        if current_baseline is not None and not candidate_ready:
            revised = await execution.has_applied_task(
                project_id=project_id,
                run_id=run.id,
                task_kind="book.revise",
                book_id=book_id,
                book_baseline_id=current_baseline,
            )
            if not revised and (has_open_book_change or state_name == "active"):
                return _TaskInstruction(
                    role="book_strategist",
                    task_kind="book.revise",
                    book_id=book_id,
                    workspace_lock_version=lock_version,
                    book_baseline_id=current_baseline,
                )
        if candidate_ready:
            return _CommandInstruction(
                kind="submit_book",
                request=SubmitBookRequest(
                    project_id=project_id,
                    book_id=book_id,
                    expected_workspace_lock_version=lock_version,
                ),
                idempotency_key=f"engine:submit-book:{book_id}:{lock_version}",
            )
        raise HarnessInvariantError("Active Book workspace has no legal next action.")

    async def _decide_arc(
        self,
        *,
        store: object,
        run: GenerationRunRecord,
        project: object,
        book: object,
        book_workspace: object,
    ) -> _Instruction:
        session = store
        arcs = getattr(session, "arcs")
        execution = getattr(session, "execution")
        project_id = cast(str, getattr(project, "id"))
        book_id = cast(str, getattr(book, "id"))
        book_baseline_id = cast(str | None, getattr(book, "current_baseline_id"))
        canon_baseline_id = cast(str, getattr(project, "current_canon_baseline_id"))
        if book_baseline_id is None:
            raise HarnessInvariantError("Arc routing requires an approved Book baseline.")
        arc = await arcs.get_unfinished_for_book(project_id=project_id, book_id=book_id)
        if arc is None:
            latest = await arcs.get_latest_for_book(project_id=project_id, book_id=book_id)
            if latest is None:
                return _CommandInstruction(
                    kind="create_arc",
                    request=CreateStoryArcRequest(
                        project_id=project_id,
                        book_id=book_id,
                        expected_book_baseline_id=book_baseline_id,
                        expected_canon_baseline_id=canon_baseline_id,
                        purpose="regular",
                    ),
                    idempotency_key=f"engine:create-initial-arc:{book_id}:{book_baseline_id}",
                )
            if latest.lifecycle_status != "completed" or latest.current_baseline_id is None:
                raise HarnessInvariantError("Book has no unfinished Arc and no completed boundary.")
            return _TaskInstruction(
                role="book_strategist",
                task_kind="book.assess_progress_or_completion",
                book_id=book_id,
                workspace_lock_version=cast(int, getattr(book_workspace, "lock_version")),
                book_baseline_id=book_baseline_id,
            )

        workspace = await arcs.get_workspace(project_id=project_id, arc_id=arc.id)
        if workspace is None:
            raise HarnessInvariantError("Current Story Arc workspace is missing.")
        if workspace.state == "stale":
            return _CommandInstruction(
                kind="rebase_arc",
                request=RebaseStaleArcRequest(
                    project_id=project_id,
                    book_id=book_id,
                    arc_id=arc.id,
                    expected_workspace_lock_version=workspace.lock_version,
                    expected_book_baseline_id=book_baseline_id,
                    expected_arc_baseline_id=arc.current_baseline_id,
                    expected_canon_baseline_id=canon_baseline_id,
                ),
                idempotency_key=(
                    f"engine:rebase-stale-arc:{arc.id}:{workspace.lock_version}:"
                    f"{book_baseline_id}:{canon_baseline_id}"
                ),
            )
        pending = await arcs.find_pending_submission(project_id=project_id, arc_id=arc.id)
        latest_review = await arcs.get_latest_review(project_id=project_id, arc_id=arc.id)
        if pending is not None:
            if latest_review is None or latest_review.submission_id != pending.id:
                task_kind = (
                    "verify_repair.arc"
                    if workspace.semantic_repair_count > 0
                    else "evaluate.arc"
                )
                return _TaskInstruction(
                    role="evaluator",
                    task_kind=task_kind,
                    book_id=book_id,
                    arc_id=arc.id,
                    workspace_lock_version=workspace.lock_version,
                    book_baseline_id=pending.book_baseline_id,
                    arc_baseline_id=pending.base_arc_baseline_id,
                )
            if latest_review.decision == "pass":
                gate = await arcs.find_pending_gate(project_id=project_id, arc_id=arc.id)
                if gate is not None:
                    raise HarnessInvariantError("Pending Arc approval gate remained runnable.")
                if cast(str, getattr(project, "operation_mode")) != "full_auto":
                    raise HarnessInvariantError("Participatory Arc review has no approval gate.")
                return _CommandInstruction(
                    kind="commit_arc_auto",
                    request=CommitArcAutoRequest(
                        project_id=project_id,
                        book_id=book_id,
                        arc_id=arc.id,
                        submission_id=pending.id,
                        review_id=latest_review.id,
                        expected_current_baseline_id=arc.current_baseline_id,
                    ),
                    idempotency_key=f"engine:commit-arc:{pending.id}:{latest_review.id}",
                )
            raise HarnessInvariantError("Rejected Arc submission remained pending.")

        if workspace.state in {"blocked_by_user", "blocked_by_upstream", "stale"}:
            raise HarnessInvariantError("Blocked Story Arc workspace remained runnable.")
        if workspace.state == "active":
            if latest_review is not None and latest_review.decision == "local_repair":
                return _TaskInstruction(
                    role="arc_planner",
                    task_kind="arc.repair",
                    book_id=book_id,
                    arc_id=arc.id,
                    workspace_lock_version=workspace.lock_version,
                    book_baseline_id=workspace.book_baseline_id,
                    arc_baseline_id=workspace.base_arc_baseline_id,
                )
            if workspace.base_arc_baseline_id is not None:
                revised = await execution.has_applied_task(
                    project_id=project_id,
                    run_id=run.id,
                    task_kind="arc.revise",
                    book_id=book_id,
                    arc_id=arc.id,
                    book_baseline_id=workspace.book_baseline_id,
                    arc_baseline_id=workspace.base_arc_baseline_id,
                )
                if not revised:
                    return _TaskInstruction(
                        role="arc_planner",
                        task_kind="arc.revise",
                        book_id=book_id,
                        arc_id=arc.id,
                        workspace_lock_version=workspace.lock_version,
                        book_baseline_id=workspace.book_baseline_id,
                        arc_baseline_id=workspace.base_arc_baseline_id,
                    )
            elif workspace.plan_ref_id is None:
                return _TaskInstruction(
                    role="arc_planner",
                    task_kind="arc.plan",
                    book_id=book_id,
                    arc_id=arc.id,
                    workspace_lock_version=workspace.lock_version,
                    book_baseline_id=workspace.book_baseline_id,
                    arc_baseline_id=None,
                )
            if workspace.plan_ref_id is not None:
                return _CommandInstruction(
                    kind="submit_arc",
                    request=SubmitArcRequest(
                        project_id=project_id,
                        book_id=book_id,
                        arc_id=arc.id,
                        expected_workspace_lock_version=workspace.lock_version,
                    ),
                    idempotency_key=f"engine:submit-arc:{arc.id}:{workspace.lock_version}",
                )
            raise HarnessInvariantError("Active Story Arc workspace has no plan action.")
        if workspace.state != "idle" or arc.current_baseline_id is None:
            raise HarnessInvariantError("Story Arc current baseline is not ready for Chapters.")
        return await self._decide_chapter(
            store=session,
            run=run,
            project_id=project_id,
            book_id=book_id,
            book_baseline_id=book_baseline_id,
            canon_baseline_id=canon_baseline_id,
            arc=arc,
            arc_baseline_id=arc.current_baseline_id,
        )

    async def _decide_chapter(
        self,
        *,
        store: object,
        run: GenerationRunRecord,
        project_id: str,
        book_id: str,
        book_baseline_id: str,
        canon_baseline_id: str,
        arc: object,
        arc_baseline_id: str,
    ) -> _Instruction:
        chapters = getattr(store, "chapters")
        arcs = getattr(store, "arcs")
        content = getattr(store, "content")
        execution = getattr(store, "execution")
        arc_id = cast(str, getattr(arc, "id"))
        active = await chapters.get_non_idle_workspace_for_arc(
            project_id=project_id,
            arc_id=arc_id,
        )
        if active is None:
            baseline = await arcs.get_baseline(
                project_id=project_id,
                arc_id=arc_id,
                baseline_id=arc_baseline_id,
            )
            if baseline is None:
                raise HarnessInvariantError("Current Story Arc baseline does not exist.")
            committed = await chapters.count_committed(arc_id=arc_id)
            if committed < baseline.target_chapter_count:
                return _CommandInstruction(
                    kind="create_chapter",
                    request=CreateChapterRequest(
                        project_id=project_id,
                        book_id=book_id,
                        arc_id=arc_id,
                        expected_book_baseline_id=book_baseline_id,
                        expected_arc_baseline_id=arc_baseline_id,
                        expected_canon_baseline_id=canon_baseline_id,
                    ),
                    idempotency_key=(
                        f"engine:create-chapter:{arc_id}:{committed + 1}:{arc_baseline_id}:"
                        f"{canon_baseline_id}"
                    ),
                )
            raise HarnessInvariantError("Story Arc reached its target but was not completed.")

        chapter, workspace = active
        if workspace.state == "stale":
            return _CommandInstruction(
                kind="rebase_chapter",
                request=RebaseStaleChapterRequest(
                    project_id=project_id,
                    book_id=book_id,
                    arc_id=arc_id,
                    chapter_id=chapter.id,
                    expected_workspace_lock_version=workspace.lock_version,
                    expected_book_baseline_id=book_baseline_id,
                    expected_arc_baseline_id=arc_baseline_id,
                    expected_chapter_baseline_id=chapter.current_baseline_id,
                    expected_canon_baseline_id=canon_baseline_id,
                ),
                idempotency_key=(
                    f"engine:rebase-stale-chapter:{chapter.id}:"
                    f"{workspace.lock_version}:{book_baseline_id}:"
                    f"{arc_baseline_id}:{canon_baseline_id}"
                ),
            )
        if workspace.state in {"blocked_by_user", "blocked_by_upstream", "stale"}:
            raise HarnessInvariantError("Blocked Chapter workspace remained runnable.")
        if workspace.state != "active":
            raise HarnessInvariantError("Non-idle Chapter workspace is not active.")
        pending = await chapters.find_pending_submission(
            project_id=project_id,
            chapter_id=chapter.id,
        )
        latest_review = await chapters.get_latest_review(
            project_id=project_id,
            chapter_id=chapter.id,
        )
        if pending is not None:
            if latest_review is None or latest_review.submission_id != pending.id:
                task_kind = (
                    "verify_repair.chapter"
                    if workspace.semantic_repair_count > 0
                    else "evaluate.chapter"
                )
                return _TaskInstruction(
                    role="evaluator",
                    task_kind=task_kind,
                    book_id=book_id,
                    arc_id=arc_id,
                    chapter_id=chapter.id,
                    workspace_lock_version=workspace.lock_version,
                    book_baseline_id=pending.book_baseline_id,
                    arc_baseline_id=pending.arc_baseline_id,
                    chapter_baseline_id=pending.base_chapter_baseline_id,
                )
            if latest_review.decision == "pass":
                return _CommandInstruction(
                    kind="commit_chapter",
                    request=CommitChapterRequest(
                        project_id=project_id,
                        chapter_id=chapter.id,
                        submission_id=pending.id,
                        review_id=latest_review.id,
                        expected_current_chapter_baseline_id=chapter.current_baseline_id,
                        expected_canon_baseline_id=canon_baseline_id,
                    ),
                    idempotency_key=(
                        f"engine:commit-chapter:{pending.id}:{latest_review.id}:"
                        f"{canon_baseline_id}"
                    ),
                )
            raise HarnessInvariantError("Rejected Chapter submission remained pending.")

        if latest_review is not None and latest_review.decision == "local_repair":
            if latest_review.repair_contract_ref_id is None:
                raise HarnessInvariantError("Chapter local repair has no contract.")
            repair = json.loads(
                (
                    await content.get_packed(
                        project_id=project_id,
                        ref_id=latest_review.repair_contract_ref_id,
                    )
                ).unpack_and_verify()
            )
            scope = set(repair.get("repair_scope", []))
            prose_repaired = await execution.has_applied_task(
                project_id=project_id,
                run_id=run.id,
                task_kind="chapter.repair.prose",
                book_id=book_id,
                arc_id=arc_id,
                chapter_id=chapter.id,
                book_baseline_id=workspace.book_baseline_id,
                arc_baseline_id=workspace.arc_baseline_id,
                chapter_baseline_id=workspace.base_chapter_baseline_id,
                created_after_ms=latest_review.created_at_ms,
            )
            observations_repaired = await execution.has_applied_task(
                project_id=project_id,
                run_id=run.id,
                task_kind="chapter.repair.observation",
                book_id=book_id,
                arc_id=arc_id,
                chapter_id=chapter.id,
                book_baseline_id=workspace.book_baseline_id,
                arc_baseline_id=workspace.arc_baseline_id,
                chapter_baseline_id=workspace.base_chapter_baseline_id,
                created_after_ms=latest_review.created_at_ms,
            )
            if "prose" in scope and not prose_repaired:
                return self._chapter_task("chapter.repair.prose", chapter, workspace)
            if scope.intersection({"observations", "canon"}) and not observations_repaired:
                return self._chapter_task("chapter.repair.observation", chapter, workspace)
            if workspace.observations_ref_id is None:
                observe_kind = (
                    "chapter.revise.observe"
                    if workspace.base_chapter_baseline_id is not None
                    else "chapter.observe"
                )
                return self._chapter_task(observe_kind, chapter, workspace)
            return _CommandInstruction(
                kind="submit_chapter",
                request=SubmitChapterRequest(
                    project_id=project_id,
                    chapter_id=chapter.id,
                    expected_workspace_lock_version=workspace.lock_version,
                ),
                idempotency_key=f"engine:submit-chapter:{chapter.id}:{workspace.lock_version}",
            )

        if workspace.base_chapter_baseline_id is not None:
            revised_plan = await execution.has_applied_task(
                project_id=project_id,
                run_id=run.id,
                task_kind="chapter.revise.plan",
                book_id=book_id,
                arc_id=arc_id,
                chapter_id=chapter.id,
                book_baseline_id=workspace.book_baseline_id,
                arc_baseline_id=workspace.arc_baseline_id,
                chapter_baseline_id=workspace.base_chapter_baseline_id,
            )
            if not revised_plan:
                return self._chapter_task("chapter.revise.plan", chapter, workspace)
            if workspace.draft_ref_id is None:
                return self._chapter_task("chapter.revise.draft", chapter, workspace)
            if workspace.observations_ref_id is None:
                return self._chapter_task("chapter.revise.observe", chapter, workspace)
        else:
            if workspace.plan_ref_id is None:
                return self._chapter_task("chapter.plan", chapter, workspace)
            if workspace.draft_ref_id is None:
                return self._chapter_task("chapter.draft", chapter, workspace)
            if workspace.observations_ref_id is None:
                return self._chapter_task("chapter.observe", chapter, workspace)
        if workspace.candidate_canon_patch_ref_id is None:
            raise HarnessInvariantError("Chapter observations have no bound Canon patch.")
        return _CommandInstruction(
            kind="submit_chapter",
            request=SubmitChapterRequest(
                project_id=project_id,
                chapter_id=chapter.id,
                expected_workspace_lock_version=workspace.lock_version,
            ),
            idempotency_key=f"engine:submit-chapter:{chapter.id}:{workspace.lock_version}",
        )

    @staticmethod
    def _chapter_task(task_kind: str, chapter: object, workspace: object) -> _TaskInstruction:
        return _TaskInstruction(
            role="chapter_writer",
            task_kind=task_kind,
            book_id=cast(str, getattr(chapter, "book_id")),
            arc_id=cast(str, getattr(chapter, "arc_id")),
            chapter_id=cast(str, getattr(chapter, "id")),
            workspace_lock_version=cast(int, getattr(workspace, "lock_version")),
            book_baseline_id=cast(str, getattr(workspace, "book_baseline_id")),
            arc_baseline_id=cast(str, getattr(workspace, "arc_baseline_id")),
            chapter_baseline_id=cast(
                str | None, getattr(workspace, "base_chapter_baseline_id")
            ),
        )

    async def _apply_command(self, instruction: _CommandInstruction) -> None:
        if instruction.kind == "activate_change":
            await self._changes.activate(
                cast(ActivateChangeRequest, instruction.request),
                idempotency_key=instruction.idempotency_key,
            )
        elif instruction.kind == "create_arc":
            await self._arcs.create_story_arc(
                cast(CreateStoryArcRequest, instruction.request),
                idempotency_key=instruction.idempotency_key,
            )
        elif instruction.kind == "create_chapter":
            await self._chapters.create_chapter(
                cast(CreateChapterRequest, instruction.request),
                idempotency_key=instruction.idempotency_key,
            )
        elif instruction.kind == "rebase_arc":
            await self._arcs.rebase_stale_workspace(
                cast(RebaseStaleArcRequest, instruction.request),
                idempotency_key=instruction.idempotency_key,
            )
        elif instruction.kind == "rebase_chapter":
            await self._chapters.rebase_stale_workspace(
                cast(RebaseStaleChapterRequest, instruction.request),
                idempotency_key=instruction.idempotency_key,
            )
        elif instruction.kind == "submit_book":
            await self._books.submit_for_review(
                cast(SubmitBookRequest, instruction.request),
                idempotency_key=instruction.idempotency_key,
            )
        elif instruction.kind == "submit_arc":
            await self._arcs.submit_for_review(
                cast(SubmitArcRequest, instruction.request),
                idempotency_key=instruction.idempotency_key,
            )
        elif instruction.kind == "submit_chapter":
            await self._chapters.submit_for_review(
                cast(SubmitChapterRequest, instruction.request),
                idempotency_key=instruction.idempotency_key,
            )
        elif instruction.kind == "commit_arc_auto":
            await self._arcs.commit_baseline_auto(
                cast(CommitArcAutoRequest, instruction.request),
                idempotency_key=instruction.idempotency_key,
            )
        elif instruction.kind == "commit_chapter":
            await self._chapters.commit_chapter_and_canon(
                cast(CommitChapterRequest, instruction.request),
                idempotency_key=instruction.idempotency_key,
            )
        else:  # pragma: no cover - Literal exhaustiveness guard.
            raise HarnessInvariantError(f"Unknown deterministic command {instruction.kind!r}.")
