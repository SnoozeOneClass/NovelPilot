from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from dataclasses import asdict
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from app.db.uow import UnitOfWork
from app.domain.arc.commands import ArcCommandService
from app.domain.arc.contracts import ApproveArcRequest
from app.domain.book.commands import BookCommandService
from app.domain.book.contracts import ApproveBookRequest, RecordBookUserInputRequest
from app.domain.commands import CommandExecution, CommandPreconditionError
from app.domain.export import ManuscriptExportResult, ManuscriptExportService
from app.domain.feedback import (
    ApplyFeedbackRequest,
    FeedbackCommandService,
    FeedbackLayer,
    RouteFeedbackRequest,
    SubmitFeedbackRequest,
)
from app.domain.project_state import (
    ProjectDiagnosticsView,
    ProjectListItem,
    ProjectStateQuery,
    ProjectStateView,
)
from app.domain.projects import (
    CreateProjectRequest,
    DeleteProjectRequest,
    ProjectCommandService,
    ProjectNotFoundError,
    UpdateProjectSettingsRequest,
)
from app.domain.snapshots import ProjectSnapshotManifest, SnapshotQueryService
from app.profiles import PublicProfile
from app.runtime.control import RetryFailedTaskRequest, RunControlRequest
from app.runtime.resources import ApplicationResources
from app.store.command_bus import CommandBus
from app.store.commands import DomainEventRecord

router = APIRouter()

IdempotencyKey = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=1, max_length=200),
]


class CreateProjectBody(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    creator_brief: str
    operation_mode: Literal["full_auto", "participatory"]
    default_profile_id: str | None = None
    book_profile_id: str | None = None
    arc_profile_id: str | None = None
    chapter_profile_id: str | None = None
    evaluator_profile_id: str | None = None


class UpdateProjectSettingsBody(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    expected_lock_version: int = Field(ge=1)
    operation_mode: Literal["full_auto", "participatory"]
    default_profile_id: str | None = None
    book_profile_id: str | None = None
    arc_profile_id: str | None = None
    chapter_profile_id: str | None = None
    evaluator_profile_id: str | None = None


class RunControlBody(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    expected_lock_version: int = Field(ge=1)


class BookInputBody(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    expected_workspace_lock_version: int = Field(ge=1)
    message: str
    suggestion_id: str | None = None


class ArcApprovalBody(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    target_chapter_count: int | None = Field(default=None, ge=1, le=30)


class FeedbackBody(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    content: str
    route_layer: FeedbackLayer
    expected_workspace_lock_version: int = Field(ge=1)


class MutationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    replayed: bool
    receipt_id: str
    state: ProjectStateView


class DeleteProjectResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    deleted: bool


class ProfileListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    selected_profile_id: str | None
    profiles: list[PublicProfile]


class DomainEventView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sequence: int
    event_id: str
    project_id: str
    run_id: str | None
    event_type: str
    aggregate_type: str
    aggregate_id: str
    payload: Any
    occurred_at_ms: int


class DomainEventPage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    events: list[DomainEventView]
    next_cursor: int


def _resources(request: Request) -> ApplicationResources:
    resources = getattr(request.app.state, "resources", None)
    if not isinstance(resources, ApplicationResources) or resources.closed:
        raise RuntimeError("Application resources are not available.")
    return resources


async def _state(resources: ApplicationResources, project_id: str) -> ProjectStateView:
    state = await ProjectStateQuery(resources.database_engine).get_project(project_id)
    if state is None:
        raise ProjectNotFoundError(project_id)
    return state


def _profile_ids(body: CreateProjectBody | UpdateProjectSettingsBody) -> set[str]:
    return {
        profile_id
        for profile_id in (
            body.default_profile_id,
            body.book_profile_id,
            body.arc_profile_id,
            body.chapter_profile_id,
            body.evaluator_profile_id,
        )
        if profile_id is not None
    }


def _validate_profiles(resources: ApplicationResources, profile_ids: set[str]) -> None:
    for profile_id in sorted(profile_ids):
        resources.profile_catalog.resolve(profile_id)


def _mutation(
    execution: CommandExecution[Any],
    state: ProjectStateView,
) -> MutationResponse:
    return MutationResponse(
        replayed=execution.replayed,
        receipt_id=execution.receipt_id,
        state=state,
    )


@router.get("/profiles", response_model=ProfileListResponse)
async def list_profiles(request: Request) -> ProfileListResponse:
    selected, profiles = _resources(request).profile_catalog.list_public()
    return ProfileListResponse(selected_profile_id=selected, profiles=profiles)


@router.get("/projects", response_model=list[ProjectListItem])
async def list_projects(request: Request) -> list[ProjectListItem]:
    return await ProjectStateQuery(_resources(request).database_engine).list_projects()


@router.post("/projects", response_model=MutationResponse, status_code=201)
async def create_project(
    body: CreateProjectBody,
    request: Request,
    idempotency_key: IdempotencyKey,
) -> MutationResponse:
    resources = _resources(request)
    selected, _ = resources.profile_catalog.list_public()
    default_profile_id = body.default_profile_id or selected
    request_model = CreateProjectRequest(
        **body.model_dump(exclude={"default_profile_id"}),
        default_profile_id=default_profile_id,
    )
    _validate_profiles(
        resources,
        {value for value in _profile_ids(body) | {default_profile_id} if value is not None},
    )
    execution = await ProjectCommandService(
        CommandBus(resources.database_engine)
    ).create_project(request_model, idempotency_key=idempotency_key)
    return _mutation(execution, await _state(resources, body.project_id))


@router.get("/projects/{project_id}", response_model=ProjectStateView)
async def get_project(project_id: str, request: Request) -> ProjectStateView:
    return await _state(_resources(request), project_id)


@router.get(
    "/projects/{project_id}/diagnostics",
    response_model=ProjectDiagnosticsView,
)
async def get_project_diagnostics(
    project_id: str,
    request: Request,
) -> ProjectDiagnosticsView:
    result = await ProjectStateQuery(
        _resources(request).database_engine
    ).get_diagnostics(project_id)
    if result is None:
        raise ProjectNotFoundError(project_id)
    return result


@router.put("/projects/{project_id}/settings", response_model=MutationResponse)
async def update_project_settings(
    project_id: str,
    body: UpdateProjectSettingsBody,
    request: Request,
    idempotency_key: IdempotencyKey,
) -> MutationResponse:
    resources = _resources(request)
    _validate_profiles(resources, _profile_ids(body))
    execution = await ProjectCommandService(
        CommandBus(resources.database_engine)
    ).update_settings(
        UpdateProjectSettingsRequest(project_id=project_id, **body.model_dump()),
        idempotency_key=idempotency_key,
    )
    resources.run_engine.wake()
    return _mutation(execution, await _state(resources, project_id))


@router.delete("/projects/{project_id}", response_model=DeleteProjectResponse)
async def delete_project(
    project_id: str,
    request: Request,
    idempotency_key: IdempotencyKey,
) -> DeleteProjectResponse:
    resources = _resources(request)
    result = await ProjectCommandService(
        CommandBus(resources.database_engine)
    ).delete_project(
        DeleteProjectRequest(project_id=project_id),
        idempotency_key=idempotency_key,
    )
    return DeleteProjectResponse(**result.model_dump())


async def _run_control(
    *,
    action: Literal["start", "pause", "resume"],
    project_id: str,
    body: RunControlBody,
    resources: ApplicationResources,
    idempotency_key: str,
) -> MutationResponse:
    state = await _state(resources, project_id)
    control_request = RunControlRequest(
        project_id=project_id,
        run_id=state.run.run_id,
        expected_lock_version=body.expected_lock_version,
    )
    method = getattr(resources.run_control, action)
    execution = await method(control_request, idempotency_key=idempotency_key)
    return _mutation(execution, await _state(resources, project_id))


@router.post("/projects/{project_id}/run/start", response_model=MutationResponse)
async def start_run(
    project_id: str,
    body: RunControlBody,
    request: Request,
    idempotency_key: IdempotencyKey,
) -> MutationResponse:
    return await _run_control(
        action="start",
        project_id=project_id,
        body=body,
        resources=_resources(request),
        idempotency_key=idempotency_key,
    )


@router.post("/projects/{project_id}/run/pause", response_model=MutationResponse)
async def pause_run(
    project_id: str,
    body: RunControlBody,
    request: Request,
    idempotency_key: IdempotencyKey,
) -> MutationResponse:
    return await _run_control(
        action="pause",
        project_id=project_id,
        body=body,
        resources=_resources(request),
        idempotency_key=idempotency_key,
    )


@router.post("/projects/{project_id}/run/resume", response_model=MutationResponse)
async def resume_run(
    project_id: str,
    body: RunControlBody,
    request: Request,
    idempotency_key: IdempotencyKey,
) -> MutationResponse:
    return await _run_control(
        action="resume",
        project_id=project_id,
        body=body,
        resources=_resources(request),
        idempotency_key=idempotency_key,
    )


@router.post("/projects/{project_id}/run/retry", response_model=MutationResponse)
async def retry_failed_task(
    project_id: str,
    body: RunControlBody,
    request: Request,
    idempotency_key: IdempotencyKey,
) -> MutationResponse:
    resources = _resources(request)
    state = await _state(resources, project_id)
    if state.run.blocking_task_id is None:
        raise CommandPreconditionError("The Run has no failed blocking task to retry.")
    execution = await resources.run_control.retry_failed_task(
        RetryFailedTaskRequest(
            project_id=project_id,
            run_id=state.run.run_id,
            expected_lock_version=body.expected_lock_version,
            task_id=state.run.blocking_task_id,
        ),
        idempotency_key=idempotency_key,
    )
    return _mutation(execution, await _state(resources, project_id))


@router.post("/projects/{project_id}/book/input", response_model=MutationResponse)
async def record_book_input(
    project_id: str,
    body: BookInputBody,
    request: Request,
    idempotency_key: IdempotencyKey,
) -> MutationResponse:
    resources = _resources(request)
    state = await _state(resources, project_id)
    execution = await BookCommandService(
        CommandBus(resources.database_engine)
    ).record_user_input(
        RecordBookUserInputRequest(
            project_id=project_id,
            book_id=state.book.book_id,
            **body.model_dump(),
        ),
        idempotency_key=idempotency_key,
    )
    resources.run_engine.wake()
    return _mutation(execution, await _state(resources, project_id))


@router.post("/projects/{project_id}/book/approve", response_model=MutationResponse)
async def approve_book(
    project_id: str,
    request: Request,
    idempotency_key: IdempotencyKey,
) -> MutationResponse:
    resources = _resources(request)
    state = await _state(resources, project_id)
    if state.book.pending_submission_id is None or state.book.pending_review_id is None:
        raise CommandPreconditionError("The Book has no reviewed candidate to approve.")
    execution = await BookCommandService(
        CommandBus(resources.database_engine)
    ).approve_and_commit(
        ApproveBookRequest(
            project_id=project_id,
            book_id=state.book.book_id,
            submission_id=state.book.pending_submission_id,
            review_id=state.book.pending_review_id,
            expected_current_baseline_id=state.book.current_baseline_id,
        ),
        idempotency_key=idempotency_key,
    )
    resources.run_engine.wake()
    return _mutation(execution, await _state(resources, project_id))


@router.post("/projects/{project_id}/arc/approve", response_model=MutationResponse)
async def approve_arc(
    project_id: str,
    body: ArcApprovalBody,
    request: Request,
    idempotency_key: IdempotencyKey,
) -> MutationResponse:
    resources = _resources(request)
    state = await _state(resources, project_id)
    arc = state.current_arc
    if (
        arc is None
        or arc.pending_submission_id is None
        or arc.pending_review_id is None
        or arc.approval_gate_id is None
    ):
        raise CommandPreconditionError("The current Story Arc has no approval gate.")
    target = body.target_chapter_count or arc.recommended_target_chapter_count
    if target is None:
        raise CommandPreconditionError("The Story Arc has no target chapter count.")
    execution = await ArcCommandService(
        CommandBus(resources.database_engine)
    ).approve_and_commit(
        ApproveArcRequest(
            project_id=project_id,
            book_id=state.book.book_id,
            arc_id=arc.arc_id,
            submission_id=arc.pending_submission_id,
            review_id=arc.pending_review_id,
            approval_gate_id=arc.approval_gate_id,
            target_chapter_count=target,
            expected_current_baseline_id=arc.current_baseline_id,
        ),
        idempotency_key=idempotency_key,
    )
    resources.run_engine.wake()
    return _mutation(execution, await _state(resources, project_id))


@router.post("/projects/{project_id}/feedback", response_model=MutationResponse)
async def submit_feedback(
    project_id: str,
    body: FeedbackBody,
    request: Request,
    idempotency_key: IdempotencyKey,
) -> MutationResponse:
    resources = _resources(request)
    state = await _state(resources, project_id)
    arc_id = None if state.current_arc is None else state.current_arc.arc_id
    chapter_id = None if state.current_chapter is None else state.current_chapter.chapter_id
    if body.route_layer in {"arc", "chapter"} and arc_id is None:
        raise CommandPreconditionError("The selected feedback layer has no current Story Arc.")
    if body.route_layer == "chapter" and chapter_id is None:
        raise CommandPreconditionError("The selected feedback layer has no current Chapter.")
    service = FeedbackCommandService(CommandBus(resources.database_engine))
    submitted = await service.submit(
        SubmitFeedbackRequest(project_id=project_id, content=body.content),
        idempotency_key=f"{idempotency_key}:submit",
    )
    feedback_id = submitted.result.feedback_id
    await service.route(
        RouteFeedbackRequest(
            project_id=project_id,
            feedback_id=feedback_id,
            route_layer=body.route_layer,
            book_id=state.book.book_id,
            arc_id=arc_id if body.route_layer in {"arc", "chapter"} else None,
            chapter_id=chapter_id if body.route_layer == "chapter" else None,
        ),
        idempotency_key=f"{idempotency_key}:route",
    )
    applied = await service.apply(
        ApplyFeedbackRequest(
            project_id=project_id,
            feedback_id=feedback_id,
            expected_workspace_lock_version=body.expected_workspace_lock_version,
        ),
        idempotency_key=f"{idempotency_key}:apply",
    )
    resources.run_engine.wake()
    return _mutation(applied, await _state(resources, project_id))


@router.post("/projects/{project_id}/export", response_model=ManuscriptExportResult)
async def export_manuscript(
    project_id: str,
    request: Request,
) -> ManuscriptExportResult:
    resources = _resources(request)
    return await ManuscriptExportService(
        CommandBus(resources.database_engine),
        export_root=request.app.state.export_root,
    ).export(project_id=project_id)


@router.get("/projects/{project_id}/snapshot", response_model=ProjectSnapshotManifest)
async def get_snapshot(
    project_id: str,
    request: Request,
) -> ProjectSnapshotManifest:
    resources = _resources(request)
    return await SnapshotQueryService(CommandBus(resources.database_engine)).current(
        project_id=project_id
    )


def _event_view(record: DomainEventRecord) -> DomainEventView:
    return DomainEventView(**asdict(record))


async def _read_events(
    resources: ApplicationResources,
    *,
    project_id: str,
    after: int,
    limit: int,
) -> list[DomainEventRecord]:
    async with UnitOfWork(resources.database_engine) as store:
        return await store.commands.list_events_after(
            project_id=project_id,
            after_sequence=after,
            limit=limit,
        )


@router.get("/projects/{project_id}/events", response_model=DomainEventPage)
async def list_events(
    project_id: str,
    request: Request,
    after: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> DomainEventPage:
    resources = _resources(request)
    await _state(resources, project_id)
    events = await _read_events(
        resources,
        project_id=project_id,
        after=after,
        limit=limit,
    )
    return DomainEventPage(
        events=[_event_view(event) for event in events],
        next_cursor=events[-1].sequence if events else after,
    )


def _sse(*, event: str, data: object, event_id: int | None = None) -> str:
    prefix = "" if event_id is None else f"id: {event_id}\n"
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"{prefix}event: {event}\ndata: {payload}\n\n"


async def _event_stream(
    *,
    request: Request,
    resources: ApplicationResources,
    project_id: str,
    cursor: int,
) -> AsyncIterator[str]:
    subscription = resources.live_events.subscribe()
    last_keepalive = time.monotonic()
    try:
        while not await request.is_disconnected():
            events = await _read_events(
                resources,
                project_id=project_id,
                after=cursor,
                limit=200,
            )
            if events:
                for record in events:
                    cursor = record.sequence
                    yield _sse(
                        event="domain_event",
                        event_id=record.sequence,
                        data=_event_view(record).model_dump(mode="json"),
                    )
                continue
            try:
                live = await asyncio.wait_for(subscription.__anext__(), timeout=0.5)
            except TimeoutError:
                live = None
            if live is not None and live.project_id == project_id:
                yield _sse(event="agent_live", data=asdict(live))
            if time.monotonic() - last_keepalive >= 15:
                yield ": keepalive\n\n"
                last_keepalive = time.monotonic()
    finally:
        subscription.close()


@router.get("/projects/{project_id}/events/stream")
async def stream_events(
    project_id: str,
    request: Request,
    after: Annotated[int, Query(ge=0)] = 0,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    resources = _resources(request)
    await _state(resources, project_id)
    cursor = after
    if last_event_id is not None:
        try:
            cursor = max(cursor, int(last_event_id))
        except ValueError:
            pass
    return StreamingResponse(
        _event_stream(
            request=request,
            resources=resources,
            project_id=project_id,
            cursor=cursor,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )
