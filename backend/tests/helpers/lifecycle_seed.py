from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncEngine

from app.agents.contracts import ArcPlanProposal
from app.db.schema import agent_task_attempts, agent_tasks
from app.domain.arc.commands import ArcCommandService
from app.domain.arc.contracts import (
    ApplyArcTaskRequest,
    ArcEvaluation,
    CommitArcAutoRequest,
    CreateStoryArcRequest,
    RecordArcReviewRequest,
    SubmitArcRequest,
)
from app.domain.book.commands import BookCommandService
from app.domain.book.contracts import (
    ApplyBookCandidateRequest,
    ApproveBookRequest,
    BookCandidatePack,
    BookEvaluation,
    CompletionContract,
    RecordBookReviewRequest,
    SubmitBookRequest,
)
from app.domain.projects import CreateProjectRequest, ProjectCommandService
from app.runtime.control import RunControlRequest, RunControlService
from app.store.command_bus import CommandBus
from app.store.content import ContentRepository, prepare_canonical_json


@dataclass(frozen=True, slots=True)
class ApprovedFoundation:
    project_id: str
    run_id: str
    book_id: str
    book_baseline_id: str
    arc_id: str
    arc_baseline_id: str
    canon_baseline_id: str
    target_chapter_count: int


async def insert_successful_task(
    engine: AsyncEngine,
    *,
    project_id: str,
    run_id: str,
    task_id: str,
    attempt_id: str,
    role: str,
    task_kind: str,
    scope_layer: str,
    book_id: str,
    canon_baseline_id: str,
    result: BaseModel,
    workspace_lock_version: int | None = None,
    book_baseline_id: str | None = None,
    arc_id: str | None = None,
    arc_baseline_id: str | None = None,
    chapter_id: str | None = None,
    chapter_baseline_id: str | None = None,
    output_mode: str = "native_json_schema",
) -> tuple[str, str]:
    prepared = prepare_canonical_json(result)
    async with engine.begin() as connection:
        result_ref = await ContentRepository(connection).put(
            project_id=project_id,
            prepared=prepared,
            semantic_kind="agent.typed_result",
            media_type="application/json",
            schema_id=f"{task_kind}-result",
            schema_version=1,
            created_at_ms=20,
        )
        await connection.execute(
            agent_tasks.insert().values(
                id=task_id,
                project_id=project_id,
                run_id=run_id,
                task_key=f"{task_kind}:{task_id}",
                action_key=task_kind,
                role=role,
                task_kind=task_kind,
                scope_layer=scope_layer,
                book_id=book_id,
                arc_id=arc_id,
                chapter_id=chapter_id,
                workspace_lock_version=workspace_lock_version,
                book_baseline_id=book_baseline_id,
                arc_baseline_id=arc_baseline_id,
                chapter_baseline_id=chapter_baseline_id,
                canon_baseline_id=canon_baseline_id,
                task_plan_ref_id=result_ref.id,
                input_manifest_ref_id=result_ref.id,
                input_messages_ref_id=result_ref.id,
                profile_snapshot_ref_id=result_ref.id,
                input_fingerprint=prepared.sha256,
                prompt_fingerprint=prepared.sha256,
                context_policy_id=f"{task_kind}-context-v1",
                context_policy_version=1,
                context_policy_fingerprint=prepared.sha256,
                output_schema_id=f"{task_kind}-result",
                output_schema_version=1,
                output_schema_fingerprint=prepared.sha256,
                rubric_id=(f"{scope_layer}-rubric" if role == "evaluator" else None),
                rubric_version=(1 if role == "evaluator" else None),
                harness_policy_id="novelpilot-domain-harness",
                harness_policy_version=1,
                profile_id="fixture-profile",
                profile_fingerprint=prepared.sha256,
                api_family="openai_responses",
                model_id="fixture-model",
                output_mode=output_mode,
                requires_native_json_schema=int(output_mode == "native_json_schema"),
                requires_text_streaming=int(output_mode == "text_streaming"),
                transport_retry_limit=5,
                model_request_limit=2 if output_mode == "native_json_schema" else 1,
                connect_timeout_ms=10_000,
                pool_timeout_ms=10_000,
                write_timeout_ms=60_000,
                read_timeout_ms=600_000,
                activation_timeout_ms=1_800_000,
                timeout_policy_id="provider-timeout-t1-v1",
                status="succeeded",
                successful_attempt_id=attempt_id,
                delivery_state="pending",
                created_at_ms=20,
                updated_at_ms=20,
            )
        )
        await connection.execute(
            agent_task_attempts.insert().values(
                id=attempt_id,
                project_id=project_id,
                task_id=task_id,
                attempt_number=1,
                retry_kind="initial",
                status="succeeded",
                framework_fingerprint=prepared.sha256,
                provider_request_count=1,
                transport_retry_count=0,
                model_request_count=1,
                input_tokens=10,
                output_tokens=5,
                total_tokens=15,
                result_ref_id=result_ref.id,
                created_at_ms=20,
                started_at_ms=20,
                finished_at_ms=21,
            )
        )
    return task_id, attempt_id


async def seed_approved_book_and_arc(
    engine: AsyncEngine,
    *,
    project_id: str = "project-a",
    target_chapter_count: int = 2,
) -> ApprovedFoundation:
    bus = CommandBus(engine)
    project = await ProjectCommandService(bus).create_project(
        CreateProjectRequest(
            project_id=project_id,
            creator_brief="A mystery about memories that rewrite their witnesses.",
            operation_mode="full_auto",
        ),
        idempotency_key=f"{project_id}:create",
    )
    await RunControlService(bus, now_ms=lambda: 11).start(
        RunControlRequest(
            project_id=project_id,
            run_id=project.result.generation_run_id,
            expected_lock_version=1,
        ),
        idempotency_key=f"{project_id}:run-start",
    )
    book_service = BookCommandService(bus)
    candidate = await book_service.apply_candidate(
        ApplyBookCandidateRequest(
            project_id=project_id,
            book_id=project.result.book_id,
            expected_workspace_lock_version=1,
            candidate=BookCandidatePack(
                direction="Conflicting testimony reveals that memory can be edited.",
                constraints={"pov": "limited-third", "planning": "rolling-arcs"},
                selected_title="Echo Testimony",
                rolling_plan={"strategy": "one-arc-at-a-time"},
                completion_contract=CompletionContract(
                    minimum_chapter_count=1,
                    maximum_chapter_count=10,
                    completion_requirements=["Resolve the central memory conflict"],
                ),
            ),
        ),
        idempotency_key=f"{project_id}:book-candidate",
    )
    submitted = await book_service.submit_for_review(
        SubmitBookRequest(
            project_id=project_id,
            book_id=project.result.book_id,
            expected_workspace_lock_version=candidate.result.workspace_lock_version,
        ),
        idempotency_key=f"{project_id}:book-submit",
    )
    book_task_id, book_attempt_id = await insert_successful_task(
        engine,
        project_id=project_id,
        run_id=project.result.generation_run_id,
        task_id=f"{project_id}:evaluate-book",
        attempt_id=f"{project_id}:evaluate-book:attempt",
        role="evaluator",
        task_kind="evaluate.book",
        scope_layer="book",
        book_id=project.result.book_id,
        canon_baseline_id=project.result.canon_baseline_id,
        workspace_lock_version=candidate.result.workspace_lock_version,
        result=BookEvaluation(
            decision="pass",
            summary="The direction and completion contract are coherent.",
        ),
    )
    reviewed = await book_service.record_review(
        RecordBookReviewRequest(
            project_id=project_id,
            book_id=project.result.book_id,
            submission_id=submitted.result.submission_id,
            evaluator_task_id=book_task_id,
            evaluator_attempt_id=book_attempt_id,
            rubric_id="book-rubric",
            rubric_version=1,
            deterministic_precheck={"passed": True},
        ),
        idempotency_key=f"{project_id}:book-review",
    )
    approved = await book_service.approve_and_commit(
        ApproveBookRequest(
            project_id=project_id,
            book_id=project.result.book_id,
            submission_id=submitted.result.submission_id,
            review_id=reviewed.result.review_id,
        ),
        idempotency_key=f"{project_id}:book-approve",
    )

    arc_service = ArcCommandService(bus)
    created_arc = await arc_service.create_story_arc(
        CreateStoryArcRequest(
            project_id=project_id,
            book_id=project.result.book_id,
            expected_book_baseline_id=approved.result.baseline_id,
            expected_canon_baseline_id=project.result.canon_baseline_id,
            purpose="regular",
        ),
        idempotency_key=f"{project_id}:create-arc",
    )
    arc_id = created_arc.result.arc_id
    planner_task_id, planner_attempt_id = await insert_successful_task(
        engine,
        project_id=project_id,
        run_id=project.result.generation_run_id,
        task_id=f"{arc_id}:plan",
        attempt_id=f"{arc_id}:plan:attempt",
        role="arc_planner",
        task_kind="arc.plan",
        scope_layer="arc",
        book_id=project.result.book_id,
        book_baseline_id=approved.result.baseline_id,
        arc_id=arc_id,
        canon_baseline_id=project.result.canon_baseline_id,
        workspace_lock_version=created_arc.result.workspace_lock_version,
        result=ArcPlanProposal(
            title="The First Contradiction",
            purpose="Expose the memory edit mechanism.",
            beats=["Witnesses disagree", "The discrepancy leaves physical evidence"],
            target_chapter_count=target_chapter_count,
            completion_signals=["The source of the first edit is identified"],
        ),
    )
    applied = await arc_service.apply_task_result(
        ApplyArcTaskRequest(
            project_id=project_id,
            book_id=project.result.book_id,
            arc_id=arc_id,
            task_id=planner_task_id,
            attempt_id=planner_attempt_id,
            expected_workspace_lock_version=created_arc.result.workspace_lock_version,
        ),
        idempotency_key=f"{project_id}:apply-arc-plan",
    )
    submitted_arc = await arc_service.submit_for_review(
        SubmitArcRequest(
            project_id=project_id,
            book_id=project.result.book_id,
            arc_id=arc_id,
            expected_workspace_lock_version=applied.result.workspace_lock_version,
        ),
        idempotency_key=f"{project_id}:submit-arc",
    )
    evaluator_task_id, evaluator_attempt_id = await insert_successful_task(
        engine,
        project_id=project_id,
        run_id=project.result.generation_run_id,
        task_id=f"{arc_id}:evaluate",
        attempt_id=f"{arc_id}:evaluate:attempt",
        role="evaluator",
        task_kind="evaluate.arc",
        scope_layer="arc",
        book_id=project.result.book_id,
        book_baseline_id=approved.result.baseline_id,
        arc_id=arc_id,
        canon_baseline_id=project.result.canon_baseline_id,
        workspace_lock_version=applied.result.workspace_lock_version,
        result=ArcEvaluation(
            decision="pass",
            summary="The rolling Arc plan fits the approved Book contract.",
        ),
    )
    reviewed_arc = await arc_service.record_review(
        RecordArcReviewRequest(
            project_id=project_id,
            book_id=project.result.book_id,
            arc_id=arc_id,
            submission_id=submitted_arc.result.submission_id,
            evaluator_task_id=evaluator_task_id,
            evaluator_attempt_id=evaluator_attempt_id,
            rubric_id="arc-rubric",
            rubric_version=1,
            deterministic_precheck={"passed": True},
        ),
        idempotency_key=f"{project_id}:review-arc",
    )
    committed_arc = await arc_service.commit_baseline_auto(
        CommitArcAutoRequest(
            project_id=project_id,
            book_id=project.result.book_id,
            arc_id=arc_id,
            submission_id=submitted_arc.result.submission_id,
            review_id=reviewed_arc.result.review_id,
        ),
        idempotency_key=f"{project_id}:commit-arc",
    )
    return ApprovedFoundation(
        project_id=project_id,
        run_id=project.result.generation_run_id,
        book_id=project.result.book_id,
        book_baseline_id=approved.result.baseline_id,
        arc_id=arc_id,
        arc_baseline_id=committed_arc.result.baseline_id,
        canon_baseline_id=project.result.canon_baseline_id,
        target_chapter_count=target_chapter_count,
    )
