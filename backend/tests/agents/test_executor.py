from __future__ import annotations

import asyncio
from pathlib import Path

from alembic import command
from pydantic_ai import ModelResponse, RequestUsage, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from sqlalchemy import select

from app.agents.binding import ProfileCredential, ResolvedModelBinding
from app.agents.contracts import ProfileCapabilities, ProfileSnapshot
from app.agents.executor import AgentExecutor, AgentLiveEvent
from app.agents.registry import DEFAULT_TASK_REGISTRY
from app.agents.transport import ActivationRequestBudget, RequestCountingModel
from app.db.engine import create_sqlite_async_engine
from app.db.maintenance import alembic_config
from app.db.schema import agent_evidence_items, agent_task_attempts, agent_tasks
from app.domain.projects import CreateProjectRequest, ProjectCommandService
from app.store.agent_tasks import AgentTaskStore
from app.store.command_bus import CommandBus
from app.store.execution import AttemptSummaryRecord, ExecutionRepository


class RecordingLivePublisher:
    def __init__(self) -> None:
        self.events: list[AgentLiveEvent] = []

    async def publish(self, event: AgentLiveEvent) -> None:
        self.events.append(event)


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

        def response(_messages: list[object], info: AgentInfo) -> ModelResponse:
            assert info.model_request_parameters.output_mode == "native"
            return ModelResponse(
                parts=[
                    TextPart(
                        '{"decision":"pass","summary":"The Book contract is coherent.",'
                        '"findings":[],"repair_contract":null}'
                    )
                ],
                usage=RequestUsage(input_tokens=17, output_tokens=9),
            )

        model = RequestCountingModel(FunctionModel(response), budget=budget)
        return ResolvedModelBinding(model=model, budget=budget, adapter_key="function-test")


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
            assert result.input_tokens == 17
            assert result.output_tokens == 9

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
        17,
        9,
        0,
    )
    assert rows[4] is not None
    assert evidence == ["completion_message", "validation"]
    assert all("delta" not in kind for kind in evidence)
    assert len(summaries) == 1
    assert summaries[0].task_kind == "evaluate.book"
    assert summaries[0].attempt_status == "succeeded"
    assert summaries[0].total_tokens == 26
    assert len(summaries[0].profile_fingerprint) == 64
    assert summaries[0].model_id == "opaque-test-model"
    assert summaries[0].harness_policy_id == "novelpilot-domain-harness"
