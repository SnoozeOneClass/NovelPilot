from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from app.core.paths import ensure_relative_artifact_path
from app.harness.agents.models import (
    AgentIdentity,
    AgentRole,
    RepairContract,
    ToolExecutionResult,
    ToolReplayRecord,
)
from app.harness.agents.persistence import (
    agent_scope_relative,
    append_activation_log,
    argument_digest,
    idempotency_record_relative,
    json_document,
    read_tool_replay,
)
from app.harness.experiment_hooks import ExperimentHookRegistry
from app.llm.gateway import ToolCall, ToolDefinition, strict_model_json_schema
from app.schemas.experiments import ExperimentHookStrategy
from app.storage.file_lock import exclusive_file_lock
from app.storage.transactions import commit_file_transaction


@dataclass(frozen=True)
class ToolExecutionContext:
    project_path: Path
    identity: AgentIdentity
    candidate_run_id: str
    activation_id: str
    tool_call_id: str
    phase: str
    expected_revision: int | None
    repair_contract: RepairContract | None = None
    experiment_strategy: ExperimentHookStrategy | None = None


@dataclass(frozen=True)
class ToolExecutionPlan:
    content: dict[str, Any]
    files: dict[str, str | bytes] = field(default_factory=dict)
    checkpoint_id: str | None = None
    artifact_paths: list[str] = field(default_factory=list)
    allowed_actions: list[str] = field(default_factory=list)


class ToolHandlerError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        recoverable: bool,
        content: dict[str, Any] | None = None,
        artifact_paths: list[str] | None = None,
        allowed_actions: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.recoverable = recoverable
        self.content = content or {}
        self.artifact_paths = artifact_paths or []
        self.allowed_actions = allowed_actions or []


ToolHandler = Callable[[ToolExecutionContext, BaseModel], ToolExecutionPlan]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    version: int
    description: str
    input_model: type[BaseModel]
    allowed_roles: frozenset[AgentRole]
    handler: ToolHandler
    allowed_phases: frozenset[str] | None = None
    read_only: bool = True
    terminal: bool = False
    expose_arguments: bool = False

    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.description,
            input_schema=strict_model_json_schema(self.input_model),
            strict=True,
        )

    def is_allowed(self, role: AgentRole, phase: str) -> bool:
        return role in self.allowed_roles and (
            self.allowed_phases is None or phase in self.allowed_phases
        )


class ToolRegistry:
    def __init__(self, experiment_hooks: ExperimentHookRegistry | None = None) -> None:
        self._specs: dict[str, ToolSpec] = {}
        self._experiment_hooks = experiment_hooks

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._specs:
            raise ValueError(f"Tool is already registered: {spec.name}")
        self._specs[spec.name] = spec

    def registered_names(self) -> list[str]:
        return sorted(self._specs)

    def version_map(self) -> dict[str, int]:
        return {name: self._specs[name].version for name in self.registered_names()}

    def resolve(
        self,
        *,
        role: AgentRole,
        phase: str,
        names: Iterable[str],
    ) -> list[ToolSpec]:
        resolved: list[ToolSpec] = []
        for name in names:
            spec = self._specs.get(name)
            if spec is None:
                raise ValueError(f"Unknown Tool requested by Harness: {name}")
            if not spec.is_allowed(role, phase):
                raise ValueError(f"Tool {name} is not allowed for {role} during {phase}.")
            resolved.append(spec)
        if not resolved:
            raise ValueError("Agent activation requires at least one allowed Tool.")
        return resolved

    def definitions(
        self,
        *,
        role: AgentRole,
        phase: str,
        names: Iterable[str],
    ) -> list[ToolDefinition]:
        return [spec.definition() for spec in self.resolve(role=role, phase=phase, names=names)]

    def execute(
        self,
        context: ToolExecutionContext,
        call: ToolCall,
    ) -> ToolExecutionResult:
        spec = self._specs.get(call.name)
        if spec is None:
            return _error_result(
                call,
                code="unknown_tool",
                message="The requested Tool was not exposed by the Harness.",
                recoverable=False,
            )
        if not spec.is_allowed(context.identity.role, context.phase):
            return _error_result(
                call,
                code="tool_not_authorized",
                message="The Tool is not authorized for this Agent role and phase.",
                recoverable=False,
            )
        if call.parse_error is not None:
            return _error_result(
                call,
                code="malformed_tool_arguments",
                message=call.parse_error,
                recoverable=True,
                allowed_actions=[f"retry:{call.name}"],
            )
        try:
            arguments = spec.input_model.model_validate(call.arguments)
        except ValidationError as exc:
            return _error_result(
                call,
                code="invalid_tool_arguments",
                message="Tool arguments failed local schema validation.",
                recoverable=True,
                content={
                    "issues": exc.errors(
                        include_url=False,
                        include_context=False,
                    )
                },
                allowed_actions=[f"retry:{call.name}"],
            )

        if spec.read_only:
            return self._execute_handler(spec, context, call, arguments)

        root = context.project_path / agent_scope_relative(context.identity)
        with exclusive_file_lock(root / ".tool.lock"):
            digest = argument_digest(call.arguments)
            replay = read_tool_replay(
                context.project_path,
                context.identity,
                context.activation_id,
                call.id,
            )
            if replay is not None:
                if replay.tool_name != call.name or replay.argument_digest != digest:
                    return _error_result(
                        call,
                        code="idempotency_conflict",
                        message="Tool call ID was already used with different arguments.",
                        recoverable=False,
                    )
                return replay.result.model_copy(update={"replayed": True})

            result, plan = self._prepare_handler_result(spec, context, call, arguments)
            if result.status == "error":
                return result
            files = dict(plan.files)
            for relative_path in files:
                ensure_relative_artifact_path(relative_path)
            replay_record = ToolReplayRecord(
                tool_name=call.name,
                tool_call_id=call.id,
                argument_digest=digest,
                result=result,
            )
            replay_path = idempotency_record_relative(
                context.identity,
                context.activation_id,
                call.id,
            )
            files[replay_path.as_posix()] = json_document(
                replay_record.model_dump(mode="json")
            )
            commit_file_transaction(
                context.project_path,
                kind=f"agent-tool-{call.name}",
                files=files,
            )
            self._record_result(context, result, spec)
            return result

    def _execute_handler(
        self,
        spec: ToolSpec,
        context: ToolExecutionContext,
        call: ToolCall,
        arguments: BaseModel,
    ) -> ToolExecutionResult:
        result, plan = self._prepare_handler_result(spec, context, call, arguments)
        if plan.files:
            return _error_result(
                call,
                code="read_tool_attempted_write",
                message="A read-only Tool handler attempted to write project files.",
                recoverable=False,
            )
        self._record_result(context, result, spec)
        return result

    def _prepare_handler_result(
        self,
        spec: ToolSpec,
        context: ToolExecutionContext,
        call: ToolCall,
        arguments: BaseModel,
    ) -> tuple[ToolExecutionResult, ToolExecutionPlan]:
        try:
            plan = spec.handler(context, arguments)
        except ToolHandlerError as exc:
            return (
                _error_result(
                    call,
                    code=exc.code,
                    message=str(exc),
                    recoverable=exc.recoverable,
                    content=exc.content,
                    artifact_paths=exc.artifact_paths,
                    allowed_actions=exc.allowed_actions,
                ),
                ToolExecutionPlan(content={}),
            )
        if spec.terminal and not plan.checkpoint_id:
            return (
                _error_result(
                    call,
                    code="terminal_checkpoint_missing",
                    message="Terminal Tool did not produce a durable checkpoint.",
                    recoverable=False,
                ),
                plan,
            )
        result = ToolExecutionResult(
            status="ok",
            tool_name=call.name,
            tool_call_id=call.id,
            content=plan.content,
            checkpoint_id=plan.checkpoint_id,
            terminal=spec.terminal,
            artifact_paths=plan.artifact_paths,
            allowed_actions=plan.allowed_actions,
        )
        return result, plan

    def _record_result(
        self,
        context: ToolExecutionContext,
        result: ToolExecutionResult,
        spec: ToolSpec,
    ) -> None:
        payload = result.model_dump(mode="json")
        if not spec.expose_arguments:
            payload["content"] = {
                "status": result.status,
                "artifact_paths": result.artifact_paths,
                "checkpoint_id": result.checkpoint_id,
            }
        append_activation_log(
            context.project_path,
            context.identity,
            context.activation_id,
            "tool-calls",
            payload,
        )
        if self._experiment_hooks is not None and context.experiment_strategy is not None:
            self._experiment_hooks.observe(
                "tool_result",
                context.experiment_strategy,
                {
                    "schema_version": 1,
                    "candidate_run_id": context.candidate_run_id,
                    "activation_id": context.activation_id,
                    "role": context.identity.role,
                    "phase": context.phase,
                    "tool_name": spec.name,
                    "tool_version": spec.version,
                    "status": result.status,
                    "terminal": result.terminal,
                    "checkpoint_id": result.checkpoint_id,
                    "artifact_paths": list(result.artifact_paths),
                },
            )


def _error_result(
    call: ToolCall,
    *,
    code: str,
    message: str,
    recoverable: bool,
    content: dict[str, Any] | None = None,
    artifact_paths: list[str] | None = None,
    allowed_actions: list[str] | None = None,
) -> ToolExecutionResult:
    return ToolExecutionResult(
        status="error",
        tool_name=call.name,
        tool_call_id=call.id,
        content=content or {},
        recoverable=recoverable,
        error_code=code,
        message=message,
        artifact_paths=artifact_paths or [],
        allowed_actions=allowed_actions or [],
    )
