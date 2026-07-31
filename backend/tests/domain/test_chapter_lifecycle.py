from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
import pytest
from alembic import command
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from app.agents.contracts import (
    ChapterCanonRepair,
    ChapterDraftResult,
    ChapterEvaluationIssue,
    ChapterObservationRepairPatch,
    ChapterObservationResult,
    ChapterObservationsRepair,
    ChapterPlanProposal,
    LayerEvaluationResult,
    SemanticCanonProposal,
)
from app.agents.registry import DEFAULT_EVALUATION_STRATEGY_REGISTRY
from app.db.engine import create_sqlite_async_engine
from app.db.maintenance import alembic_config
from app.db.schema import (
    agent_tasks,
    canon_baselines,
    chapter_baselines,
    chapter_arc_change_requests,
    chapter_reviews,
    chapter_review_submissions,
    chapter_workspaces,
    chapters,
    content_refs,
    projects,
    story_arcs,
    generation_runs,
)
from app.domain.chapter.commands import ChapterCommandService
from app.domain.chapter.canon import CanonCategory, CanonEntry
from app.domain.chapter.contracts import (
    ApplyChapterTaskRequest,
    CommittedChapterObservation,
    CommitChapterRequest,
    CreateChapterRequest,
    RecordChapterReviewRequest,
    SubmitChapterRequest,
)
from app.domain.chapter.queries import ChapterQueryService
from app.domain.commands import CommandPreconditionError
from app.store.canon import CanonRepository
from app.store.command_bus import CommandBus
from app.store.content import ContentRepository
from tests.helpers.lifecycle_seed import (
    ApprovedFoundation,
    insert_successful_task,
    seed_approved_book_and_arc,
)


@dataclass(frozen=True, slots=True)
class ReviewedChapter:
    foundation: ApprovedFoundation
    chapter_id: str
    submission_id: str
    review_id: str
    workspace_lock_version: int


async def _prepare_reviewed_chapter(
    engine: AsyncEngine,
    *,
    project_id: str,
    target_chapter_count: int,
    canon_change: bool,
    evaluation: LayerEvaluationResult | None = None,
    arc_contract_count: int = 1,
    foundation: ApprovedFoundation | None = None,
    idempotency_suffix: str = "",
    canon_evidence_hint: str = "The blue ink changed while she watched",
    canon_category: CanonCategory = "characters",
    canon_proposals: list[SemanticCanonProposal] | None = None,
) -> ReviewedChapter:
    if foundation is None:
        foundation = await seed_approved_book_and_arc(
            engine,
            project_id=project_id,
            target_chapter_count=target_chapter_count,
            arc_contract_count=arc_contract_count,
        )
    elif foundation.project_id != project_id:
        raise ValueError("The supplied Chapter foundation belongs to another project.")
    service = ChapterCommandService(CommandBus(engine))
    created = await service.create_chapter(
        CreateChapterRequest(
            project_id=project_id,
            book_id=foundation.book_id,
            arc_id=foundation.arc_id,
            expected_book_baseline_id=foundation.book_baseline_id,
            expected_arc_baseline_id=foundation.arc_baseline_id,
            expected_canon_baseline_id=foundation.canon_baseline_id,
        ),
        idempotency_key=f"{project_id}:chapter-create{idempotency_suffix}",
    )
    chapter_id = created.result.chapter_id
    plan_task, plan_attempt = await insert_successful_task(
        engine,
        project_id=project_id,
        run_id=foundation.run_id,
        task_id=f"{chapter_id}:plan",
        attempt_id=f"{chapter_id}:plan:attempt",
        role="chapter_writer",
        task_kind="chapter.plan",
        scope_layer="chapter",
        book_id=foundation.book_id,
        book_baseline_id=foundation.book_baseline_id,
        arc_id=foundation.arc_id,
        arc_baseline_id=foundation.arc_baseline_id,
        chapter_id=chapter_id,
        chapter_baseline_id=None,
        canon_baseline_id=foundation.canon_baseline_id,
        workspace_lock_version=1,
        result=ChapterPlanProposal(
            title="The Witness Who Remembered Twice",
            purpose="Reveal the first physical trace of memory editing.",
            scene_beats=["Mara compares two incompatible statements", "The ink changes"],
            required_continuity=["Mara distrusts her own notes"],
        ),
    )
    applied_plan = await service.apply_plan_result(
        ApplyChapterTaskRequest(
            project_id=project_id,
            chapter_id=chapter_id,
            task_id=plan_task,
            attempt_id=plan_attempt,
            expected_workspace_lock_version=1,
        ),
        idempotency_key=f"{chapter_id}:apply-plan",
    )
    draft_task, draft_attempt = await insert_successful_task(
        engine,
        project_id=project_id,
        run_id=foundation.run_id,
        task_id=f"{chapter_id}:draft",
        attempt_id=f"{chapter_id}:draft:attempt",
        role="chapter_writer",
        task_kind="chapter.draft",
        scope_layer="chapter",
        book_id=foundation.book_id,
        book_baseline_id=foundation.book_baseline_id,
        arc_id=foundation.arc_id,
        arc_baseline_id=foundation.arc_baseline_id,
        chapter_id=chapter_id,
        chapter_baseline_id=None,
        canon_baseline_id=foundation.canon_baseline_id,
        workspace_lock_version=applied_plan.result.workspace_lock_version,
        output_mode="text_streaming",
        result=ChapterDraftResult(
            prose=(
                "Mara laid the two statements side by side. "
                "The blue ink changed while she watched, adding a confession she had never heard."
            )
        ),
    )
    applied_draft = await service.apply_draft_result(
        ApplyChapterTaskRequest(
            project_id=project_id,
            chapter_id=chapter_id,
            task_id=draft_task,
            attempt_id=draft_attempt,
            expected_workspace_lock_version=applied_plan.result.workspace_lock_version,
        ),
        idempotency_key=f"{chapter_id}:apply-draft",
    )
    proposals = (
        canon_proposals
        if canon_proposals is not None
        else (
            [
                SemanticCanonProposal(
                    category=canon_category,
                    subject="Mara",
                    semantic_change=(
                        "Mara directly witnesses written memory evidence changing."
                    ),
                    resolved=False,
                    evidence_hint=canon_evidence_hint,
                )
            ]
            if canon_change
            else []
        )
    )
    observation_task, observation_attempt = await insert_successful_task(
        engine,
        project_id=project_id,
        run_id=foundation.run_id,
        task_id=f"{chapter_id}:observe",
        attempt_id=f"{chapter_id}:observe:attempt",
        role="chapter_writer",
        task_kind="chapter.observe",
        scope_layer="chapter",
        book_id=foundation.book_id,
        book_baseline_id=foundation.book_baseline_id,
        arc_id=foundation.arc_id,
        arc_baseline_id=foundation.arc_baseline_id,
        chapter_id=chapter_id,
        chapter_baseline_id=None,
        canon_baseline_id=foundation.canon_baseline_id,
        workspace_lock_version=applied_draft.result.workspace_lock_version,
        result=ChapterObservationResult(
            summary="Mara obtains physical evidence that memory edits affect documents.",
            established_facts=[
                {
                    "statement": "Mara now has a reason to preserve analogue copies.",
                    "evidence_hint": (
                        "The Chapter shows the physical evidence motivating that choice."
                    ),
                }
            ],
            canon_proposals=proposals,
        ),
    )
    applied_observation = await service.apply_observation_result(
        ApplyChapterTaskRequest(
            project_id=project_id,
            chapter_id=chapter_id,
            task_id=observation_task,
            attempt_id=observation_attempt,
            expected_workspace_lock_version=applied_draft.result.workspace_lock_version,
        ),
        idempotency_key=f"{chapter_id}:apply-observation",
    )
    submitted = await service.submit_for_review(
        SubmitChapterRequest(
            project_id=project_id,
            chapter_id=chapter_id,
            expected_workspace_lock_version=applied_observation.result.workspace_lock_version,
        ),
        idempotency_key=f"{chapter_id}:submit",
    )
    evaluator_task_kind = "evaluate.chapter"
    evaluator_task, evaluator_attempt = await insert_successful_task(
        engine,
        project_id=project_id,
        run_id=foundation.run_id,
        task_id=f"{chapter_id}:evaluate",
        attempt_id=f"{chapter_id}:evaluate:attempt",
        role="evaluator",
        task_kind=evaluator_task_kind,
        scope_layer="chapter",
        book_id=foundation.book_id,
        book_baseline_id=foundation.book_baseline_id,
        arc_id=foundation.arc_id,
        arc_baseline_id=foundation.arc_baseline_id,
        chapter_id=chapter_id,
        chapter_baseline_id=None,
        canon_baseline_id=foundation.canon_baseline_id,
        workspace_lock_version=applied_observation.result.workspace_lock_version,
        result=(
            evaluation
            or LayerEvaluationResult(
                guidance_authority_judgment="not_present",
                decision="pass",
                summary=(
                    "The Chapter is coherent and the evidence span supports the Canon proposal."
                ),
            )
        ),
    )
    reviewed = await service.record_review(
        RecordChapterReviewRequest(
            project_id=project_id,
            chapter_id=chapter_id,
            submission_id=submitted.result.submission_id,
            evaluator_task_id=evaluator_task,
            evaluator_attempt_id=evaluator_attempt,
            rubric_id=DEFAULT_EVALUATION_STRATEGY_REGISTRY.for_task(
                evaluator_task_kind
            ).rubric_id,
            rubric_version=DEFAULT_EVALUATION_STRATEGY_REGISTRY.for_task(
                evaluator_task_kind
            ).rubric_version,
        ),
        idempotency_key=f"{chapter_id}:review",
    )
    async with engine.connect() as connection:
        final_workspace_lock = await connection.scalar(
            select(chapter_workspaces.c.lock_version).where(
                chapter_workspaces.c.chapter_id == chapter_id
            )
        )
    assert final_workspace_lock is not None
    return ReviewedChapter(
        foundation=foundation,
        chapter_id=chapter_id,
        submission_id=submitted.result.submission_id,
        review_id=reviewed.result.review_id,
        workspace_lock_version=final_workspace_lock,
    )


def test_real_precheck_routes_conflicting_canon_assertions_to_repair(
    tmp_path: Path,
) -> None:
    database = tmp_path / "chapter-real-precheck.sqlite3"
    command.upgrade(alembic_config(database), "head")

    async def exercise() -> None:
        engine = create_sqlite_async_engine(database)
        try:
            first = SemanticCanonProposal(
                category="world_facts",
                subject="The analogue record",
                semantic_change="The record supports the witness.",
                resolved=False,
                evidence_hint="The analogue record agrees with the witness.",
            )
            conflicting = first.model_copy(
                update={
                    "semantic_change": "The record disproves the witness.",
                    "evidence_hint": "The analogue record contradicts the witness.",
                }
            )
            ready = await _prepare_reviewed_chapter(
                engine,
                project_id="project-real-precheck",
                target_chapter_count=2,
                canon_change=False,
                canon_proposals=[first, conflicting],
            )

            async with engine.connect() as connection:
                review = (
                    await connection.execute(
                        select(
                            chapter_reviews.c.decision,
                            chapter_reviews.c.precheck_ref_id,
                            chapter_reviews.c.detail_ref_id,
                            chapter_reviews.c.repair_contract_ref_id,
                        ).where(chapter_reviews.c.id == ready.review_id)
                    )
                ).one()
                observations_ref_id = await connection.scalar(
                    select(chapter_review_submissions.c.observations_ref_id).where(
                        chapter_review_submissions.c.id == ready.submission_id
                    )
                )
                assert observations_ref_id is not None
                content_versions = {
                    row.semantic_kind: row.schema_version
                    for row in (
                        await connection.execute(
                            select(
                                content_refs.c.semantic_kind,
                                content_refs.c.schema_version,
                            ).where(
                                content_refs.c.id.in_(
                                    (
                                        review.precheck_ref_id,
                                        review.detail_ref_id,
                                        review.repair_contract_ref_id,
                                        observations_ref_id,
                                    )
                                )
                            )
                        )
                    )
                }
                workspace_state = await connection.scalar(
                    select(chapter_workspaces.c.state).where(
                        chapter_workspaces.c.chapter_id == ready.chapter_id
                    )
                )
                run_status = await connection.scalar(
                    select(generation_runs.c.status).where(
                        generation_runs.c.id == ready.foundation.run_id
                    )
                )
                content = ContentRepository(connection)
                precheck = json.loads(
                    (
                        await content.get_packed(
                            project_id=ready.foundation.project_id,
                            ref_id=review.precheck_ref_id,
                        )
                    ).unpack_and_verify()
                )
                assert review.repair_contract_ref_id is not None
                repair = json.loads(
                    (
                        await content.get_packed(
                            project_id=ready.foundation.project_id,
                            ref_id=review.repair_contract_ref_id,
                        )
                    ).unpack_and_verify()
                )

            assert review.decision == "local_repair"
            assert precheck["passed"] is False
            assert precheck["checks"]["canon_patch_applicable"] is False
            assert precheck["issues"][0]["code"] == "canon_subject_assertion_conflict"
            assert repair["authorized_components"] == ["canon"]
            assert repair["repair_stage"] == "primary_semantic"
            assert repair["schema"] == "chapter-repair-contract-v5"
            assert content_versions == {
                "chapter.deterministic_precheck": 4,
                "chapter.review_detail": 5,
                "chapter.repair_contract": 5,
                "chapter.observations": 3,
            }
            assert workspace_state == "active"
            assert run_status == "running"
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_plan_repair_replaces_mutable_plan_and_invalidates_downstream(
    tmp_path: Path,
) -> None:
    database = tmp_path / "chapter-plan-repair.sqlite3"
    command.upgrade(alembic_config(database), "head")

    async def exercise() -> None:
        engine = create_sqlite_async_engine(database)
        try:
            ready = await _prepare_reviewed_chapter(
                engine,
                project_id="project-plan-repair",
                target_chapter_count=2,
                canon_change=False,
                evaluation=LayerEvaluationResult(
                    guidance_authority_judgment="not_present",
                    decision="local_repair",
                    summary=(
                        "The mutable Chapter plan overclaims evidence but can be "
                        "replaced under the same Arc."
                    ),
                    issues=[
                        ChapterEvaluationIssue(
                            kind="unsupported_strong_conclusion",
                            code="chapter_plan_infeasible",
                            subject="mutable Chapter plan",
                            summary="The plan overclaims evidence available under this Arc.",
                            evidence=[
                                "The plan reaches a categorical conclusion from limited evidence."
                            ],
                            support_gap=(
                                "The plan does not schedule observable evidence strong "
                                "enough for its required conclusion."
                            ),
                            affected_components=["plan"],
                        )
                    ],
                ),
            )
            replacement = ChapterPlanProposal(
                title="A Signal Without a Name",
                purpose=(
                    "Preserve the Arc evidence boundary while advancing the investigation."
                ),
                scene_beats=[
                    "The witness records only the light that was actually observed.",
                    "Investigators separate identity from timing.",
                ],
                required_continuity=[
                    "No current physical evidence identifies the signaler or exact time."
                ],
            )
            task_id, attempt_id = await insert_successful_task(
                engine,
                project_id=ready.foundation.project_id,
                run_id=ready.foundation.run_id,
                task_id=f"{ready.chapter_id}:repair-plan",
                attempt_id=f"{ready.chapter_id}:repair-plan:attempt",
                role="chapter_writer",
                task_kind="chapter.repair.plan",
                scope_layer="chapter",
                book_id=ready.foundation.book_id,
                book_baseline_id=ready.foundation.book_baseline_id,
                arc_id=ready.foundation.arc_id,
                arc_baseline_id=ready.foundation.arc_baseline_id,
                chapter_id=ready.chapter_id,
                canon_baseline_id=ready.foundation.canon_baseline_id,
                workspace_lock_version=ready.workspace_lock_version,
                result=replacement,
            )
            applied = await ChapterCommandService(CommandBus(engine)).apply_repair_result(
                ApplyChapterTaskRequest(
                    project_id=ready.foundation.project_id,
                    chapter_id=ready.chapter_id,
                    task_id=task_id,
                    attempt_id=attempt_id,
                    expected_workspace_lock_version=ready.workspace_lock_version,
                ),
                idempotency_key=f"{ready.chapter_id}:apply-plan-repair",
            )

            assert applied.result.component == "repair_plan"
            async with engine.connect() as connection:
                workspace = (
                    await connection.execute(
                        select(
                            chapter_workspaces.c.plan_ref_id,
                            chapter_workspaces.c.draft_ref_id,
                            chapter_workspaces.c.observations_ref_id,
                            chapter_workspaces.c.candidate_canon_patch_ref_id,
                            chapter_workspaces.c.semantic_repair_count,
                        ).where(chapter_workspaces.c.chapter_id == ready.chapter_id)
                    )
                ).one()
                assert workspace.plan_ref_id is not None
                stored_plan = ChapterPlanProposal.model_validate_json(
                    (
                        await ContentRepository(connection).get_packed(
                            project_id=ready.foundation.project_id,
                            ref_id=workspace.plan_ref_id,
                        )
                    ).unpack_and_verify()
                )

            assert stored_plan == replacement
            assert workspace.draft_ref_id is None
            assert workspace.observations_ref_id is None
            assert workspace.candidate_canon_patch_ref_id is None
            assert workspace.semantic_repair_count == 1
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_chapter_and_changed_canon_commit_atomically_and_open_arc_closure(
    tmp_path: Path,
) -> None:
    database = tmp_path / "chapter-canon.sqlite3"
    command.upgrade(alembic_config(database), "head")

    async def exercise() -> None:
        engine = create_sqlite_async_engine(database)
        try:
            ready = await _prepare_reviewed_chapter(
                engine,
                project_id="project-changed",
                target_chapter_count=1,
                canon_change=True,
            )
            service = ChapterCommandService(CommandBus(engine))
            request = CommitChapterRequest(
                project_id=ready.foundation.project_id,
                chapter_id=ready.chapter_id,
                submission_id=ready.submission_id,
                review_id=ready.review_id,
                expected_canon_baseline_id=ready.foundation.canon_baseline_id,
            )
            committed = await service.commit_chapter_and_canon(
                request,
                idempotency_key=f"{ready.chapter_id}:commit",
            )
            replayed = await service.commit_chapter_and_canon(
                request,
                idempotency_key=f"{ready.chapter_id}:commit",
            )
            assert replayed.replayed
            assert replayed.result == committed.result
            assert committed.result.canon_changed
            assert committed.result.canon_after_id != committed.result.canon_before_id
            assert committed.result.arc_closure_due

            async with engine.connect() as connection:
                chapter = (
                    await connection.execute(
                        select(
                            chapters.c.lifecycle_status,
                            chapters.c.current_baseline_id,
                        ).where(chapters.c.id == ready.chapter_id)
                    )
                ).one()
                arc_status = await connection.scalar(
                    select(story_arcs.c.lifecycle_status).where(
                        story_arcs.c.id == ready.foundation.arc_id
                    )
                )
                current_canon = await connection.scalar(
                    select(projects.c.current_canon_baseline_id).where(
                        projects.c.id == ready.foundation.project_id
                    )
                )
                assert (
                    await connection.scalar(
                        select(func.count()).select_from(chapter_baselines)
                    )
                    == 1
                )
                assert (
                    await connection.scalar(select(func.count()).select_from(canon_baselines))
                    == 2
                )
                assert (
                    await connection.scalar(
                        select(chapter_review_submissions.c.disposition).where(
                            chapter_review_submissions.c.id == ready.submission_id
                        )
                    )
                    == "promoted"
                )
            assert tuple(chapter) == (
                "committed",
                committed.result.chapter_baseline_id,
            )
            assert arc_status == "closing"
            assert current_canon == committed.result.canon_after_id
            text = await ChapterQueryService(engine).get_current_text(
                project_id=ready.foundation.project_id,
                chapter_id=ready.chapter_id,
            )
            assert text.chapter_title == "The Witness Who Remembered Twice"
            assert "The blue ink changed" in text.prose
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_paraphrased_canon_evidence_commits_without_exact_copy(
    tmp_path: Path,
) -> None:
    database = tmp_path / "chapter-semantic-evidence.sqlite3"
    command.upgrade(alembic_config(database), "head")

    async def exercise() -> None:
        engine = create_sqlite_async_engine(database)
        try:
            semantic_hint = (
                "Mara sees documentary evidence rewrite itself in real time."
            )
            ready = await _prepare_reviewed_chapter(
                engine,
                project_id="project-semantic-evidence",
                target_chapter_count=1,
                canon_change=True,
                canon_evidence_hint=semantic_hint,
                canon_category="world_facts",
            )
            committed = await ChapterCommandService(
                CommandBus(engine)
            ).commit_chapter_and_canon(
                CommitChapterRequest(
                    project_id=ready.foundation.project_id,
                    chapter_id=ready.chapter_id,
                    submission_id=ready.submission_id,
                    review_id=ready.review_id,
                    expected_canon_baseline_id=ready.foundation.canon_baseline_id,
                ),
                idempotency_key=f"{ready.chapter_id}:commit-semantic-evidence",
            )
            assert committed.result.canon_changed

            async with engine.connect() as connection:
                world_facts_ref_id = await connection.scalar(
                    select(canon_baselines.c.world_facts_ref_id).where(
                        canon_baselines.c.id == committed.result.canon_after_id
                    )
                )
                assert world_facts_ref_id is not None
                schema = (
                    await connection.execute(
                        select(
                            content_refs.c.schema_id,
                            content_refs.c.schema_version,
                        ).where(content_refs.c.id == world_facts_ref_id)
                    )
                ).one()
                packed = await ContentRepository(connection).get_packed(
                    project_id=ready.foundation.project_id,
                    ref_id=world_facts_ref_id,
                )
                entries = [
                    CanonEntry.model_validate(item)
                    for item in json.loads(packed.unpack_and_verify())
                ]
                source_refs = (
                    await connection.execute(
                        select(
                            chapter_baselines.c.observations_ref_id,
                            chapter_baselines.c.prose_ref_id,
                            chapter_review_submissions.c.observations_ref_id.label(
                                "candidate_observations_ref_id"
                            ),
                        )
                        .select_from(
                            chapter_baselines.join(
                                chapter_review_submissions,
                                chapter_review_submissions.c.id
                                == chapter_baselines.c.submission_id,
                            )
                        )
                        .where(
                            chapter_baselines.c.id
                            == committed.result.chapter_baseline_id
                        )
                    )
                ).one()
                committed_observations_packed = await ContentRepository(
                    connection
                ).get_packed(
                    project_id=ready.foundation.project_id,
                    ref_id=source_refs.observations_ref_id,
                )
                committed_observations = (
                    CommittedChapterObservation.model_validate_json(
                        committed_observations_packed.unpack_and_verify()
                    )
                )

            assert tuple(schema) == ("canon-world-facts", 3)
            assert len(entries) == 1
            assert entries[0].evidence.hint == semantic_hint
            assert entries[0].evidence.exact_span is None
            assert entries[0].source_chapter_baseline_id == (
                committed.result.chapter_baseline_id
            )
            assert entries[0].source_prose_ref_id == source_refs.prose_ref_id
            assert source_refs.observations_ref_id != (
                source_refs.candidate_observations_ref_id
            )
            assert (
                committed_observations.source.chapter_baseline_id
                == committed.result.chapter_baseline_id
            )
            assert (
                committed_observations.source.prose_ref_id
                == source_refs.prose_ref_id
            )
            assert [
                fact.fact_ordinal
                for fact in committed_observations.established_facts
            ] == [1]
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_noop_canon_patch_reuses_current_pointer(tmp_path: Path) -> None:
    database = tmp_path / "chapter-noop.sqlite3"
    command.upgrade(alembic_config(database), "head")

    async def exercise() -> None:
        engine = create_sqlite_async_engine(database)
        try:
            ready = await _prepare_reviewed_chapter(
                engine,
                project_id="project-noop",
                target_chapter_count=2,
                canon_change=False,
            )
            committed = await ChapterCommandService(CommandBus(engine)).commit_chapter_and_canon(
                CommitChapterRequest(
                    project_id=ready.foundation.project_id,
                    chapter_id=ready.chapter_id,
                    submission_id=ready.submission_id,
                    review_id=ready.review_id,
                    expected_canon_baseline_id=ready.foundation.canon_baseline_id,
                ),
                idempotency_key=f"{ready.chapter_id}:commit",
            )
            assert not committed.result.canon_changed
            assert committed.result.canon_before_id == committed.result.canon_after_id
            assert not committed.result.arc_closure_due
            async with engine.connect() as connection:
                assert (
                    await connection.scalar(select(func.count()).select_from(canon_baselines))
                    == 1
                )
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_stale_chapter_task_is_discarded_without_overwriting_workspace(
    tmp_path: Path,
) -> None:
    database = tmp_path / "chapter-stale.sqlite3"
    command.upgrade(alembic_config(database), "head")

    async def exercise() -> None:
        engine = create_sqlite_async_engine(database)
        try:
            foundation = await seed_approved_book_and_arc(
                engine,
                project_id="project-stale",
            )
            service = ChapterCommandService(CommandBus(engine))
            created = await service.create_chapter(
                CreateChapterRequest(
                    project_id=foundation.project_id,
                    book_id=foundation.book_id,
                    arc_id=foundation.arc_id,
                    expected_book_baseline_id=foundation.book_baseline_id,
                    expected_arc_baseline_id=foundation.arc_baseline_id,
                    expected_canon_baseline_id=foundation.canon_baseline_id,
                ),
                idempotency_key="create-chapter",
            )
            chapter_id = created.result.chapter_id
            tasks: list[tuple[str, str]] = []
            for suffix, title in (("new", "Current Plan"), ("old", "Stale Plan")):
                tasks.append(
                    await insert_successful_task(
                        engine,
                        project_id=foundation.project_id,
                        run_id=foundation.run_id,
                        task_id=f"{chapter_id}:plan:{suffix}",
                        attempt_id=f"{chapter_id}:plan:{suffix}:attempt",
                        role="chapter_writer",
                        task_kind="chapter.plan",
                        scope_layer="chapter",
                        book_id=foundation.book_id,
                        book_baseline_id=foundation.book_baseline_id,
                        arc_id=foundation.arc_id,
                        arc_baseline_id=foundation.arc_baseline_id,
                        chapter_id=chapter_id,
                        canon_baseline_id=foundation.canon_baseline_id,
                        workspace_lock_version=1,
                        result=ChapterPlanProposal(
                            title=title,
                            purpose="Test stale delivery.",
                            scene_beats=["One beat"],
                        ),
                    )
                )
            await service.apply_plan_result(
                ApplyChapterTaskRequest(
                    project_id=foundation.project_id,
                    chapter_id=chapter_id,
                    task_id=tasks[0][0],
                    attempt_id=tasks[0][1],
                    expected_workspace_lock_version=1,
                ),
                idempotency_key="apply-current",
            )
            stale = await service.apply_plan_result(
                ApplyChapterTaskRequest(
                    project_id=foundation.project_id,
                    chapter_id=chapter_id,
                    task_id=tasks[1][0],
                    attempt_id=tasks[1][1],
                    expected_workspace_lock_version=1,
                ),
                idempotency_key="apply-stale",
            )
            assert stale.result.delivery == "discarded_stale"
            assert stale.result.workspace_lock_version == 2
            async with engine.connect() as connection:
                assert (
                    await connection.scalar(
                        select(agent_tasks.c.delivery_state).where(
                            agent_tasks.c.id == tasks[1][0]
                        )
                    )
                    == "discarded_stale"
                )
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_local_repair_changes_only_authorized_component_and_consumes_one_budget(
    tmp_path: Path,
) -> None:
    database = tmp_path / "chapter-repair.sqlite3"
    command.upgrade(alembic_config(database), "head")

    async def exercise() -> None:
        engine = create_sqlite_async_engine(database)
        try:
            ready = await _prepare_reviewed_chapter(
                engine,
                project_id="project-repair",
                target_chapter_count=2,
                canon_change=False,
                evaluation=LayerEvaluationResult(
                    guidance_authority_judgment="not_present",
                    decision="local_repair",
                    summary="One paragraph overstates what Mara can know.",
                    issues=[
                        ChapterEvaluationIssue(
                            kind="unsupported_strong_conclusion",
                            code="prose_knowledge_overclaim",
                            subject="Mara's knowledge",
                            summary="The prose overstates what Mara can know.",
                            evidence=[
                                "The prose states certainty while the scene establishes only suspicion."
                            ],
                            support_gap=(
                                "No observable evidence supports converting uncertainty "
                                "into knowledge."
                            ),
                            affected_components=["prose"],
                        )
                    ],
                ),
            )
            task_id, attempt_id = await insert_successful_task(
                engine,
                project_id=ready.foundation.project_id,
                run_id=ready.foundation.run_id,
                task_id=f"{ready.chapter_id}:repair-prose",
                attempt_id=f"{ready.chapter_id}:repair-prose:attempt",
                role="chapter_writer",
                task_kind="chapter.repair.prose",
                scope_layer="chapter",
                book_id=ready.foundation.book_id,
                book_baseline_id=ready.foundation.book_baseline_id,
                arc_id=ready.foundation.arc_id,
                arc_baseline_id=ready.foundation.arc_baseline_id,
                chapter_id=ready.chapter_id,
                canon_baseline_id=ready.foundation.canon_baseline_id,
                workspace_lock_version=ready.workspace_lock_version,
                source_chapter_candidate_review_id=ready.review_id,
                output_mode="text_streaming",
                result=ChapterDraftResult(
                    prose="Mara compared the statements and documented only what she directly observed."
                ),
            )
            applied = await ChapterCommandService(CommandBus(engine)).apply_repair_result(
                ApplyChapterTaskRequest(
                    project_id=ready.foundation.project_id,
                    chapter_id=ready.chapter_id,
                    task_id=task_id,
                    attempt_id=attempt_id,
                    expected_workspace_lock_version=ready.workspace_lock_version,
                ),
                idempotency_key=f"{ready.chapter_id}:apply-repair",
            )
            assert applied.result.component == "repair_prose"
            assert applied.result.delivery == "applied"
            async with engine.connect() as connection:
                workspace = (
                    await connection.execute(
                        select(
                            chapter_workspaces.c.semantic_repair_count,
                            chapter_workspaces.c.draft_ref_id,
                            chapter_workspaces.c.observations_ref_id,
                            chapter_workspaces.c.candidate_canon_patch_ref_id,
                        ).where(chapter_workspaces.c.chapter_id == ready.chapter_id)
                    )
                ).one()
            assert workspace.semantic_repair_count == 1
            assert workspace.draft_ref_id is not None
            assert workspace.observations_ref_id is None
            assert workspace.candidate_canon_patch_ref_id is None
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_observation_repair_patch_preserves_unauthorized_canon_component(
    tmp_path: Path,
) -> None:
    database = tmp_path / "chapter-observation-repair.sqlite3"
    command.upgrade(alembic_config(database), "head")

    async def exercise() -> None:
        engine = create_sqlite_async_engine(database)
        try:
            ready = await _prepare_reviewed_chapter(
                engine,
                project_id="project-observation-repair",
                target_chapter_count=2,
                canon_change=True,
                evaluation=LayerEvaluationResult(
                    guidance_authority_judgment="not_present",
                    decision="local_repair",
                    summary="The summary and continuity observation need clarification.",
                    issues=[
                        ChapterEvaluationIssue(
                            kind="derived_evidence_mismatch",
                            code="observation_prose_mismatch",
                            subject="Chapter observations",
                            summary="The observation states more than the frozen prose establishes.",
                            evidence=[
                                "The candidate observation and frozen prose make opposite claims."
                            ],
                            candidate_claim=(
                                "The observation says Mara has proved who altered the record."
                            ),
                            contrary_formal_statement=(
                                "The frozen prose states that Mara cannot yet identify the actor."
                            ),
                            affected_components=["observations"],
                        )
                    ],
                ),
            )
            service = ChapterCommandService(CommandBus(engine))
            unauthorized = ChapterObservationRepairPatch(
                changes=[
                    ChapterCanonRepair(
                        component="canon",
                        canon_proposals=[],
                    )
                ]
            )
            unauthorized_task, unauthorized_attempt = await insert_successful_task(
                engine,
                project_id=ready.foundation.project_id,
                run_id=ready.foundation.run_id,
                task_id=f"{ready.chapter_id}:repair-unauthorized-canon",
                attempt_id=f"{ready.chapter_id}:repair-unauthorized-canon:attempt",
                role="chapter_writer",
                task_kind="chapter.repair.observation",
                scope_layer="chapter",
                book_id=ready.foundation.book_id,
                book_baseline_id=ready.foundation.book_baseline_id,
                arc_id=ready.foundation.arc_id,
                arc_baseline_id=ready.foundation.arc_baseline_id,
                chapter_id=ready.chapter_id,
                canon_baseline_id=ready.foundation.canon_baseline_id,
                workspace_lock_version=ready.workspace_lock_version,
                result=unauthorized,
            )
            with pytest.raises(CommandPreconditionError, match="unauthorized components"):
                await service.apply_repair_result(
                    ApplyChapterTaskRequest(
                        project_id=ready.foundation.project_id,
                        chapter_id=ready.chapter_id,
                        task_id=unauthorized_task,
                        attempt_id=unauthorized_attempt,
                        expected_workspace_lock_version=ready.workspace_lock_version,
                    ),
                    idempotency_key=f"{ready.chapter_id}:reject-unauthorized-canon",
                )

            no_op = ChapterObservationRepairPatch(
                changes=[
                    ChapterObservationsRepair(
                        component="observations",
                        summary=(
                            "Mara obtains physical evidence that memory edits affect documents."
                        ),
                        established_facts=[
                            {
                                "statement": (
                                    "Mara now has a reason to preserve analogue copies."
                                ),
                                "evidence_hint": (
                                    "The Chapter shows the physical evidence "
                                    "motivating that choice."
                                ),
                            }
                        ],
                    )
                ]
            )
            no_op_task, no_op_attempt = await insert_successful_task(
                engine,
                project_id=ready.foundation.project_id,
                run_id=ready.foundation.run_id,
                task_id=f"{ready.chapter_id}:repair-observations-no-op",
                attempt_id=f"{ready.chapter_id}:repair-observations-no-op:attempt",
                role="chapter_writer",
                task_kind="chapter.repair.observation",
                scope_layer="chapter",
                book_id=ready.foundation.book_id,
                book_baseline_id=ready.foundation.book_baseline_id,
                arc_id=ready.foundation.arc_id,
                arc_baseline_id=ready.foundation.arc_baseline_id,
                chapter_id=ready.chapter_id,
                canon_baseline_id=ready.foundation.canon_baseline_id,
                workspace_lock_version=ready.workspace_lock_version,
                result=no_op,
            )
            with pytest.raises(CommandPreconditionError, match="no authorized change"):
                await service.apply_repair_result(
                    ApplyChapterTaskRequest(
                        project_id=ready.foundation.project_id,
                        chapter_id=ready.chapter_id,
                        task_id=no_op_task,
                        attempt_id=no_op_attempt,
                        expected_workspace_lock_version=ready.workspace_lock_version,
                    ),
                    idempotency_key=f"{ready.chapter_id}:reject-observation-no-op",
                )

            result = ChapterObservationRepairPatch(
                changes=[
                    ChapterObservationsRepair(
                        component="observations",
                        summary="Mara directly observes documentary evidence changing.",
                        established_facts=[
                            {
                                "statement": (
                                    "Mara preserves analogue copies before continuing "
                                    "the investigation."
                                ),
                                "evidence_hint": (
                                    "The frozen prose shows Mara preserving the copies."
                                ),
                            }
                        ],
                    )
                ]
            )
            task_id, attempt_id = await insert_successful_task(
                engine,
                project_id=ready.foundation.project_id,
                run_id=ready.foundation.run_id,
                task_id=f"{ready.chapter_id}:repair-observations",
                attempt_id=f"{ready.chapter_id}:repair-observations:attempt",
                role="chapter_writer",
                task_kind="chapter.repair.observation",
                scope_layer="chapter",
                book_id=ready.foundation.book_id,
                book_baseline_id=ready.foundation.book_baseline_id,
                arc_id=ready.foundation.arc_id,
                arc_baseline_id=ready.foundation.arc_baseline_id,
                chapter_id=ready.chapter_id,
                canon_baseline_id=ready.foundation.canon_baseline_id,
                workspace_lock_version=ready.workspace_lock_version,
                result=result,
            )
            applied = await service.apply_repair_result(
                ApplyChapterTaskRequest(
                    project_id=ready.foundation.project_id,
                    chapter_id=ready.chapter_id,
                    task_id=task_id,
                    attempt_id=attempt_id,
                    expected_workspace_lock_version=ready.workspace_lock_version,
                ),
                idempotency_key=f"{ready.chapter_id}:apply-observation-repair",
            )
            assert applied.result.component == "repair_observations"
            async with engine.connect() as connection:
                observations_ref_id = await connection.scalar(
                    select(chapter_workspaces.c.observations_ref_id).where(
                        chapter_workspaces.c.chapter_id == ready.chapter_id
                    )
                )
                assert observations_ref_id is not None
                packed = await ContentRepository(connection).get_packed(
                    project_id=ready.foundation.project_id,
                    ref_id=observations_ref_id,
                )
                observations = ChapterObservationResult.model_validate(
                    json.loads(packed.unpack_and_verify())
                )
            assert observations.summary == result.changes[0].summary
            assert observations.established_facts == (
                result.changes[0].established_facts
            )
            assert len(observations.canon_proposals) == 1
            assert observations.canon_proposals[0].subject == "Mara"
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_canon_repair_patch_preserves_unauthorized_observation_components(
    tmp_path: Path,
) -> None:
    database = tmp_path / "chapter-canon-repair.sqlite3"
    command.upgrade(alembic_config(database), "head")

    async def exercise() -> None:
        engine = create_sqlite_async_engine(database)
        try:
            ready = await _prepare_reviewed_chapter(
                engine,
                project_id="project-canon-repair",
                target_chapter_count=2,
                canon_change=True,
                evaluation=LayerEvaluationResult(
                    guidance_authority_judgment="not_present",
                    decision="local_repair",
                    summary="Only the Canon proposal needs correction.",
                    issues=[
                        ChapterEvaluationIssue(
                            kind="derived_evidence_mismatch",
                            code="canon_assertion_inaccurate",
                            subject="mutable documentary evidence",
                            summary="Only the Canon assertion needs correction.",
                            evidence=[
                                "The candidate Canon assertion contradicts the frozen prose."
                            ],
                            candidate_claim=(
                                "The Canon proposal says the documentary mutation is resolved."
                            ),
                            contrary_formal_statement=(
                                "The frozen prose leaves the mutation mechanism unresolved."
                            ),
                            affected_components=["canon"],
                        )
                    ],
                ),
            )
            replacement = SemanticCanonProposal(
                category="world_facts",
                subject="Mutable documentary evidence",
                semantic_change="Written statements can change while a witness watches.",
                resolved=False,
                evidence_hint="The blue ink changed while she watched",
            )
            result = ChapterObservationRepairPatch(
                changes=[
                    ChapterCanonRepair(
                        component="canon",
                        canon_proposals=[replacement],
                    )
                ]
            )
            task_id, attempt_id = await insert_successful_task(
                engine,
                project_id=ready.foundation.project_id,
                run_id=ready.foundation.run_id,
                task_id=f"{ready.chapter_id}:repair-canon",
                attempt_id=f"{ready.chapter_id}:repair-canon:attempt",
                role="chapter_writer",
                task_kind="chapter.repair.observation",
                scope_layer="chapter",
                book_id=ready.foundation.book_id,
                book_baseline_id=ready.foundation.book_baseline_id,
                arc_id=ready.foundation.arc_id,
                arc_baseline_id=ready.foundation.arc_baseline_id,
                chapter_id=ready.chapter_id,
                canon_baseline_id=ready.foundation.canon_baseline_id,
                workspace_lock_version=ready.workspace_lock_version,
                result=result,
            )
            await ChapterCommandService(CommandBus(engine)).apply_repair_result(
                ApplyChapterTaskRequest(
                    project_id=ready.foundation.project_id,
                    chapter_id=ready.chapter_id,
                    task_id=task_id,
                    attempt_id=attempt_id,
                    expected_workspace_lock_version=ready.workspace_lock_version,
                ),
                idempotency_key=f"{ready.chapter_id}:apply-canon-repair",
            )
            async with engine.connect() as connection:
                observations_ref_id = await connection.scalar(
                    select(chapter_workspaces.c.observations_ref_id).where(
                        chapter_workspaces.c.chapter_id == ready.chapter_id
                    )
                )
                assert observations_ref_id is not None
                packed = await ContentRepository(connection).get_packed(
                    project_id=ready.foundation.project_id,
                    ref_id=observations_ref_id,
                )
                observations = ChapterObservationResult.model_validate_json(
                    packed.unpack_and_verify()
                )
            assert observations.summary == (
                "Mara obtains physical evidence that memory edits affect documents."
            )
            assert [
                fact.statement for fact in observations.established_facts
            ] == ["Mara now has a reason to preserve analogue copies."]
            assert observations.canon_proposals == [replacement]
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_multi_component_repair_union_stalls_when_same_issue_persists(
    tmp_path: Path,
) -> None:
    database = tmp_path / "chapter-repair-stalled.sqlite3"
    command.upgrade(alembic_config(database), "head")

    async def exercise() -> None:
        engine = create_sqlite_async_engine(database)
        try:
            ready = await _prepare_reviewed_chapter(
                engine,
                project_id="project-repair-stalled",
                target_chapter_count=2,
                canon_change=True,
                evaluation=LayerEvaluationResult(
                    guidance_authority_judgment="not_present",
                    decision="local_repair",
                    summary="The observations and Canon assertion disagree.",
                    issues=[
                        ChapterEvaluationIssue(
                            kind="derived_evidence_mismatch",
                            code="documentary_evidence_mismatch",
                            subject="Mara's documentary evidence",
                            summary=(
                                "The observation summary and Canon assertion must be "
                                "corrected together."
                            ),
                            evidence=[
                                "The candidate observation and Canon projection disagree."
                            ],
                            candidate_claim=(
                                "The observation says the evidence remains uncertain."
                            ),
                            contrary_formal_statement=(
                                "The candidate Canon projection states a settled conclusion."
                            ),
                            affected_components=["observations", "canon"],
                        )
                    ],
                ),
            )
            service = ChapterCommandService(CommandBus(engine))
            async with engine.connect() as connection:
                repair_ref_id = await connection.scalar(
                    select(chapter_reviews.c.repair_contract_ref_id).where(
                        chapter_reviews.c.id == ready.review_id
                    )
                )
                assert repair_ref_id is not None
                repair_contract = json.loads(
                    (
                        await ContentRepository(connection).get_packed(
                            project_id=ready.foundation.project_id,
                            ref_id=repair_ref_id,
                        )
                    ).unpack_and_verify()
                )
            assert repair_contract["authorized_components"] == [
                "observations",
                "canon",
            ]

            repair = ChapterObservationRepairPatch(
                changes=[
                    ChapterObservationsRepair(
                        component="observations",
                        summary=(
                            "Mara sees the written confession change but cannot yet "
                            "identify who caused it."
                        ),
                        established_facts=[
                            {
                                "statement": (
                                    "Mara preserves analogue copies for later comparison."
                                ),
                                "evidence_hint": (
                                    "The repaired prose shows Mara preserving the copies."
                                ),
                            }
                        ],
                    ),
                    ChapterCanonRepair(
                        component="canon",
                        canon_proposals=[
                            SemanticCanonProposal(
                                category="world_facts",
                                subject="Mutable documentary evidence",
                                semantic_change=(
                                    "Written evidence can change while an observer watches."
                                ),
                                resolved=False,
                                evidence_hint="The blue ink changed while she watched",
                            )
                        ],
                    ),
                ]
            )
            repair_task, repair_attempt = await insert_successful_task(
                engine,
                project_id=ready.foundation.project_id,
                run_id=ready.foundation.run_id,
                task_id=f"{ready.chapter_id}:repair-observation-and-canon",
                attempt_id=(
                    f"{ready.chapter_id}:repair-observation-and-canon:attempt"
                ),
                role="chapter_writer",
                task_kind="chapter.repair.observation",
                scope_layer="chapter",
                book_id=ready.foundation.book_id,
                book_baseline_id=ready.foundation.book_baseline_id,
                arc_id=ready.foundation.arc_id,
                arc_baseline_id=ready.foundation.arc_baseline_id,
                chapter_id=ready.chapter_id,
                canon_baseline_id=ready.foundation.canon_baseline_id,
                workspace_lock_version=ready.workspace_lock_version,
                result=repair,
            )
            applied = await service.apply_repair_result(
                ApplyChapterTaskRequest(
                    project_id=ready.foundation.project_id,
                    chapter_id=ready.chapter_id,
                    task_id=repair_task,
                    attempt_id=repair_attempt,
                    expected_workspace_lock_version=ready.workspace_lock_version,
                ),
                idempotency_key=f"{ready.chapter_id}:apply-multi-repair",
            )
            submitted = await service.submit_for_review(
                SubmitChapterRequest(
                    project_id=ready.foundation.project_id,
                    chapter_id=ready.chapter_id,
                    expected_workspace_lock_version=(
                        applied.result.workspace_lock_version
                    ),
                ),
                idempotency_key=f"{ready.chapter_id}:resubmit-after-multi-repair",
            )
            verification = LayerEvaluationResult(
                guidance_authority_judgment="not_present",
                decision="local_repair",
                summary="The same documentary-evidence mismatch remains.",
                issues=[
                    ChapterEvaluationIssue(
                        kind="derived_evidence_mismatch",
                        code="documentary_evidence_mismatch",
                        subject="Mara's documentary evidence",
                        summary=(
                            "The observation summary and Canon assertion still do "
                            "not establish the same fact."
                        ),
                        evidence=[
                            "The repaired Canon projection still disagrees with frozen prose."
                        ],
                        candidate_claim=(
                            "The repaired Canon projection states a settled cause."
                        ),
                        contrary_formal_statement=(
                            "The frozen prose leaves the cause unsettled."
                        ),
                        affected_components=["canon"],
                    )
                ],
            )
            verify_task, verify_attempt = await insert_successful_task(
                engine,
                project_id=ready.foundation.project_id,
                run_id=ready.foundation.run_id,
                task_id=f"{ready.chapter_id}:verify-multi-repair",
                attempt_id=f"{ready.chapter_id}:verify-multi-repair:attempt",
                role="evaluator",
                task_kind="verify_repair.chapter",
                scope_layer="chapter",
                book_id=ready.foundation.book_id,
                book_baseline_id=ready.foundation.book_baseline_id,
                arc_id=ready.foundation.arc_id,
                arc_baseline_id=ready.foundation.arc_baseline_id,
                chapter_id=ready.chapter_id,
                canon_baseline_id=ready.foundation.canon_baseline_id,
                workspace_lock_version=applied.result.workspace_lock_version,
                result=verification,
            )
            strategy = DEFAULT_EVALUATION_STRATEGY_REGISTRY.for_task(
                "verify_repair.chapter"
            )
            await service.record_review(
                RecordChapterReviewRequest(
                    project_id=ready.foundation.project_id,
                    chapter_id=ready.chapter_id,
                    submission_id=submitted.result.submission_id,
                    evaluator_task_id=verify_task,
                    evaluator_attempt_id=verify_attempt,
                    rubric_id=strategy.rubric_id,
                    rubric_version=strategy.rubric_version,
                ),
                idempotency_key=f"{ready.chapter_id}:verify-stalled-repair",
            )
            async with engine.connect() as connection:
                run = (
                    await connection.execute(
                        select(
                            generation_runs.c.status,
                            generation_runs.c.failure_code,
                        ).where(generation_runs.c.id == ready.foundation.run_id)
                    )
                ).one()
                repair_tasks = await connection.scalar(
                    select(func.count())
                    .select_from(agent_tasks)
                    .where(
                        agent_tasks.c.project_id == ready.foundation.project_id,
                        agent_tasks.c.task_kind.like("chapter.repair.%"),
                    )
                )
            assert tuple(run) == ("failure_paused", "semantic_repair_stalled")
            assert repair_tasks == 1
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_prose_repair_allows_one_bounded_derived_dependency_closure(
    tmp_path: Path,
) -> None:
    database = tmp_path / "chapter-derived-dependency-closure.sqlite3"
    command.upgrade(alembic_config(database), "head")

    async def exercise() -> None:
        engine = create_sqlite_async_engine(database)
        try:
            ready = await _prepare_reviewed_chapter(
                engine,
                project_id="project-derived-dependency-closure",
                target_chapter_count=2,
                canon_change=True,
                evaluation=LayerEvaluationResult(
                    guidance_authority_judgment="not_present",
                    decision="local_repair",
                    summary="The candidate places the archive device in the wrong room.",
                    issues=[
                        ChapterEvaluationIssue(
                            kind="explicit_conflict",
                            code="archive_device_location",
                            subject="archive scanner location",
                            summary=(
                                "Both prose and Canon place the device outside the "
                                "locked data room required by the frozen assignment."
                            ),
                            evidence=[
                                "The candidate says the device is in the maintenance room."
                            ],
                            candidate_claim=(
                                "The archive scanner stands in the maintenance room."
                            ),
                            contrary_formal_statement=(
                                "The frozen assignment requires the device inside "
                                "the locked data room."
                            ),
                            affected_components=["prose", "canon"],
                        )
                    ],
                ),
            )
            service = ChapterCommandService(CommandBus(engine))
            repaired_prose = (
                "Mara unlocked the data room and crossed to its innermost operation "
                "table. The archive scanner stood there, still sealed inside the "
                "room, while she preserved the original record."
            )
            prose_task, prose_attempt = await insert_successful_task(
                engine,
                project_id=ready.foundation.project_id,
                run_id=ready.foundation.run_id,
                task_id=f"{ready.chapter_id}:repair-prose-for-derived-closure",
                attempt_id=(
                    f"{ready.chapter_id}:repair-prose-for-derived-closure:attempt"
                ),
                role="chapter_writer",
                task_kind="chapter.repair.prose",
                scope_layer="chapter",
                book_id=ready.foundation.book_id,
                book_baseline_id=ready.foundation.book_baseline_id,
                arc_id=ready.foundation.arc_id,
                arc_baseline_id=ready.foundation.arc_baseline_id,
                chapter_id=ready.chapter_id,
                canon_baseline_id=ready.foundation.canon_baseline_id,
                workspace_lock_version=ready.workspace_lock_version,
                output_mode="text_streaming",
                result=ChapterDraftResult(prose=repaired_prose),
            )
            prose_applied = await service.apply_repair_result(
                ApplyChapterTaskRequest(
                    project_id=ready.foundation.project_id,
                    chapter_id=ready.chapter_id,
                    task_id=prose_task,
                    attempt_id=prose_attempt,
                    expected_workspace_lock_version=ready.workspace_lock_version,
                ),
                idempotency_key=f"{ready.chapter_id}:apply-prose-derived-closure",
            )

            regenerated = ChapterObservationResult(
                summary=(
                    "Mara inspects the archive scanner while preserving the original."
                ),
                established_facts=[
                    {
                        "statement": "Mara preserves the original archive record.",
                        "evidence_hint": (
                            "The repaired prose shows her preserving the original."
                        ),
                    }
                ],
                canon_proposals=[
                    SemanticCanonProposal(
                        category="world_facts",
                        subject="Archive scanner location",
                        semantic_change=(
                            "The archive scanner is in the adjacent maintenance room."
                        ),
                        resolved=False,
                        evidence_hint=(
                            "The regenerated observation incorrectly places it outside."
                        ),
                    )
                ],
            )
            observe_task, observe_attempt = await insert_successful_task(
                engine,
                project_id=ready.foundation.project_id,
                run_id=ready.foundation.run_id,
                task_id=f"{ready.chapter_id}:observe-after-prose-repair",
                attempt_id=f"{ready.chapter_id}:observe-after-prose-repair:attempt",
                role="chapter_writer",
                task_kind="chapter.observe",
                scope_layer="chapter",
                book_id=ready.foundation.book_id,
                book_baseline_id=ready.foundation.book_baseline_id,
                arc_id=ready.foundation.arc_id,
                arc_baseline_id=ready.foundation.arc_baseline_id,
                chapter_id=ready.chapter_id,
                canon_baseline_id=ready.foundation.canon_baseline_id,
                workspace_lock_version=(
                    prose_applied.result.workspace_lock_version
                ),
                source_chapter_candidate_review_id=ready.review_id,
                result=regenerated,
            )
            observation_applied = await service.apply_observation_result(
                ApplyChapterTaskRequest(
                    project_id=ready.foundation.project_id,
                    chapter_id=ready.chapter_id,
                    task_id=observe_task,
                    attempt_id=observe_attempt,
                    expected_workspace_lock_version=(
                        prose_applied.result.workspace_lock_version
                    ),
                ),
                idempotency_key=f"{ready.chapter_id}:apply-regenerated-observation",
            )
            submitted = await service.submit_for_review(
                SubmitChapterRequest(
                    project_id=ready.foundation.project_id,
                    chapter_id=ready.chapter_id,
                    expected_workspace_lock_version=(
                        observation_applied.result.workspace_lock_version
                    ),
                ),
                idempotency_key=f"{ready.chapter_id}:submit-derived-mismatch",
            )
            verify_task, verify_attempt = await insert_successful_task(
                engine,
                project_id=ready.foundation.project_id,
                run_id=ready.foundation.run_id,
                task_id=f"{ready.chapter_id}:verify-derived-mismatch",
                attempt_id=f"{ready.chapter_id}:verify-derived-mismatch:attempt",
                role="evaluator",
                task_kind="verify_repair.chapter",
                scope_layer="chapter",
                book_id=ready.foundation.book_id,
                book_baseline_id=ready.foundation.book_baseline_id,
                arc_id=ready.foundation.arc_id,
                arc_baseline_id=ready.foundation.arc_baseline_id,
                chapter_id=ready.chapter_id,
                canon_baseline_id=ready.foundation.canon_baseline_id,
                workspace_lock_version=(
                    observation_applied.result.workspace_lock_version
                ),
                source_chapter_candidate_review_id=ready.review_id,
                result=LayerEvaluationResult(
                    guidance_authority_judgment="not_present",
                    decision="local_repair",
                    summary=(
                        "The repaired prose is correct; only regenerated Canon is wrong."
                    ),
                    issues=[
                        ChapterEvaluationIssue(
                            kind="explicit_conflict",
                            code="archive_device_location",
                            subject="archive scanner location",
                            summary=(
                                "The regenerated Canon contradicts the repaired prose."
                            ),
                            evidence=[
                                "Canon says maintenance room while prose says data room."
                            ],
                            candidate_claim=(
                                "The archive scanner is in the maintenance room."
                            ),
                            contrary_formal_statement=(
                                "The repaired prose places it inside the locked data room."
                            ),
                            affected_components=["canon"],
                            recurrence="persists_after_authorized_repair",
                        )
                    ],
                ),
            )
            strategy = DEFAULT_EVALUATION_STRATEGY_REGISTRY.for_task(
                "verify_repair.chapter"
            )
            closure_review = await service.record_review(
                RecordChapterReviewRequest(
                    project_id=ready.foundation.project_id,
                    chapter_id=ready.chapter_id,
                    submission_id=submitted.result.submission_id,
                    evaluator_task_id=verify_task,
                    evaluator_attempt_id=verify_attempt,
                    rubric_id=strategy.rubric_id,
                    rubric_version=strategy.rubric_version,
                ),
                idempotency_key=f"{ready.chapter_id}:open-derived-closure",
            )
            async with engine.connect() as connection:
                closure_workspace = (
                    await connection.execute(
                        select(
                            chapter_workspaces.c.lock_version,
                            chapter_workspaces.c.semantic_repair_count,
                            chapter_workspaces.c.active_repair_review_id,
                        ).where(
                            chapter_workspaces.c.chapter_id == ready.chapter_id
                        )
                    )
                ).one()
                closure_ref_id = await connection.scalar(
                    select(chapter_reviews.c.repair_contract_ref_id).where(
                        chapter_reviews.c.id == closure_review.result.review_id
                    )
                )
                run_before_closure = (
                    await connection.execute(
                        select(
                            generation_runs.c.status,
                            generation_runs.c.failure_code,
                        ).where(
                            generation_runs.c.id == ready.foundation.run_id
                        )
                    )
                ).one()
                assert closure_ref_id is not None
                closure_contract = json.loads(
                    (
                        await ContentRepository(connection).get_packed(
                            project_id=ready.foundation.project_id,
                            ref_id=closure_ref_id,
                        )
                    ).unpack_and_verify()
                )
            assert tuple(run_before_closure) == ("running", None)
            assert closure_workspace.semantic_repair_count == 1
            assert closure_contract["repair_stage"] == (
                "derived_dependency_closure"
            )
            assert closure_contract["authorized_components"] == ["canon"]

            evidence_task, evidence_attempt = await insert_successful_task(
                engine,
                project_id=ready.foundation.project_id,
                run_id=ready.foundation.run_id,
                task_id=f"{ready.chapter_id}:close-derived-dependency",
                attempt_id=f"{ready.chapter_id}:close-derived-dependency:attempt",
                role="chapter_writer",
                task_kind="chapter.repair.observation",
                scope_layer="chapter",
                book_id=ready.foundation.book_id,
                book_baseline_id=ready.foundation.book_baseline_id,
                arc_id=ready.foundation.arc_id,
                arc_baseline_id=ready.foundation.arc_baseline_id,
                chapter_id=ready.chapter_id,
                canon_baseline_id=ready.foundation.canon_baseline_id,
                workspace_lock_version=closure_workspace.lock_version,
                source_chapter_candidate_review_id=(
                    closure_review.result.review_id
                ),
                result=ChapterObservationRepairPatch(
                    changes=[
                        ChapterCanonRepair(
                            component="canon",
                            canon_proposals=[
                                SemanticCanonProposal(
                                    category="world_facts",
                                    subject="Archive scanner location",
                                    semantic_change=(
                                        "The archive scanner is inside the locked "
                                        "data room on its innermost operation table."
                                    ),
                                    resolved=False,
                                    evidence_hint=(
                                        "The repaired prose places it inside the room."
                                    ),
                                )
                            ],
                        )
                    ]
                ),
            )
            evidence_applied = await service.apply_repair_result(
                ApplyChapterTaskRequest(
                    project_id=ready.foundation.project_id,
                    chapter_id=ready.chapter_id,
                    task_id=evidence_task,
                    attempt_id=evidence_attempt,
                    expected_workspace_lock_version=closure_workspace.lock_version,
                ),
                idempotency_key=f"{ready.chapter_id}:apply-derived-closure",
            )
            async with engine.connect() as connection:
                count_after_closure = await connection.scalar(
                    select(chapter_workspaces.c.semantic_repair_count).where(
                        chapter_workspaces.c.chapter_id == ready.chapter_id
                    )
                )
            assert count_after_closure == 1

            final_submission = await service.submit_for_review(
                SubmitChapterRequest(
                    project_id=ready.foundation.project_id,
                    chapter_id=ready.chapter_id,
                    expected_workspace_lock_version=(
                        evidence_applied.result.workspace_lock_version
                    ),
                ),
                idempotency_key=f"{ready.chapter_id}:submit-closed-dependency",
            )
            final_task, final_attempt = await insert_successful_task(
                engine,
                project_id=ready.foundation.project_id,
                run_id=ready.foundation.run_id,
                task_id=f"{ready.chapter_id}:verify-closed-dependency",
                attempt_id=f"{ready.chapter_id}:verify-closed-dependency:attempt",
                role="evaluator",
                task_kind="verify_repair.chapter",
                scope_layer="chapter",
                book_id=ready.foundation.book_id,
                book_baseline_id=ready.foundation.book_baseline_id,
                arc_id=ready.foundation.arc_id,
                arc_baseline_id=ready.foundation.arc_baseline_id,
                chapter_id=ready.chapter_id,
                canon_baseline_id=ready.foundation.canon_baseline_id,
                workspace_lock_version=(
                    evidence_applied.result.workspace_lock_version
                ),
                source_chapter_candidate_review_id=(
                    closure_review.result.review_id
                ),
                result=LayerEvaluationResult(
                    guidance_authority_judgment="not_present",
                    decision="pass",
                    summary=(
                        "Repaired prose and its derived evidence now agree."
                    ),
                ),
            )
            final_review = await service.record_review(
                RecordChapterReviewRequest(
                    project_id=ready.foundation.project_id,
                    chapter_id=ready.chapter_id,
                    submission_id=final_submission.result.submission_id,
                    evaluator_task_id=final_task,
                    evaluator_attempt_id=final_attempt,
                    rubric_id=strategy.rubric_id,
                    rubric_version=strategy.rubric_version,
                ),
                idempotency_key=f"{ready.chapter_id}:pass-derived-closure",
            )
            committed = await service.commit_chapter_and_canon(
                CommitChapterRequest(
                    project_id=ready.foundation.project_id,
                    chapter_id=ready.chapter_id,
                    submission_id=final_submission.result.submission_id,
                    review_id=final_review.result.review_id,
                    expected_canon_baseline_id=(
                        ready.foundation.canon_baseline_id
                    ),
                ),
                idempotency_key=f"{ready.chapter_id}:commit-derived-closure",
            )
            async with engine.connect() as connection:
                baseline_prose_ref = await connection.scalar(
                    select(chapter_baselines.c.prose_ref_id).where(
                        chapter_baselines.c.id
                        == committed.result.chapter_baseline_id
                    )
                )
                workspace_prose_ref = await connection.scalar(
                    select(chapter_workspaces.c.draft_ref_id).where(
                        chapter_workspaces.c.chapter_id == ready.chapter_id
                    )
                )
            assert baseline_prose_ref == workspace_prose_ref
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_second_distinct_semantic_repair_is_not_started_and_run_failure_pauses(
    tmp_path: Path,
) -> None:
    database = tmp_path / "chapter-repair-cap.sqlite3"
    command.upgrade(alembic_config(database), "head")

    async def exercise() -> None:
        engine = create_sqlite_async_engine(database)
        try:
            ready = await _prepare_reviewed_chapter(
                engine,
                project_id="project-repair-cap",
                target_chapter_count=2,
                canon_change=False,
                evaluation=LayerEvaluationResult(
                    guidance_authority_judgment="not_present",
                    decision="local_repair",
                    summary="The observation overstates what Mara knows.",
                    issues=[
                        ChapterEvaluationIssue(
                            kind="unsupported_strong_conclusion",
                            code="knowledge_overclaim",
                            subject="Mara's knowledge",
                            summary="The observation overstates what Mara knows.",
                            evidence=[
                                "The observation states certainty absent from the prose."
                            ],
                            support_gap=(
                                "The frozen prose supports suspicion but not certain knowledge."
                            ),
                            affected_components=["observations"],
                        )
                    ],
                ),
            )
            service = ChapterCommandService(CommandBus(engine))
            repair_task, repair_attempt = await insert_successful_task(
                engine,
                project_id=ready.foundation.project_id,
                run_id=ready.foundation.run_id,
                task_id=f"{ready.chapter_id}:repair-observation-before-cap",
                attempt_id=(
                    f"{ready.chapter_id}:repair-observation-before-cap:attempt"
                ),
                role="chapter_writer",
                task_kind="chapter.repair.observation",
                scope_layer="chapter",
                book_id=ready.foundation.book_id,
                book_baseline_id=ready.foundation.book_baseline_id,
                arc_id=ready.foundation.arc_id,
                arc_baseline_id=ready.foundation.arc_baseline_id,
                chapter_id=ready.chapter_id,
                canon_baseline_id=ready.foundation.canon_baseline_id,
                workspace_lock_version=ready.workspace_lock_version,
                result=ChapterObservationRepairPatch(
                    changes=[
                        ChapterObservationsRepair(
                            component="observations",
                            summary=(
                                "Mara records only that the blue ink changed before her."
                            ),
                            established_facts=[
                                {
                                    "statement": (
                                        "Mara preserves analogue copies for later comparison."
                                    ),
                                    "evidence_hint": (
                                        "The repaired prose shows the preserved copies."
                                    ),
                                }
                            ],
                        )
                    ]
                ),
            )
            applied = await service.apply_repair_result(
                ApplyChapterTaskRequest(
                    project_id=ready.foundation.project_id,
                    chapter_id=ready.chapter_id,
                    task_id=repair_task,
                    attempt_id=repair_attempt,
                    expected_workspace_lock_version=ready.workspace_lock_version,
                ),
                idempotency_key=f"{ready.chapter_id}:apply-repair-before-cap",
            )
            submitted = await service.submit_for_review(
                SubmitChapterRequest(
                    project_id=ready.foundation.project_id,
                    chapter_id=ready.chapter_id,
                    expected_workspace_lock_version=(
                        applied.result.workspace_lock_version
                    ),
                ),
                idempotency_key=f"{ready.chapter_id}:resubmit-at-cap",
            )
            verify_task, verify_attempt = await insert_successful_task(
                engine,
                project_id=ready.foundation.project_id,
                run_id=ready.foundation.run_id,
                task_id=f"{ready.chapter_id}:verify-distinct-issue-at-cap",
                attempt_id=f"{ready.chapter_id}:verify-distinct-issue-at-cap:attempt",
                role="evaluator",
                task_kind="verify_repair.chapter",
                scope_layer="chapter",
                book_id=ready.foundation.book_id,
                book_baseline_id=ready.foundation.book_baseline_id,
                arc_id=ready.foundation.arc_id,
                arc_baseline_id=ready.foundation.arc_baseline_id,
                chapter_id=ready.chapter_id,
                canon_baseline_id=ready.foundation.canon_baseline_id,
                workspace_lock_version=applied.result.workspace_lock_version,
                result=LayerEvaluationResult(
                    guidance_authority_judgment="not_present",
                    decision="local_repair",
                    summary="A distinct continuity issue remains.",
                    issues=[
                        ChapterEvaluationIssue(
                            kind="contract_unfulfilled",
                            code="continuity_gap",
                            subject="Mara's analogue copies",
                            summary=(
                                "The revised prose omits the already-required "
                                "continuity consequence."
                            ),
                            evidence=[
                                "The repaired candidate omits an explicit Chapter continuity obligation."
                            ],
                            contract_item=(
                                "The Chapter must preserve Mara's already-established "
                                "analogue-copy consequence."
                            ),
                            affected_components=["observations"],
                        )
                    ],
                ),
            )
            strategy = DEFAULT_EVALUATION_STRATEGY_REGISTRY.for_task(
                "verify_repair.chapter"
            )
            await service.record_review(
                RecordChapterReviewRequest(
                    project_id=ready.foundation.project_id,
                    chapter_id=ready.chapter_id,
                    submission_id=submitted.result.submission_id,
                    evaluator_task_id=verify_task,
                    evaluator_attempt_id=verify_attempt,
                    rubric_id=strategy.rubric_id,
                    rubric_version=strategy.rubric_version,
                ),
                idempotency_key=f"{ready.chapter_id}:review-distinct-issue-at-cap",
            )
            async with engine.connect() as connection:
                run = (
                    await connection.execute(
                        select(
                            generation_runs.c.status,
                            generation_runs.c.failure_code,
                        ).where(generation_runs.c.id == ready.foundation.run_id)
                    )
                ).one()
                repair_tasks = await connection.scalar(
                    select(func.count())
                    .select_from(agent_tasks)
                    .where(
                        agent_tasks.c.project_id == ready.foundation.project_id,
                        agent_tasks.c.task_kind.like("chapter.repair.%"),
                    )
                )
            assert tuple(run) == ("failure_paused", "semantic_repair_exhausted")
            assert repair_tasks == 1
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_chapter_escalation_opens_explicit_arc_request_and_blocks_workspace(
    tmp_path: Path,
) -> None:
    database = tmp_path / "chapter-escalation.sqlite3"
    command.upgrade(alembic_config(database), "head")

    async def exercise() -> None:
        engine = create_sqlite_async_engine(database)
        try:
            ready = await _prepare_reviewed_chapter(
                engine,
                project_id="project-escalation",
                target_chapter_count=2,
                canon_change=False,
                evaluation=LayerEvaluationResult(
                    guidance_authority_judgment="not_present",
                    decision="escalate_to_arc",
                    summary="The approved Arc requires a contradiction this Chapter cannot resolve.",
                    issues=[
                        ChapterEvaluationIssue(
                            kind="parent_authority_concern",
                            code="arc_contract_concern",
                            subject="required Arc contradiction",
                            summary=(
                                "The approved Arc requires a contradiction this Chapter "
                                "cannot resolve."
                            ),
                            evidence=[
                                "The frozen Chapter assignment conflicts with committed facts."
                            ],
                            affected_components=["plan"],
                        )
                    ],
                ),
            )
            async with engine.connect() as connection:
                request = (
                    await connection.execute(
                        select(
                            chapter_arc_change_requests.c.status,
                            chapter_arc_change_requests.c.target_arc_baseline_id,
                        ).where(
                            chapter_arc_change_requests.c.chapter_id == ready.chapter_id
                        )
                    )
                ).one()
                workspace_state = await connection.scalar(
                    select(chapter_workspaces.c.state).where(
                        chapter_workspaces.c.chapter_id == ready.chapter_id
                    )
                )
            assert tuple(request) == ("open", ready.foundation.arc_baseline_id)
            assert workspace_state == "blocked_by_upstream"
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_canon_insert_failure_rolls_back_chapter_baseline_and_pointers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "chapter-rollback.sqlite3"
    command.upgrade(alembic_config(database), "head")

    async def fail_insert(*args: object, **kwargs: object) -> None:
        raise RuntimeError("injected Canon baseline failure")

    monkeypatch.setattr(CanonRepository, "insert_baseline", fail_insert)

    async def exercise() -> None:
        engine = create_sqlite_async_engine(database)
        try:
            ready = await _prepare_reviewed_chapter(
                engine,
                project_id="project-rollback",
                target_chapter_count=1,
                canon_change=True,
            )
            with pytest.raises(RuntimeError, match="injected Canon"):
                await ChapterCommandService(CommandBus(engine)).commit_chapter_and_canon(
                    CommitChapterRequest(
                        project_id=ready.foundation.project_id,
                        chapter_id=ready.chapter_id,
                        submission_id=ready.submission_id,
                        review_id=ready.review_id,
                        expected_canon_baseline_id=ready.foundation.canon_baseline_id,
                    ),
                    idempotency_key=f"{ready.chapter_id}:commit",
                )
            async with engine.connect() as connection:
                chapter = (
                    await connection.execute(
                        select(
                            chapters.c.lifecycle_status,
                            chapters.c.current_baseline_id,
                        ).where(chapters.c.id == ready.chapter_id)
                    )
                ).one()
                current_canon = await connection.scalar(
                    select(projects.c.current_canon_baseline_id).where(
                        projects.c.id == ready.foundation.project_id
                    )
                )
                assert (
                    await connection.scalar(
                        select(func.count()).select_from(chapter_baselines)
                    )
                    == 0
                )
                # Prepared category refs were inserted in the failed transaction too.
                changed_refs = await connection.scalar(
                    select(func.count())
                    .select_from(content_refs)
                    .where(content_refs.c.semantic_kind == "canon.characters")
                )
            assert tuple(chapter) == ("drafting", None)
            assert current_canon == ready.foundation.canon_baseline_id
            assert changed_refs == 1  # only the Project seed characters ref remains
        finally:
            await engine.dispose()

    asyncio.run(exercise())
