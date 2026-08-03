"""Synthetic setup helpers for narrow Domain/DB tests.

These helpers insert completed Agent evidence and therefore must never be imported by
project-level acceptance scenarios. Real acceptance owns all state through the public API.
"""

from __future__ import annotations

from dataclasses import dataclass
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.agents.contracts import (
    ArcChapterOutlineEntry,
    ArcClosureSignal,
    ArcPlanProposal,
    ArcStateTransition,
)
from app.agents.registry import DEFAULT_EVALUATION_STRATEGY_REGISTRY
from app.db.schema import (
    agent_task_attempts,
    agent_tasks,
    arc_workspaces,
    book_workspaces,
    chapter_workspaces,
)
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
    BookArcContract,
    BookArcTopology,
    BookCandidatePack,
    BookCompletionRequirement,
    BookCreativeConstraints,
    BookEvaluation,
    BookRollingPlan,
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
    workspace_work_cycle_id: str | None = None,
    book_baseline_id: str | None = None,
    arc_id: str | None = None,
    arc_baseline_id: str | None = None,
    chapter_id: str | None = None,
    chapter_baseline_id: str | None = None,
    subject_arc_baseline_id: str | None = None,
    correction_lineage_id: str | None = None,
    correction_lineage_origin: str | None = None,
    automatic_correction_round: int | None = None,
    source_arc_parent_review_id: str | None = None,
    source_book_parent_review_id: str | None = None,
    source_arc_closure_review_id: str | None = None,
    source_book_completion_review_id: str | None = None,
    source_book_candidate_review_id: str | None = None,
    source_arc_candidate_review_id: str | None = None,
    source_chapter_candidate_review_id: str | None = None,
    source_book_progress_handoff_id: str | None = None,
    source_chapter_arc_request_id: str | None = None,
    source_arc_book_request_id: str | None = None,
    source_arc_closure_id: str | None = None,
    source_feedback_id: str | None = None,
    output_mode: str = "native_json_schema",
) -> tuple[str, str]:
    prepared = prepare_canonical_json(result)
    evaluation_strategy = (
        DEFAULT_EVALUATION_STRATEGY_REGISTRY.for_task(task_kind)
        if role == "evaluator"
        else None
    )
    async with engine.begin() as connection:
        if workspace_lock_version is not None:
            if chapter_id is not None:
                workspace_table = chapter_workspaces
                scope_column = chapter_workspaces.c.chapter_id
                scope_value = chapter_id
            elif arc_id is not None:
                workspace_table = arc_workspaces
                scope_column = arc_workspaces.c.arc_id
                scope_value = arc_id
            else:
                workspace_table = book_workspaces
                scope_column = book_workspaces.c.book_id
                scope_value = book_id
            workspace_identity = (
                await connection.execute(
                    select(
                        workspace_table.c.work_cycle_id,
                        workspace_table.c.active_repair_review_id,
                    ).where(
                        workspace_table.c.project_id == project_id,
                        scope_column == scope_value,
                    )
                )
            ).one_or_none()
            if workspace_identity is not None:
                if workspace_work_cycle_id is None:
                    workspace_work_cycle_id = workspace_identity.work_cycle_id
                if (
                    "repair" in task_kind
                    and source_book_candidate_review_id is None
                    and source_arc_candidate_review_id is None
                    and source_chapter_candidate_review_id is None
                ):
                    if scope_layer == "book":
                        source_book_candidate_review_id = (
                            workspace_identity.active_repair_review_id
                        )
                    elif scope_layer == "arc":
                        source_arc_candidate_review_id = (
                            workspace_identity.active_repair_review_id
                        )
                    else:
                        source_chapter_candidate_review_id = (
                            workspace_identity.active_repair_review_id
                        )
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
                workspace_work_cycle_id=workspace_work_cycle_id,
                book_baseline_id=book_baseline_id,
                arc_baseline_id=arc_baseline_id,
                chapter_baseline_id=chapter_baseline_id,
                subject_arc_baseline_id=subject_arc_baseline_id,
                canon_baseline_id=canon_baseline_id,
                correction_lineage_id=correction_lineage_id,
                correction_lineage_origin=correction_lineage_origin,
                automatic_correction_round=automatic_correction_round,
                source_arc_parent_review_id=source_arc_parent_review_id,
                source_book_parent_review_id=source_book_parent_review_id,
                source_arc_closure_review_id=source_arc_closure_review_id,
                source_book_completion_review_id=(
                    source_book_completion_review_id
                ),
                source_book_candidate_review_id=source_book_candidate_review_id,
                source_arc_candidate_review_id=source_arc_candidate_review_id,
                source_chapter_candidate_review_id=(
                    source_chapter_candidate_review_id
                ),
                source_book_progress_handoff_id=(
                    source_book_progress_handoff_id
                ),
                source_chapter_arc_request_id=source_chapter_arc_request_id,
                source_arc_book_request_id=source_arc_book_request_id,
                source_arc_closure_id=source_arc_closure_id,
                source_feedback_id=source_feedback_id,
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
                evaluation_strategy_id=(
                    None
                    if evaluation_strategy is None
                    else evaluation_strategy.strategy_id
                ),
                evaluation_strategy_version=(
                    None
                    if evaluation_strategy is None
                    else evaluation_strategy.strategy_version
                ),
                rubric_id=(
                    None if evaluation_strategy is None else evaluation_strategy.rubric_id
                ),
                rubric_version=(
                    None
                    if evaluation_strategy is None
                    else evaluation_strategy.rubric_version
                ),
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
    arc_contract_count: int = 1,
) -> ApprovedFoundation:
    if arc_contract_count < 1:
        raise ValueError("arc_contract_count must be positive.")
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
                constraints=BookCreativeConstraints(
                    genre_reader_promise="A fair-play memory mystery.",
                    premise_story_engine="Physical evidence contradicts rewritten memory.",
                    stable_world_invariants=["Physical evidence cannot be memory-edited."],
                    stable_character_invariants=["The investigator pursues verifiable truth."],
                    core_selling_points=["Each contradiction can be investigated."],
                    prohibited_outcomes=["Committed facts cannot be dismissed as a dream."],
                ),
                selected_title="Echo Testimony",
                rolling_plan=BookRollingPlan(
                    long_term_character_directions=["Trust evidence over memory."],
                    whole_book_pacing_strategy="Escalate through bounded rolling Arcs.",
                    ending_tendency="The investigator chooses truth at personal cost.",
                    arc_planning_guidelines=["Each Arc must close observable evidence."],
                    whole_book_scale_guidance=(
                        f"Around {target_chapter_count} Chapters is a creator "
                        "preference, not a completion gate."
                    ),
                ),
                completion_contract=CompletionContract(
                    completion_requirements=[
                        BookCompletionRequirement(
                            requirement_key="memory_conflict_resolved",
                            description="Resolve the central memory conflict.",
                            evidence_expectation="A final committed Chapter proves the resolution.",
                        )
                    ],
                ),
                arc_topology=BookArcTopology(
                    arcs=[
                        BookArcContract(
                            whole_book_role=(
                                "Develop the central memory mystery."
                                if ordinal < arc_contract_count
                                else "Resolve the central memory mystery."
                            ),
                            core_goal=(
                                f"Advance evidence stage {ordinal} through "
                                "physical investigation."
                            ),
                            handoff_from_previous=(
                                "Open from the creator-approved mystery premise."
                                if ordinal == 1
                                else f"Receive the formal closure of Arc {ordinal - 1}."
                            ),
                            exit_conditions=[
                                f"Evidence stage {ordinal} is resolved.",
                                (
                                    "The central memory conflict is resolved."
                                    if ordinal == arc_contract_count
                                    else "A stable handoff to the next Arc exists."
                                ),
                            ],
                            completion_requirement_keys=(
                                ["memory_conflict_resolved"]
                                if ordinal == arc_contract_count
                                else []
                            ),
                            is_final=ordinal == arc_contract_count,
                        )
                        for ordinal in range(1, arc_contract_count + 1)
                    ]
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
            requirement_coverage=[
                {
                    "requirement_key": "memory_conflict_resolved",
                    "judgment": "aligned",
                    "rationale": "The final planned Arc resolves this requirement.",
                }
            ],
        ),
    )
    reviewed = await book_service.record_review(
        RecordBookReviewRequest(
            project_id=project_id,
            book_id=project.result.book_id,
            submission_id=submitted.result.submission_id,
            evaluator_task_id=book_task_id,
            evaluator_attempt_id=book_attempt_id,
            rubric_id=DEFAULT_EVALUATION_STRATEGY_REGISTRY.for_task(
                "evaluate.book"
            ).rubric_id,
            rubric_version=DEFAULT_EVALUATION_STRATEGY_REGISTRY.for_task(
                "evaluate.book"
            ).rubric_version,
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
            expected_ordinal=1,
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
            desired_state_transition=ArcStateTransition(
                start_state="The first memory contradiction is unexplained.",
                end_state="The first edit source is identified with physical evidence.",
            ),
            conflict_trajectory=["Witnesses disagree", "Physical evidence survives"],
            pacing_trajectory=["Establish contradiction", "Test it", "Close the stage"],
            character_obligations=["The investigator changes one belief about memory."],
            foreshadowing_obligations=["Leave one clue for the next Arc."],
            prohibitions=["Do not contradict committed Canon."],
            closure_signals=[
                ArcClosureSignal(
                    signal_key="first_edit_identified",
                    description="The source of the first edit is identified.",
                    evidence_expectation="Committed Chapter observations identify it.",
                )
            ],
            chapter_outline=[
                ArcChapterOutlineEntry(
                    title=f"Chapter {index + 1}",
                    core_event=(
                        "Witnesses disagree"
                        if index == 0
                        else (
                            "The discrepancy leaves physical evidence "
                            f"at assignment {index + 1}"
                        )
                    ),
                    hook=(
                        "The surviving evidence demands another test."
                        if index + 1 < target_chapter_count
                        else "The stage is ready for semantic closure review."
                    ),
                    scenes=[
                        "Investigate the current contradiction.",
                        "Commit one verifiable consequence.",
                    ],
                )
                for index in range(target_chapter_count)
            ],
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
            guidance_authority_judgment="not_present",
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
            rubric_id=DEFAULT_EVALUATION_STRATEGY_REGISTRY.for_task(
                "evaluate.arc"
            ).rubric_id,
            rubric_version=DEFAULT_EVALUATION_STRATEGY_REGISTRY.for_task(
                "evaluate.arc"
            ).rubric_version,
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
