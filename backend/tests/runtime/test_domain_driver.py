from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Literal
from unittest.mock import AsyncMock

import pytest
from alembic import command
from pydantic import ValidationError
from sqlalchemy import func, select

from app.agents.contracts import (
    ArcPlanProposal,
    ChapterRepairVerificationIssue,
    ProfileCapabilities,
    ProfileSnapshot,
)
from app.agents.executor import AgentExecutor
from app.agents.registry import DEFAULT_TASK_REGISTRY
from app.db.engine import create_sqlite_async_engine
from app.db.maintenance import alembic_config
from app.db.schema import (
    agent_task_attempts,
    agent_tasks,
    command_receipts,
    domain_events,
    generation_runs,
)
from app.domain.arc.contracts import ArcOutlineRegeneration
from app.domain.book.contracts import (
    BookArcContract,
    BookArcTopology,
    BookCandidatePack,
    BookCompletionRequirement,
    BookCreativeConstraints,
    BookDiscussionState,
    BookRollingPlan,
    CompletionContract,
)
from app.domain.commands import CommandPreconditionError
from app.domain.chapter.contracts import ChapterRepairContract
from app.domain.projects import CreateProjectRequest, ProjectCommandService
from app.profiles import ProfileCatalog, profile_configuration_fingerprint
from app.runtime.control import (
    RetryFailedActionRequest,
    RetryFailedTaskRequest,
    RunControlRequest,
    RunControlService,
)
from app.runtime.context import ContextFactError
from app.runtime.driver import (
    DomainRunDriver,
    HarnessInvariantError,
    _book_parent_review_instruction,
    _normalize_delivery_failure,
)
from app.runtime.engine import RunEngine
from app.runtime.reconcile import ReconcileService
from app.store.command_bus import CommandBus
from app.store.content import ContentRepository
from tests.helpers.lifecycle_seed import insert_successful_task


pytestmark = pytest.mark.synthetic_integration


def _chapter_repair_contract_bytes(
    components: list[Literal["plan", "prose", "observations", "canon"]],
    *,
    stage: Literal[
        "primary_semantic",
        "derived_dependency_closure",
    ] = "primary_semantic",
) -> bytes:
    issue = ChapterRepairVerificationIssue(
        kind="contract_unfulfilled",
        code="synthetic_driver_repair",
        subject="synthetic Chapter repair",
        summary="The synthetic Driver fixture requires the declared repair scope.",
        evidence=["The fixture exercises deterministic Route sequencing only."],
        contract_item="Apply the exact declared synthetic repair scope.",
        observed_components=components,
    )
    return (
        ChapterRepairContract(
            repair_stage=stage,
            authorized_components=components,
            issues=[issue],
            issue_fingerprints=["synthetic-driver-repair"],
        )
        .model_dump_json()
        .encode()
    )


@pytest.mark.parametrize(
    (
        "lineage_origin",
        "automatic_round",
        "source_parent_review_id",
        "source_feedback_id",
    ),
    (
        ("review_initiated", 0, None, None),
        ("user_initiated", 0, "book-parent-review", "creator-feedback"),
        ("review_initiated", 1, "book-parent-review", None),
    ),
    ids=("open-request", "user-feedback-successor", "automatic-round-one-successor"),
)
def test_book_parent_review_instruction_freezes_only_book_scope(
    lineage_origin: Literal["review_initiated", "user_initiated"],
    automatic_round: Literal[0, 1],
    source_parent_review_id: str | None,
    source_feedback_id: str | None,
) -> None:
    instruction = _book_parent_review_instruction(
        book_id="book",
        workspace_lock_version=7,
        book_baseline_id="book-baseline",
        canon_baseline_id="canon-baseline",
        correction_lineage_id="book-parent-lineage",
        correction_lineage_origin=lineage_origin,
        automatic_correction_round=automatic_round,
        source_arc_book_request_id="arc-book-request",
        subject_arc_baseline_id="arc-subject-baseline",
        source_book_parent_review_id=source_parent_review_id,
        source_feedback_id=source_feedback_id,
    )

    assert instruction.arc_id is None
    assert instruction.arc_baseline_id is None
    assert instruction.chapter_id is None
    assert instruction.chapter_baseline_id is None
    assert instruction.subject_arc_baseline_id == "arc-subject-baseline"
    assert instruction.source_arc_book_request_id == "arc-book-request"
    assert instruction.source_book_parent_review_id == source_parent_review_id
    assert instruction.source_feedback_id == source_feedback_id

    profile = ProfileSnapshot.create(
        profile_id="book-parent-profile",
        display_name="Book Parent Profile",
        api_family="openai_responses",
        base_url="https://provider.example/v1",
        model_id="opaque-model",
        capabilities=ProfileCapabilities(
            text_streaming=True,
            native_json_schema=True,
        ),
    )
    plan = DEFAULT_TASK_REGISTRY.freeze_plan(
        task_id=f"book-parent-{automatic_round}-{lineage_origin}",
        project_id="project",
        run_id="run",
        task_key=f"book-parent:{automatic_round}:{lineage_origin}",
        action_key="evaluate.book_parent_contract:book",
        role=instruction.role,
        task_kind=instruction.task_kind,
        contract_version=1,
        book_id=instruction.book_id,
        canon_baseline_id="canon-baseline",
        semantic_goal="Review one Arc-to-Book request at Book authority.",
        prompt="Evaluate the frozen Arc-to-Book evidence.",
        context_manifest={"source_arc_book_request_id": "arc-book-request"},
        profile_snapshot=profile,
        workspace_lock_version=instruction.workspace_lock_version,
        workspace_work_cycle_id="book-parent-work-cycle",
        book_baseline_id=instruction.book_baseline_id,
        arc_baseline_id=instruction.arc_baseline_id,
        chapter_baseline_id=instruction.chapter_baseline_id,
        subject_arc_baseline_id=instruction.subject_arc_baseline_id,
        correction_lineage_id=instruction.correction_lineage_id,
        correction_lineage_origin=instruction.correction_lineage_origin,
        automatic_correction_round=instruction.automatic_correction_round,
        source_book_parent_review_id=instruction.source_book_parent_review_id,
        source_arc_book_request_id=instruction.source_arc_book_request_id,
        source_feedback_id=instruction.source_feedback_id,
    )

    assert plan.scope_layer == "book"
    assert plan.book_baseline_id == "book-baseline"
    assert plan.arc_baseline_id is None
    assert plan.chapter_baseline_id is None
    assert plan.subject_arc_baseline_id == "arc-subject-baseline"


def test_delivery_validation_failure_diagnostics_do_not_copy_model_input() -> None:
    secret_model_input = "sk-must-not-enter-delivery-diagnostics"
    with pytest.raises(ValidationError) as captured:
        ArcOutlineRegeneration.model_validate(
            {
                "chapter_outline": [],
                "untrusted_provider_value": secret_model_input,
            }
        )

    normalized = _normalize_delivery_failure(captured.value)
    assert normalized.code == "domain_delivery_contract_invalid"
    serialized_details = json.dumps(normalized.details, ensure_ascii=False)
    assert secret_model_input not in serialized_details
    assert '"input"' not in serialized_details


def test_book_local_repair_review_is_consumed_once_before_verification() -> None:
    async def exercise() -> None:
        review = SimpleNamespace(
            id="book-local-repair-review",
            decision="local_repair",
            submission_id="reviewed-submission",
        )
        pending = SimpleNamespace(
            id="repaired-submission",
            base_book_baseline_id=None,
        )
        books = SimpleNamespace(
            find_pending_submission=AsyncMock(return_value=None),
            get_review_for_submission=AsyncMock(return_value=None),
            get_review=AsyncMock(return_value=review),
        )
        execution = SimpleNamespace(has_applied_task=AsyncMock(side_effect=(False, True)))
        discussion = BookDiscussionState(
            turn_count=1,
            direction_draft="A stable whole-book direction.",
            discussion_summary="The creator decisions are complete.",
            selected_title="The Echo Ledger",
            selected_title_source="custom",
            readiness_status="ready",
            readiness_reason="The Book contract can be synthesized and reviewed.",
        )
        content = SimpleNamespace(
            get_packed=AsyncMock(
                return_value=SimpleNamespace(
                    unpack_and_verify=lambda: discussion.model_dump_json().encode()
                )
            )
        )
        store = SimpleNamespace(books=books, execution=execution, content=content)
        workspace = SimpleNamespace(
            lock_version=9,
            state="active",
            work_cycle_id="book-work-cycle",
            active_repair_review_id=review.id,
            semantic_repair_count=0,
            semantic_repair_limit=1,
            discussion_state_ref_id="discussion-ref",
            candidate_constraints_ref_id="constraints-ref",
            candidate_titles_ref_id="titles-ref",
            candidate_rolling_plan_ref_id="rolling-ref",
            candidate_completion_contract_ref_id="completion-ref",
            candidate_arc_topology_ref_id="topology-ref",
        )
        driver = object.__new__(DomainRunDriver)
        arguments = {
            "store": store,
            "run": SimpleNamespace(id="run-book"),
            "project": SimpleNamespace(id="project-book"),
            "book": SimpleNamespace(id="book", current_baseline_id=None),
            "workspace": workspace,
            "has_open_book_change": False,
        }

        first = await driver._decide_book(**arguments)
        assert first is not None
        assert first.task_kind == "book.repair"

        second = await driver._decide_book(**arguments)
        assert second is not None
        assert second.kind == "submit_book"
        execution.has_applied_task.assert_awaited_with(
            project_id="project-book",
            run_id="run-book",
            task_kind="book.repair",
            book_id="book",
            book_baseline_id=None,
            workspace_work_cycle_id="book-work-cycle",
            source_book_candidate_review_id=review.id,
        )

        books.find_pending_submission.return_value = pending
        workspace.semantic_repair_count = 1
        third = await driver._decide_book(**arguments)
        assert third is not None
        assert third.task_kind == "verify_repair.book"

    asyncio.run(exercise())


def test_arc_local_repair_route_survives_restart_then_is_consumed_once() -> None:
    async def exercise() -> None:
        review = SimpleNamespace(
            id="arc-local-repair-review",
            decision="local_repair",
            submission_id="reviewed-submission",
        )
        pending = SimpleNamespace(
            id="repaired-submission",
            book_baseline_id="book-baseline",
            base_arc_baseline_id=None,
        )
        arc = SimpleNamespace(
            id="arc",
            lifecycle_status="active",
            current_baseline_id=None,
            current_closure_id=None,
        )
        workspace = SimpleNamespace(
            state="active",
            lock_version=11,
            work_cycle_id="arc-work-cycle",
            active_repair_review_id=review.id,
            semantic_repair_count=0,
            semantic_repair_limit=1,
            book_baseline_id="book-baseline",
            base_arc_baseline_id=None,
            plan_ref_id="arc-plan-ref",
            source_feedback_id=None,
            source_arc_parent_review_id=None,
            source_arc_closure_review_id=None,
            source_book_parent_review_id=None,
            source_book_completion_review_id=None,
            book_progress_handoff_id=None,
        )
        arcs = SimpleNamespace(
            get_unfinished_for_book=AsyncMock(return_value=arc),
            get_workspace=AsyncMock(return_value=workspace),
            find_pending_submission=AsyncMock(return_value=None),
            get_review_for_submission=AsyncMock(return_value=None),
            get_review=AsyncMock(return_value=review),
        )
        execution = SimpleNamespace(has_applied_task=AsyncMock(side_effect=(False, False, True)))
        books = SimpleNamespace(
            get_baseline=AsyncMock(return_value=SimpleNamespace(id="book-baseline"))
        )
        store = SimpleNamespace(arcs=arcs, books=books, execution=execution)
        driver = object.__new__(DomainRunDriver)
        arguments = {
            "store": store,
            "run": SimpleNamespace(id="run-arc"),
            "project": SimpleNamespace(
                id="project-arc",
                current_canon_baseline_id="canon-baseline",
                operation_mode="full_auto",
            ),
            "book": SimpleNamespace(
                id="book",
                current_baseline_id="book-baseline",
            ),
            "book_workspace": SimpleNamespace(id="book-workspace", lock_version=3),
        }

        first = await driver._decide_arc(**arguments)
        assert first is not None
        assert first.task_kind == "arc.repair"

        restarted_driver = object.__new__(DomainRunDriver)
        after_restart = await restarted_driver._decide_arc(**arguments)
        assert after_restart == first

        second = await restarted_driver._decide_arc(**arguments)
        assert second is not None
        assert second.kind == "submit_arc"
        execution.has_applied_task.assert_awaited_with(
            project_id="project-arc",
            run_id="run-arc",
            task_kind="arc.repair",
            book_id="book",
            arc_id="arc",
            book_baseline_id="book-baseline",
            arc_baseline_id=None,
            workspace_work_cycle_id="arc-work-cycle",
            source_arc_candidate_review_id=review.id,
            source_arc_parent_review_id=None,
            source_arc_closure_review_id=None,
            source_book_parent_review_id=None,
            source_book_completion_review_id=None,
            source_book_progress_handoff_id=None,
            source_feedback_id=None,
        )

        arcs.find_pending_submission.return_value = pending
        workspace.semantic_repair_count = 1
        third = await driver._decide_arc(**arguments)
        assert third is not None
        assert third.task_kind == "verify_repair.arc"

    asyncio.run(exercise())


def test_chapter_plan_repair_regenerates_invalidated_downstream_content() -> None:
    async def exercise() -> None:
        review = SimpleNamespace(
            id="chapter-local-repair-review",
            decision="local_repair",
            submission_id="reviewed-submission",
            repair_contract_ref_id="repair-contract-ref",
        )
        chapter = SimpleNamespace(
            id="chapter",
            book_id="book",
            arc_id="arc",
            current_baseline_id=None,
        )
        workspace = SimpleNamespace(
            state="active",
            lock_version=13,
            work_cycle_id="chapter-work-cycle",
            active_repair_review_id=review.id,
            semantic_repair_count=0,
            semantic_repair_limit=1,
            book_baseline_id="book-baseline",
            arc_baseline_id="arc-baseline",
            base_chapter_baseline_id=None,
            plan_ref_id="original-plan-ref",
            draft_ref_id="original-draft-ref",
            observations_ref_id="original-observations-ref",
            candidate_canon_patch_ref_id="original-canon-patch-ref",
            correction_lineage_id=None,
            correction_lineage_origin=None,
            automatic_correction_round=None,
            source_arc_parent_review_id=None,
            source_arc_closure_review_id=None,
            source_feedback_id=None,
        )
        applied_tasks: set[str] = set()

        async def has_applied_task(**kwargs: object) -> bool:
            return str(kwargs["task_kind"]) in applied_tasks

        packed_repair = SimpleNamespace(
            unpack_and_verify=lambda: _chapter_repair_contract_bytes(["plan"])
        )
        chapters = SimpleNamespace(
            get_non_idle_workspace_for_arc=AsyncMock(return_value=(chapter, workspace)),
            find_pending_submission=AsyncMock(return_value=None),
            get_review_for_submission=AsyncMock(return_value=None),
            get_review=AsyncMock(return_value=review),
        )
        store = SimpleNamespace(
            chapters=chapters,
            arcs=SimpleNamespace(),
            books=SimpleNamespace(),
            content=SimpleNamespace(get_packed=AsyncMock(return_value=packed_repair)),
            execution=SimpleNamespace(has_applied_task=AsyncMock(side_effect=has_applied_task)),
        )
        driver = object.__new__(DomainRunDriver)
        arguments = {
            "store": store,
            "run": SimpleNamespace(id="run-chapter"),
            "project_id": "project-chapter",
            "book_id": "book",
            "book_baseline_id": "book-baseline",
            "canon_baseline_id": "canon-baseline",
            "arc": SimpleNamespace(id="arc"),
            "arc_baseline_id": "arc-baseline",
        }

        first = await driver._decide_chapter(**arguments)
        assert first.task_kind == "chapter.repair.plan"

        applied_tasks.add("chapter.repair.plan")
        workspace.plan_ref_id = "replacement-plan-ref"
        workspace.draft_ref_id = None
        workspace.observations_ref_id = None
        workspace.candidate_canon_patch_ref_id = None
        workspace.lock_version += 1
        second = await driver._decide_chapter(**arguments)
        assert second.task_kind == "chapter.draft"

        workspace.draft_ref_id = "regenerated-draft-ref"
        workspace.lock_version += 1
        third = await driver._decide_chapter(**arguments)
        assert third.task_kind == "chapter.observe"

        workspace.observations_ref_id = "regenerated-observations-ref"
        workspace.candidate_canon_patch_ref_id = "regenerated-canon-patch-ref"
        workspace.lock_version += 1
        fourth = await driver._decide_chapter(**arguments)
        assert fourth.kind == "submit_chapter"

    asyncio.run(exercise())


def test_chapter_prose_repair_regenerates_observations_instead_of_repairing_them() -> None:
    async def exercise() -> None:
        review = SimpleNamespace(
            id="chapter-prose-repair-review",
            decision="local_repair",
            submission_id="reviewed-submission",
            repair_contract_ref_id="prose-repair-contract-ref",
        )
        chapter = SimpleNamespace(
            id="chapter",
            book_id="book",
            arc_id="arc",
            current_baseline_id=None,
        )
        workspace = SimpleNamespace(
            state="active",
            lock_version=21,
            work_cycle_id="chapter-prose-work-cycle",
            active_repair_review_id=review.id,
            semantic_repair_count=0,
            semantic_repair_limit=1,
            book_baseline_id="book-baseline",
            arc_baseline_id="arc-baseline",
            base_chapter_baseline_id=None,
            plan_ref_id="plan-ref",
            draft_ref_id="draft-ref",
            observations_ref_id="observations-ref",
            candidate_canon_patch_ref_id="canon-patch-ref",
            correction_lineage_id=None,
            correction_lineage_origin=None,
            automatic_correction_round=None,
            source_arc_parent_review_id=None,
            source_arc_closure_review_id=None,
            source_feedback_id=None,
        )
        applied_tasks: set[str] = set()

        async def has_applied_task(**kwargs: object) -> bool:
            return str(kwargs["task_kind"]) in applied_tasks

        chapters = SimpleNamespace(
            get_non_idle_workspace_for_arc=AsyncMock(return_value=(chapter, workspace)),
            find_pending_submission=AsyncMock(return_value=None),
            get_review_for_submission=AsyncMock(return_value=None),
            get_review=AsyncMock(return_value=review),
        )
        store = SimpleNamespace(
            chapters=chapters,
            arcs=SimpleNamespace(),
            books=SimpleNamespace(),
            content=SimpleNamespace(
                get_packed=AsyncMock(
                    return_value=SimpleNamespace(
                        unpack_and_verify=lambda: _chapter_repair_contract_bytes(
                            ["prose", "observations"]
                        )
                    )
                )
            ),
            execution=SimpleNamespace(has_applied_task=AsyncMock(side_effect=has_applied_task)),
        )
        driver = object.__new__(DomainRunDriver)
        arguments = {
            "store": store,
            "run": SimpleNamespace(id="run-chapter-prose"),
            "project_id": "project-chapter-prose",
            "book_id": "book",
            "book_baseline_id": "book-baseline",
            "canon_baseline_id": "canon-baseline",
            "arc": SimpleNamespace(id="arc"),
            "arc_baseline_id": "arc-baseline",
        }

        first = await driver._decide_chapter(**arguments)
        assert first.task_kind == "chapter.repair.prose"
        assert first.source_chapter_candidate_review_id == review.id

        applied_tasks.add("chapter.repair.prose")
        workspace.draft_ref_id = "repaired-draft-ref"
        workspace.observations_ref_id = None
        workspace.candidate_canon_patch_ref_id = None
        workspace.semantic_repair_count = 1
        workspace.lock_version += 1
        second = await driver._decide_chapter(**arguments)
        assert second.task_kind == "chapter.observe"
        assert second.task_kind != "chapter.repair.observation"

        workspace.observations_ref_id = "regenerated-observations-ref"
        workspace.candidate_canon_patch_ref_id = "regenerated-canon-patch-ref"
        workspace.lock_version += 1
        third = await driver._decide_chapter(**arguments)
        assert third.kind == "submit_chapter"

        derived_review = SimpleNamespace(
            id="chapter-derived-closure-review",
            decision="local_repair",
            submission_id="repaired-submission",
            repair_contract_ref_id="derived-closure-contract-ref",
        )
        workspace.active_repair_review_id = derived_review.id
        chapters.get_review.return_value = derived_review
        store.content.get_packed.return_value = SimpleNamespace(
            unpack_and_verify=lambda: _chapter_repair_contract_bytes(
                ["canon"],
                stage="derived_dependency_closure",
            )
        )
        fourth = await driver._decide_chapter(**arguments)
        assert fourth.task_kind == "chapter.repair.observation"

        applied_tasks.add("chapter.repair.observation")
        workspace.lock_version += 1
        fifth = await driver._decide_chapter(**arguments)
        assert fifth.kind == "submit_chapter"

    asyncio.run(exercise())


def _arc_plan(*, chapter_count: int) -> ArcPlanProposal:
    return ArcPlanProposal.model_validate(
        {
            "title": "Bounded fixture Arc",
            "chapter_outline": [
                {
                    "title": f"Fixture Chapter {index + 1}",
                    "core_event": f"Advance bounded fixture step {index + 1}.",
                    "hook": "Hand off to the next assignment or closure.",
                    "scenes": ["Investigate the current bounded clue."],
                }
                for index in range(chapter_count)
            ],
        }
    )


def test_driver_routes_from_arc_outline_without_a_book_chapter_maximum() -> None:
    async def exercise() -> None:
        proposal = _arc_plan(chapter_count=10)
        chapters = SimpleNamespace(
            get_non_idle_workspace_for_arc=AsyncMock(return_value=None),
            count_committed_for_book=AsyncMock(return_value=9),
            next_ordinals=AsyncMock(return_value=(10, 10)),
        )
        store = SimpleNamespace(
            chapters=chapters,
            arcs=SimpleNamespace(
                get_baseline=AsyncMock(
                    return_value=SimpleNamespace(
                        id="arc-baseline",
                        baseline_version=1,
                        plan_ref_id="arc-plan-ref",
                        planned_after_cumulative_chapter_count=0,
                        planned_after_arc_chapter_count=0,
                        closure_cumulative_chapter_count=10,
                    )
                )
            ),
            content=SimpleNamespace(
                get_packed=AsyncMock(
                    return_value=SimpleNamespace(
                        unpack_and_verify=lambda: proposal.model_dump_json().encode()
                    )
                )
            ),
            execution=SimpleNamespace(),
        )
        driver = object.__new__(DomainRunDriver)
        instruction = await driver._decide_chapter(
            store=store,
            run=SimpleNamespace(id="run-book-scale"),
            project_id="project-book-scale",
            book_id="book",
            book_baseline_id="book-baseline",
            canon_baseline_id="canon-baseline",
            arc=SimpleNamespace(id="arc"),
            arc_baseline_id="arc-baseline",
        )
        assert instruction.kind == "create_chapter"
        chapters.count_committed_for_book.assert_awaited_once_with(book_id="book")

    asyncio.run(exercise())


def test_driver_rejects_missing_arc_outline_slot_before_scheduling_chapter() -> None:
    async def exercise() -> None:
        proposal = _arc_plan(chapter_count=0)
        chapters = SimpleNamespace(
            get_non_idle_workspace_for_arc=AsyncMock(return_value=None),
            count_committed_for_book=AsyncMock(return_value=0),
            next_ordinals=AsyncMock(return_value=(1, 1)),
        )
        store = SimpleNamespace(
            chapters=chapters,
            arcs=SimpleNamespace(
                get_baseline=AsyncMock(
                    return_value=SimpleNamespace(
                        id="arc-baseline",
                        baseline_version=1,
                        plan_ref_id="arc-plan-ref",
                        planned_after_cumulative_chapter_count=0,
                        planned_after_arc_chapter_count=0,
                        closure_cumulative_chapter_count=1,
                    )
                )
            ),
            content=SimpleNamespace(
                get_packed=AsyncMock(
                    return_value=SimpleNamespace(
                        unpack_and_verify=lambda: proposal.model_dump_json().encode()
                    )
                )
            ),
            execution=SimpleNamespace(),
        )
        driver = object.__new__(DomainRunDriver)

        with pytest.raises(HarnessInvariantError) as captured:
            await driver._decide_chapter(
                store=store,
                run=SimpleNamespace(id="run-outline-gap"),
                project_id="project-outline-gap",
                book_id="book",
                book_baseline_id="book-baseline",
                canon_baseline_id="canon-baseline",
                arc=SimpleNamespace(id="arc"),
                arc_baseline_id="arc-baseline",
            )

        assert captured.value.failure_code == "arc_outline_slot_missing"
        chapters.next_ordinals.assert_awaited_once_with(
            book_id="book",
            arc_id="arc",
        )

    asyncio.run(exercise())


def _write_profile(path: Path) -> None:
    capabilities = ProfileCapabilities(text_streaming=True, native_json_schema=True)
    fingerprint = profile_configuration_fingerprint(
        api_family="openai_responses",
        base_url="https://provider.invalid/v1",
        model_id="offline-model",
        request_options={},
    )
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "selected_profile_id": "offline-profile",
                "profiles": [
                    {
                        "id": "offline-profile",
                        "display_name": "Offline profile",
                        "api_family": "openai_responses",
                        "base_url": "https://provider.invalid/v1",
                        "api_key": "offline-secret",
                        "model_id": "offline-model",
                        "request_options": {},
                        "enabled": True,
                        "capability_test": {
                            "checked_at": "2026-07-23T00:00:00Z",
                            "profile_fingerprint": fingerprint,
                            "source": "pydantic-ai-capability-v1",
                            "capabilities": capabilities.model_dump(mode="json"),
                        },
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_stale_profile_preflight_fails_once_without_provider_request(
    tmp_path: Path,
) -> None:
    database = tmp_path / "stale-profile.sqlite3"
    profile_path = tmp_path / "profiles.local.json"
    command.upgrade(alembic_config(database), "head")
    _write_profile(profile_path)
    profile_document = json.loads(profile_path.read_text(encoding="utf-8"))
    profile_document["profiles"][0]["model_id"] = "stale-offline-model"
    profile_path.write_text(
        json.dumps(profile_document, ensure_ascii=False),
        encoding="utf-8",
    )

    async def exercise() -> None:
        engine = create_sqlite_async_engine(database)
        try:
            bus = CommandBus(engine)
            created = await ProjectCommandService(bus).create_project(
                CreateProjectRequest(
                    project_id="project-stale-profile",
                    creator_brief="A Profile preflight must fail without calling the Provider.",
                    operation_mode="full_auto",
                    default_profile_id="offline-profile",
                ),
                idempotency_key="create-stale-profile-project",
            )
            await RunControlService(bus).start(
                RunControlRequest(
                    project_id=created.result.project_id,
                    run_id=created.result.generation_run_id,
                    expected_lock_version=1,
                ),
                idempotency_key="start-stale-profile-run",
            )
            resolver = SimpleNamespace(
                resolve=AsyncMock(
                    side_effect=AssertionError("A stale Profile must fail before model binding.")
                )
            )
            executor = AgentExecutor(
                engine,
                registry=DEFAULT_TASK_REGISTRY,
                resolver=resolver,
            )
            run_engine = RunEngine(
                engine,
                driver=DomainRunDriver(
                    engine,
                    profile_catalog=ProfileCatalog(profile_path),
                    executor=executor,
                    now_ms=lambda: 100,
                ),
                reconciler=ReconcileService(engine, bus, now_ms=lambda: 100),
                instance_id="stale-profile-engine",
                now_ms=lambda: 100,
            )

            assert await run_engine.run_once()  # Freeze a preflight diagnostic Task Plan.
            assert await run_engine.run_once()  # Persist the zero-request failed attempt.
            assert not await run_engine.run_once()  # Reconcile into failure_paused.
            assert not await run_engine.run_once()  # No queued/pending retry loop remains.

            async with engine.connect() as connection:
                run = (
                    await connection.execute(
                        select(
                            generation_runs.c.status,
                            generation_runs.c.blocking_task_id,
                            generation_runs.c.failure_code,
                            generation_runs.c.lock_version,
                        ).where(generation_runs.c.id == created.result.generation_run_id)
                    )
                ).one()
                task = (
                    await connection.execute(
                        select(
                            agent_tasks.c.id,
                            agent_tasks.c.status,
                            agent_tasks.c.delivery_state,
                        ).where(agent_tasks.c.project_id == created.result.project_id)
                    )
                ).one()
                attempt = (
                    await connection.execute(
                        select(
                            agent_task_attempts.c.status,
                            agent_task_attempts.c.provider_request_count,
                            agent_task_attempts.c.transport_retry_count,
                            agent_task_attempts.c.model_request_count,
                            agent_task_attempts.c.error_code,
                            agent_task_attempts.c.error_category,
                        ).where(agent_task_attempts.c.project_id == created.result.project_id)
                    )
                ).one()
                attempt_count = int(
                    (
                        await connection.execute(
                            select(func.count())
                            .select_from(agent_task_attempts)
                            .where(agent_task_attempts.c.project_id == created.result.project_id)
                        )
                    ).scalar_one()
                )

            assert tuple(run) == (
                "failure_paused",
                task.id,
                "profile_capability_missing",
                3,
            )
            assert (task.status, task.delivery_state) == ("failed", "not_ready")
            assert tuple(attempt) == (
                "failed",
                0,
                0,
                0,
                "profile_capability_missing",
                "capability",
            )
            assert attempt_count == 1
            resolver.resolve.assert_not_called()
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_rejected_domain_delivery_failure_pauses_once_and_requires_explicit_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "delivery-failure.sqlite3"
    profile_path = tmp_path / "profiles.local.json"
    command.upgrade(alembic_config(database), "head")
    _write_profile(profile_path)

    async def exercise() -> None:
        engine = create_sqlite_async_engine(database)
        try:
            bus = CommandBus(engine)
            created = await ProjectCommandService(bus).create_project(
                CreateProjectRequest(
                    project_id="project-delivery-failure",
                    creator_brief="A deterministic delivery failure test.",
                    operation_mode="full_auto",
                ),
                idempotency_key="create-delivery-failure-project",
            )
            await RunControlService(bus).start(
                RunControlRequest(
                    project_id=created.result.project_id,
                    run_id=created.result.generation_run_id,
                    expected_lock_version=1,
                ),
                idempotency_key="start-delivery-failure-run",
            )
            task_id, attempt_id = await insert_successful_task(
                engine,
                project_id=created.result.project_id,
                run_id=created.result.generation_run_id,
                task_id="delivery-failure-task",
                attempt_id="delivery-failure-attempt",
                role="book_strategist",
                task_kind="book.synthesize",
                scope_layer="book",
                book_id=created.result.book_id,
                canon_baseline_id=created.result.canon_baseline_id,
                workspace_lock_version=1,
                result=BookCandidatePack(
                    direction="A direction that will be rejected by the delivery stub.",
                    constraints=BookCreativeConstraints(
                        genre_reader_promise="A deterministic test mystery.",
                        premise_story_engine="Evidence exposes a controlled contradiction.",
                        stable_world_invariants=["Committed evidence remains stable."],
                        stable_character_invariants=["The investigator follows evidence."],
                        core_selling_points=["Deterministic delivery behavior"],
                        prohibited_outcomes=["Do not erase committed facts."],
                    ),
                    selected_title="Rejected Delivery",
                    rolling_plan=BookRollingPlan(
                        long_term_character_directions=[
                            "The investigator learns from verified evidence."
                        ],
                        whole_book_pacing_strategy="Use one bounded fixture Arc.",
                        ending_tendency="End at the deterministic assertion.",
                        arc_planning_guidelines=["Close only after committed evidence."],
                        whole_book_scale_guidance="One or two Chapters is advisory.",
                    ),
                    completion_contract=CompletionContract(
                        completion_requirements=[
                            BookCompletionRequirement(
                                requirement_key="delivery_failure_fixture",
                                description="Reach the deterministic fixture ending.",
                                evidence_expectation=(
                                    "A committed final Chapter proves the fixture ending."
                                ),
                            )
                        ],
                    ),
                    arc_topology=BookArcTopology(
                        arcs=[
                            BookArcContract(
                                whole_book_role="Reach the fixture ending.",
                                core_goal="Complete the deterministic assertion.",
                                handoff_from_previous="Begin from the fixture premise.",
                                exit_conditions=["The fixture ending is committed."],
                                completion_requirement_keys=["delivery_failure_fixture"],
                                is_final=True,
                            )
                        ]
                    ),
                ),
            )
            driver = DomainRunDriver(
                engine,
                profile_catalog=ProfileCatalog(profile_path),
                now_ms=lambda: 100,
            )
            delivery_calls = 0

            async def reject_delivery(_task: object) -> None:
                nonlocal delivery_calls
                delivery_calls += 1
                raise CommandPreconditionError("Repair patch changed an unauthorized component.")

            monkeypatch.setattr(driver, "_deliver_task", reject_delivery)
            run_engine = RunEngine(
                engine,
                driver=driver,
                reconciler=ReconcileService(engine, bus, now_ms=lambda: 100),
                instance_id="delivery-failure-engine",
                now_ms=lambda: 100,
            )

            assert await run_engine.run_once()
            assert not await run_engine.run_once()
            assert delivery_calls == 1

            async with engine.connect() as connection:
                task = (
                    await connection.execute(
                        select(
                            agent_tasks.c.status,
                            agent_tasks.c.delivery_state,
                            agent_tasks.c.successful_attempt_id,
                        ).where(agent_tasks.c.id == task_id)
                    )
                ).one()
                attempt = (
                    await connection.execute(
                        select(
                            agent_task_attempts.c.status,
                            agent_task_attempts.c.result_ref_id,
                            agent_task_attempts.c.error_code,
                            agent_task_attempts.c.error_category,
                            agent_task_attempts.c.error_ref_id,
                        ).where(agent_task_attempts.c.id == attempt_id)
                    )
                ).one()
                run = (
                    await connection.execute(
                        select(
                            generation_runs.c.status,
                            generation_runs.c.blocking_task_id,
                            generation_runs.c.failure_code,
                            generation_runs.c.failure_ref_id,
                            generation_runs.c.lock_version,
                        ).where(generation_runs.c.id == created.result.generation_run_id)
                    )
                ).one()
                receipt = (
                    await connection.execute(
                        select(
                            command_receipts.c.command_kind,
                            command_receipts.c.actor,
                            command_receipts.c.source_task_id,
                        ).where(
                            command_receipts.c.idempotency_key
                            == f"failure-pause-delivery:{attempt_id}"
                        )
                    )
                ).one()
                event = (
                    await connection.execute(
                        select(
                            domain_events.c.event_type,
                            domain_events.c.aggregate_id,
                            domain_events.c.payload_json,
                        ).where(
                            domain_events.c.event_type == "run.failure_paused",
                            domain_events.c.aggregate_id == created.result.generation_run_id,
                        )
                    )
                ).one()

            assert tuple(task) == ("failed", "failed", None)
            assert attempt.status == "delivery_failed"
            assert attempt.result_ref_id is not None
            assert attempt.error_code == "domain_delivery_rejected"
            assert attempt.error_category == "domain_delivery"
            assert attempt.error_ref_id is not None
            assert tuple(run[:3]) == (
                "failure_paused",
                task_id,
                "domain_delivery_rejected",
            )
            assert run.failure_ref_id == attempt.error_ref_id
            assert tuple(receipt) == (
                "failure_pause_for_domain_delivery",
                "system",
                task_id,
            )
            assert json.loads(event.payload_json) == {
                "attempt_id": attempt_id,
                "failure_code": "domain_delivery_rejected",
                "failure_kind": "domain_delivery",
                "task_id": task_id,
            }
            async with engine.connect() as connection:
                packed_failure = await ContentRepository(connection).get_packed(
                    project_id=created.result.project_id,
                    ref_id=attempt.error_ref_id,
                )
            failure_payload = json.loads(packed_failure.unpack_and_verify())
            assert failure_payload == {
                "attempt_id": attempt_id,
                "code": "domain_delivery_rejected",
                "details": None,
                "exception_type": "CommandPreconditionError",
                "message": "Repair patch changed an unauthorized component.",
                "schema_id": "domain-delivery-failure-v1",
                "task_id": task_id,
                "task_kind": "book.synthesize",
            }

            retried = await RunControlService(bus).retry_failed_task(
                RetryFailedTaskRequest(
                    project_id=created.result.project_id,
                    run_id=created.result.generation_run_id,
                    expected_lock_version=run.lock_version,
                    task_id=task_id,
                ),
                idempotency_key="retry-domain-delivery-failure",
            )
            assert retried.result.status == "running"
            assert retried.result.attempt_id is not None
            async with engine.connect() as connection:
                reset_task = (
                    await connection.execute(
                        select(
                            agent_tasks.c.status,
                            agent_tasks.c.delivery_state,
                        ).where(agent_tasks.c.id == task_id)
                    )
                ).one()
                retry_attempt = (
                    await connection.execute(
                        select(
                            agent_task_attempts.c.attempt_number,
                            agent_task_attempts.c.retry_kind,
                            agent_task_attempts.c.status,
                            agent_task_attempts.c.predecessor_attempt_id,
                        ).where(agent_task_attempts.c.id == retried.result.attempt_id)
                    )
                ).one()
            assert tuple(reset_task) == ("queued", "not_ready")
            assert tuple(retry_attempt) == (2, "user_retry", "queued", attempt_id)
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_context_assembly_failure_binds_real_harness_action_and_requires_action_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "context-action-failure.sqlite3"
    profile_path = tmp_path / "profiles.local.json"
    command.upgrade(alembic_config(database), "head")
    _write_profile(profile_path)

    async def exercise() -> None:
        engine = create_sqlite_async_engine(database)
        try:
            bus = CommandBus(engine)
            created = await ProjectCommandService(bus).create_project(
                CreateProjectRequest(
                    project_id="project-context-action-failure",
                    creator_brief="A pre-task context assembly failure test.",
                    operation_mode="full_auto",
                    default_profile_id="offline-profile",
                ),
                idempotency_key="create-context-action-failure-project",
            )
            started = await RunControlService(bus).start(
                RunControlRequest(
                    project_id=created.result.project_id,
                    run_id=created.result.generation_run_id,
                    expected_lock_version=1,
                ),
                idempotency_key="start-context-action-failure-run",
            )
            driver = DomainRunDriver(
                engine,
                profile_catalog=ProfileCatalog(profile_path),
                now_ms=lambda: 100,
            )

            async def reject_context(**_kwargs: object) -> object:
                raise ContextFactError(
                    "fixture_authority_input_present",
                    (
                        "fixture deliberately removed one authority input\n"
                        "Authorization: Bearer do-not-store-auth-token\n"
                        "api_key=do-not-store-api-key\n"
                        "prompt=do-not-store-private-prompt"
                    ),
                )

            monkeypatch.setattr(driver._context, "build", reject_context)
            run_engine = RunEngine(
                engine,
                driver=driver,
                reconciler=ReconcileService(engine, bus, now_ms=lambda: 100),
                instance_id="context-action-failure-engine",
                now_ms=lambda: 100,
            )

            assert await run_engine.run_once()
            assert not await run_engine.run_once()

            async with engine.connect() as connection:
                run = (
                    await connection.execute(
                        select(
                            generation_runs.c.status,
                            generation_runs.c.failure_source_kind,
                            generation_runs.c.blocking_task_id,
                            generation_runs.c.blocking_action_key,
                            generation_runs.c.failure_code,
                            generation_runs.c.failure_ref_id,
                            generation_runs.c.lock_version,
                        ).where(generation_runs.c.id == created.result.generation_run_id)
                    )
                ).one()
                task_count = (
                    await connection.execute(
                        select(func.count())
                        .select_from(agent_tasks)
                        .where(agent_tasks.c.run_id == created.result.generation_run_id)
                    )
                ).scalar_one()
                assert run.failure_ref_id is not None
                failure_payload = json.loads(
                    (
                        await ContentRepository(connection).get_packed(
                            project_id=created.result.project_id,
                            ref_id=run.failure_ref_id,
                        )
                    ).unpack_and_verify()
                )

            assert run.status == "failure_paused"
            assert run.failure_source_kind == "harness_action"
            assert run.blocking_task_id is None
            assert run.blocking_action_key.startswith("freeze-task:book.discuss:")
            assert run.failure_code == "context_assembly_invalid"
            assert task_count == 0
            assert failure_payload["schema_id"] == "harness-action-failure-v2"
            details = failure_payload["details"]
            assert details["phase"] == "context"
            assert details["task_kind"] == "book.discuss"
            assert details["failed_invariant"] == "fixture_authority_input_present"
            assert [item["exception_type"] for item in details["cause_chain"]] == [
                "ContextAssemblyError",
                "ContextFactError",
            ]
            assert (
                "fixture deliberately removed one authority input"
                in details["cause_chain"][1]["message"]
            )
            serialized_failure = json.dumps(failure_payload, ensure_ascii=False)
            assert "do-not-store-auth-token" not in serialized_failure
            assert "do-not-store-api-key" not in serialized_failure
            assert "do-not-store-private-prompt" not in serialized_failure

            with pytest.raises(CommandPreconditionError):
                await RunControlService(bus).retry_failed_task(
                    RetryFailedTaskRequest(
                        project_id=created.result.project_id,
                        run_id=created.result.generation_run_id,
                        expected_lock_version=run.lock_version,
                        task_id="not-a-real-task",
                    ),
                    idempotency_key="wrong-source-task-retry",
                )

            retried = await RunControlService(bus).retry_failed_action(
                RetryFailedActionRequest(
                    project_id=created.result.project_id,
                    run_id=created.result.generation_run_id,
                    expected_lock_version=run.lock_version,
                    action_key=run.blocking_action_key,
                ),
                idempotency_key="retry-context-action-failure",
            )
            assert retried.result.status == "running"
            assert retried.result.lock_version == started.result.lock_version + 2
            async with engine.connect() as connection:
                resumed = (
                    await connection.execute(
                        select(
                            generation_runs.c.failure_source_kind,
                            generation_runs.c.blocking_task_id,
                            generation_runs.c.blocking_action_key,
                            generation_runs.c.failure_code,
                            generation_runs.c.failure_ref_id,
                        ).where(generation_runs.c.id == created.result.generation_run_id)
                    )
                ).one()
            assert tuple(resumed) == (None, None, None, None, None)
        finally:
            await engine.dispose()

    asyncio.run(exercise())
