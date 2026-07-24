from __future__ import annotations

from dataclasses import asdict
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncEngine

from app.db.uow import UnitOfWork
from app.domain.book.contracts import BookDiscussionState, BookTranscript
from app.domain.evaluation import CreatorInputNeed


class ProjectListItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    title: str | None
    operation_mode: Literal["full_auto", "participatory"]
    lifecycle_status: str
    run_status: str
    wait_reason_code: str | None
    current_arc_id: str | None
    current_chapter_id: str | None
    committed_chapter_count: int
    created_at_ms: int
    updated_at_ms: int


class RunStateView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    run_number: int
    status: str
    desired_state: str
    lock_version: int
    wait_reason_code: str | None
    failure_source_kind: str | None
    blocking_task_id: str | None
    blocking_action_key: str | None
    failure_code: str | None
    failure_ref_id: str | None
    started_at_ms: int | None
    finished_at_ms: int | None


class BookStateView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    book_id: str
    lifecycle_status: str
    current_baseline_id: str | None
    latest_boundary_review_id: str | None
    current_progress_handoff_id: str | None
    current_completion_id: str | None
    baseline_version: int | None
    approved_title: str | None
    minimum_chapter_count: int | None
    maximum_chapter_count: int | None
    workspace_state: str
    workspace_lock_version: int
    semantic_repair_count: int
    semantic_repair_limit: int
    discussion: BookDiscussionState
    transcript: BookTranscript
    pending_submission_id: str | None
    pending_review_id: str | None
    pending_review_decision: str | None


class ArcStateView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    arc_id: str
    ordinal: int
    purpose: str
    lifecycle_status: str
    current_baseline_id: str | None
    latest_closure_review_id: str | None
    current_closure_id: str | None
    baseline_version: int | None
    minimum_chapter_count: int | None
    recommended_closure_chapter_count: int | None
    maximum_chapter_count: int | None
    closure_chapter_count: int | None
    committed_chapter_count: int
    workspace_state: str
    workspace_lock_version: int
    semantic_repair_count: int
    semantic_repair_limit: int
    pending_submission_id: str | None
    pending_review_id: str | None
    pending_review_decision: str | None
    approval_gate_id: str | None
    approval_gate_state: str | None
    revision_origin: str
    automatic_correction_round: int | None


class ChapterStateView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    chapter_id: str
    book_ordinal: int
    arc_ordinal: int
    lifecycle_status: str
    current_baseline_id: str | None
    chapter_title: str | None
    workspace_state: str
    workspace_lock_version: int
    semantic_repair_count: int
    semantic_repair_limit: int
    has_plan: bool
    has_prose: bool
    has_observations: bool
    has_canon_patch: bool
    pending_submission_id: str | None
    pending_review_id: str | None
    pending_review_decision: str | None
    revision_origin: str
    automatic_correction_round: int | None


AuthorityReviewKind = Literal[
    "arc_parent",
    "book_parent",
    "arc_closure",
    "book_boundary",
]


class CreatorInputRequestView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    review_kind: AuthorityReviewKind
    review_id: str
    route_layer: Literal["book", "arc"]
    book_id: str
    arc_id: str | None
    automatic_correction_round: Literal[0, 1]
    question: CreatorInputNeed


class FeedbackStateView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    feedback_id: str
    feedback_kind: Literal["unsolicited", "correction_wait_response"]
    status: Literal["pending", "routed", "applied", "dismissed"]
    content: str
    route_layer: Literal["book", "arc", "chapter"] | None
    book_id: str | None
    arc_id: str | None
    chapter_id: str | None
    captured_run_id: str
    captured_book_baseline_id: str | None
    captured_arc_baseline_id: str | None
    captured_chapter_baseline_id: str | None
    arc_parent_review_id: str | None
    book_parent_review_id: str | None
    arc_closure_review_id: str | None
    book_boundary_review_id: str | None
    resulting_correction_lineage_id: str | None
    dismiss_reason_code: str | None
    applied_command_id: str | None
    created_at_ms: int
    routed_at_ms: int | None
    applied_at_ms: int | None


class AgentTaskStateView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str
    run_id: str
    role: str
    task_kind: str
    scope_layer: str
    arc_id: str | None
    chapter_id: str | None
    status: str
    delivery_state: str
    profile_id: str
    model_id: str
    attempt_id: str | None
    attempt_number: int | None
    attempt_status: str | None
    retry_kind: str | None
    provider_request_count: int | None
    transport_retry_count: int | None
    model_request_count: int | None
    input_tokens: int | None
    output_tokens: int | None
    error_code: str | None
    error_ref_id: str | None
    diagnostic_ref_id: str | None
    created_at_ms: int
    updated_at_ms: int


class AgentAttemptEvidenceView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str
    run_id: str
    role: str
    task_kind: str
    scope_layer: str
    arc_id: str | None
    chapter_id: str | None
    task_status: str
    delivery_state: str
    profile_id: str
    model_id: str
    profile_fingerprint: str
    output_schema_id: str
    output_schema_version: int
    harness_policy_id: str
    harness_policy_version: int
    attempt_id: str
    attempt_number: int
    retry_kind: str
    attempt_status: str
    framework_fingerprint: str
    provider_request_count: int
    transport_retry_count: int
    model_request_count: int
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    error_code: str | None
    error_category: str | None
    http_status: int | None
    error_ref_id: str | None
    diagnostic_ref_id: str | None
    created_at_ms: int
    started_at_ms: int | None
    finished_at_ms: int | None


class ProjectDiagnosticsView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    run_id: str
    task_count: int = Field(ge=0)
    attempt_count: int = Field(ge=0)
    arc_count: int = Field(ge=0)
    completion_id: str | None
    completion_version: int | None
    attempts: list[AgentAttemptEvidenceView] = Field(default_factory=list)


CommandId = Literal[
    "start_run",
    "pause_run",
    "resume_run",
    "retry_failed_task",
    "retry_failed_action",
    "send_book_input",
    "approve_book",
    "approve_arc",
    "submit_feedback",
    "export_markdown",
]


class ExecutableCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    command_id: CommandId
    enabled: bool
    reason: str


class ProjectStateView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project: ProjectListItem
    settings_lock_version: int
    default_profile_id: str | None
    book_profile_id: str | None
    arc_profile_id: str | None
    chapter_profile_id: str | None
    evaluator_profile_id: str | None
    run: RunStateView
    book: BookStateView
    current_arc: ArcStateView | None
    current_chapter: ChapterStateView | None
    creator_input_request: CreatorInputRequestView | None
    recent_feedback: list[FeedbackStateView] = Field(default_factory=list)
    latest_event_sequence: int
    commands: list[ExecutableCommand]
    recent_tasks: list[AgentTaskStateView] = Field(default_factory=list)


class ProjectStateQuery:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def list_projects(self) -> list[ProjectListItem]:
        async with UnitOfWork(self._engine) as store:
            projects = await store.projects.list_all()
            results: list[ProjectListItem] = []
            for project in projects:
                book = await store.books.get_for_project(project.id)
                run = await store.runs.get_latest_for_project(project.id)
                if book is None or run is None:
                    continue
                title = None
                if book.current_baseline_id is not None:
                    baseline = await store.books.get_baseline(
                        project_id=project.id,
                        book_id=book.id,
                        baseline_id=book.current_baseline_id,
                    )
                    title = None if baseline is None else baseline.approved_title
                else:
                    workspace = await store.books.get_workspace(
                        project_id=project.id,
                        book_id=book.id,
                    )
                    if workspace is not None:
                        discussion = BookDiscussionState.model_validate_json(
                            (
                                await store.content.get_packed(
                                    project_id=project.id,
                                    ref_id=workspace.discussion_state_ref_id,
                                )
                            ).unpack_and_verify()
                        )
                        title = discussion.selected_title
                arc = await store.arcs.get_unfinished_for_book(
                    project_id=project.id,
                    book_id=book.id,
                ) or await store.arcs.get_latest_for_book(
                    project_id=project.id,
                    book_id=book.id,
                )
                chapter = (
                    None
                    if arc is None
                    else await store.chapters.get_non_idle_workspace_for_arc(
                        project_id=project.id,
                        arc_id=arc.id,
                    )
                )
                latest_chapter = (
                    chapter[0]
                    if chapter is not None
                    else None
                    if arc is None
                    else await store.chapters.get_latest_for_arc(
                        project_id=project.id,
                        arc_id=arc.id,
                    )
                )
                results.append(
                    ProjectListItem(
                        project_id=project.id,
                        title=title,
                        operation_mode=cast(
                            Literal["full_auto", "participatory"],
                            project.operation_mode,
                        ),
                        lifecycle_status=project.lifecycle_status,
                        run_status=run.status,
                        wait_reason_code=run.wait_reason_code,
                        current_arc_id=None if arc is None else arc.id,
                        current_chapter_id=(
                            None if latest_chapter is None else latest_chapter.id
                        ),
                        committed_chapter_count=await store.chapters.count_committed_for_book(
                            book_id=book.id
                        ),
                        created_at_ms=project.created_at_ms,
                        updated_at_ms=project.updated_at_ms,
                    )
                )
            return sorted(results, key=lambda item: (item.updated_at_ms, item.project_id), reverse=True)

    async def get_diagnostics(self, project_id: str) -> ProjectDiagnosticsView | None:
        async with UnitOfWork(self._engine) as store:
            project = await store.projects.get(project_id)
            book = await store.books.get_for_project(project_id)
            run = await store.runs.get_latest_for_project(project_id)
            if project is None or book is None or run is None:
                return None
            attempts = await store.execution.list_attempt_summaries(project_id=project_id)
            arcs = await store.arcs.list_for_book(project_id=project_id, book_id=book.id)
            completion = await store.completion.get_latest_identity(book_id=book.id)
            return ProjectDiagnosticsView(
                project_id=project_id,
                run_id=run.id,
                task_count=len({item.task_id for item in attempts}),
                attempt_count=len(attempts),
                arc_count=len(arcs),
                completion_id=None if completion is None else completion[0],
                completion_version=None if completion is None else completion[1],
                attempts=[
                    AgentAttemptEvidenceView.model_validate(asdict(item)) for item in attempts
                ],
            )

    async def get_project(self, project_id: str) -> ProjectStateView | None:
        async with UnitOfWork(self._engine) as store:
            project = await store.projects.get(project_id)
            book = await store.books.get_for_project(project_id)
            run = await store.runs.get_latest_for_project(project_id)
            if project is None or book is None or run is None:
                return None
            workspace = await store.books.get_workspace(
                project_id=project_id,
                book_id=book.id,
            )
            if workspace is None:
                return None
            discussion = BookDiscussionState.model_validate_json(
                (
                    await store.content.get_packed(
                        project_id=project_id,
                        ref_id=workspace.discussion_state_ref_id,
                    )
                ).unpack_and_verify()
            )
            transcript = BookTranscript.model_validate_json(
                (
                    await store.content.get_packed(
                        project_id=project_id,
                        ref_id=workspace.transcript_ref_id,
                    )
                ).unpack_and_verify()
            )
            baseline = (
                None
                if book.current_baseline_id is None
                else await store.books.get_baseline(
                    project_id=project_id,
                    book_id=book.id,
                    baseline_id=book.current_baseline_id,
                )
            )
            book_submission = await store.books.find_pending_submission(
                project_id=project_id,
                book_id=book.id,
            )
            book_review = await store.books.get_latest_review(
                project_id=project_id,
                book_id=book.id,
            )
            if book_submission is None or (
                book_review is not None and book_review.submission_id != book_submission.id
            ):
                pending_book_review = None
            else:
                pending_book_review = book_review

            arc = await store.arcs.get_unfinished_for_book(
                project_id=project_id,
                book_id=book.id,
            ) or await store.arcs.get_latest_for_book(project_id=project_id, book_id=book.id)
            arc_view = None
            chapter_view = None
            if arc is not None:
                arc_workspace = await store.arcs.get_workspace(
                    project_id=project_id,
                    arc_id=arc.id,
                )
                if arc_workspace is not None:
                    arc_baseline = (
                        None
                        if arc.current_baseline_id is None
                        else await store.arcs.get_baseline(
                            project_id=project_id,
                            arc_id=arc.id,
                            baseline_id=arc.current_baseline_id,
                        )
                    )
                    arc_submission = await store.arcs.find_pending_submission(
                        project_id=project_id,
                        arc_id=arc.id,
                    )
                    arc_review = await store.arcs.get_latest_review(
                        project_id=project_id,
                        arc_id=arc.id,
                    )
                    if arc_submission is None or (
                        arc_review is not None and arc_review.submission_id != arc_submission.id
                    ):
                        pending_arc_review = None
                    else:
                        pending_arc_review = arc_review
                    gate = await store.arcs.find_pending_gate(
                        project_id=project_id,
                        arc_id=arc.id,
                    )
                    arc_view = ArcStateView(
                        arc_id=arc.id,
                        ordinal=arc.ordinal,
                        purpose=arc.purpose,
                        lifecycle_status=arc.lifecycle_status,
                        current_baseline_id=arc.current_baseline_id,
                        latest_closure_review_id=arc.latest_closure_review_id,
                        current_closure_id=arc.current_closure_id,
                        baseline_version=(
                            None if arc_baseline is None else arc_baseline.baseline_version
                        ),
                        minimum_chapter_count=(
                            arc_workspace.minimum_chapter_count
                            if arc_baseline is None
                            else arc_baseline.minimum_chapter_count
                        ),
                        recommended_closure_chapter_count=(
                            arc_workspace.recommended_closure_chapter_count
                        ),
                        maximum_chapter_count=(
                            arc_workspace.maximum_chapter_count
                            if arc_baseline is None
                            else arc_baseline.maximum_chapter_count
                        ),
                        closure_chapter_count=(
                            arc_workspace.closure_chapter_count
                            if arc_baseline is None
                            else arc_baseline.closure_chapter_count
                        ),
                        committed_chapter_count=await store.chapters.count_committed(
                            arc_id=arc.id
                        ),
                        workspace_state=arc_workspace.state,
                        workspace_lock_version=arc_workspace.lock_version,
                        semantic_repair_count=arc_workspace.semantic_repair_count,
                        semantic_repair_limit=arc_workspace.semantic_repair_limit,
                        pending_submission_id=(
                            None if arc_submission is None else arc_submission.id
                        ),
                        pending_review_id=(
                            None if pending_arc_review is None else pending_arc_review.id
                        ),
                        pending_review_decision=(
                            None
                            if pending_arc_review is None
                            else pending_arc_review.decision
                        ),
                        approval_gate_id=None if gate is None else gate.id,
                        approval_gate_state=None if gate is None else gate.state,
                        revision_origin=arc_workspace.revision_origin,
                        automatic_correction_round=(
                            arc_workspace.automatic_correction_round
                        ),
                    )
                    active_chapter = await store.chapters.get_non_idle_workspace_for_arc(
                        project_id=project_id,
                        arc_id=arc.id,
                    )
                    chapter = (
                        active_chapter[0]
                        if active_chapter is not None
                        else await store.chapters.get_latest_for_arc(
                            project_id=project_id,
                            arc_id=arc.id,
                        )
                    )
                    chapter_workspace = (
                        active_chapter[1]
                        if active_chapter is not None
                        else None
                        if chapter is None
                        else await store.chapters.get_workspace(
                            project_id=project_id,
                            chapter_id=chapter.id,
                        )
                    )
                    if chapter is not None and chapter_workspace is not None:
                        chapter_submission = await store.chapters.find_pending_submission(
                            project_id=project_id,
                            chapter_id=chapter.id,
                        )
                        chapter_review = await store.chapters.get_latest_review(
                            project_id=project_id,
                            chapter_id=chapter.id,
                        )
                        if chapter_submission is None or (
                            chapter_review is not None
                            and chapter_review.submission_id != chapter_submission.id
                        ):
                            pending_chapter_review = None
                        else:
                            pending_chapter_review = chapter_review
                        chapter_baseline = (
                            None
                            if chapter.current_baseline_id is None
                            else await store.chapters.get_baseline(
                                project_id=project_id,
                                chapter_id=chapter.id,
                                baseline_id=chapter.current_baseline_id,
                            )
                        )
                        chapter_view = ChapterStateView(
                            chapter_id=chapter.id,
                            book_ordinal=chapter.book_ordinal,
                            arc_ordinal=chapter.arc_ordinal,
                            lifecycle_status=chapter.lifecycle_status,
                            current_baseline_id=chapter.current_baseline_id,
                            chapter_title=(
                                None if chapter_baseline is None else chapter_baseline.chapter_title
                            ),
                            workspace_state=chapter_workspace.state,
                            workspace_lock_version=chapter_workspace.lock_version,
                            semantic_repair_count=chapter_workspace.semantic_repair_count,
                            semantic_repair_limit=chapter_workspace.semantic_repair_limit,
                            has_plan=chapter_workspace.plan_ref_id is not None,
                            has_prose=chapter_workspace.draft_ref_id is not None,
                            has_observations=chapter_workspace.observations_ref_id is not None,
                            has_canon_patch=(
                                chapter_workspace.candidate_canon_patch_ref_id is not None
                            ),
                            pending_submission_id=(
                                None if chapter_submission is None else chapter_submission.id
                            ),
                            pending_review_id=(
                                None
                                if pending_chapter_review is None
                                else pending_chapter_review.id
                            ),
                            pending_review_decision=(
                                None
                                if pending_chapter_review is None
                                else pending_chapter_review.decision
                            ),
                            revision_origin=chapter_workspace.revision_origin,
                            automatic_correction_round=(
                                chapter_workspace.automatic_correction_round
                            ),
                        )

            committed_count = await store.chapters.count_committed_for_book(book_id=book.id)
            creator_input_request = await _creator_input_request(
                store=store,
                project_id=project_id,
                book_id=book.id,
                current_arc_id=None if arc is None else arc.id,
                run_status=run.status,
                wait_reason_code=run.wait_reason_code,
            )
            title = baseline.approved_title if baseline is not None else discussion.selected_title
            summary = ProjectListItem(
                project_id=project.id,
                title=title,
                operation_mode=cast(
                    Literal["full_auto", "participatory"],
                    project.operation_mode,
                ),
                lifecycle_status=project.lifecycle_status,
                run_status=run.status,
                wait_reason_code=run.wait_reason_code,
                current_arc_id=None if arc_view is None else arc_view.arc_id,
                current_chapter_id=(
                    None if chapter_view is None else chapter_view.chapter_id
                ),
                committed_chapter_count=committed_count,
                created_at_ms=project.created_at_ms,
                updated_at_ms=project.updated_at_ms,
            )
            recent = await store.execution.list_task_summaries(
                project_id=project_id,
                limit=100,
            )
            feedback_records = await store.feedback.list_recent(
                project_id=project_id,
                limit=50,
            )
            recent_feedback = [
                FeedbackStateView(
                    feedback_id=item.id,
                    feedback_kind=cast(
                        Literal["unsolicited", "correction_wait_response"],
                        item.feedback_kind,
                    ),
                    status=cast(
                        Literal["pending", "routed", "applied", "dismissed"],
                        item.status,
                    ),
                    content=(
                        await store.content.get_packed(
                            project_id=project_id,
                            ref_id=item.content_ref_id,
                        )
                    )
                    .unpack_and_verify()
                    .decode("utf-8"),
                    route_layer=cast(
                        Literal["book", "arc", "chapter"] | None,
                        item.route_layer,
                    ),
                    book_id=item.book_id,
                    arc_id=item.arc_id,
                    chapter_id=item.chapter_id,
                    captured_run_id=item.captured_run_id,
                    captured_book_baseline_id=item.captured_book_baseline_id,
                    captured_arc_baseline_id=item.captured_arc_baseline_id,
                    captured_chapter_baseline_id=(
                        item.captured_chapter_baseline_id
                    ),
                    arc_parent_review_id=item.arc_parent_review_id,
                    book_parent_review_id=item.book_parent_review_id,
                    arc_closure_review_id=item.arc_closure_review_id,
                    book_boundary_review_id=item.book_boundary_review_id,
                    resulting_correction_lineage_id=(
                        item.resulting_correction_lineage_id
                    ),
                    dismiss_reason_code=item.dismiss_reason_code,
                    applied_command_id=item.applied_command_id,
                    created_at_ms=item.created_at_ms,
                    routed_at_ms=item.routed_at_ms,
                    applied_at_ms=item.applied_at_ms,
                )
                for item in feedback_records
            ]
            commands = _commands(
                run=run,
                has_book_input=(
                    discussion.readiness_status != "awaiting_agent"
                ),
                has_book_approval=(
                    book_submission is not None
                    and pending_book_review is not None
                    and pending_book_review.decision == "pass"
                ),
                has_arc_approval=(
                    arc_view is not None and arc_view.approval_gate_id is not None
                ),
                has_formal_baseline=book.current_baseline_id is not None,
                committed_chapter_count=committed_count,
            )
            return ProjectStateView(
                project=summary,
                settings_lock_version=project.settings_lock_version,
                default_profile_id=project.default_profile_id,
                book_profile_id=project.book_profile_id,
                arc_profile_id=project.arc_profile_id,
                chapter_profile_id=project.chapter_profile_id,
                evaluator_profile_id=project.evaluator_profile_id,
                run=RunStateView(
                    run_id=run.id,
                    run_number=run.run_number,
                    status=run.status,
                    desired_state=run.desired_state,
                    lock_version=run.lock_version,
                    wait_reason_code=run.wait_reason_code,
                    failure_source_kind=run.failure_source_kind,
                    blocking_task_id=run.blocking_task_id,
                    blocking_action_key=run.blocking_action_key,
                    failure_code=run.failure_code,
                    failure_ref_id=run.failure_ref_id,
                    started_at_ms=run.started_at_ms,
                    finished_at_ms=run.finished_at_ms,
                ),
                book=BookStateView(
                    book_id=book.id,
                    lifecycle_status=book.lifecycle_status,
                    current_baseline_id=book.current_baseline_id,
                    latest_boundary_review_id=book.latest_boundary_review_id,
                    current_progress_handoff_id=book.current_progress_handoff_id,
                    current_completion_id=book.current_completion_id,
                    baseline_version=None if baseline is None else baseline.baseline_version,
                    approved_title=None if baseline is None else baseline.approved_title,
                    minimum_chapter_count=(
                        None if baseline is None else baseline.minimum_chapter_count
                    ),
                    maximum_chapter_count=(
                        None if baseline is None else baseline.maximum_chapter_count
                    ),
                    workspace_state=workspace.state,
                    workspace_lock_version=workspace.lock_version,
                    semantic_repair_count=workspace.semantic_repair_count,
                    semantic_repair_limit=workspace.semantic_repair_limit,
                    discussion=discussion,
                    transcript=transcript,
                    pending_submission_id=(
                        None if book_submission is None else book_submission.id
                    ),
                    pending_review_id=(
                        None if pending_book_review is None else pending_book_review.id
                    ),
                    pending_review_decision=(
                        None
                        if pending_book_review is None
                        else pending_book_review.decision
                    ),
                ),
                current_arc=arc_view,
                current_chapter=chapter_view,
                creator_input_request=creator_input_request,
                recent_feedback=recent_feedback,
                latest_event_sequence=await store.commands.latest_event_sequence(
                    project_id=project_id
                ),
                commands=commands,
                recent_tasks=[
                    AgentTaskStateView.model_validate(asdict(item)) for item in recent
                ],
            )


async def _creator_input_request(
    *,
    store: Any,
    project_id: str,
    book_id: str,
    current_arc_id: str | None,
    run_status: str,
    wait_reason_code: str | None,
) -> CreatorInputRequestView | None:
    if run_status != "waiting_for_user" or wait_reason_code is None:
        return None

    candidates: list[
        tuple[AuthorityReviewKind, Literal["book", "arc"], object]
    ] = []
    for change in await store.changes.list_unresolved(project_id=project_id):
        if change.status != "reviewed" or change.latest_parent_review_id is None:
            continue
        if change.request_kind == "chapter_to_arc":
            review = await store.arc_parent_reviews.get(
                project_id=project_id,
                review_id=change.latest_parent_review_id,
            )
            if review is not None:
                candidates.append(("arc_parent", "arc", review))
        else:
            review = await store.book_parent_reviews.get(
                project_id=project_id,
                review_id=change.latest_parent_review_id,
            )
            if review is not None:
                candidates.append(("book_parent", "book", review))

    if current_arc_id is not None:
        closure_review = await store.arc_closure_reviews.get_latest_for_arc(
            project_id=project_id,
            arc_id=current_arc_id,
        )
        if closure_review is not None:
            candidates.append(("arc_closure", "arc", closure_review))
    boundary_review = await store.book_boundary_reviews.get_latest_for_book(
        project_id=project_id,
        book_id=book_id,
    )
    if boundary_review is not None:
        candidates.append(("book_boundary", "book", boundary_review))

    initial_reasons: dict[AuthorityReviewKind, str] = {
        "arc_parent": "arc_parent_review_needs_user",
        "book_parent": "book_parent_review_needs_user",
        "arc_closure": "arc_closure_needs_user",
        "book_boundary": "book_boundary_needs_user",
    }
    matching: list[
        tuple[AuthorityReviewKind, Literal["book", "arc"], object]
    ] = []
    for review_kind, route_layer, review in candidates:
        correction_round = getattr(review, "automatic_correction_round", None)
        expected_reason = (
            "evidence_correction_needs_user"
            if correction_round == 1
            else initial_reasons[review_kind]
        )
        if (
            expected_reason == wait_reason_code
            and getattr(review, "disposition", None) == "waiting_for_user"
            and getattr(review, "resolution_owner", None) == "creator"
            and getattr(review, "user_question_ref_id", None) is not None
            and correction_round in {0, 1}
        ):
            matching.append((review_kind, route_layer, review))
    if not matching:
        return None

    review_kind, route_layer, review = max(
        matching,
        key=lambda item: (
            int(getattr(item[2], "created_at_ms")),
            str(getattr(item[2], "id")),
        ),
    )
    question_ref_id = str(getattr(review, "user_question_ref_id"))
    question = CreatorInputNeed.model_validate_json(
        (
            await store.content.get_packed(
                project_id=project_id,
                ref_id=question_ref_id,
            )
        ).unpack_and_verify()
    )
    correction_round = cast(
        Literal[0, 1],
        int(getattr(review, "automatic_correction_round")),
    )
    return CreatorInputRequestView(
        review_kind=review_kind,
        review_id=str(getattr(review, "id")),
        route_layer=route_layer,
        book_id=book_id,
        arc_id=(
            str(getattr(review, "arc_id"))
            if route_layer == "arc"
            else None
        ),
        automatic_correction_round=correction_round,
        question=question,
    )


def _commands(
    *,
    run: object,
    has_book_input: bool,
    has_book_approval: bool,
    has_arc_approval: bool,
    has_formal_baseline: bool,
    committed_chapter_count: int,
) -> list[ExecutableCommand]:
    status = str(getattr(run, "status"))
    started_at = getattr(run, "started_at_ms")
    wait_reason = getattr(run, "wait_reason_code")
    failure_source_kind = getattr(run, "failure_source_kind")
    blocking_task_id = getattr(run, "blocking_task_id")
    blocking_action_key = getattr(run, "blocking_action_key")
    return [
        ExecutableCommand(
            command_id="start_run",
            enabled=(status == "waiting_for_user" and started_at is None),
            reason="开始生成" if status == "waiting_for_user" and started_at is None else "运行已开始",
        ),
        ExecutableCommand(
            command_id="pause_run",
            enabled=status in {"running", "waiting_for_user"},
            reason="请求在安全边界暂停" if status in {"running", "waiting_for_user"} else "当前不可暂停",
        ),
        ExecutableCommand(
            command_id="resume_run",
            enabled=status == "paused",
            reason="继续已暂停流程" if status == "paused" else "仅普通暂停可继续",
        ),
        ExecutableCommand(
            command_id="retry_failed_task",
            enabled=(
                status == "failure_paused"
                and failure_source_kind == "agent_task"
                and blocking_task_id is not None
            ),
            reason="显式重试失败任务" if status == "failure_paused" else "当前没有失败任务",
        ),
        ExecutableCommand(
            command_id="retry_failed_action",
            enabled=(
                status == "failure_paused"
                and failure_source_kind == "harness_action"
                and blocking_action_key is not None
            ),
            reason=(
                "显式重建失败的 Harness 动作"
                if status == "failure_paused"
                and failure_source_kind == "harness_action"
                else "当前失败来源不是 Harness 动作"
            ),
        ),
        ExecutableCommand(
            command_id="send_book_input",
            enabled=(
                status == "waiting_for_user"
                and wait_reason in {"book_direction_input", "book_review_needs_user"}
                and has_book_input
            ),
            reason="回答当前 Book 问题" if has_book_input else "当前没有待回答问题",
        ),
        ExecutableCommand(
            command_id="approve_book",
            enabled=status == "waiting_for_user" and has_book_approval,
            reason="批准正式全书规划" if has_book_approval else "尚无通过评审的 Book 候选",
        ),
        ExecutableCommand(
            command_id="approve_arc",
            enabled=status == "waiting_for_user" and has_arc_approval,
            reason="批准当前故事弧" if has_arc_approval else "当前没有 Story Arc 审批门禁",
        ),
        ExecutableCommand(
            command_id="submit_feedback",
            enabled=has_formal_baseline and status != "completed",
            reason=(
                "提交分层反馈"
                if has_formal_baseline and status != "completed"
                else "需要先批准全书正式基线"
            ),
        ),
        ExecutableCommand(
            command_id="export_markdown",
            enabled=committed_chapter_count > 0,
            reason="导出已提交章节" if committed_chapter_count > 0 else "尚无已提交章节",
        ),
    ]
