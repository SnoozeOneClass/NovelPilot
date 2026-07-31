from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Literal
from unittest.mock import AsyncMock

import pytest
from alembic import command
from pydantic import ValidationError
from pydantic_ai import ModelResponse, RequestUsage, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from sqlalchemy import func, select

from app.agents.binding import ProfileCredential, ResolvedModelBinding
from app.agents.contracts import (
    CapabilityName,
    ChapterEvaluationIssue,
    ProfileCapabilities,
    ProfileSnapshot,
)
from app.agents.executor import AgentExecutor
from app.agents.registry import DEFAULT_TASK_REGISTRY
from app.agents.transport import ActivationRequestBudget, RequestCountingModel
from app.db.engine import create_sqlite_async_engine
from app.db.maintenance import alembic_config
from app.db.schema import (
    agent_task_attempts,
    agent_tasks,
    arc_approval_gates,
    arc_baselines,
    arc_closures,
    book_approvals,
    chapter_baselines,
    command_receipts,
    domain_events,
    generation_runs,
    projects,
)
from app.db.uow import UnitOfWork
from app.domain.arc.commands import ArcCommandService
from app.domain.arc.contracts import ApproveArcRequest, ArcRepairPatch
from app.domain.book.commands import BookCommandService
from app.domain.book.contracts import ApproveBookRequest
from app.domain.book.contracts import (
    BookArcContract,
    BookArcTopology,
    BookCandidatePack,
    BookCompletionRequirement,
    BookCreativeConstraints,
    BookDiscussionState,
    BookRollingPlan,
    CompletionContract,
    RecordBookUserInputRequest,
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
    issue = ChapterEvaluationIssue(
        kind="contract_unfulfilled",
        code="synthetic_driver_repair",
        subject="synthetic Chapter repair",
        summary="The synthetic Driver fixture requires the declared repair scope.",
        evidence=["The fixture exercises deterministic Route sequencing only."],
        contract_item="Apply the exact declared synthetic repair scope.",
        affected_components=components,
    )
    return ChapterRepairContract(
        repair_stage=stage,
        authorized_components=components,
        issues=[issue],
        issue_fingerprints=["synthetic-driver-repair"],
    ).model_dump_json().encode()


class DeterministicNovelResolver:
    """Offline Pydantic AI model that exercises the real Executor and task contracts."""

    def resolve(
        self,
        *,
        profile: ProfileSnapshot,
        expected_profile_fingerprint: str,
        required_capabilities: tuple[CapabilityName, ...],
        model_request_limit: int,
        credential: ProfileCredential,
    ) -> ResolvedModelBinding:
        del expected_profile_fingerprint, credential
        assert all(
            profile.capabilities.supports(capability)
            for capability in required_capabilities
        )
        budget = ActivationRequestBudget(model_request_limit=model_request_limit)

        def response(messages: list[object], _info: AgentInfo) -> ModelResponse:
            prompt = _message_text(messages)
            task_kind = re.search(r"NovelPilot task: ([^\s]+)", prompt)
            assert task_kind is not None
            payload = _task_output(task_kind.group(1), prompt)
            text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
            return ModelResponse(
                parts=[TextPart(text)],
                usage=RequestUsage(input_tokens=37, output_tokens=23),
            )

        async def stream_response(
            messages: list[object],
            _info: AgentInfo,
        ) -> AsyncIterator[str]:
            prompt = _message_text(messages)
            task_kind = re.search(r"NovelPilot task: ([^\s]+)", prompt)
            assert task_kind is not None
            payload = _task_output(task_kind.group(1), prompt)
            text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
            midpoint = max(1, len(text) // 2)
            yield text[:midpoint]
            yield text[midpoint:]

        model = RequestCountingModel(
            FunctionModel(response, stream_function=stream_response),
            budget=budget,
        )
        return ResolvedModelBinding(model=model, budget=budget, adapter_key="offline-function")


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
        source_book_parent_review_id=source_parent_review_id,
        source_feedback_id=source_feedback_id,
    )

    assert instruction.arc_id is None
    assert instruction.arc_baseline_id is None
    assert instruction.chapter_id is None
    assert instruction.chapter_baseline_id is None
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


def test_delivery_validation_failure_diagnostics_do_not_copy_model_input() -> None:
    secret_model_input = "sk-must-not-enter-delivery-diagnostics"
    with pytest.raises(ValidationError) as captured:
        ArcRepairPatch.model_validate(
            {
                "changes": [
                    {
                        "component": "beats",
                        "value": [],
                        "untrusted_provider_value": secret_model_input,
                    }
                ]
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
        execution = SimpleNamespace(
            has_applied_task=AsyncMock(side_effect=(False, True))
        )
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
        execution = SimpleNamespace(
            has_applied_task=AsyncMock(side_effect=(False, False, True))
        )
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
            get_non_idle_workspace_for_arc=AsyncMock(
                return_value=(chapter, workspace)
            ),
            find_pending_submission=AsyncMock(return_value=None),
            get_review_for_submission=AsyncMock(return_value=None),
            get_review=AsyncMock(return_value=review),
        )
        store = SimpleNamespace(
            chapters=chapters,
            arcs=SimpleNamespace(),
            books=SimpleNamespace(),
            content=SimpleNamespace(get_packed=AsyncMock(return_value=packed_repair)),
            execution=SimpleNamespace(
                has_applied_task=AsyncMock(side_effect=has_applied_task)
            ),
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
            get_non_idle_workspace_for_arc=AsyncMock(
                return_value=(chapter, workspace)
            ),
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
            execution=SimpleNamespace(
                has_applied_task=AsyncMock(side_effect=has_applied_task)
            ),
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


def test_driver_routes_from_arc_outline_without_a_book_chapter_maximum() -> None:
    async def exercise() -> None:
        proposal = _task_output(
            "arc.plan",
            '"arc_ordinal":1 "book_cumulative_committed_chapter_count":0',
        )
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
                        unpack_and_verify=lambda: json.dumps(
                            proposal,
                            ensure_ascii=False,
                        ).encode()
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
        proposal = _task_output(
            "arc.plan",
            '"arc_ordinal":1 "book_cumulative_committed_chapter_count":0',
        )
        assert isinstance(proposal, dict)
        proposal["chapter_outline"] = []
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
                        unpack_and_verify=lambda: json.dumps(
                            proposal,
                            ensure_ascii=False,
                        ).encode()
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


def _message_text(messages: list[object]) -> str:
    fragments: list[str] = []
    for message in messages:
        for part in getattr(message, "parts", ()):
            content = getattr(part, "content", None)
            if isinstance(content, str):
                fragments.append(content)
    return "\n".join(fragments)


def _task_output(task_kind: str, prompt: str) -> dict[str, object] | str:
    if task_kind == "book.discuss":
        if '"selected_title":"《回声证词》"' not in prompt:
            return {
                "reply": "全书方向已经明确，现在需要由创作者确定正式书名。",
                "direction_draft": "一名调查员发现城市会改写证人的记忆，并追查篡改源头。",
                "discussion_summary": "已确定悬疑主线、有限视角、代价与结局方向。",
                "newly_confirmed_decisions": ["主角主动调查记忆篡改"],
                "superseded_decisions": [],
                "unresolved_questions": ["正式书名"],
                "assumptions": [],
                "contradictions": [],
                "newly_selected_title": None,
                "readiness": {
                    "status": "continue",
                    "reason": "正式书名必须由创作者选择。",
                    "question": "这部小说使用哪个正式书名？",
                    "suggestions": [
                        {
                            "label": "回声证词",
                            "message": "使用《回声证词》作为正式书名。",
                            "rationale": "同时指向证词与记忆回响。",
                            "recommended": True,
                            "formal_title": "《回声证词》",
                        },
                        {
                            "label": "被改写的人",
                            "message": "使用《被改写的人》作为正式书名。",
                            "rationale": "强调人物身份危机。",
                            "recommended": False,
                            "formal_title": "《被改写的人》",
                        },
                    ],
                },
            }
        return {
            "reply": "创作方向已经足够明确，可以形成全书规划。",
            "direction_draft": "一名调查员发现城市会改写证人的记忆，并追查篡改源头。",
            "discussion_summary": "已确定悬疑主线、有限视角、代价与结局方向。",
            "newly_confirmed_decisions": ["主角主动调查记忆篡改"],
            "superseded_decisions": [],
            "unresolved_questions": [],
            "assumptions": [],
            "contradictions": [],
            "newly_selected_title": None,
            "readiness": {"status": "ready", "reason": "全书方向已经闭合。"},
        }
    if task_kind in {"book.synthesize", "book.revise"}:
        candidate = {
            "direction": "调查员逐步揭开城市记忆篡改系统，并为恢复真相付出私人代价。",
            "constraints": {
                "genre_reader_promise": "一部证据不断反转、最终能够闭合真相的悬疑长篇。",
                "premise_story_engine": "调查员用不受记忆篡改影响的物证追查被改写的证词。",
                "stable_world_invariants": ["物证不会随人的记忆一同改变。"],
                "stable_character_invariants": ["主角始终主动追查真相并承担选择代价。"],
                "core_selling_points": ["证词与物证之间持续出现可验证的矛盾。"],
                "prohibited_outcomes": ["不得用梦境或幻觉否定已经提交的事实。"],
            },
            "selected_title": "《回声证词》",
            "rolling_plan": {
                "long_term_character_directions": ["主角从相信记忆转为相信可复核证据。"],
                "whole_book_pacing_strategy": "前半建立机制，后半集中回收线索。",
                "ending_tendency": "主角公开真相并承担私人记忆受损的代价。",
                "arc_planning_guidelines": ["每个故事弧都必须留下可供上层验证的正式证据。"],
                "whole_book_scale_guidance": "用户希望大约二十章；这只是软性建议。",
            },
            "completion_contract": {
                "completion_requirements": [
                    {
                        "requirement_key": "truth_exposed",
                        "description": "揭示记忆篡改的来源与运作方式。",
                        "evidence_expectation": "正式章节和 Canon 中存在完整揭示。",
                        "required": True,
                    },
                    {
                        "requirement_key": "cost_paid",
                        "description": "主角完成最终选择并承担其后果。",
                        "evidence_expectation": "终局章节明确写出选择及不可撤销代价。",
                        "required": True,
                    },
                ],
            },
        }
        topology = {
            "arcs": [
                {
                    "whole_book_role": "建立记忆篡改机制。",
                    "core_goal": "形成第一条可复核证据链。",
                    "handoff_from_previous": "承接已批准的悬疑开局。",
                    "exit_conditions": ["物证已经证明记忆篡改机制。"],
                    "is_final": False,
                },
                {
                    "whole_book_role": "解决记忆篡改阴谋。",
                    "core_goal": "揭露操作者并完成主角的最终选择。",
                    "handoff_from_previous": "承接第一故事弧的正式证据。",
                    "exit_conditions": [
                        "操作者已经被揭露。",
                        "主角承担了约定的代价。",
                    ],
                    "is_final": True,
                },
            ]
        }
        candidate[
            "arc_topology_suffix"
            if task_kind == "book.revise"
            else "arc_topology"
        ] = topology
        return candidate
    if task_kind == "book.repair":
        return {
            "changes": [
                {
                    "component": "direction",
                    "value": "调查员补强了证据链，并继续追查城市记忆篡改系统。",
                }
            ]
        }
    if task_kind in {"evaluate.book", "verify_repair.book"}:
        return {
            "decision": "pass",
            "summary": "全书方向、约束与完成合同一致。",
            "findings": [],
            "repair_contract": None,
        }
    if task_kind in {"arc.plan", "arc.revise"}:
        arc_match = re.search(r'"arc_ordinal":(\d+)', prompt)
        ordinal = int(arc_match.group(1)) if arc_match else 1
        committed_match = re.search(
            r'"book_cumulative_committed_chapter_count":(\d+)',
            prompt,
        )
        committed_count = int(committed_match.group(1)) if committed_match else 0
        planned_chapter_count = 10
        return {
            "title": f"第{ordinal}故事弧",
            "desired_state_transition": {
                "start_state": f"第{ordinal}阶段关键证据尚未闭合。",
                "end_state": f"第{ordinal}阶段关键证据已经形成可复核结论。",
            },
            "conflict_trajectory": ["发现矛盾证词", "用物证加压", "锁定阶段责任人"],
            "pacing_trajectory": ["建立疑点", "连续验证", "阶段收束"],
            "character_obligations": ["主角必须因调查结果改变一个重要判断。"],
            "foreshadowing_obligations": ["留下通往下一阶段或终局的可验证线索。"],
            "prohibitions": ["不得推翻既有正式 Canon。"],
            "closure_signals": [
                {
                    "signal_key": "stage_complete",
                    "description": "本阶段核心证据得到解释并形成可复核结论。",
                    "evidence_expectation": "本 Arc 的正式章节观察中存在阶段结论。",
                    "required": True,
                }
            ],
            "chapter_outline": [
                {
                    "title": f"第{committed_count + index + 1}章 阶段证据",
                    "core_event": (
                        f"完成第{ordinal}阶段的第{index + 1}个因果推进。"
                    ),
                    "hook": (
                        "把尚未完成的证据问题交给下一章。"
                        if index + 1 < planned_chapter_count
                        else "把完整阶段证据交给 Arc 收束评估。"
                    ),
                    "scenes": ["发现本章矛盾", "验证并提交一个后果"],
                }
                for index in range(planned_chapter_count)
            ],
        }
    if task_kind == "arc.repair":
        effective_match = re.search(
            r'"arc_planned_after_cumulative_chapter_count":(\d+)',
            prompt,
        )
        closure_match = re.search(
            r'"arc_closure_cumulative_chapter_count":(\d+)',
            prompt,
        )
        effective_count = int(effective_match.group(1)) if effective_match else 0
        closure_count = int(closure_match.group(1)) if closure_match else effective_count + 1
        return {
            "changes": [
                {
                    "component": "chapter_outline",
                    "value": [
                        {
                            "title": f"第{effective_count + index + 1}章 修复证据",
                            "core_event": "重新验证物证并补全证词矛盾。",
                            "hook": "把验证结果交给下一任务或 Arc 收束评估。",
                            "scenes": ["重查物证", "提交修复后的因果结果"],
                        }
                        for index in range(closure_count - effective_count)
                    ],
                }
            ]
        }
    if task_kind in {"evaluate.arc", "verify_repair.arc"}:
        return {
            "guidance_authority_judgment": "not_present",
            "decision": "pass",
            "summary": "故事弧符合全书合同与当前 Canon。",
            "issues": [],
            "repair_scope": [],
        }
    if task_kind in {"chapter.plan", "chapter.revise.plan", "chapter.repair.plan"}:
        chapter_match = re.search(r'"chapter_book_ordinal":(\d+)', prompt)
        ordinal = int(chapter_match.group(1)) if chapter_match else 1
        return {
            "title": f"第{ordinal}章 证词裂缝",
            "purpose": "推进当前故事弧并产生一个可验证的新线索。",
            "scene_beats": ["调查现场", "证词冲突", "物证反转"],
            "required_continuity": ["保留既有角色动机与时间线"],
        }
    if task_kind in {"chapter.draft", "chapter.revise.draft", "chapter.repair.prose"}:
        chapter_match = re.search(r'"chapter_book_ordinal":(\d+)', prompt)
        ordinal = int(chapter_match.group(1)) if chapter_match else 1
        return (
            f"第{ordinal}章里，调查员重新核对证词与现场记录。"
            "同一句话在不同人的记忆中留下了不同顺序，但物证的磨损方向没有改变。"
            "她因此确认这不是普通误记，而是有人刻意改写叙述。"
            "章末，她找到通往下一名责任人的线索，同时意识到自己的记忆也可能被动过。"
        )
    if task_kind in {"chapter.observe", "chapter.revise.observe"}:
        return {
            "summary": "调查员通过不受记忆影响的物证确认了证词被篡改。",
            "established_facts": [
                {
                    "statement": "调查主线继续推进",
                    "evidence_hint": "本章行动直接推进调查。",
                },
                {
                    "statement": "主角开始怀疑自身记忆",
                    "evidence_hint": "本章内心与行动显示这一怀疑。",
                },
            ],
            "canon_proposals": [],
        }
    if task_kind == "chapter.repair.observation":
        return {
            "changes": [
                {
                    "component": "observations",
                    "summary": "修正后的观察与冻结正文一致。",
                    "established_facts": [
                        {
                            "statement": "调查主线继续推进",
                            "evidence_hint": "修复后的事实索引与正文一致。",
                        }
                    ],
                }
            ]
        }
    if task_kind in {"evaluate.chapter", "verify_repair.chapter"}:
        return {
            "guidance_authority_judgment": "not_present",
            "decision": "pass",
            "summary": "章节计划、正文、观察与上游合同一致。",
            "issues": [],
        }
    if task_kind == "evaluate.arc_closure":
        return {
            "signal_statuses": [
                {
                    "signal_key": "stage_complete",
                    "status": "satisfied",
                    "evidence": ["当前 Arc 的十个正式章节已经形成阶段结论。"],
                    "rationale": "冻结章节观察共同满足阶段收束信号。",
                }
            ],
            "arc_contract_judgment": "remains_applicable",
            "book_review_concern": "not_required",
            "chapter_evidence_concern": "not_required",
            "summary": "当前 Arc 契约的必需收束信号均已有正式证据。",
            "issues": [],
            "creator_input_need": None,
        }
    if task_kind == "evaluate.book_completion":
        count_match = re.search(r'"committed_chapter_count":(\d+)', prompt)
        count = int(count_match.group(1)) if count_match else 0
        if count >= 20:
            formal_closure_tag = (
                '<NOVELPILOT_CONTEXT role="formal_outcome" '
                'scope="arc" time="cumulative" use="narrative_evidence" '
                'access="read_only" target="false">'
            )
            assert prompt.count(formal_closure_tag) == 2
            return {
                "requirement_statuses": [
                    {
                        "requirement_key": key,
                        "status": "satisfied",
                        "evidence": ["二十章正式历史已经完成该要求。"],
                        "rationale": "终局 Arc 的正式收束证据足以支持完成。",
                    }
                    for key in ("truth_exposed", "cost_paid")
                ],
                "book_contract_judgment": "remains_applicable",
                "summary": "全书要求和终局方向均已满足。",
                "issues": [],
                "creator_input_need": None,
            }
        raise AssertionError(
            "Book completion must not run before the planned final Arc."
        )
    raise AssertionError(f"Unhandled offline task kind: {task_kind}")


def _write_profile(
    path: Path,
    *,
    capability_source: str = "pydantic-ai-capability-v1",
) -> None:
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
                            "source": capability_source,
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
    _write_profile(
        profile_path,
        capability_source="legacy-responses-capability-v1",
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
            executor = AgentExecutor(
                engine,
                registry=DEFAULT_TASK_REGISTRY,
                resolver=DeterministicNovelResolver(),
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
                        ).where(
                            generation_runs.c.id == created.result.generation_run_id
                        )
                    )
                ).one()
                task = (
                    await connection.execute(
                        select(
                            agent_tasks.c.id,
                            agent_tasks.c.status,
                            agent_tasks.c.delivery_state,
                        ).where(
                            agent_tasks.c.project_id == created.result.project_id
                        )
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
                        ).where(
                            agent_task_attempts.c.project_id == created.result.project_id
                        )
                    )
                ).one()
                attempt_count = int(
                    (
                        await connection.execute(
                            select(func.count()).select_from(agent_task_attempts).where(
                                agent_task_attempts.c.project_id
                                == created.result.project_id
                            )
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

            profile_document = json.loads(profile_path.read_text(encoding="utf-8"))
            profile_document["profiles"][0]["capability_test"]["source"] = (
                "pydantic-ai-capability-v1"
            )
            profile_path.write_text(
                json.dumps(profile_document, ensure_ascii=False),
                encoding="utf-8",
            )
            retried = await RunControlService(bus).retry_failed_task(
                RetryFailedTaskRequest(
                    project_id=created.result.project_id,
                    run_id=created.result.generation_run_id,
                    expected_lock_version=run.lock_version,
                    task_id=task.id,
                ),
                idempotency_key="retry-stale-profile-task",
            )
            assert retried.result.status == "running"
            assert await run_engine.run_once()
            assert await run_engine.run_once()

            async with engine.connect() as connection:
                recovered_run = (
                    await connection.execute(
                        select(
                            generation_runs.c.status,
                            generation_runs.c.failure_code,
                        ).where(
                            generation_runs.c.id == created.result.generation_run_id
                        )
                    )
                ).one()
                attempts = (
                    await connection.execute(
                        select(
                            agent_task_attempts.c.attempt_number,
                            agent_task_attempts.c.retry_kind,
                            agent_task_attempts.c.status,
                            agent_task_attempts.c.error_code,
                        )
                        .where(
                            agent_task_attempts.c.project_id
                            == created.result.project_id
                        )
                        .order_by(agent_task_attempts.c.attempt_number)
                    )
                ).all()

            assert tuple(recovered_run) == ("waiting_for_user", None)
            assert [tuple(item) for item in attempts] == [
                (1, "initial", "failed", "profile_capability_missing"),
                (2, "user_retry", "succeeded", None),
            ]
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
                            arc_planning_guidelines=[
                                "Close only after committed evidence."
                            ],
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
                raise CommandPreconditionError(
                    "Repair patch changed an unauthorized component."
                )

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
                            domain_events.c.aggregate_id
                            == created.result.generation_run_id,
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
                        ).where(
                            agent_task_attempts.c.id == retried.result.attempt_id
                        )
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
                        ).where(
                            generation_runs.c.id
                            == created.result.generation_run_id
                        )
                    )
                ).one()
                task_count = (
                    await connection.execute(
                        select(func.count()).select_from(agent_tasks).where(
                            agent_tasks.c.run_id
                            == created.result.generation_run_id
                        )
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
            assert (
                details["failed_invariant"]
                == "fixture_authority_input_present"
            )
            assert [
                item["exception_type"] for item in details["cause_chain"]
            ] == ["ContextAssemblyError", "ContextFactError"]
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
                        ).where(
                            generation_runs.c.id
                            == created.result.generation_run_id
                        )
                    )
                ).one()
            assert tuple(resumed) == (None, None, None, None, None)
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.parametrize("operation_mode", ["full_auto", "participatory"])
def test_synthetic_driver_completes_twenty_chapter_book_with_only_product_gates(
    tmp_path: Path,
    operation_mode: str,
) -> None:
    database = tmp_path / f"driver-{operation_mode}.sqlite3"
    profile_path = tmp_path / f"profiles-{operation_mode}.json"
    command.upgrade(alembic_config(database), "head")
    _write_profile(profile_path)

    async def exercise() -> tuple[int, int, int, str]:
        engine = create_sqlite_async_engine(database)
        try:
            bus = CommandBus(engine)
            created = await ProjectCommandService(bus).create_project(
                CreateProjectRequest(
                    project_id=f"project-{operation_mode}",
                    creator_brief="写一部约二十章、围绕记忆篡改证词展开的悬疑长篇。",
                    operation_mode=operation_mode,
                    default_profile_id="offline-profile",
                ),
                idempotency_key=f"create:{operation_mode}",
            )
            run_service = RunControlService(bus)
            await run_service.start(
                RunControlRequest(
                    project_id=created.result.project_id,
                    run_id=created.result.generation_run_id,
                    expected_lock_version=1,
                ),
                idempotency_key=f"start:{operation_mode}",
            )
            executor = AgentExecutor(
                engine,
                registry=DEFAULT_TASK_REGISTRY,
                resolver=DeterministicNovelResolver(),
            )
            driver = DomainRunDriver(
                engine,
                profile_catalog=ProfileCatalog(profile_path),
                executor=executor,
            )
            book_gate_count = 0
            arc_gate_count = 0
            for step in range(1_000):
                async with UnitOfWork(engine) as store:
                    run = await store.runs.get_open_for_project(created.result.project_id)
                    if run is None:
                        break
                    book = await store.books.get_for_project(created.result.project_id)
                    assert book is not None
                    if run.status == "waiting_for_user":
                        if run.wait_reason_code == "book_direction_input":
                            workspace = await store.books.get_workspace(
                                project_id=created.result.project_id,
                                book_id=book.id,
                            )
                            assert workspace is not None
                            state = BookDiscussionState.model_validate_json(
                                (
                                    await store.content.get_packed(
                                        project_id=created.result.project_id,
                                        ref_id=workspace.discussion_state_ref_id,
                                    )
                                ).unpack_and_verify()
                            )
                            suggestion = next(item for item in state.suggestions if item.recommended)
                            input_request = RecordBookUserInputRequest(
                                project_id=created.result.project_id,
                                book_id=book.id,
                                expected_workspace_lock_version=workspace.lock_version,
                                message=suggestion.message,
                                suggestion_id=suggestion.id,
                            )
                            action = ("book_input", input_request)
                        elif run.wait_reason_code == "book_approval_required":
                            pending = await store.books.find_pending_submission(
                                project_id=created.result.project_id,
                                book_id=book.id,
                            )
                            review = await store.books.get_latest_review(
                                project_id=created.result.project_id,
                                book_id=book.id,
                            )
                            assert pending is not None and review is not None
                            book_request = ApproveBookRequest(
                                project_id=created.result.project_id,
                                book_id=book.id,
                                submission_id=pending.id,
                                review_id=review.id,
                                expected_current_baseline_id=book.current_baseline_id,
                            )
                            action = ("book", book_request)
                        elif run.wait_reason_code == "arc_approval_required":
                            arc = await store.arcs.get_unfinished_for_book(
                                project_id=created.result.project_id,
                                book_id=book.id,
                            )
                            assert arc is not None
                            pending = await store.arcs.find_pending_submission(
                                project_id=created.result.project_id,
                                arc_id=arc.id,
                            )
                            review = await store.arcs.get_latest_review(
                                project_id=created.result.project_id,
                                arc_id=arc.id,
                            )
                            gate = await store.arcs.find_pending_gate(
                                project_id=created.result.project_id,
                                arc_id=arc.id,
                            )
                            assert pending is not None and review is not None and gate is not None
                            arc_request = ApproveArcRequest(
                                project_id=created.result.project_id,
                                book_id=book.id,
                                arc_id=arc.id,
                                submission_id=pending.id,
                                review_id=review.id,
                                approval_gate_id=gate.id,
                                expected_current_baseline_id=arc.current_baseline_id,
                            )
                            action = ("arc", arc_request)
                        else:
                            raise AssertionError(f"Unexpected product wait: {run.wait_reason_code}")
                    elif run.status == "running":
                        action = ("driver", run)
                    else:
                        raise AssertionError(f"Unexpected Run status: {run.status}")
                if action[0] == "book_input":
                    await BookCommandService(bus).record_user_input(
                        action[1],
                        idempotency_key=f"book-input:{operation_mode}",
                    )
                elif action[0] == "book":
                    book_gate_count += 1
                    await BookCommandService(bus).approve_and_commit(
                        action[1],
                        idempotency_key=f"approve-book:{operation_mode}:{book_gate_count}",
                    )
                elif action[0] == "arc":
                    arc_gate_count += 1
                    await ArcCommandService(bus).approve_and_commit(
                        action[1],
                        idempotency_key=f"approve-arc:{operation_mode}:{arc_gate_count}",
                    )
                else:
                    await driver.drive_one(action[1])
            else:
                raise AssertionError("Offline whole-book driver exceeded its step budget.")

            async with engine.connect() as connection:
                chapter_count = await connection.scalar(
                    select(func.count()).select_from(chapter_baselines)
                )
                project_status = await connection.scalar(
                    select(projects.c.lifecycle_status).where(
                        projects.c.id == created.result.project_id
                    )
                )
                completed_run = await connection.scalar(
                    select(generation_runs.c.status).where(
                        generation_runs.c.id == created.result.generation_run_id
                    )
                )
                stored_book_approvals = await connection.scalar(
                    select(func.count()).select_from(book_approvals)
                )
                stored_arc_gates = await connection.scalar(
                    select(func.count()).select_from(arc_approval_gates)
                )
                arc_checkpoints = list(
                    (
                        await connection.scalars(
                            select(
                                arc_baselines.c.closure_cumulative_chapter_count
                            ).order_by(
                                arc_baselines.c.closure_cumulative_chapter_count
                            )
                        )
                    ).all()
                )
                closure_counts = list(
                    (
                        await connection.scalars(
                            select(
                                arc_closures.c.cumulative_committed_chapter_count
                            ).order_by(
                                arc_closures.c.cumulative_committed_chapter_count
                            )
                        )
                    ).all()
                )
            assert chapter_count is not None
            assert stored_book_approvals == book_gate_count
            assert stored_arc_gates is not None
            assert completed_run == "completed"
            assert arc_checkpoints == [10, 20]
            assert closure_counts == [10, 20]
            return int(chapter_count), book_gate_count, int(stored_arc_gates), str(project_status)
        finally:
            await engine.dispose()

    chapter_count, book_gates, arc_gates, project_status = asyncio.run(exercise())
    assert chapter_count == 20
    assert book_gates == 1
    assert arc_gates == (0 if operation_mode == "full_auto" else 2)
    assert project_status == "completed"
