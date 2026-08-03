from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Iterator

import httpx
import pytest
from alembic import command
from pydantic_ai import ModelResponse
from pydantic_ai.exceptions import ModelHTTPError, UnexpectedModelBehavior
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.function import AgentInfo, FunctionModel
from sqlalchemy import select

from app.agents.binding import (
    ModelBindingResolver,
    ProfileCredential,
    ResolvedModelBinding,
)
from app.agents.contracts import ProfileCapabilities, ProfileSnapshot
from app.agents.executor import (
    AgentExecutionResult,
    AgentExecutor,
    AgentLiveEvent,
    classify_execution_error,
    retryable_provider_failure,
)
from app.agents.registry import DEFAULT_TASK_REGISTRY
from app.agents.roles import build_agent
from app.agents.transport import (
    ActivationRequestBudget,
    ProviderEmptyOutput,
    ProviderOutputTruncated,
    ProviderStreamIncomplete,
    RequestCountingModel,
    build_observed_transport,
    raise_for_incomplete_stream,
)
from app.db.engine import create_sqlite_async_engine
from app.db.maintenance import alembic_config
from app.db.uow import UnitOfWork
from app.db.schema import (
    agent_evidence_items,
    agent_task_attempts,
    agent_tasks,
    content_refs,
)
from app.domain.projects import CreateProjectRequest, ProjectCommandService
from app.store.agent_tasks import AgentTaskStore
from app.store.command_bus import CommandBus
from app.store.execution import AttemptSummaryRecord, ExecutionRepository


pytestmark = pytest.mark.synthetic_integration


class RecordingLivePublisher:
    def __init__(self) -> None:
        self.events: list[AgentLiveEvent] = []

    async def publish(self, event: AgentLiveEvent) -> None:
        self.events.append(event)


def _responses_completed_payload(
    *,
    response_id: str,
    output: list[dict[str, object]],
) -> dict[str, object]:
    return {
        "id": response_id,
        "created_at": 1.0,
        "model": "loopback-responses-model",
        "object": "response",
        "output": output,
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
        "status": "completed",
    }


def _sse_body(events: list[dict[str, object]]) -> bytes:
    chunks = [
        "data: " + json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n\n"
        for event in events
    ]
    chunks.append("data: [DONE]\n\n")
    return "".join(chunks).encode("utf-8")


@contextmanager
def _thinking_then_result_responses_server() -> Iterator[
    tuple[str, list[dict[str, object]]]
]:
    requests: list[dict[str, object]] = []
    valid_result = json.dumps(
        {
            "decision": "pass",
            "summary": "The responsible Arc reaches the Book requirement.",
            "findings": [],
            "requirement_coverage": [
                {
                    "requirement_key": "ending_resolved",
                    "judgment": "aligned",
                    "rationale": "The final Arc establishes the required ending.",
                }
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:  # noqa: N802 - stdlib handler API.
            content_length = int(self.headers.get("Content-Length", "0"))
            requests.append(
                json.loads(self.rfile.read(content_length).decode("utf-8"))
            )
            sequence = len(requests)
            if sequence == 1:
                reasoning_item: dict[str, object] = {
                    "id": "rs_loopback_1",
                    "summary": [
                        {"text": "Reasoning completed without a final answer.", "type": "summary_text"}
                    ],
                    "type": "reasoning",
                    "status": "completed",
                }
                events = [
                    {
                        "type": "response.reasoning_summary_text.delta",
                        "delta": "Reasoning completed without a final answer.",
                        "item_id": "rs_loopback_1",
                        "output_index": 0,
                        "sequence_number": 0,
                        "summary_index": 0,
                    },
                    {
                        "type": "response.completed",
                        "response": _responses_completed_payload(
                            response_id="resp_loopback_1",
                            output=[reasoning_item],
                        ),
                        "sequence_number": 1,
                    },
                ]
            else:
                output_item: dict[str, object] = {
                    "id": "msg_loopback_2",
                    "content": [
                        {
                            "annotations": [],
                            "text": valid_result,
                            "type": "output_text",
                        }
                    ],
                    "role": "assistant",
                    "status": "completed",
                    "type": "message",
                }
                events = [
                    {
                        "type": "response.output_text.delta",
                        "content_index": 0,
                        "delta": valid_result,
                        "item_id": "msg_loopback_2",
                        "logprobs": [],
                        "output_index": 0,
                        "sequence_number": 0,
                    },
                    {
                        "type": "response.output_text.done",
                        "content_index": 0,
                        "item_id": "msg_loopback_2",
                        "logprobs": [],
                        "output_index": 0,
                        "sequence_number": 1,
                        "text": valid_result,
                    },
                    {
                        "type": "response.completed",
                        "response": _responses_completed_payload(
                            response_id="resp_loopback_2",
                            output=[output_item],
                        ),
                        "sequence_number": 2,
                    },
                ]
            body = _sse_body(events)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()

        def log_message(self, _format: str, *_args: object) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}/v1", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class FunctionBindingResolver:
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

        async def response(
            _messages: list[object],
            info: AgentInfo,
        ) -> AsyncIterator[str]:
            assert info.model_request_parameters.output_mode == "native"
            yield (
                '{"decision":"pass","summary":"The Book contract is coherent.",'
                '"findings":[],"requirement_coverage":[{'
                '"requirement_key":"ending_resolved","judgment":"aligned",'
                '"rationale":"The responsible Arc reaches the requirement."}]}'
            )

        model = RequestCountingModel(
            FunctionModel(stream_function=response),
            budget=budget,
        )
        return ResolvedModelBinding(model=model, budget=budget, adapter_key="function-test")


class InvalidBookBindingResolver:
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

        async def response(
            _messages: list[object],
            info: AgentInfo,
        ) -> AsyncIterator[str]:
            assert info.model_request_parameters.output_mode == "native"
            yield (
                '{"reply":"not-persisted-secret","direction_draft":"A memory mystery.",'
                '"discussion_summary":"One creator decision remains.",'
                '"newly_confirmed_decisions":[],"superseded_decisions":[],'
                '"unresolved_questions":["protagonist"],"assumptions":[],'
                '"contradictions":[],"newly_selected_title":null,'
                '"readiness":{"status":"continue","reason":"The protagonist is open.",'
                '"question":"Who carries the investigation",'
                '"suggestions":[{"label":"Witness","message":"Use the witness.",'
                '"rationale":"","recommended":true,"formal_title":null}]}}'
            )

        model = RequestCountingModel(
            FunctionModel(stream_function=response),
            budget=budget,
        )
        return ResolvedModelBinding(model=model, budget=budget, adapter_key="invalid-book-test")


class AlwaysReadTimeoutBindingResolver:
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
        budget = ActivationRequestBudget(
            model_request_limit=model_request_limit,
            protocol="openai_responses",
        )

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("provider stream stalled", request=request)

        client = httpx.AsyncClient(
            transport=build_observed_transport(
                budget=budget,
                wrapped=httpx.MockTransport(handler),
            )
        )

        async def response(
            _messages: list[object],
            _info: AgentInfo,
        ) -> AsyncIterator[str]:
            await client.post("https://provider.example/v1/responses", json={})
            yield "unreachable"

        model = RequestCountingModel(
            FunctionModel(stream_function=response),
            budget=budget,
        )
        return ResolvedModelBinding(
            model=model,
            budget=budget,
            adapter_key="openai_responses",
            _http_client=client,
        )


class AlwaysEmptyBindingResolver:
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
        budget = ActivationRequestBudget(
            model_request_limit=model_request_limit,
            protocol="openai_responses",
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, request=request, content=b" ")

        client = httpx.AsyncClient(
            transport=build_observed_transport(
                budget=budget,
                wrapped=httpx.MockTransport(handler),
            )
        )

        async def response(
            _messages: list[object],
            _info: AgentInfo,
        ) -> AsyncIterator[str]:
            async with client.stream(
                "POST",
                "https://provider.example/v1/responses",
            ) as provider_response:
                async for chunk in provider_response.aiter_bytes():
                    if chunk:
                        yield chunk.decode("utf-8")

        return ResolvedModelBinding(
            model=RequestCountingModel(
                FunctionModel(stream_function=response),
                budget=budget,
            ),
            budget=budget,
            adapter_key="openai_responses",
            _http_client=client,
        )


class AttemptTextStream(httpx.AsyncByteStream):
    def __init__(
        self,
        *,
        request: httpx.Request,
        content: bytes,
        interrupt: bool,
    ) -> None:
        self._request = request
        self._content = content
        self._interrupt = interrupt

    async def __aiter__(self):
        yield self._content
        if self._interrupt:
            raise httpx.ReadTimeout("stream interrupted", request=self._request)

    async def aclose(self) -> None:
        return None


def test_executor_persists_complete_task_evidence_without_token_deltas(tmp_path: Path) -> None:
    database = tmp_path / "agent-executor.sqlite3"
    command.upgrade(alembic_config(database), "head")

    async def exercise() -> tuple[
        list[str], tuple[object, ...], list[str], list[AttemptSummaryRecord]
    ]:
        engine = create_sqlite_async_engine(database)
        try:
            created = await ProjectCommandService(CommandBus(engine)).create_project(
                CreateProjectRequest(
                    project_id="project-a",
                    creator_brief="A mystery about contradictory memory testimony.",
                    operation_mode="full_auto",
                ),
                idempotency_key="create-project-a",
            )
            profile = ProfileSnapshot.create(
                profile_id="function-profile",
                display_name="Function test profile",
                api_family="openai_responses",
                base_url="https://provider.example/v1",
                model_id="opaque-test-model",
                capabilities=ProfileCapabilities(
                    text_streaming=True,
                    native_json_schema=True,
                ),
            )
            plan = DEFAULT_TASK_REGISTRY.freeze_plan(
                task_id="evaluate-book-task",
                project_id="project-a",
                run_id=created.result.generation_run_id,
                task_key="evaluate.book:workspace:1",
                action_key="evaluate.book",
                role="evaluator",
                task_kind="evaluate.book",
                contract_version=1,
                book_id=created.result.book_id,
                canon_baseline_id=created.result.canon_baseline_id,
                semantic_goal="Evaluate a frozen Book candidate.",
                prompt="Evaluate the supplied frozen Book candidate.",
                context_manifest={"candidate": {"direction": "Memory changes testimony."}},
                profile_snapshot=profile,
                workspace_lock_version=1,
                workspace_work_cycle_id="evaluate-book-work-cycle",
            )
            await AgentTaskStore(engine).create_initial(
                plan=plan,
                attempt_id="evaluate-book-attempt",
                created_at_ms=20,
            )
            live = RecordingLivePublisher()
            executor = AgentExecutor(
                engine,
                registry=DEFAULT_TASK_REGISTRY,
                resolver=FunctionBindingResolver(),
                live_publisher=live,
                now_ms=lambda: 100,
            )
            result = await executor.execute(
                project_id="project-a",
                task_id=plan.task_id,
                attempt_id="evaluate-book-attempt",
                owner_instance_id="test-engine",
                lease_token="lease-a",
                credential=ProfileCredential.from_plaintext("not-persisted"),
            )
            assert result.status == "succeeded"
            assert result.input_tokens == 50
            assert result.output_tokens == 25

            async with engine.connect() as connection:
                task_row = (
                    await connection.execute(
                        select(
                            agent_tasks.c.status,
                            agent_tasks.c.delivery_state,
                            agent_tasks.c.successful_attempt_id,
                        ).where(agent_tasks.c.id == plan.task_id)
                    )
                ).one()
                task_plan_schema = (
                    await connection.execute(
                        select(
                            content_refs.c.schema_id,
                            content_refs.c.schema_version,
                        )
                        .select_from(
                            agent_tasks.join(
                                content_refs,
                                agent_tasks.c.task_plan_ref_id == content_refs.c.id,
                            )
                        )
                        .where(agent_tasks.c.id == plan.task_id)
                    )
                ).one()
                assert tuple(task_plan_schema) == ("agent-task-plan", 4)
                attempt_row = (
                    await connection.execute(
                        select(
                            agent_task_attempts.c.status,
                            agent_task_attempts.c.result_ref_id,
                            agent_task_attempts.c.input_tokens,
                            agent_task_attempts.c.output_tokens,
                            agent_task_attempts.c.provider_request_count,
                        ).where(agent_task_attempts.c.id == "evaluate-book-attempt")
                    )
                ).one()
                evidence = list(
                    (
                        await connection.scalars(
                            select(agent_evidence_items.c.item_kind)
                            .where(agent_evidence_items.c.attempt_id == "evaluate-book-attempt")
                            .order_by(agent_evidence_items.c.sequence_number)
                        )
                    ).all()
                )
                summaries = await ExecutionRepository(connection).list_attempt_summaries(
                    project_id="project-a"
                )
            return (
                [event.kind for event in live.events],
                tuple(task_row) + tuple(attempt_row),
                evidence,
                summaries,
            )
        finally:
            await engine.dispose()

    live_kinds, rows, evidence, summaries = asyncio.run(exercise())

    assert live_kinds == ["task_started", "task_succeeded"]
    assert rows == (
        "succeeded",
        "pending",
        "evaluate-book-attempt",
        "succeeded",
        rows[4],
        50,
        25,
        0,
    )
    assert rows[4] is not None
    assert evidence == ["completion_message", "validation"]
    assert all("delta" not in kind for kind in evidence)
    assert len(summaries) == 1
    assert summaries[0].task_kind == "evaluate.book"
    assert summaries[0].attempt_status == "succeeded"
    assert summaries[0].total_tokens == 75
    assert len(summaries[0].profile_fingerprint) == 64
    assert summaries[0].model_id == "opaque-test-model"
    assert summaries[0].harness_policy_id == "novelpilot-domain-harness"


def test_executor_persists_failed_typed_output_messages_and_exact_validation(
    tmp_path: Path,
) -> None:
    database = tmp_path / "agent-executor-invalid.sqlite3"
    command.upgrade(alembic_config(database), "head")

    async def exercise() -> tuple[
        AgentExecutionResult,
        list[str],
        str,
        str,
        list[str],
        tuple[object, ...],
    ]:
        engine = create_sqlite_async_engine(database)
        try:
            created = await ProjectCommandService(CommandBus(engine)).create_project(
                CreateProjectRequest(
                    project_id="project-invalid-book",
                    creator_brief="A mystery about edited testimony.",
                    operation_mode="full_auto",
                ),
                idempotency_key="create-project-invalid-book",
            )
            profile = ProfileSnapshot.create(
                profile_id="invalid-book-profile",
                display_name="Invalid Book test profile",
                api_family="openai_responses",
                base_url="https://provider.example/v1",
                model_id="opaque-test-model",
                capabilities=ProfileCapabilities(
                    text_streaming=True,
                    native_json_schema=True,
                ),
            )
            plan = DEFAULT_TASK_REGISTRY.freeze_plan(
                task_id="book-discussion-invalid-task",
                project_id="project-invalid-book",
                run_id=created.result.generation_run_id,
                task_key="book.discuss:workspace:1",
                action_key="book.discuss",
                role="book_strategist",
                task_kind="book.discuss",
                contract_version=1,
                book_id=created.result.book_id,
                canon_baseline_id=created.result.canon_baseline_id,
                semantic_goal="Advance one Book discussion decision.",
                prompt="Advance the supplied Book discussion.",
                context_manifest={"creator_brief": "A mystery about edited testimony."},
                profile_snapshot=profile,
                workspace_lock_version=1,
                workspace_work_cycle_id="book-discussion-work-cycle",
            )
            await AgentTaskStore(engine).create_initial(
                plan=plan,
                attempt_id="book-discussion-invalid-attempt",
                created_at_ms=20,
            )
            live = RecordingLivePublisher()
            result = await AgentExecutor(
                engine,
                registry=DEFAULT_TASK_REGISTRY,
                resolver=InvalidBookBindingResolver(),
                live_publisher=live,
                now_ms=lambda: 100,
            ).execute(
                project_id="project-invalid-book",
                task_id=plan.task_id,
                attempt_id="book-discussion-invalid-attempt",
                owner_instance_id="test-engine",
                lease_token="lease-invalid",
                credential=ProfileCredential.from_plaintext("not-persisted-secret"),
            )

            async with engine.connect() as connection:
                attempt_row = (
                    await connection.execute(
                        select(
                            agent_task_attempts.c.status,
                            agent_task_attempts.c.error_code,
                            agent_task_attempts.c.error_category,
                            agent_task_attempts.c.input_tokens,
                            agent_task_attempts.c.output_tokens,
                            agent_task_attempts.c.total_tokens,
                            agent_task_attempts.c.usage_ref_id,
                            agent_task_attempts.c.diagnostic_ref_id,
                        ).where(
                            agent_task_attempts.c.id
                            == "book-discussion-invalid-attempt"
                        )
                    )
                ).one()
                evidence_rows = list(
                    (
                        await connection.execute(
                            select(
                                agent_evidence_items.c.item_kind,
                                agent_evidence_items.c.content_ref_id,
                            )
                            .where(
                                agent_evidence_items.c.attempt_id
                                == "book-discussion-invalid-attempt"
                            )
                            .order_by(agent_evidence_items.c.sequence_number)
                        )
                    ).all()
                )

            refs = {kind: ref_id for kind, ref_id in evidence_rows if ref_id is not None}
            async with UnitOfWork(engine) as store:
                messages = (
                    await store.content.get_packed(
                        project_id="project-invalid-book",
                        ref_id=refs["completion_message"],
                    )
                ).unpack_and_verify().decode("utf-8")
                diagnostic = (
                    await store.content.get_packed(
                        project_id="project-invalid-book",
                        ref_id=refs["diagnostic_attachment"],
                    )
                ).unpack_and_verify().decode("utf-8")

            return (
                result,
                [event.kind for event in live.events],
                messages,
                diagnostic,
                [kind for kind, _ref_id in evidence_rows],
                tuple(attempt_row),
            )
        finally:
            await engine.dispose()

    result, live_kinds, messages, diagnostic, evidence, attempt_row = asyncio.run(exercise())

    assert result.status == "failed"
    assert result.error_code == "typed_output_invalid"
    assert result.input_tokens == 100
    assert result.output_tokens == 106
    assert live_kinds == ["task_started", "task_failed"]
    assert evidence[:3] == [
        "completion_message",
        "validation",
        "diagnostic_attachment",
    ]
    assert attempt_row[:6] == (
        "failed",
        "typed_output_invalid",
        "output_validation",
        100,
        106,
        206,
    )
    assert attempt_row[6] is not None
    assert attempt_row[7] is not None

    assert "not-persisted-secret" not in messages
    assert "not-persisted-secret" not in diagnostic
    assert "[REDACTED]" in messages
    assert "thinking" not in messages
    validation_errors = json.loads(diagnostic)["validation_errors"]
    assert "suggestions" in json.dumps(validation_errors)
    assert "at least 2 items" in json.dumps(validation_errors)


def test_transient_failure_exhaustion_is_six_visible_requests_and_terminal_failure(
    tmp_path: Path,
) -> None:
    database = tmp_path / "agent-executor-retry-exhaustion.sqlite3"
    command.upgrade(alembic_config(database), "head")

    async def no_sleep(_delay: float) -> None:
        return None

    async def exercise() -> tuple[AgentExecutionResult, list[AgentLiveEvent], dict[str, object]]:
        engine = create_sqlite_async_engine(database)
        try:
            created = await ProjectCommandService(CommandBus(engine)).create_project(
                CreateProjectRequest(
                    project_id="project-retry-exhaustion",
                    creator_brief="A mystery about an unreachable archive.",
                    operation_mode="full_auto",
                ),
                idempotency_key="create-project-retry-exhaustion",
            )
            profile = ProfileSnapshot.create(
                profile_id="retry-profile",
                display_name="Retry profile",
                api_family="openai_responses",
                base_url="https://provider.example/v1",
                model_id="opaque-test-model",
                capabilities=ProfileCapabilities(
                    text_streaming=True,
                    native_json_schema=True,
                ),
            )
            plan = DEFAULT_TASK_REGISTRY.freeze_plan(
                task_id="retry-exhaustion-task",
                project_id="project-retry-exhaustion",
                run_id=created.result.generation_run_id,
                task_key="evaluate.book:retry-exhaustion",
                action_key="evaluate.book",
                role="evaluator",
                task_kind="evaluate.book",
                contract_version=1,
                book_id=created.result.book_id,
                canon_baseline_id=created.result.canon_baseline_id,
                semantic_goal="Evaluate a frozen Book candidate.",
                prompt="Evaluate the supplied frozen Book candidate.",
                context_manifest={"candidate": {"direction": "An unreachable archive."}},
                profile_snapshot=profile,
                workspace_lock_version=1,
                workspace_work_cycle_id="retry-exhaustion-work-cycle",
            )
            await AgentTaskStore(engine).create_initial(
                plan=plan,
                attempt_id="retry-exhaustion-attempt",
                created_at_ms=20,
            )
            live = RecordingLivePublisher()
            result = await AgentExecutor(
                engine,
                registry=DEFAULT_TASK_REGISTRY,
                resolver=AlwaysReadTimeoutBindingResolver(),
                live_publisher=live,
                now_ms=lambda: 100,
                sleep=no_sleep,
            ).execute(
                project_id=plan.project_id,
                task_id=plan.task_id,
                attempt_id="retry-exhaustion-attempt",
                owner_instance_id="test-engine",
                lease_token="lease-retry-exhaustion",
                credential=ProfileCredential.from_plaintext("not-persisted"),
            )
            async with engine.connect() as connection:
                evidence_json = await connection.scalar(
                    select(agent_evidence_items.c.metadata_json).where(
                        agent_evidence_items.c.attempt_id == "retry-exhaustion-attempt",
                        agent_evidence_items.c.item_kind == "transport_retry",
                    )
                )
            assert isinstance(evidence_json, str)
            return result, live.events, json.loads(evidence_json)
        finally:
            await engine.dispose()

    result, events, evidence = asyncio.run(exercise())

    assert result.status == "failed"
    assert result.error_code == "provider_read_timeout_retries_exhausted"
    assert result.provider_request_count == 6
    assert result.transport_retry_count == 5
    assert result.model_request_count == 1
    assert [event.kind for event in events] == [
        "task_started",
        "attempt_restarting",
        "attempt_restarting",
        "attempt_restarting",
        "attempt_restarting",
        "attempt_restarting",
        "task_failed",
    ]
    attempts = evidence["attempts"]
    assert isinstance(attempts, list)
    assert [attempt["sequence"] for attempt in attempts] == [1, 2, 3, 4, 5, 6]
    assert [attempt["retry_decision"] for attempt in attempts] == [
        "retry",
        "retry",
        "retry",
        "retry",
        "retry",
        "failed",
    ]
    assert all(attempt["error_type"] == "ReadTimeout" for attempt in attempts)
    assert all(attempt["retry_reason"] == "provider_first_event_timeout" for attempt in attempts)


def test_prose_replay_discards_partial_text_and_restarts_from_frozen_plan(
    tmp_path: Path,
) -> None:
    async def no_sleep(_delay: float) -> None:
        return None

    async def exercise() -> tuple[str, list[AgentLiveEvent], ActivationRequestBudget]:
        engine = create_sqlite_async_engine(tmp_path / "prose-replay.sqlite3")
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(
                200,
                request=request,
                stream=AttemptTextStream(
                    request=request,
                    content=(
                        b"abandoned partial prose"
                        if calls == 1
                        else b"replacement complete prose"
                    ),
                    interrupt=calls == 1,
                ),
            )

        budget = ActivationRequestBudget(
            model_request_limit=1,
            protocol="openai_responses",
        )
        client = httpx.AsyncClient(
            transport=build_observed_transport(
                budget=budget,
                wrapped=httpx.MockTransport(handler),
            )
        )

        async def response(
            _messages: list[object],
            _info: AgentInfo,
        ) -> AsyncIterator[str]:
            async with client.stream(
                "POST",
                "https://provider.example/v1/responses",
            ) as provider_response:
                async for chunk in provider_response.aiter_bytes():
                    yield chunk.decode("utf-8")

        profile = ProfileSnapshot.create(
            profile_id="prose-replay-profile",
            display_name="Prose replay profile",
            api_family="openai_responses",
            base_url="https://provider.example/v1",
            model_id="opaque-test-model",
            capabilities=ProfileCapabilities(
                text_streaming=True,
                native_json_schema=True,
            ),
        )
        plan = DEFAULT_TASK_REGISTRY.freeze_plan(
            task_id="chapter-draft-task",
            project_id="project-prose-replay",
            run_id="run-prose-replay",
            task_key="chapter.draft:chapter-a:1",
            action_key="chapter.draft",
            role="chapter_writer",
            task_kind="chapter.draft",
            contract_version=1,
            book_id="book-a",
            arc_id="arc-a",
            chapter_id="chapter-a",
            book_baseline_id="book-baseline-a",
            arc_baseline_id="arc-baseline-a",
            canon_baseline_id="canon-a",
            semantic_goal="Draft one complete chapter.",
            prompt="Write the complete chapter from the frozen context.",
            context_manifest={"plan": {"goal": "Recover the archive."}},
            profile_snapshot=profile,
            workspace_lock_version=1,
            workspace_work_cycle_id="chapter-draft-work-cycle",
        )
        definition = DEFAULT_TASK_REGISTRY.get(
            role=plan.role,
            task_kind=plan.task_kind,
            contract_version=plan.contract_version,
        )
        live = RecordingLivePublisher()
        executor = AgentExecutor(
            engine,
            registry=DEFAULT_TASK_REGISTRY,
            live_publisher=live,
            sleep=no_sleep,
        )
        messages: list[ModelMessage] = []
        try:
            output, _usage = await executor._run_agent(
                plan=plan,
                attempt_id="chapter-draft-attempt",
                definition=definition,
                agent=build_agent(
                    model=RequestCountingModel(
                        FunctionModel(stream_function=response),
                        budget=budget,
                    ),
                    definition=definition,
                ),
                budget=budget,
                captured_messages=messages,
            )
            return output.prose, live.events, budget
        finally:
            await client.aclose()
            await engine.dispose()

    prose, events, budget = asyncio.run(exercise())

    assert prose == "replacement complete prose"
    assert [event.kind for event in events] == [
        "prose_delta",
        "prose_discarded",
        "attempt_restarting",
        "prose_delta",
    ]
    assert [event.delta for event in events if event.kind == "prose_delta"] == [
        "abandoned partial prose",
        "replacement complete prose",
    ]
    assert budget.provider_request_count == 2
    assert budget.transport_retry_count == 1
    assert budget.attempts[0].retry_reason == "provider_stream_idle_timeout"
    assert budget.attempts[1].retry_decision == "completed"


def test_empty_http_success_replays_same_prose_task_then_succeeds(
    tmp_path: Path,
) -> None:
    async def no_sleep(_delay: float) -> None:
        return None

    async def exercise() -> tuple[str, list[AgentLiveEvent], ActivationRequestBudget]:
        engine = create_sqlite_async_engine(tmp_path / "empty-prose-replay.sqlite3")
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(
                200,
                request=request,
                stream=AttemptTextStream(
                    request=request,
                    content=(
                        b" " if calls == 1 else b"complete prose after replay"
                    ),
                    interrupt=False,
                ),
            )

        budget = ActivationRequestBudget(
            model_request_limit=1,
            protocol="openai_responses",
        )
        client = httpx.AsyncClient(
            transport=build_observed_transport(
                budget=budget,
                wrapped=httpx.MockTransport(handler),
            )
        )

        async def response(
            _messages: list[object],
            _info: AgentInfo,
        ) -> AsyncIterator[str]:
            async with client.stream(
                "POST",
                "https://provider.example/v1/responses",
            ) as provider_response:
                async for chunk in provider_response.aiter_bytes():
                    if chunk:
                        yield chunk.decode("utf-8")

        profile = ProfileSnapshot.create(
            profile_id="empty-prose-replay-profile",
            display_name="Empty prose replay profile",
            api_family="openai_responses",
            base_url="https://provider.example/v1",
            model_id="opaque-test-model",
            capabilities=ProfileCapabilities(
                text_streaming=True,
                native_json_schema=True,
            ),
        )
        plan = DEFAULT_TASK_REGISTRY.freeze_plan(
            task_id="empty-chapter-draft-task",
            project_id="project-empty-prose-replay",
            run_id="run-empty-prose-replay",
            task_key="chapter.draft:chapter-a:1",
            action_key="chapter.draft",
            role="chapter_writer",
            task_kind="chapter.draft",
            contract_version=1,
            book_id="book-a",
            arc_id="arc-a",
            chapter_id="chapter-a",
            book_baseline_id="book-baseline-a",
            arc_baseline_id="arc-baseline-a",
            canon_baseline_id="canon-a",
            semantic_goal="Draft one complete chapter.",
            prompt="Write the complete chapter from the frozen context.",
            context_manifest={"plan": {"goal": "Recover the archive."}},
            profile_snapshot=profile,
            workspace_lock_version=1,
            workspace_work_cycle_id="empty-chapter-draft-work-cycle",
        )
        definition = DEFAULT_TASK_REGISTRY.get(
            role=plan.role,
            task_kind=plan.task_kind,
            contract_version=plan.contract_version,
        )
        live = RecordingLivePublisher()
        executor = AgentExecutor(
            engine,
            registry=DEFAULT_TASK_REGISTRY,
            live_publisher=live,
            sleep=no_sleep,
        )
        try:
            output, _usage = await executor._run_agent(
                plan=plan,
                attempt_id="empty-chapter-draft-attempt",
                definition=definition,
                agent=build_agent(
                    model=RequestCountingModel(
                        FunctionModel(stream_function=response),
                        budget=budget,
                    ),
                    definition=definition,
                ),
                budget=budget,
                captured_messages=[],
            )
            return output.prose, live.events, budget
        finally:
            await client.aclose()
            await engine.dispose()

    prose, events, budget = asyncio.run(exercise())

    assert prose == "complete prose after replay"
    assert [event.kind for event in events] == [
        "prose_delta",
        "prose_discarded",
        "attempt_restarting",
        "prose_delta",
    ]
    assert budget.provider_request_count == 2
    assert budget.transport_retry_count == 1
    assert budget.attempts[0].retry_reason == "provider_empty_output"
    assert budget.attempts[1].retry_decision == "completed"


def test_real_responses_stack_replays_thinking_only_http_success(
    tmp_path: Path,
) -> None:
    """Exercise HTTP SSE -> OpenAI SDK -> Pydantic AI -> Executor replay."""

    async def no_sleep(_delay: float) -> None:
        return None

    async def exercise(
        base_url: str,
    ) -> tuple[object, list[AgentLiveEvent], ActivationRequestBudget]:
        engine = create_sqlite_async_engine(tmp_path / "responses-loopback.sqlite3")
        profile = ProfileSnapshot.create(
            profile_id="responses-loopback",
            display_name="Responses loopback",
            api_family="openai_responses",
            base_url=base_url,
            model_id="loopback-responses-model",
            capabilities=ProfileCapabilities(
                text_streaming=True,
                native_json_schema=True,
            ),
        )
        definition = DEFAULT_TASK_REGISTRY.get(
            role="evaluator",
            task_kind="evaluate.book",
            contract_version=1,
        )
        plan = DEFAULT_TASK_REGISTRY.freeze_plan(
            task_id="responses-loopback-task",
            project_id="responses-loopback-project",
            run_id="responses-loopback-run",
            task_key="evaluate.book:responses-loopback",
            action_key="evaluate.book",
            role="evaluator",
            task_kind="evaluate.book",
            contract_version=1,
            book_id="responses-loopback-book",
            canon_baseline_id="responses-loopback-canon",
            semantic_goal="Evaluate one frozen Book candidate.",
            prompt="Evaluate the supplied frozen Book candidate.",
            context_manifest={"candidate_kind": "initial"},
            profile_snapshot=profile,
            workspace_lock_version=1,
            workspace_work_cycle_id="responses-loopback-cycle",
        )
        binding = ModelBindingResolver().resolve(
            profile=profile,
            expected_profile_fingerprint=profile.fingerprint,
            required_capabilities=definition.required_capabilities,
            model_request_limit=definition.model_request_limit,
            credential=ProfileCredential.from_plaintext("loopback-secret"),
        )
        live = RecordingLivePublisher()
        try:
            output, _usage = await AgentExecutor(
                engine,
                registry=DEFAULT_TASK_REGISTRY,
                live_publisher=live,
                sleep=no_sleep,
            )._run_agent(
                plan=plan,
                attempt_id="responses-loopback-attempt",
                definition=definition,
                agent=build_agent(model=binding.model, definition=definition),
                budget=binding.budget,
                captured_messages=[],
            )
            return output, live.events, binding.budget
        finally:
            await binding.aclose()
            await engine.dispose()

    with _thinking_then_result_responses_server() as (base_url, requests):
        output, events, budget = asyncio.run(exercise(base_url))

    assert getattr(output, "decision") == "pass"
    assert len(requests) == 2
    assert requests[0] == requests[1]
    assert [event.kind for event in events] == ["attempt_restarting"]
    assert events[0].reason == "provider_empty_output"
    assert budget.provider_request_count == 2
    assert budget.transport_retry_count == 1
    assert budget.model_request_count == 1
    assert budget.attempts[0].retry_reason == "provider_empty_output"
    assert budget.attempts[1].retry_decision == "completed"


def test_six_empty_http_successes_exhaust_stream_replay_budget(
    tmp_path: Path,
) -> None:
    database = tmp_path / "agent-executor-empty-exhaustion.sqlite3"
    command.upgrade(alembic_config(database), "head")

    async def no_sleep(_delay: float) -> None:
        return None

    async def exercise() -> AgentExecutionResult:
        engine = create_sqlite_async_engine(database)
        try:
            created = await ProjectCommandService(CommandBus(engine)).create_project(
                CreateProjectRequest(
                    project_id="project-empty-exhaustion",
                    creator_brief="A mystery returned through an empty Provider body.",
                    operation_mode="full_auto",
                ),
                idempotency_key="create-project-empty-exhaustion",
            )
            profile = ProfileSnapshot.create(
                profile_id="empty-retry-profile",
                display_name="Empty retry profile",
                api_family="openai_responses",
                base_url="https://provider.example/v1",
                model_id="opaque-test-model",
                capabilities=ProfileCapabilities(
                    text_streaming=True,
                    native_json_schema=True,
                ),
            )
            plan = DEFAULT_TASK_REGISTRY.freeze_plan(
                task_id="empty-retry-exhaustion-task",
                project_id=created.result.project_id,
                run_id=created.result.generation_run_id,
                task_key="evaluate.book:empty-retry-exhaustion",
                action_key="evaluate.book",
                role="evaluator",
                task_kind="evaluate.book",
                contract_version=1,
                book_id=created.result.book_id,
                canon_baseline_id=created.result.canon_baseline_id,
                semantic_goal="Evaluate a frozen Book candidate.",
                prompt="Evaluate the supplied frozen Book candidate.",
                context_manifest={"candidate": {"direction": "An empty archive."}},
                profile_snapshot=profile,
                workspace_lock_version=1,
                workspace_work_cycle_id="empty-retry-exhaustion-work-cycle",
            )
            await AgentTaskStore(engine).create_initial(
                plan=plan,
                attempt_id="empty-retry-exhaustion-attempt",
                created_at_ms=20,
            )
            result = await AgentExecutor(
                engine,
                registry=DEFAULT_TASK_REGISTRY,
                resolver=AlwaysEmptyBindingResolver(),
                now_ms=lambda: 100,
                sleep=no_sleep,
            ).execute(
                project_id=plan.project_id,
                task_id=plan.task_id,
                attempt_id="empty-retry-exhaustion-attempt",
                owner_instance_id="test-engine",
                lease_token="lease-empty-retry-exhaustion",
                credential=ProfileCredential.from_plaintext("not-persisted"),
            )
            return result
        finally:
            await engine.dispose()

    result = asyncio.run(exercise())

    assert result.status == "failed"
    assert result.error_code == "provider_empty_output_retries_exhausted"
    assert result.provider_request_count == 6
    assert result.transport_retry_count == 5
    assert result.model_request_count == 1


@pytest.mark.parametrize(
    ("error", "retry_reason", "category", "code"),
    [
        (
            ModelHTTPError(503, "opaque-model", {"error": "overloaded"}),
            "provider_http_503",
            "transport",
            "provider_transient_retries_exhausted",
        ),
        (
            ModelHTTPError(429, "opaque-model", {"error": "insufficient_quota"}),
            None,
            "quota",
            "provider_quota_exhausted",
        ),
        (
            ModelHTTPError(400, "opaque-model", {"error": "unsupported parameter"}),
            None,
            "invalid_request",
            "provider_invalid_request",
        ),
        (
            ProviderStreamIncomplete("stream ended"),
            "provider_stream_incomplete",
            "transport",
            "provider_stream_retries_exhausted",
        ),
        (
            ProviderOutputTruncated("length"),
            None,
            "output_truncation",
            "provider_output_truncated",
        ),
        (
            UnexpectedModelBehavior("schema invalid"),
            None,
            "output_validation",
            "typed_output_invalid",
        ),
        (
            TimeoutError("activation deadline"),
            None,
            "timeout",
            "activation_deadline_exceeded",
        ),
    ],
)
def test_execution_error_categories_do_not_blur_retry_boundaries(
    error: BaseException,
    retry_reason: str | None,
    category: str,
    code: str,
) -> None:
    retryable = retryable_provider_failure(error)
    classified = classify_execution_error(error)

    assert (None if retryable is None else retryable.reason) == retry_reason
    assert classified.category == category
    assert classified.code == code


def test_length_finish_reason_is_rejected_before_any_result_commit() -> None:
    response = ModelResponse(parts=[], finish_reason="length", state="complete")

    with pytest.raises(ProviderOutputTruncated):
        raise_for_incomplete_stream(response)


def test_complete_response_without_usable_output_is_incomplete() -> None:
    response = ModelResponse(parts=[], finish_reason="stop", state="complete")

    with pytest.raises(ProviderEmptyOutput, match="no usable final output"):
        raise_for_incomplete_stream(response)
