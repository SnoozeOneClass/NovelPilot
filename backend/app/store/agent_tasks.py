from __future__ import annotations

import time
from dataclasses import dataclass
from importlib.metadata import version
from typing import Literal

from pydantic import TypeAdapter
from sqlalchemy.ext.asyncio import AsyncEngine

from app.agents.contracts import AgentTaskPlan
from app.db.uow import UnitOfWork
from app.store.content import prepare_canonical_json
from app.store.execution import FrozenTaskContentRefs


def framework_fingerprint() -> str:
    return prepare_canonical_json(
        {
            "novelpilot_agent_contract": 1,
            "pydantic_ai": version("pydantic-ai-slim"),
            "pydantic": version("pydantic"),
            "httpx": version("httpx"),
            "openai": version("openai"),
        }
    ).sha256


@dataclass(frozen=True, slots=True)
class CreatedAgentTask:
    project_id: str
    task_id: str
    attempt_id: str
    task_plan_fingerprint: str
    framework_fingerprint: str


class AgentTaskStore:
    """Freeze/load Agent tasks in short transactions; never execute a Provider call."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def create_initial(
        self,
        *,
        plan: AgentTaskPlan,
        attempt_id: str,
        created_at_ms: int | None = None,
    ) -> CreatedAgentTask:
        timestamp = time.time_ns() // 1_000_000 if created_at_ms is None else created_at_ms
        prepared_plan = prepare_canonical_json(plan)
        prepared_manifest = prepare_canonical_json(plan.context_manifest)
        prepared_messages = prepare_canonical_json(
            {"messages": [{"role": "user", "content": plan.prompt}]}
        )
        prepared_profile = prepare_canonical_json(plan.profile_snapshot)
        frozen_framework = framework_fingerprint()

        async with UnitOfWork(self._engine, begin_mode="IMMEDIATE") as store:
            task_plan_ref = await store.content.put(
                project_id=plan.project_id,
                prepared=prepared_plan,
                semantic_kind="agent.task_plan",
                media_type="application/json",
                schema_id="agent-task-plan",
                schema_version=1,
                created_at_ms=timestamp,
            )
            manifest_ref = await store.content.put(
                project_id=plan.project_id,
                prepared=prepared_manifest,
                semantic_kind="agent.input_manifest",
                media_type="application/json",
                schema_id="agent-input-manifest",
                schema_version=1,
                created_at_ms=timestamp,
            )
            messages_ref = await store.content.put(
                project_id=plan.project_id,
                prepared=prepared_messages,
                semantic_kind="agent.input_messages",
                media_type="application/json",
                schema_id="pydantic-ai-input-messages",
                schema_version=1,
                created_at_ms=timestamp,
            )
            profile_ref = await store.content.put(
                project_id=plan.project_id,
                prepared=prepared_profile,
                semantic_kind="agent.profile_snapshot",
                media_type="application/json",
                schema_id="llm-profile-snapshot",
                schema_version=1,
                created_at_ms=timestamp,
            )
            await store.execution.insert_task(
                plan=plan,
                refs=FrozenTaskContentRefs(
                    task_plan_ref_id=task_plan_ref.id,
                    input_manifest_ref_id=manifest_ref.id,
                    input_messages_ref_id=messages_ref.id,
                    profile_snapshot_ref_id=profile_ref.id,
                ),
                created_at_ms=timestamp,
            )
            await store.execution.insert_attempt(
                attempt_id=attempt_id,
                project_id=plan.project_id,
                task_id=plan.task_id,
                attempt_number=1,
                retry_kind="initial",
                predecessor_attempt_id=None,
                framework_fingerprint=frozen_framework,
                created_at_ms=timestamp,
            )
        return CreatedAgentTask(
            project_id=plan.project_id,
            task_id=plan.task_id,
            attempt_id=attempt_id,
            task_plan_fingerprint=prepared_plan.sha256,
            framework_fingerprint=frozen_framework,
        )

    async def create_retry_attempt(
        self,
        *,
        project_id: str,
        task_id: str,
        attempt_id: str,
        attempt_number: int,
        retry_kind: Literal["crash_replay", "user_retry"],
        predecessor_attempt_id: str,
        frozen_framework_fingerprint: str,
        created_at_ms: int | None = None,
    ) -> None:
        timestamp = time.time_ns() // 1_000_000 if created_at_ms is None else created_at_ms
        async with UnitOfWork(self._engine, begin_mode="IMMEDIATE") as store:
            await store.execution.insert_attempt(
                attempt_id=attempt_id,
                project_id=project_id,
                task_id=task_id,
                attempt_number=attempt_number,
                retry_kind=retry_kind,
                predecessor_attempt_id=predecessor_attempt_id,
                framework_fingerprint=frozen_framework_fingerprint,
                created_at_ms=timestamp,
            )

    async def load_plan(self, *, project_id: str, task_id: str) -> AgentTaskPlan:
        async with UnitOfWork(self._engine) as store:
            successful_or_any = await store.execution.get_task_plan_ref(
                project_id=project_id,
                task_id=task_id,
            )
            if successful_or_any is None:
                raise LookupError(f"Agent task {task_id!r} does not exist in project {project_id!r}.")
            packed = await store.content.get_packed(
                project_id=project_id,
                ref_id=successful_or_any,
            )
            raw = packed.unpack_and_verify()
        return TypeAdapter(AgentTaskPlan).validate_json(raw)
