from __future__ import annotations

import hashlib
import time
import uuid
from collections.abc import Callable
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.db.uow import StoreSession
from app.domain.book.contracts import (
    BookDiscussionState,
    BookTranscript,
    BookTranscriptMessage,
)
from app.domain.commands import (
    CommandEffect,
    CommandEnvelope,
    CommandExecution,
    CommandPreconditionError,
    EventDraft,
    IdempotencyConflictError,
    canonical_json_bytes,
)
from app.store.books import BookRecord, BookWorkspaceRecord
from app.store.arcs import ArcApprovalGateRecord
from app.store.canon import CanonSeedRecord
from app.store.command_bus import CommandBus
from app.store.content import PreparedContent, prepare_canonical_json, prepare_exact_text
from app.store.projects import ProjectRecord
from app.store.runs import GenerationRunRecord

OperationMode = Literal["full_auto", "participatory"]


class CreateProjectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    creator_brief: str
    operation_mode: OperationMode
    default_profile_id: str | None = None
    book_profile_id: str | None = None
    arc_profile_id: str | None = None
    chapter_profile_id: str | None = None
    evaluator_profile_id: str | None = None

    @field_validator("project_id", "creator_brief")
    @classmethod
    def _required_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("project_id and creator_brief must be non-blank")
        return value

    @field_validator(
        "default_profile_id",
        "book_profile_id",
        "arc_profile_id",
        "chapter_profile_id",
        "evaluator_profile_id",
    )
    @classmethod
    def _optional_non_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("profile IDs must be non-blank when present")
        return value


class CreateProjectResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    book_id: str
    book_workspace_id: str
    canon_baseline_id: str
    generation_run_id: str
    settings_lock_version: int = 1
    workspace_lock_version: int = 1


class UpdateProjectSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    expected_lock_version: int = Field(ge=1)
    operation_mode: OperationMode
    default_profile_id: str | None = None
    book_profile_id: str | None = None
    arc_profile_id: str | None = None
    chapter_profile_id: str | None = None
    evaluator_profile_id: str | None = None

    @field_validator("project_id")
    @classmethod
    def _project_id_non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("project_id must be non-blank")
        return value

    @field_validator(
        "default_profile_id",
        "book_profile_id",
        "arc_profile_id",
        "chapter_profile_id",
        "evaluator_profile_id",
    )
    @classmethod
    def _profile_non_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("profile IDs must be non-blank when present")
        return value


class UpdateProjectSettingsResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    operation_mode: OperationMode
    settings_lock_version: int
    arc_approval_gate_id: str | None = None


class DeleteProjectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str

    @field_validator("project_id")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("project_id must be non-blank")
        return value


class DeleteProjectResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    deleted: bool


class ProjectNotFoundError(LookupError):
    pass


class ProjectBusyError(RuntimeError):
    pass


class _ProjectAssets(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    direction: PreparedContent
    discussion: PreparedContent
    transcript: PreparedContent
    canon_characters: PreparedContent
    canon_relationships: PreparedContent
    canon_world_facts: PreparedContent
    canon_foreshadowing: PreparedContent


class ProjectCommandService:
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

    def _envelope(
        self,
        *,
        request: BaseModel,
        project_id: str,
        idempotency_key: str,
        command_kind: str,
        actor: Literal["user", "engine", "system"],
        created_at_ms: int,
    ) -> CommandEnvelope:
        return CommandEnvelope.for_request(
            project_id=project_id,
            idempotency_key=idempotency_key,
            command_kind=command_kind,
            request_schema=f"{command_kind}.request.v1",
            request_payload=request,
            actor=actor,
            command_id=self._id_factory(),
            created_at_ms=created_at_ms,
        )

    async def create_project(
        self,
        request: CreateProjectRequest,
        *,
        idempotency_key: str,
    ) -> CommandExecution[CreateProjectResult]:
        timestamp = self._now_ms()
        envelope = self._envelope(
            request=request,
            project_id=request.project_id,
            idempotency_key=idempotency_key,
            command_kind="create_project",
            actor="user",
            created_at_ms=timestamp,
        )
        book_id = self._id_factory()
        workspace_id = self._id_factory()
        canon_id = self._id_factory()
        run_id = self._id_factory()
        ref_ids = [self._id_factory() for _ in range(7)]
        assets = _ProjectAssets(
            direction=prepare_exact_text(request.creator_brief),
            discussion=prepare_canonical_json(
                BookDiscussionState(
                    turn_count=0,
                    direction_draft=request.creator_brief,
                    discussion_summary="",
                    readiness_status="awaiting_agent",
                    readiness_reason="The creator brief is ready for the first Book turn.",
                )
            ),
            transcript=prepare_canonical_json(
                BookTranscript(
                    messages=[
                        BookTranscriptMessage(
                            sequence=1,
                            role="user",
                            content=request.creator_brief,
                        )
                    ]
                )
            ),
            canon_characters=prepare_canonical_json([]),
            canon_relationships=prepare_canonical_json([]),
            canon_world_facts=prepare_canonical_json([]),
            canon_foreshadowing=prepare_canonical_json([]),
        )

        async def handler(session: StoreSession) -> CommandEffect[CreateProjectResult]:
            await session.projects.insert(
                ProjectRecord(
                    id=request.project_id,
                    operation_mode=request.operation_mode,
                    lifecycle_status="active",
                    settings_lock_version=1,
                    default_profile_id=request.default_profile_id,
                    book_profile_id=request.book_profile_id,
                    arc_profile_id=request.arc_profile_id,
                    chapter_profile_id=request.chapter_profile_id,
                    evaluator_profile_id=request.evaluator_profile_id,
                    current_canon_baseline_id=canon_id,
                    created_at_ms=timestamp,
                    updated_at_ms=timestamp,
                )
            )
            await session.books.insert(
                BookRecord(
                    id=book_id,
                    project_id=request.project_id,
                    lifecycle_status="developing",
                    current_baseline_id=None,
                    current_completion_id=None,
                    created_at_ms=timestamp,
                    updated_at_ms=timestamp,
                )
            )

            content_specs = (
                (assets.direction, "book.direction_draft", "text/plain; charset=utf-8", None, None),
                (
                    assets.discussion,
                    "book.discussion_state",
                    "application/json",
                    "book-discussion-state",
                    1,
                ),
                (
                    assets.transcript,
                    "book.transcript",
                    "application/json",
                    "book-transcript",
                    1,
                ),
                (
                    assets.canon_characters,
                    "canon.characters",
                    "application/json",
                    "canon-characters",
                    1,
                ),
                (
                    assets.canon_relationships,
                    "canon.relationships",
                    "application/json",
                    "canon-relationships",
                    1,
                ),
                (
                    assets.canon_world_facts,
                    "canon.world_facts",
                    "application/json",
                    "canon-world-facts",
                    1,
                ),
                (
                    assets.canon_foreshadowing,
                    "canon.foreshadowing",
                    "application/json",
                    "canon-foreshadowing",
                    1,
                ),
            )
            references = [
                await session.content.put(
                    project_id=request.project_id,
                    prepared=prepared,
                    semantic_kind=semantic_kind,
                    media_type=media_type,
                    schema_id=schema_id,
                    schema_version=schema_version,
                    ref_id=ref_id,
                    created_at_ms=timestamp,
                )
                for ref_id, (
                    prepared,
                    semantic_kind,
                    media_type,
                    schema_id,
                    schema_version,
                ) in zip(ref_ids, content_specs, strict=True)
            ]
            direction_ref, discussion_ref, transcript_ref = references[:3]
            canon_refs = references[3:]
            manifest_fingerprint = hashlib.sha256(
                canonical_json_bytes(
                    {
                        "schema": "canon-manifest-v1",
                        "characters_ref_id": canon_refs[0].id,
                        "relationships_ref_id": canon_refs[1].id,
                        "world_facts_ref_id": canon_refs[2].id,
                        "foreshadowing_ref_id": canon_refs[3].id,
                    }
                )
            ).hexdigest()
            await session.canon.insert_seed(
                CanonSeedRecord(
                    id=canon_id,
                    project_id=request.project_id,
                    characters_ref_id=canon_refs[0].id,
                    relationships_ref_id=canon_refs[1].id,
                    world_facts_ref_id=canon_refs[2].id,
                    foreshadowing_ref_id=canon_refs[3].id,
                    manifest_fingerprint=manifest_fingerprint,
                    created_at_ms=timestamp,
                )
            )
            await session.books.insert_workspace(
                BookWorkspaceRecord(
                    id=workspace_id,
                    project_id=request.project_id,
                    book_id=book_id,
                    state="active",
                    lock_version=1,
                    base_book_baseline_id=None,
                    base_canon_baseline_id=canon_id,
                    direction_draft_ref_id=direction_ref.id,
                    discussion_state_ref_id=discussion_ref.id,
                    transcript_ref_id=transcript_ref.id,
                    candidate_constraints_ref_id=None,
                    candidate_titles_ref_id=None,
                    candidate_rolling_plan_ref_id=None,
                    candidate_completion_contract_ref_id=None,
                    readiness_status="continue",
                    repair_policy_id="semantic-repair-v1",
                    semantic_repair_count=0,
                    semantic_repair_limit=5,
                    stale_reason_code=None,
                    stale_at_ms=None,
                    created_at_ms=timestamp,
                    updated_at_ms=timestamp,
                )
            )
            await session.runs.insert(
                GenerationRunRecord(
                    id=run_id,
                    project_id=request.project_id,
                    run_number=1,
                    status="waiting_for_user",
                    desired_state="running",
                    lock_version=1,
                    wait_reason_code="book_direction_input",
                    created_at_ms=timestamp,
                    updated_at_ms=timestamp,
                )
            )
            result = CreateProjectResult(
                project_id=request.project_id,
                book_id=book_id,
                book_workspace_id=workspace_id,
                canon_baseline_id=canon_id,
                generation_run_id=run_id,
            )
            return CommandEffect(
                result=result,
                events=(
                    EventDraft(
                        event_type="project.created",
                        aggregate_type="project",
                        aggregate_id=request.project_id,
                        payload={
                            "operation_mode": request.operation_mode,
                            "book_id": book_id,
                            "run_id": run_id,
                        },
                    ),
                ),
            )

        return await self._command_bus.execute(
            envelope=envelope,
            result_type=CreateProjectResult,
            handler=handler,
        )

    async def update_settings(
        self,
        request: UpdateProjectSettingsRequest,
        *,
        idempotency_key: str,
    ) -> CommandExecution[UpdateProjectSettingsResult]:
        timestamp = self._now_ms()
        arc_gate_id = self._id_factory()
        envelope = self._envelope(
            request=request,
            project_id=request.project_id,
            idempotency_key=idempotency_key,
            command_kind="update_project_settings",
            actor="user",
            created_at_ms=timestamp,
        )

        async def handler(session: StoreSession) -> CommandEffect[UpdateProjectSettingsResult]:
            project = await session.projects.get(request.project_id)
            if project is None:
                raise ProjectNotFoundError(request.project_id)
            if await session.projects.has_active_execution(request.project_id):
                raise ProjectBusyError("Project settings cannot change during active execution.")
            pending_arc_review = None
            if (
                project.operation_mode != "participatory"
                and request.operation_mode == "participatory"
            ):
                book = await session.books.get_for_project(request.project_id)
                if book is not None:
                    pending_arc_review = await session.arcs.find_passed_pending_without_gate(
                        project_id=request.project_id,
                        book_id=book.id,
                    )
            changed = await session.projects.compare_and_set_settings(
                project_id=request.project_id,
                expected_lock_version=request.expected_lock_version,
                operation_mode=request.operation_mode,
                default_profile_id=request.default_profile_id,
                book_profile_id=request.book_profile_id,
                arc_profile_id=request.arc_profile_id,
                chapter_profile_id=request.chapter_profile_id,
                evaluator_profile_id=request.evaluator_profile_id,
                updated_at_ms=timestamp,
            )
            if not changed:
                raise CommandPreconditionError("Project settings lock version is stale.")
            events = [
                EventDraft(
                    event_type="project.settings_changed",
                    aggregate_type="project",
                    aggregate_id=request.project_id,
                    payload={
                        "operation_mode": request.operation_mode,
                        "settings_lock_version": request.expected_lock_version + 1,
                    },
                )
            ]
            created_gate_id = None
            if pending_arc_review is not None:
                submission, review = pending_arc_review
                await session.arcs.insert_approval_gate(
                    ArcApprovalGateRecord(
                        id=arc_gate_id,
                        project_id=request.project_id,
                        book_id=submission.book_id,
                        arc_id=submission.arc_id,
                        submission_id=submission.id,
                        review_id=review.id,
                        reason="mode_switch",
                        state="pending",
                        created_at_ms=timestamp,
                        closed_at_ms=None,
                    )
                )
                created_gate_id = arc_gate_id
                run = await session.runs.get_open_for_project(request.project_id)
                if run is not None and run.status == "running":
                    if not await session.runs.wait_for_user(
                        run_id=run.id,
                        reason_code="arc_approval_required",
                        now_ms=timestamp,
                    ):
                        raise CommandPreconditionError("Run could not enter Arc approval wait.")
                events.append(
                    EventDraft(
                        event_type="arc.approval_required",
                        aggregate_type="arc",
                        aggregate_id=submission.arc_id,
                        payload={
                            "approval_gate_id": arc_gate_id,
                            "reason": "mode_switch",
                        },
                    )
                )
            result = UpdateProjectSettingsResult(
                project_id=request.project_id,
                operation_mode=request.operation_mode,
                settings_lock_version=request.expected_lock_version + 1,
                arc_approval_gate_id=created_gate_id,
            )
            return CommandEffect(result=result, events=tuple(events))

        return await self._command_bus.execute(
            envelope=envelope,
            result_type=UpdateProjectSettingsResult,
            handler=handler,
        )

    async def delete_project(
        self,
        request: DeleteProjectRequest,
        *,
        idempotency_key: str,
    ) -> DeleteProjectResult:
        """Delete the root; its temporary receipt/event intentionally cascade with it."""
        timestamp = self._now_ms()
        envelope = self._envelope(
            request=request,
            project_id=request.project_id,
            idempotency_key=idempotency_key,
            command_kind="delete_project",
            actor="user",
            created_at_ms=timestamp,
        )
        async with self._command_bus.unit_of_work() as session:
            project = await session.projects.get(request.project_id)
            if project is None:
                raise ProjectNotFoundError(request.project_id)
            existing = await session.commands.find_receipt(
                project_id=request.project_id,
                idempotency_key=idempotency_key,
            )
            if existing is not None and (
                existing.command_kind != envelope.command_kind
                or existing.request_fingerprint != envelope.request_fingerprint
            ):
                raise IdempotencyConflictError(
                    "The idempotency key is already bound to a different command request."
                )
            if await session.projects.has_active_execution(request.project_id):
                raise ProjectBusyError("Project has a running attempt or owns the engine slot.")
            result = DeleteProjectResult(project_id=request.project_id, deleted=True)
            receipt = await session.commands.insert_receipt(
                envelope=envelope,
                result=result.model_dump(mode="json"),
            )
            sequence = await session.commands.append_event(
                envelope=envelope,
                receipt_id=receipt.id,
                draft=EventDraft(
                    event_type="project.deleted",
                    aggregate_type="project",
                    aggregate_id=request.project_id,
                    payload={"deleted": True},
                ),
            )
            await session.commands.set_event_range(
                receipt_id=receipt.id,
                first_sequence=sequence,
                last_sequence=sequence,
            )
            if not await session.projects.delete_root(request.project_id):
                raise ProjectNotFoundError(request.project_id)
            return result
