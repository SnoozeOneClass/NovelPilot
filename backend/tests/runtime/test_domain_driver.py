from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from alembic import command
from pydantic import ValidationError
from pydantic_ai import ModelResponse, RequestUsage, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from sqlalchemy import func, select

from app.agents.binding import ProfileCredential, ResolvedModelBinding
from app.agents.contracts import ProfileCapabilities
from app.agents.executor import AgentExecutor
from app.agents.registry import DEFAULT_TASK_REGISTRY
from app.agents.transport import ActivationRequestBudget, RequestCountingModel
from app.db.engine import create_sqlite_async_engine
from app.db.maintenance import alembic_config
from app.db.schema import (
    agent_task_attempts,
    agent_tasks,
    arc_approval_gates,
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
    BookCandidatePack,
    BookDiscussionState,
    CompletionContract,
    RecordBookUserInputRequest,
)
from app.domain.commands import CommandPreconditionError
from app.domain.projects import CreateProjectRequest, ProjectCommandService
from app.profiles import ProfileCatalog, profile_configuration_fingerprint
from app.runtime.control import (
    RetryFailedTaskRequest,
    RunControlRequest,
    RunControlService,
)
from app.runtime.driver import DomainRunDriver, _normalize_delivery_failure
from app.runtime.engine import RunEngine
from app.runtime.reconcile import ReconcileService
from app.store.command_bus import CommandBus
from app.store.content import ContentRepository
from tests.helpers.lifecycle_seed import insert_successful_task


class DeterministicNovelResolver:
    """Offline Pydantic AI model that exercises the real Executor and task contracts."""

    def resolve(
        self,
        *,
        profile: object,
        expected_profile_fingerprint: str,
        required_capabilities: object,
        model_request_limit: int,
        credential: ProfileCredential,
    ) -> ResolvedModelBinding:
        del profile, expected_profile_fingerprint, required_capabilities, credential
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
            assert isinstance(payload, str)
            midpoint = max(1, len(payload) // 2)
            yield payload[:midpoint]
            yield payload[midpoint:]

        model = RequestCountingModel(
            FunctionModel(response, stream_function=stream_response),
            budget=budget,
        )
        return ResolvedModelBinding(model=model, budget=budget, adapter_key="offline-function")


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
        return {
            "direction": "调查员逐步揭开城市记忆篡改系统，并为恢复真相付出私人代价。",
            "constraints": {
                "pov": "limited-third",
                "tone": "suspense",
                "continuity": "每章线索必须可回溯",
            },
            "selected_title": "《回声证词》",
            "rolling_plan": {"strategy": "one-arc-at-a-time", "arc_chapters": 2},
            "completion_contract": {
                "minimum_chapter_count": 18,
                "maximum_chapter_count": 22,
                "completion_requirements": ["揭示篡改源头", "主角承担最终选择的后果"],
            },
        }
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
        return {
            "title": f"第{ordinal}故事弧",
            "purpose": f"推进第{ordinal}阶段调查并留下可验证的新证据。",
            "beats": ["发现矛盾证词", "验证物证", "确认下一层责任人"],
            "target_chapter_count": 2,
            "completion_signals": ["本弧核心证据得到解释"],
        }
    if task_kind == "arc.repair":
        return {
            "changes": [
                {
                    "component": "beats",
                    "value": ["重新验证物证", "补全证词矛盾", "确认下一层责任人"],
                }
            ]
        }
    if task_kind in {"evaluate.arc", "verify_repair.arc"}:
        return {
            "decision": "pass",
            "summary": "故事弧符合全书合同与当前 Canon。",
            "issues": [],
            "repair_scope": [],
        }
    if task_kind in {"chapter.plan", "chapter.revise.plan"}:
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
            "continuity_observations": ["调查主线继续推进", "主角开始怀疑自身记忆"],
            "canon_proposals": [],
        }
    if task_kind == "chapter.repair.observation":
        return {
            "changes": [
                {
                    "component": "observations",
                    "summary": "修正后的观察与冻结正文一致。",
                    "continuity_observations": ["调查主线继续推进"],
                }
            ]
        }
    if task_kind in {"evaluate.chapter", "verify_repair.chapter"}:
        return {
            "decision": "pass",
            "summary": "章节计划、正文、观察与上游合同一致。",
            "issues": [],
            "repair_scope": [],
            "escalation_target": None,
        }
    if task_kind == "book.assess_progress_or_completion":
        count_match = re.search(r'"committed_chapter_count":(\d+)', prompt)
        count = int(count_match.group(1)) if count_match else 0
        if count >= 20:
            return {
                "decision": "complete",
                "rationale": "二十章已经满足完成合同，主谜题和人物代价均已闭合。",
                "unresolved_requirements": [],
            }
        return {
            "decision": "continue",
            "rationale": "尚未达到二十章稳定收束点，继续规划下一故事弧。",
            "unresolved_requirements": ["继续推进至二十章并闭合最终选择"],
        }
    raise AssertionError(f"Unhandled offline task kind: {task_kind}")


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
                    constraints={},
                    selected_title="Rejected Delivery",
                    rolling_plan={},
                    completion_contract=CompletionContract(
                        minimum_chapter_count=1,
                        maximum_chapter_count=2,
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


@pytest.mark.parametrize("operation_mode", ["full_auto", "participatory"])
def test_driver_completes_twenty_chapter_book_with_only_product_gates(
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
                                target_chapter_count=pending.recommended_target_chapter_count,
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
            assert chapter_count is not None
            assert stored_book_approvals == book_gate_count
            assert stored_arc_gates is not None
            assert completed_run == "completed"
            return int(chapter_count), book_gate_count, int(stored_arc_gates), str(project_status)
        finally:
            await engine.dispose()

    chapter_count, book_gates, arc_gates, project_status = asyncio.run(exercise())
    assert chapter_count == 20
    assert book_gates == 1
    assert arc_gates == (0 if operation_mode == "full_auto" else 10)
    assert project_status == "completed"
