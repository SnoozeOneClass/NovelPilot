from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import replace
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.db.uow import StoreSession
from app.domain.commands import (
    Actor,
    CommandEffect,
    CommandEnvelope,
    CommandExecution,
    CommandPreconditionError,
    EventDraft,
)
from app.domain.projects import ProjectNotFoundError
from app.store.command_bus import CommandBus
from app.store.content import prepare_exact_text
from app.store.feedback import FeedbackRecord

FeedbackLayer = Literal["book", "arc", "chapter"]


class SubmitFeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    content: str

    @field_validator("project_id", "content")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Feedback project and content must be non-blank.")
        return value


class SubmitFeedbackResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    feedback_id: str
    status: Literal["pending"] = "pending"


class RouteFeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    feedback_id: str
    route_layer: FeedbackLayer
    book_id: str
    arc_id: str | None = None
    chapter_id: str | None = None

    @model_validator(mode="after")
    def _target_shape(self) -> RouteFeedbackRequest:
        valid = (
            (self.route_layer == "book" and self.arc_id is None and self.chapter_id is None)
            or (
                self.route_layer == "arc"
                and self.arc_id is not None
                and self.chapter_id is None
            )
            or (
                self.route_layer == "chapter"
                and self.arc_id is not None
                and self.chapter_id is not None
            )
        )
        if not valid:
            raise ValueError("Feedback target IDs do not match the selected layer.")
        return self


class RouteFeedbackResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    feedback_id: str
    route_layer: FeedbackLayer
    book_id: str
    arc_id: str | None
    chapter_id: str | None
    status: Literal["routed"] = "routed"


class ApplyFeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    feedback_id: str
    expected_workspace_lock_version: int = Field(ge=1)


class ApplyFeedbackResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    feedback_id: str
    route_layer: FeedbackLayer
    target_id: str
    workspace_lock_version: int = Field(ge=1)
    status: Literal["applied"] = "applied"


class DismissFeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    feedback_id: str
    reason: str

    @field_validator("reason")
    @classmethod
    def _reason_non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Feedback dismissal reason must be non-blank.")
        return value


class DismissFeedbackResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    feedback_id: str
    status: Literal["dismissed"] = "dismissed"


class FeedbackCommandService:
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
        actor: Actor,
        timestamp: int,
    ) -> CommandEnvelope:
        return CommandEnvelope.for_request(
            project_id=project_id,
            idempotency_key=idempotency_key,
            command_kind=command_kind,
            request_schema=f"{command_kind}.request.v1",
            request_payload=request,
            actor=actor,
            command_id=self._id_factory(),
            created_at_ms=timestamp,
        )

    async def submit(
        self,
        request: SubmitFeedbackRequest,
        *,
        idempotency_key: str,
    ) -> CommandExecution[SubmitFeedbackResult]:
        timestamp = self._now_ms()
        feedback_id = self._id_factory()
        content_ref_id = self._id_factory()
        prepared = prepare_exact_text(request.content)
        envelope = self._envelope(
            request=request,
            project_id=request.project_id,
            idempotency_key=idempotency_key,
            command_kind="submit_feedback",
            actor="user",
            timestamp=timestamp,
        )

        async def handler(session: StoreSession) -> CommandEffect[SubmitFeedbackResult]:
            if await session.projects.get(request.project_id) is None:
                raise ProjectNotFoundError(request.project_id)
            content_ref = await session.content.put(
                project_id=request.project_id,
                prepared=prepared,
                semantic_kind="user.feedback",
                media_type="text/plain; charset=utf-8",
                schema_id=None,
                schema_version=None,
                ref_id=content_ref_id,
                created_at_ms=timestamp,
            )
            await session.feedback.insert(
                FeedbackRecord(
                    id=feedback_id,
                    project_id=request.project_id,
                    content_ref_id=content_ref.id,
                    status="pending",
                    route_layer=None,
                    book_id=None,
                    arc_id=None,
                    chapter_id=None,
                    applied_command_id=None,
                    created_at_ms=timestamp,
                    routed_at_ms=None,
                    applied_at_ms=None,
                )
            )
            result = SubmitFeedbackResult(
                project_id=request.project_id,
                feedback_id=feedback_id,
            )
            return CommandEffect(
                result=result,
                events=(
                    EventDraft(
                        event_type="feedback.submitted",
                        aggregate_type="feedback",
                        aggregate_id=feedback_id,
                        payload={},
                    ),
                ),
            )

        return await self._command_bus.execute(
            envelope=envelope,
            result_type=SubmitFeedbackResult,
            handler=handler,
        )

    async def route(
        self,
        request: RouteFeedbackRequest,
        *,
        idempotency_key: str,
    ) -> CommandExecution[RouteFeedbackResult]:
        timestamp = self._now_ms()
        envelope = self._envelope(
            request=request,
            project_id=request.project_id,
            idempotency_key=idempotency_key,
            command_kind="route_feedback",
            actor="engine",
            timestamp=timestamp,
        )

        async def handler(session: StoreSession) -> CommandEffect[RouteFeedbackResult]:
            feedback = await session.feedback.get(
                project_id=request.project_id,
                feedback_id=request.feedback_id,
            )
            book = await session.books.get_for_project(request.project_id)
            if feedback is None or feedback.status != "pending":
                raise CommandPreconditionError("Feedback is not pending.")
            if book is None or book.id != request.book_id:
                raise CommandPreconditionError("Feedback Book target does not exist.")
            if request.route_layer in {"arc", "chapter"}:
                assert request.arc_id is not None
                arc = await session.arcs.get(
                    project_id=request.project_id,
                    arc_id=request.arc_id,
                )
                if arc is None or arc.book_id != request.book_id:
                    raise CommandPreconditionError("Feedback Arc target does not exist.")
            if request.route_layer == "chapter":
                assert request.chapter_id is not None and request.arc_id is not None
                chapter = await session.chapters.get(
                    project_id=request.project_id,
                    chapter_id=request.chapter_id,
                )
                if (
                    chapter is None
                    or chapter.book_id != request.book_id
                    or chapter.arc_id != request.arc_id
                ):
                    raise CommandPreconditionError("Feedback Chapter target does not exist.")
            if not await session.feedback.route(
                project_id=request.project_id,
                feedback_id=request.feedback_id,
                route_layer=request.route_layer,
                book_id=request.book_id,
                arc_id=request.arc_id,
                chapter_id=request.chapter_id,
                routed_at_ms=timestamp,
            ):
                raise CommandPreconditionError("Feedback routing changed concurrently.")
            result = RouteFeedbackResult(**request.model_dump())
            return CommandEffect(
                result=result,
                events=(
                    EventDraft(
                        event_type="feedback.routed",
                        aggregate_type="feedback",
                        aggregate_id=request.feedback_id,
                        payload={
                            "route_layer": request.route_layer,
                            "book_id": request.book_id,
                            "arc_id": request.arc_id,
                            "chapter_id": request.chapter_id,
                        },
                    ),
                ),
            )

        return await self._command_bus.execute(
            envelope=envelope,
            result_type=RouteFeedbackResult,
            handler=handler,
        )

    async def apply(
        self,
        request: ApplyFeedbackRequest,
        *,
        idempotency_key: str,
    ) -> CommandExecution[ApplyFeedbackResult]:
        timestamp = self._now_ms()
        envelope = self._envelope(
            request=request,
            project_id=request.project_id,
            idempotency_key=idempotency_key,
            command_kind="apply_feedback",
            actor="engine",
            timestamp=timestamp,
        )

        async def handler(session: StoreSession) -> CommandEffect[ApplyFeedbackResult]:
            project = await session.projects.get(request.project_id)
            feedback = await session.feedback.get(
                project_id=request.project_id,
                feedback_id=request.feedback_id,
            )
            if project is None:
                raise ProjectNotFoundError(request.project_id)
            if project.lifecycle_status != "active":
                raise CommandPreconditionError("Completed projects must be reopened first.")
            if feedback is None or feedback.status != "routed" or feedback.book_id is None:
                raise CommandPreconditionError("Feedback has no current routed target.")
            route_layer = feedback.route_layer
            if route_layer == "book":
                target_id, lock_version = await self._activate_book(
                    session,
                    feedback=feedback,
                    expected_lock_version=request.expected_workspace_lock_version,
                    timestamp=timestamp,
                    canon_baseline_id=project.current_canon_baseline_id,
                )
            elif route_layer == "arc":
                target_id, lock_version = await self._activate_arc(
                    session,
                    feedback=feedback,
                    expected_lock_version=request.expected_workspace_lock_version,
                    timestamp=timestamp,
                    canon_baseline_id=project.current_canon_baseline_id,
                )
            elif route_layer == "chapter":
                target_id, lock_version = await self._activate_chapter(
                    session,
                    feedback=feedback,
                    expected_lock_version=request.expected_workspace_lock_version,
                    timestamp=timestamp,
                    canon_baseline_id=project.current_canon_baseline_id,
                )
            else:  # pragma: no cover - protected by the feedback table constraint.
                raise CommandPreconditionError("Feedback route layer is invalid.")
            if not await session.feedback.mark_applied(
                project_id=request.project_id,
                feedback_id=request.feedback_id,
                command_id=envelope.command_id,
                applied_at_ms=timestamp,
            ):
                raise CommandPreconditionError("Feedback application changed concurrently.")
            run = await session.runs.get_open_for_project(request.project_id)
            if run is not None and run.status == "waiting_for_user":
                if not await session.runs.start_waiting_run(
                    project_id=request.project_id,
                    run_id=run.id,
                    expected_lock_version=run.lock_version,
                    now_ms=timestamp,
                ):
                    raise CommandPreconditionError("Feedback could not wake the waiting Run.")
            result = ApplyFeedbackResult(
                project_id=request.project_id,
                feedback_id=request.feedback_id,
                route_layer=cast(FeedbackLayer, route_layer),
                target_id=target_id,
                workspace_lock_version=lock_version,
            )
            return CommandEffect(
                result=result,
                events=(
                    EventDraft(
                        event_type="feedback.applied",
                        aggregate_type="feedback",
                        aggregate_id=request.feedback_id,
                        payload={
                            "route_layer": route_layer,
                            "target_id": target_id,
                            "workspace_lock_version": lock_version,
                        },
                    ),
                ),
            )

        return await self._command_bus.execute(
            envelope=envelope,
            result_type=ApplyFeedbackResult,
            handler=handler,
        )

    async def dismiss(
        self,
        request: DismissFeedbackRequest,
        *,
        idempotency_key: str,
    ) -> CommandExecution[DismissFeedbackResult]:
        timestamp = self._now_ms()
        envelope = self._envelope(
            request=request,
            project_id=request.project_id,
            idempotency_key=idempotency_key,
            command_kind="dismiss_feedback",
            actor="user",
            timestamp=timestamp,
        )

        async def handler(session: StoreSession) -> CommandEffect[DismissFeedbackResult]:
            if not await session.feedback.dismiss(
                project_id=request.project_id,
                feedback_id=request.feedback_id,
                dismissed_at_ms=timestamp,
            ):
                raise CommandPreconditionError("Feedback is not dismissible.")
            result = DismissFeedbackResult(
                project_id=request.project_id,
                feedback_id=request.feedback_id,
            )
            return CommandEffect(
                result=result,
                events=(
                    EventDraft(
                        event_type="feedback.dismissed",
                        aggregate_type="feedback",
                        aggregate_id=request.feedback_id,
                        payload={"reason": request.reason},
                    ),
                ),
            )

        return await self._command_bus.execute(
            envelope=envelope,
            result_type=DismissFeedbackResult,
            handler=handler,
        )

    @staticmethod
    async def _activate_book(
        session: StoreSession,
        *,
        feedback: FeedbackRecord,
        expected_lock_version: int,
        timestamp: int,
        canon_baseline_id: str,
    ) -> tuple[str, int]:
        assert feedback.book_id is not None
        book = await session.books.get_for_project(feedback.project_id)
        workspace = await session.books.get_workspace(
            project_id=feedback.project_id,
            book_id=feedback.book_id,
        )
        if (
            book is None
            or book.id != feedback.book_id
            or book.current_baseline_id is None
            or book.current_completion_id is not None
            or workspace is None
            or workspace.lock_version != expected_lock_version
        ):
            raise CommandPreconditionError("Book feedback target is stale.")
        pending = await session.books.find_pending_submission(
            project_id=feedback.project_id,
            book_id=feedback.book_id,
        )
        if pending is not None and not await session.books.close_submission(
            project_id=feedback.project_id,
            submission_id=pending.id,
            disposition="superseded",
            reason_code="feedback_applied",
            closed_at_ms=timestamp,
        ):
            raise CommandPreconditionError("Book feedback could not supersede review.")
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
            guidance_ref_id=feedback.content_ref_id,
            semantic_repair_count=0,
            stale_reason_code=None,
            stale_at_ms=None,
            updated_at_ms=timestamp,
        )
        if not await session.books.compare_and_set_workspace(
            record=updated,
            expected_lock_version=expected_lock_version,
        ):
            raise CommandPreconditionError("Book feedback workspace CAS failed.")
        return book.id, updated.lock_version

    @staticmethod
    async def _activate_arc(
        session: StoreSession,
        *,
        feedback: FeedbackRecord,
        expected_lock_version: int,
        timestamp: int,
        canon_baseline_id: str,
    ) -> tuple[str, int]:
        assert feedback.book_id is not None and feedback.arc_id is not None
        book = await session.books.get_for_project(feedback.project_id)
        arc = await session.arcs.get(project_id=feedback.project_id, arc_id=feedback.arc_id)
        workspace = await session.arcs.get_workspace(
            project_id=feedback.project_id,
            arc_id=feedback.arc_id,
        )
        if (
            book is None
            or book.id != feedback.book_id
            or book.current_baseline_id is None
            or book.current_completion_id is not None
            or arc is None
            or arc.book_id != feedback.book_id
            or workspace is None
            or workspace.lock_version != expected_lock_version
        ):
            raise CommandPreconditionError("Arc feedback target is stale.")
        gate = await session.arcs.find_pending_gate(
            project_id=feedback.project_id,
            arc_id=feedback.arc_id,
        )
        if gate is not None and not await session.arcs.close_approval_gate(
            project_id=feedback.project_id,
            gate_id=gate.id,
            state="superseded",
            closed_at_ms=timestamp,
        ):
            raise CommandPreconditionError("Arc feedback could not supersede its gate.")
        pending = await session.arcs.find_pending_submission(
            project_id=feedback.project_id,
            arc_id=feedback.arc_id,
        )
        if pending is not None and not await session.arcs.close_submission(
            project_id=feedback.project_id,
            submission_id=pending.id,
            disposition="superseded",
            reason_code="feedback_applied",
            closed_at_ms=timestamp,
        ):
            raise CommandPreconditionError("Arc feedback could not supersede review.")
        updated = replace(
            workspace,
            state="active",
            lock_version=workspace.lock_version + 1,
            base_arc_baseline_id=arc.current_baseline_id,
            book_baseline_id=book.current_baseline_id,
            canon_baseline_id=canon_baseline_id,
            plan_ref_id=(
                None if arc.current_baseline_id is None else workspace.plan_ref_id
            ),
            recommended_target_chapter_count=(
                None
                if arc.current_baseline_id is None
                else workspace.recommended_target_chapter_count
            ),
            guidance_ref_id=feedback.content_ref_id,
            semantic_repair_count=0,
            stale_reason_code=None,
            stale_at_ms=None,
            updated_at_ms=timestamp,
        )
        if not await session.arcs.compare_and_set_workspace(
            record=updated,
            expected_lock_version=expected_lock_version,
        ):
            raise CommandPreconditionError("Arc feedback workspace CAS failed.")
        return arc.id, updated.lock_version

    @staticmethod
    async def _activate_chapter(
        session: StoreSession,
        *,
        feedback: FeedbackRecord,
        expected_lock_version: int,
        timestamp: int,
        canon_baseline_id: str,
    ) -> tuple[str, int]:
        assert (
            feedback.book_id is not None
            and feedback.arc_id is not None
            and feedback.chapter_id is not None
        )
        book = await session.books.get_for_project(feedback.project_id)
        arc = await session.arcs.get(project_id=feedback.project_id, arc_id=feedback.arc_id)
        chapter = await session.chapters.get(
            project_id=feedback.project_id,
            chapter_id=feedback.chapter_id,
        )
        workspace = await session.chapters.get_workspace(
            project_id=feedback.project_id,
            chapter_id=feedback.chapter_id,
        )
        if (
            book is None
            or book.id != feedback.book_id
            or book.current_baseline_id is None
            or book.current_completion_id is not None
            or arc is None
            or arc.book_id != feedback.book_id
            or arc.current_baseline_id is None
            or chapter is None
            or chapter.book_id != feedback.book_id
            or chapter.arc_id != feedback.arc_id
            or workspace is None
            or workspace.lock_version != expected_lock_version
        ):
            raise CommandPreconditionError("Chapter feedback target is stale.")
        pending = await session.chapters.find_pending_submission(
            project_id=feedback.project_id,
            chapter_id=feedback.chapter_id,
        )
        if pending is not None and not await session.chapters.close_submission(
            project_id=feedback.project_id,
            submission_id=pending.id,
            disposition="superseded",
            reason_code="feedback_applied",
            closed_at_ms=timestamp,
        ):
            raise CommandPreconditionError("Chapter feedback could not supersede review.")
        updated = replace(
            workspace,
            state="active",
            lock_version=workspace.lock_version + 1,
            base_chapter_baseline_id=chapter.current_baseline_id,
            book_baseline_id=book.current_baseline_id,
            arc_baseline_id=arc.current_baseline_id,
            canon_baseline_id=canon_baseline_id,
            plan_ref_id=(
                None if chapter.current_baseline_id is None else workspace.plan_ref_id
            ),
            draft_ref_id=(
                None if chapter.current_baseline_id is None else workspace.draft_ref_id
            ),
            observations_ref_id=(
                None
                if chapter.current_baseline_id is None
                else workspace.observations_ref_id
            ),
            candidate_canon_patch_ref_id=(
                None
                if chapter.current_baseline_id is None
                else workspace.candidate_canon_patch_ref_id
            ),
            guidance_ref_id=feedback.content_ref_id,
            semantic_repair_count=0,
            stale_reason_code=None,
            stale_at_ms=None,
            updated_at_ms=timestamp,
        )
        if not await session.chapters.compare_and_set_workspace(
            record=updated,
            expected_lock_version=expected_lock_version,
        ):
            raise CommandPreconditionError("Chapter feedback workspace CAS failed.")
        return chapter.id, updated.lock_version
