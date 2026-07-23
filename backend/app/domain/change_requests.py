from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import replace
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.db.uow import StoreSession
from app.domain.commands import (
    CommandEffect,
    CommandEnvelope,
    CommandExecution,
    CommandPreconditionError,
    EventDraft,
)
from app.store.change_requests import (
    ArcBookChangeRequestRecord,
    ChapterArcChangeRequestRecord,
    ChapterBookChangeRequestRecord,
)
from app.store.command_bus import CommandBus

ChangeRequestKind = Literal["chapter_to_arc", "chapter_to_book", "arc_to_book"]
TargetLayer = Literal["arc", "book"]


class ActivateChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    change_request_id: str
    request_kind: ChangeRequestKind
    expected_target_baseline_id: str
    expected_workspace_lock_version: int = Field(ge=1)


class ActivateChangeResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    change_request_id: str
    target_layer: TargetLayer
    target_id: str
    workspace_lock_version: int = Field(ge=1)


class RejectChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    change_request_id: str
    request_kind: ChangeRequestKind
    reason: str

    @field_validator("reason")
    @classmethod
    def _reason_non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Change request rejection reason must be non-blank.")
        return value


class RejectChangeResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    change_request_id: str
    request_kind: ChangeRequestKind
    rejected: bool = True


class ChangeRequestCommandService:
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
        timestamp: int,
    ) -> CommandEnvelope:
        return CommandEnvelope.for_request(
            project_id=project_id,
            idempotency_key=idempotency_key,
            command_kind=command_kind,
            request_schema=f"{command_kind}.request.v1",
            request_payload=request,
            actor="user",
            command_id=self._id_factory(),
            created_at_ms=timestamp,
        )

    async def activate(
        self,
        request: ActivateChangeRequest,
        *,
        idempotency_key: str,
    ) -> CommandExecution[ActivateChangeResult]:
        timestamp = self._now_ms()
        envelope = self._envelope(
            request=request,
            project_id=request.project_id,
            idempotency_key=idempotency_key,
            command_kind="activate_change_request",
            timestamp=timestamp,
        )

        async def handler(session: StoreSession) -> CommandEffect[ActivateChangeResult]:
            project = await session.projects.get(request.project_id)
            if project is None or project.lifecycle_status != "active":
                raise CommandPreconditionError("Project is not active for revision.")
            if request.request_kind == "chapter_to_arc":
                chapter_arc_change = await session.changes.get_chapter_arc(
                    project_id=request.project_id,
                    request_id=request.change_request_id,
                )
                result = await self._activate_arc(
                    session,
                    change=chapter_arc_change,
                    request=request,
                    canon_baseline_id=project.current_canon_baseline_id,
                    timestamp=timestamp,
                )
            elif request.request_kind == "chapter_to_book":
                chapter_book_change = await session.changes.get_chapter_book(
                    project_id=request.project_id,
                    request_id=request.change_request_id,
                )
                result = await self._activate_book(
                    session,
                    change=chapter_book_change,
                    request=request,
                    canon_baseline_id=project.current_canon_baseline_id,
                    timestamp=timestamp,
                )
            else:
                arc_book_change = await session.changes.get_arc_book(
                    project_id=request.project_id,
                    request_id=request.change_request_id,
                )
                result = await self._activate_book(
                    session,
                    change=arc_book_change,
                    request=request,
                    canon_baseline_id=project.current_canon_baseline_id,
                    timestamp=timestamp,
                )
            return CommandEffect(
                result=result,
                events=(
                    EventDraft(
                        event_type="change_request.activated",
                        aggregate_type="change_request",
                        aggregate_id=request.change_request_id,
                        payload={
                            "request_kind": request.request_kind,
                            "target_layer": result.target_layer,
                            "target_id": result.target_id,
                            "workspace_lock_version": result.workspace_lock_version,
                        },
                    ),
                ),
            )

        return await self._command_bus.execute(
            envelope=envelope,
            result_type=ActivateChangeResult,
            handler=handler,
        )

    async def reject(
        self,
        request: RejectChangeRequest,
        *,
        idempotency_key: str,
    ) -> CommandExecution[RejectChangeResult]:
        timestamp = self._now_ms()
        envelope = self._envelope(
            request=request,
            project_id=request.project_id,
            idempotency_key=idempotency_key,
            command_kind="reject_change_request",
            timestamp=timestamp,
        )

        async def handler(session: StoreSession) -> CommandEffect[RejectChangeResult]:
            if request.request_kind == "chapter_to_arc":
                chapter_arc_change = await session.changes.get_chapter_arc(
                    project_id=request.project_id,
                    request_id=request.change_request_id,
                )
                if chapter_arc_change is None or chapter_arc_change.status != "open":
                    raise CommandPreconditionError("Chapter-to-Arc request is not open.")
                rejected = await session.changes.reject_chapter_arc(
                    project_id=request.project_id,
                    request_id=request.change_request_id,
                    reason=request.reason,
                    now_ms=timestamp,
                )
                await self._block_chapter_for_user(
                    session,
                    project_id=request.project_id,
                    chapter_id=chapter_arc_change.chapter_id,
                    timestamp=timestamp,
                )
            elif request.request_kind == "chapter_to_book":
                chapter_book_change = await session.changes.get_chapter_book(
                    project_id=request.project_id,
                    request_id=request.change_request_id,
                )
                if chapter_book_change is None or chapter_book_change.status != "open":
                    raise CommandPreconditionError("Chapter-to-Book request is not open.")
                rejected = await session.changes.reject_chapter_book(
                    project_id=request.project_id,
                    request_id=request.change_request_id,
                    reason=request.reason,
                    now_ms=timestamp,
                )
                await self._block_chapter_for_user(
                    session,
                    project_id=request.project_id,
                    chapter_id=chapter_book_change.chapter_id,
                    timestamp=timestamp,
                )
            else:
                arc_book_change = await session.changes.get_arc_book(
                    project_id=request.project_id,
                    request_id=request.change_request_id,
                )
                if arc_book_change is None or arc_book_change.status != "open":
                    raise CommandPreconditionError("Arc-to-Book request is not open.")
                rejected = await session.changes.reject_arc_book(
                    project_id=request.project_id,
                    request_id=request.change_request_id,
                    reason=request.reason,
                    now_ms=timestamp,
                )
                await self._block_arc_for_user(
                    session,
                    project_id=request.project_id,
                    arc_id=arc_book_change.arc_id,
                    timestamp=timestamp,
                )
            if not rejected:
                raise CommandPreconditionError("Change request changed concurrently.")
            result = RejectChangeResult(
                project_id=request.project_id,
                change_request_id=request.change_request_id,
                request_kind=request.request_kind,
            )
            return CommandEffect(
                result=result,
                events=(
                    EventDraft(
                        event_type="change_request.rejected",
                        aggregate_type="change_request",
                        aggregate_id=request.change_request_id,
                        payload={
                            "request_kind": request.request_kind,
                            "reason": request.reason,
                        },
                    ),
                ),
            )

        return await self._command_bus.execute(
            envelope=envelope,
            result_type=RejectChangeResult,
            handler=handler,
        )

    @staticmethod
    async def _activate_arc(
        session: StoreSession,
        *,
        change: ChapterArcChangeRequestRecord | None,
        request: ActivateChangeRequest,
        canon_baseline_id: str,
        timestamp: int,
    ) -> ActivateChangeResult:
        if (
            change is None
            or change.status != "open"
            or change.target_arc_baseline_id != request.expected_target_baseline_id
        ):
            raise CommandPreconditionError("Chapter-to-Arc request is stale.")
        book = await session.books.get_for_project(request.project_id)
        arc = await session.arcs.get(project_id=request.project_id, arc_id=change.arc_id)
        workspace = await session.arcs.get_workspace(
            project_id=request.project_id,
            arc_id=change.arc_id,
        )
        if (
            book is None
            or book.id != change.book_id
            or book.current_baseline_id is None
            or book.current_completion_id is not None
            or arc is None
            or arc.current_baseline_id != request.expected_target_baseline_id
            or workspace is None
            or workspace.lock_version != request.expected_workspace_lock_version
        ):
            raise CommandPreconditionError("Arc revision target is no longer current.")
        gate = await session.arcs.find_pending_gate(
            project_id=request.project_id,
            arc_id=arc.id,
        )
        if gate is not None and not await session.arcs.close_approval_gate(
            project_id=request.project_id,
            gate_id=gate.id,
            state="superseded",
            closed_at_ms=timestamp,
        ):
            raise CommandPreconditionError("Arc revision gate changed concurrently.")
        pending = await session.arcs.find_pending_submission(
            project_id=request.project_id,
            arc_id=arc.id,
        )
        if pending is not None and not await session.arcs.close_submission(
            project_id=request.project_id,
            submission_id=pending.id,
            disposition="superseded",
            reason_code="change_request_activated",
            closed_at_ms=timestamp,
        ):
            raise CommandPreconditionError("Arc revision submission changed concurrently.")
        updated = replace(
            workspace,
            state="active",
            lock_version=workspace.lock_version + 1,
            base_arc_baseline_id=arc.current_baseline_id,
            book_baseline_id=book.current_baseline_id,
            canon_baseline_id=canon_baseline_id,
            plan_ref_id=None,
            recommended_target_chapter_count=None,
            guidance_ref_id=change.evidence_ref_id,
            semantic_repair_count=0,
            stale_reason_code=None,
            stale_at_ms=None,
            updated_at_ms=timestamp,
        )
        if not await session.arcs.compare_and_set_workspace(
            record=updated,
            expected_lock_version=workspace.lock_version,
        ):
            raise CommandPreconditionError("Arc revision workspace CAS failed.")
        return ActivateChangeResult(
            project_id=request.project_id,
            change_request_id=request.change_request_id,
            target_layer="arc",
            target_id=arc.id,
            workspace_lock_version=updated.lock_version,
        )

    @staticmethod
    async def _activate_book(
        session: StoreSession,
        *,
        change: ChapterBookChangeRequestRecord | ArcBookChangeRequestRecord | None,
        request: ActivateChangeRequest,
        canon_baseline_id: str,
        timestamp: int,
    ) -> ActivateChangeResult:
        if (
            change is None
            or change.status != "open"
            or change.target_book_baseline_id != request.expected_target_baseline_id
        ):
            raise CommandPreconditionError("Book change request is stale.")
        book = await session.books.get_for_project(request.project_id)
        workspace = await session.books.get_workspace(
            project_id=request.project_id,
            book_id=change.book_id,
        )
        if (
            book is None
            or book.id != change.book_id
            or book.current_baseline_id != request.expected_target_baseline_id
            or book.current_completion_id is not None
            or workspace is None
            or workspace.lock_version != request.expected_workspace_lock_version
        ):
            raise CommandPreconditionError("Book revision target is no longer current.")
        pending = await session.books.find_pending_submission(
            project_id=request.project_id,
            book_id=book.id,
        )
        if pending is not None and not await session.books.close_submission(
            project_id=request.project_id,
            submission_id=pending.id,
            disposition="superseded",
            reason_code="change_request_activated",
            closed_at_ms=timestamp,
        ):
            raise CommandPreconditionError("Book revision submission changed concurrently.")
        updated = replace(
            workspace,
            state="active",
            lock_version=workspace.lock_version + 1,
            base_book_baseline_id=book.current_baseline_id,
            base_canon_baseline_id=canon_baseline_id,
            candidate_constraints_ref_id=None,
            candidate_titles_ref_id=None,
            candidate_rolling_plan_ref_id=None,
            candidate_completion_contract_ref_id=None,
            guidance_ref_id=change.evidence_ref_id,
            semantic_repair_count=0,
            stale_reason_code=None,
            stale_at_ms=None,
            updated_at_ms=timestamp,
        )
        if not await session.books.compare_and_set_workspace(
            record=updated,
            expected_lock_version=workspace.lock_version,
        ):
            raise CommandPreconditionError("Book revision workspace CAS failed.")
        return ActivateChangeResult(
            project_id=request.project_id,
            change_request_id=request.change_request_id,
            target_layer="book",
            target_id=book.id,
            workspace_lock_version=updated.lock_version,
        )

    @staticmethod
    async def _block_chapter_for_user(
        session: StoreSession,
        *,
        project_id: str,
        chapter_id: str,
        timestamp: int,
    ) -> None:
        workspace = await session.chapters.get_workspace(
            project_id=project_id,
            chapter_id=chapter_id,
        )
        if workspace is None or workspace.state != "blocked_by_upstream":
            raise CommandPreconditionError("Source Chapter is not blocked by the request.")
        updated = replace(
            workspace,
            state="blocked_by_user",
            lock_version=workspace.lock_version + 1,
            updated_at_ms=timestamp,
        )
        if not await session.chapters.compare_and_set_workspace(
            record=updated,
            expected_lock_version=workspace.lock_version,
        ):
            raise CommandPreconditionError("Source Chapter workspace CAS failed.")

    @staticmethod
    async def _block_arc_for_user(
        session: StoreSession,
        *,
        project_id: str,
        arc_id: str,
        timestamp: int,
    ) -> None:
        workspace = await session.arcs.get_workspace(project_id=project_id, arc_id=arc_id)
        if workspace is None or workspace.state != "blocked_by_upstream":
            raise CommandPreconditionError("Source Arc is not blocked by the request.")
        updated = replace(
            workspace,
            state="blocked_by_user",
            lock_version=workspace.lock_version + 1,
            updated_at_ms=timestamp,
        )
        if not await session.arcs.compare_and_set_workspace(
            record=updated,
            expected_lock_version=workspace.lock_version,
        ):
            raise CommandPreconditionError("Source Arc workspace CAS failed.")
