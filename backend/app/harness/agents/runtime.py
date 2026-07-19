from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any, Literal
from uuid import uuid4

from app.harness.agents.models import (
    AgentBudgets,
    AgentIdentity,
    AgentRunResult,
    AgentState,
    FailureCategory,
    FailureEnvelope,
    RepairContract,
    ToolExecutionResult,
)
from app.harness.agents.persistence import (
    append_activation_log,
    clone_activation_candidate_workspace,
    persist_pending_repair,
    read_agent_state,
    read_repair_chain,
    save_agent_state,
    write_activation_document,
)
from app.harness.agents.policy import ResolvedAgentPolicy
from app.harness.agents.registry import ToolExecutionContext, ToolRegistry
from app.llm.gateway import (
    ChatMessage,
    ChatRequest,
    ChatResult,
    ToolChoice,
    ToolCall,
    ToolResult,
    call_llm,
)
from app.llm.redaction import redact_profile_secrets
from app.llm.retry import call_llm_with_transport_retries, is_retryable_provider_error
from app.llm.usage import merge_usage
from app.schemas.experiments import ExperimentHookStrategy
from app.schemas.projects import RETRY_BUDGET_SCOPE_VERSION


AgentEventCallback = Callable[[dict[str, Any]], None]
ChatCall = Callable[[Any, ChatRequest], ChatResult]


@dataclass(frozen=True)
class AgentActivation:
    project_path: Path
    identity: AgentIdentity
    candidate_run_id: str
    phase: str
    expected_revision: int | None
    allowed_tools: tuple[str, ...]
    system_prompt: str
    messages: tuple[ChatMessage, ...]
    policy: ResolvedAgentPolicy
    expected_candidate_revision: int | None = None
    initial_checkpoint_id: str | None = None
    on_event: AgentEventCallback | None = None
    on_text_delta: Callable[[Any], None] | None = None
    on_tool_event: Callable[[Any], None] | None = None
    experiment_strategy: ExperimentHookStrategy | None = None
    repair_contract: RepairContract | None = None


@dataclass
class _ActivationTelemetry:
    started_at: datetime
    started_monotonic: float
    llm_calls: int = 0
    tool_calls: int = 0
    tool_errors: int = 0
    validation_failures: int = 0
    tool_schema_repairs: int = 0
    transport_retries: int = 0
    context_characters: int = 0
    usage: dict[str, Any] = field(default_factory=dict)
    model_snapshot: str | None = None
    provider_snapshot: str | None = None


class AgentRuntime:
    def __init__(
        self,
        registry: ToolRegistry,
        *,
        chat_call: ChatCall | None = None,
    ) -> None:
        self._registry = registry
        self._chat_call = chat_call or call_llm

    def run(self, activation: AgentActivation) -> AgentRunResult:
        if (
            activation.experiment_strategy is not None
            and activation.experiment_strategy.mode == "none"
        ):
            raise ValueError(
                "The frozen none/direct-v1 baseline must use direct generation, not AgentRuntime."
            )
        if activation.identity.role != activation.policy.role:
            raise ValueError("Agent identity role does not match its resolved policy.")
        specs = self._registry.resolve(
            role=activation.identity.role,
            phase=activation.phase,
            names=activation.allowed_tools,
        )
        state = self._prepare_state(activation)
        prior_failed_activation_id = (
            state.activation_id
            if activation.identity.role == "chapter"
            and state.lifecycle == "failed"
            and state.candidate_run_id == activation.candidate_run_id
            else None
        )
        source_activation_id = (
            activation.repair_contract.source_activation_id
            if activation.identity.role == "chapter"
            and activation.repair_contract is not None
            else prior_failed_activation_id
        )
        activation_id = uuid4().hex[:12]
        restored_candidate_paths = (
            clone_activation_candidate_workspace(
                activation.project_path,
                activation.identity,
                source_activation_id=source_activation_id,
                target_activation_id=activation_id,
            )
            if source_activation_id is not None
            else []
        )
        state.activation_id = activation_id
        state.lifecycle = "running"
        state.phase = activation.phase
        state.expected_revision = activation.expected_revision
        budgets = _required_budgets(state)
        _reset_activation_usage(budgets)
        schema_repair_attempts: dict[str, int] = {}
        telemetry = _ActivationTelemetry(
            started_at=datetime.now(UTC),
            started_monotonic=perf_counter(),
        )
        save_agent_state(activation.project_path, state)
        self._write_request_snapshot(activation, activation_id, state, specs)
        self._emit(
            activation,
            {
                "kind": "agent_activation_started",
                "activation_id": activation_id,
                "candidate_run_id": activation.candidate_run_id,
                "role": activation.identity.role,
                "phase": activation.phase,
                "allowed_tools": [spec.name for spec in specs],
                "logical_candidate_revision": (
                    activation.repair_contract.next_candidate_revision
                    if activation.repair_contract is not None
                    else 1
                ),
                "allowed_components": (
                    list(activation.repair_contract.allowed_components)
                    if activation.repair_contract is not None
                    else []
                ),
            },
        )

        messages = [ChatMessage(role="system", content=activation.system_prompt)]
        messages.extend(message.model_copy(deep=True) for message in activation.messages)
        if restored_candidate_paths:
            messages.append(
                ChatMessage(
                    role="user",
                    content=(
                        "Harness restored the uncommitted Chapter candidate workspace "
                        "for this candidate run. Reuse every preserved component and change "
                        "only components authorized by the repair contract and exposed Tools. "
                        "This workspace is candidate evidence, not committed prose."
                    ),
                )
            )
        while budgets.used_turns < budgets.max_turns:
            result = self._call_with_transport_retries(
                activation,
                activation_id,
                state,
                messages,
                telemetry,
            )
            if isinstance(result, FailureEnvelope):
                return self._fail(
                    activation,
                    activation_id,
                    state,
                    result,
                    telemetry=telemetry,
                )

            telemetry.llm_calls += 1
            telemetry.usage = merge_usage(telemetry.usage, result.usage)
            telemetry.model_snapshot = result.model_snapshot
            telemetry.provider_snapshot = result.provider_snapshot
            budgets.used_turns += 1
            save_agent_state(activation.project_path, state)
            assistant_message = ChatMessage(
                role="assistant",
                content=result.content,
                tool_calls=result.tool_calls,
            )
            messages.append(assistant_message)
            self._record_message(
                activation,
                activation_id,
                assistant_message,
                turn=budgets.used_turns,
            )

            if not result.tool_calls:
                telemetry.validation_failures += 1
                correction = self._consume_schema_repair(
                    activation,
                    activation_id,
                    state,
                    schema_repair_attempts,
                    telemetry,
                    action_key="assistant:terminal_tool_required",
                    code="terminal_tool_required",
                    message=(
                        "A Tool Agent turn must call an exposed Tool; assistant text alone "
                        "cannot complete or advance the Harness checkpoint."
                    ),
                )
                if isinstance(correction, FailureEnvelope):
                    return self._fail(
                        activation,
                        activation_id,
                        state,
                        correction,
                        telemetry=telemetry,
                    )
                messages.append(correction)
                continue

            tool_messages: list[ChatMessage] = []
            for call in result.tool_calls:
                telemetry.tool_calls += 1
                call = _redacted_tool_call(call, activation.policy.profile)
                context = ToolExecutionContext(
                    project_path=activation.project_path,
                    identity=activation.identity,
                    candidate_run_id=activation.candidate_run_id,
                    activation_id=activation_id,
                    tool_call_id=call.id,
                    phase=activation.phase,
                    expected_revision=activation.expected_revision,
                    expected_candidate_revision=activation.expected_candidate_revision,
                    repair_contract=activation.repair_contract,
                    experiment_strategy=activation.experiment_strategy,
                    allowed_tools=frozenset(activation.allowed_tools),
                )
                tool_result = self._registry.execute(context, call)
                if tool_result.status == "error":
                    telemetry.tool_errors += 1
                    telemetry.validation_failures += 1
                else:
                    telemetry.context_characters += _context_characters(tool_result)
                self._emit_tool_result(activation, activation_id, tool_result)
                tool_message = ChatMessage(
                    role="tool",
                    tool_results=[
                        ToolResult(
                            tool_call_id=call.id,
                            name=call.name,
                            content=_tool_result_content(tool_result),
                            is_error=tool_result.status == "error",
                        )
                    ],
                )
                tool_messages.append(tool_message)
                self._record_message(
                    activation,
                    activation_id,
                    tool_message,
                    turn=budgets.used_turns,
                )

                if tool_result.status == "error":
                    if not tool_result.recoverable:
                        failure = self._failure(
                            activation,
                            activation_id,
                            state,
                            category="harness_conflict",
                            code=tool_result.error_code or "tool_execution_failed",
                            message=tool_result.message or "Tool execution failed.",
                            recoverable=False,
                            allowed_actions=tool_result.allowed_actions,
                        )
                        return self._fail(
                            activation,
                            activation_id,
                            state,
                            failure,
                            telemetry=telemetry,
                        )
                    correction_failure = self._increment_schema_repairs(
                        activation,
                        activation_id,
                        state,
                        schema_repair_attempts,
                        telemetry,
                        action_key=(
                            f"{call.name}:"
                            f"{tool_result.error_code or 'invalid_tool_call'}"
                        ),
                        code=tool_result.error_code or "invalid_tool_call",
                        message=tool_result.message or "Tool call requires correction.",
                        evidence=tool_result.artifact_paths,
                        allowed_actions=tool_result.allowed_actions,
                    )
                    if correction_failure is not None:
                        return self._fail(
                            activation,
                            activation_id,
                            state,
                            correction_failure,
                            telemetry=telemetry,
                        )
                    continue

                schema_repair_attempts.clear()
                budgets.used_tool_schema_repairs = 0
                save_agent_state(activation.project_path, state)

                if tool_result.terminal:
                    if (
                        not tool_result.checkpoint_id
                        or tool_result.checkpoint_id == activation.initial_checkpoint_id
                    ):
                        failure = self._failure(
                            activation,
                            activation_id,
                            state,
                            category="harness_conflict",
                            code="checkpoint_delta_required",
                            message="Terminal Tool did not advance the Harness checkpoint.",
                            recoverable=False,
                        )
                        return self._fail(
                            activation,
                            activation_id,
                            state,
                            failure,
                            telemetry=telemetry,
                        )
                    state.last_checkpoint_id = tool_result.checkpoint_id
                    outcome = _terminal_outcome(call.name)
                    state.lifecycle = _terminal_lifecycle(outcome)
                    state.summary = tool_result.message or str(
                        tool_result.content.get("summary", "")
                    )[:20_000]
                    save_agent_state(activation.project_path, state)
                    telemetry_path = self._write_telemetry(
                        activation,
                        activation_id,
                        state,
                        telemetry,
                        outcome=outcome,
                        evidence_paths=tool_result.artifact_paths,
                    )
                    self._emit(
                        activation,
                        {
                            "kind": "agent_activation_completed",
                            "activation_id": activation_id,
                            "candidate_run_id": activation.candidate_run_id,
                            "outcome": outcome,
                            "checkpoint_id": tool_result.checkpoint_id,
                            "turns_used": budgets.used_turns,
                            "evidence_paths": [
                                *tool_result.artifact_paths,
                                telemetry_path,
                            ],
                        },
                    )
                    return AgentRunResult(
                        outcome=outcome,
                        identity=activation.identity,
                        candidate_run_id=activation.candidate_run_id,
                        activation_id=activation_id,
                        turns_used=budgets.used_turns,
                        terminal_result=tool_result,
                        model_snapshot=result.model_snapshot,
                        provider_snapshot=result.provider_snapshot,
                        usage=telemetry.usage,
                    )
            messages.extend(tool_messages)

        failure = self._failure(
            activation,
            activation_id,
            state,
            category="exhausted",
            code="agent_turn_limit_exhausted",
            message="Agent reached its activation turn limit without a terminal checkpoint.",
            recoverable=False,
        )
        return self._fail(
            activation,
            activation_id,
            state,
            failure,
            telemetry=telemetry,
        )

    def request_semantic_revision(
        self,
        activation: AgentActivation,
        *,
        repair_contract: RepairContract,
    ) -> bool:
        """Consume one candidate-local semantic-revision slot before another activation.

        Evaluators can propose a local repair, but only the Harness-owned runtime may
        spend the revision budget and reactivate the same logical Agent. Returning
        ``False`` means the budget is exhausted and a typed failure was persisted.
        """
        state = read_agent_state(activation.project_path, activation.identity)
        if state.candidate_run_id != activation.candidate_run_id:
            raise ValueError("Semantic revision does not match the active candidate run.")
        budgets = _required_budgets(state)
        activation_id = state.activation_id
        if activation_id is None:
            raise ValueError("Semantic revision requires a completed Agent activation.")
        if budgets.used_semantic_revisions >= budgets.semantic_revision_limit:
            failure = self._failure(
                activation,
                activation_id,
                state,
                category="local_semantic",
                code="semantic_revision_exhausted",
                message=(
                    "Candidate still requires local semantic repair after its "
                    "automatic revision budget was exhausted."
                ),
                recoverable=True,
                evidence=[
                    repair_contract.source_candidate_artifact_id,
                    repair_contract.evaluation_id,
                ],
                allowed_actions=["retry_candidate_run", "request_user_decision"],
            )
            self._fail(activation, activation_id, state, failure)
            return False

        candidate_kind: Literal["book_direction", "story_arc", "chapter"] = (
            "book_direction"
            if activation.identity.role == "book"
            else activation.identity.role
        )
        chain = read_repair_chain(
            activation.project_path,
            activation.identity,
            candidate_run_id=activation.candidate_run_id,
            candidate_kind=candidate_kind,
            semantic_revision_limit=budgets.semantic_revision_limit,
        )
        persist_pending_repair(activation.project_path, chain, repair_contract)
        budgets.used_semantic_revisions += 1
        state.lifecycle = "idle"
        state.summary = "Evaluator requested a bounded local candidate revision."
        save_agent_state(activation.project_path, state)
        write_activation_document(
            activation.project_path,
            activation.identity,
            activation_id,
            "semantic-repair.json",
            {
                "schema_version": 2,
                "retry_budget_scope_version": RETRY_BUDGET_SCOPE_VERSION,
                "repair_contract": repair_contract.model_dump(mode="json"),
                "revision": budgets.used_semantic_revisions,
                "limit": budgets.semantic_revision_limit,
            },
        )
        self._emit(
            activation,
            {
                "kind": "agent_semantic_revision_scheduled",
                "activation_id": activation_id,
                "candidate_run_id": activation.candidate_run_id,
                "evaluation_id": repair_contract.evaluation_id,
                "revision": budgets.used_semantic_revisions,
                "limit": budgets.semantic_revision_limit,
                "logical_candidate_revision": repair_contract.next_candidate_revision,
                "allowed_components": list(repair_contract.allowed_components),
            },
        )
        return True

    def _prepare_state(self, activation: AgentActivation) -> AgentState:
        state = read_agent_state(activation.project_path, activation.identity)
        if state.candidate_run_id != activation.candidate_run_id or state.budgets is None:
            state = AgentState(
                identity=activation.identity,
                candidate_run_id=activation.candidate_run_id,
                lifecycle="idle",
                budgets=AgentBudgets(
                    max_turns=activation.policy.max_turns,
                    tool_schema_repair_limit=activation.policy.tool_schema_repair_limit,
                    semantic_revision_limit=activation.policy.semantic_revision_limit,
                    transport_retry_limit=activation.policy.transport_retry_limit,
                ),
                last_checkpoint_id=activation.initial_checkpoint_id,
            )
        return state

    def _call_with_transport_retries(
        self,
        activation: AgentActivation,
        activation_id: str,
        state: AgentState,
        messages: list[ChatMessage],
        telemetry: _ActivationTelemetry,
    ) -> ChatResult | FailureEnvelope:
        budgets = _required_budgets(state)
        if budgets.used_transport_retries:
            budgets.used_transport_retries = 0
            save_agent_state(activation.project_path, state)

        request = ChatRequest(
            profile_id=activation.policy.profile.id,
            messages=messages,
            stream=True,
            tools=self._registry.definitions(
                role=activation.identity.role,
                phase=activation.phase,
                names=activation.allowed_tools,
            ),
            tool_choice=ToolChoice(mode="required"),
            metadata={
                **(
                    {"on_text_delta": activation.on_text_delta}
                    if activation.on_text_delta is not None
                    else {}
                ),
                **(
                    {"on_tool_event": activation.on_tool_event}
                    if activation.on_tool_event is not None
                    else {}
                ),
            },
        )

        def on_retry(retry: int, limit: int, exc: Exception) -> None:
            message = redact_profile_secrets(str(exc), activation.policy.profile)
            budgets.used_transport_retries = retry
            telemetry.transport_retries += 1
            save_agent_state(activation.project_path, state)
            self._emit(
                activation,
                {
                    "kind": "agent_transport_retry",
                    "activation_id": activation_id,
                    "retry": retry,
                    "limit": limit,
                    "message": message,
                },
            )

        try:
            return call_llm_with_transport_retries(
                activation.policy.profile,
                request,
                retry_limit=budgets.transport_retry_limit,
                llm_call=self._chat_call,
                on_retry=on_retry,
            )
        except Exception as exc:
            message = redact_profile_secrets(str(exc), activation.policy.profile)
            provider_failure = is_retryable_provider_error(exc)
            return self._failure(
                activation,
                activation_id,
                state,
                category=(
                    "transport_provider" if provider_failure else "malformed_model_output"
                ),
                code=(
                    "provider_retry_exhausted"
                    if provider_failure
                    else "provider_response_invalid"
                ),
                message=message,
                recoverable=False,
                allowed_actions=["retry_candidate_run", "change_profile"],
            )

    def _consume_schema_repair(
        self,
        activation: AgentActivation,
        activation_id: str,
        state: AgentState,
        attempts_by_action: dict[str, int],
        telemetry: _ActivationTelemetry,
        *,
        action_key: str,
        code: str,
        message: str,
    ) -> ChatMessage | FailureEnvelope:
        failure = self._increment_schema_repairs(
            activation,
            activation_id,
            state,
            attempts_by_action,
            telemetry,
            action_key=action_key,
            code=code,
            message=message,
        )
        if failure is not None:
            return failure
        return ChatMessage(
            role="user",
            content=(
                f"Harness validation error ({code}): {message} "
                "Call one of the exposed Tools with valid arguments."
            ),
        )

    def _increment_schema_repairs(
        self,
        activation: AgentActivation,
        activation_id: str,
        state: AgentState,
        attempts_by_action: dict[str, int],
        telemetry: _ActivationTelemetry,
        *,
        action_key: str,
        code: str,
        message: str,
        evidence: list[str] | None = None,
        allowed_actions: list[str] | None = None,
    ) -> FailureEnvelope | None:
        budgets = _required_budgets(state)
        used = attempts_by_action.get(action_key, 0)
        budgets.used_tool_schema_repairs = used
        if used >= budgets.tool_schema_repair_limit:
            return self._failure(
                activation,
                activation_id,
                state,
                category="malformed_model_output",
                code="tool_schema_repair_exhausted",
                cause_code=code,
                message=message,
                recoverable=True,
                evidence=list(dict.fromkeys([code, *(evidence or [])])),
                allowed_actions=list(
                    dict.fromkeys([*(allowed_actions or []), "retry_failed_run"])
                ),
            )
        used += 1
        attempts_by_action[action_key] = used
        budgets.used_tool_schema_repairs = used
        telemetry.tool_schema_repairs += 1
        save_agent_state(activation.project_path, state)
        return None

    def _write_request_snapshot(
        self,
        activation: AgentActivation,
        activation_id: str,
        state: AgentState,
        specs: list[Any],
    ) -> None:
        write_activation_document(
            activation.project_path,
            activation.identity,
            activation_id,
            "request.json",
            {
                "schema_version": 2,
                "retry_budget_scope_version": RETRY_BUDGET_SCOPE_VERSION,
                "identity": activation.identity.model_dump(mode="json"),
                "candidate_run_id": activation.candidate_run_id,
                "activation_id": activation_id,
                "phase": activation.phase,
                "expected_revision": activation.expected_revision,
                "expected_candidate_revision": activation.expected_candidate_revision,
                "initial_checkpoint_id": activation.initial_checkpoint_id,
                "profile": {
                    "id": activation.policy.profile.id,
                    "protocol": activation.policy.profile.protocol,
                    "model": activation.policy.profile.model,
                },
                "evaluator_profile": {
                    "id": activation.policy.evaluator_profile.id,
                    "protocol": activation.policy.evaluator_profile.protocol,
                    "model": activation.policy.evaluator_profile.model,
                },
                "tools": [
                    {"name": spec.name, "version": spec.version} for spec in specs
                ],
                "budgets": _required_budgets(state).model_dump(mode="json"),
                "created_at": datetime.now(UTC).isoformat(),
                "experiment_strategy": (
                    activation.experiment_strategy.model_dump(mode="json")
                    if activation.experiment_strategy is not None
                    else None
                ),
                "repair_contract": (
                    activation.repair_contract.model_dump(mode="json")
                    if activation.repair_contract is not None
                    else None
                ),
            },
        )

    def _record_message(
        self,
        activation: AgentActivation,
        activation_id: str,
        message: ChatMessage,
        *,
        turn: int,
    ) -> None:
        payload = message.model_dump(mode="json")
        payload = _redact_value(payload, activation.policy.profile)
        append_activation_log(
            activation.project_path,
            activation.identity,
            activation_id,
            "transcript",
            {"turn": turn, "message": payload},
        )

    def _emit_tool_result(
        self,
        activation: AgentActivation,
        activation_id: str,
        result: ToolExecutionResult,
    ) -> None:
        self._emit(
            activation,
            {
                "kind": "agent_tool_result",
                "activation_id": activation_id,
                "tool_name": result.tool_name,
                "tool_call_id": result.tool_call_id,
                "status": result.status,
                "error_code": result.error_code,
                "checkpoint_id": result.checkpoint_id,
                "terminal": result.terminal,
                "artifact_paths": result.artifact_paths,
            },
        )

    def _failure(
        self,
        activation: AgentActivation,
        activation_id: str,
        state: AgentState,
        *,
        category: FailureCategory,
        code: str,
        cause_code: str | None = None,
        message: str,
        recoverable: bool,
        evidence: list[str] | None = None,
        allowed_actions: list[str] | None = None,
    ) -> FailureEnvelope:
        budgets = _required_budgets(state)
        return FailureEnvelope(
            category=category,
            code=code,
            cause_code=cause_code,
            scope=activation.identity.role,
            recoverable=recoverable,
            responsible_component=(
                "llm_gateway" if category == "transport_provider" else "agent_runtime"
            ),
            identity=activation.identity,
            candidate_run_id=activation.candidate_run_id,
            activation_id=activation_id,
            checkpoint=activation.phase,
            candidate_revision=activation.expected_revision,
            message=message,
            evidence=evidence or [],
            consumed_budgets={
                "turns": budgets.used_turns,
                "tool_schema_repairs": budgets.used_tool_schema_repairs,
                "semantic_revisions": budgets.used_semantic_revisions,
                "transport_retries": budgets.used_transport_retries,
            },
            remaining_budgets={
                "turns": budgets.max_turns - budgets.used_turns,
                "tool_schema_repairs": (
                    budgets.tool_schema_repair_limit - budgets.used_tool_schema_repairs
                ),
                "semantic_revisions": (
                    budgets.semantic_revision_limit - budgets.used_semantic_revisions
                ),
                "transport_retries": (
                    budgets.transport_retry_limit - budgets.used_transport_retries
                ),
            },
            allowed_actions=allowed_actions or [],
        )

    def _fail(
        self,
        activation: AgentActivation,
        activation_id: str,
        state: AgentState,
        failure: FailureEnvelope,
        *,
        telemetry: _ActivationTelemetry | None = None,
    ) -> AgentRunResult:
        state.lifecycle = "failed"
        state.summary = failure.message[:20_000]
        save_agent_state(activation.project_path, state)
        failure_path = write_activation_document(
            activation.project_path,
            activation.identity,
            activation_id,
            "failure.json",
            failure.model_dump(mode="json"),
        )
        evidence_paths = [failure_path]
        if telemetry is not None:
            telemetry_path = self._write_telemetry(
                activation,
                activation_id,
                state,
                telemetry,
                outcome="failed",
                evidence_paths=evidence_paths,
            )
            evidence_paths.append(telemetry_path)
        self._emit(
            activation,
            {
                "kind": "agent_activation_failed",
                "activation_id": activation_id,
                "candidate_run_id": activation.candidate_run_id,
                "failure_id": failure.failure_id,
                "category": failure.category,
                "code": failure.code,
                "cause_code": failure.cause_code,
                "recoverable": failure.recoverable,
                "allowed_actions": failure.allowed_actions,
                "message": failure.message,
                "evidence_paths": evidence_paths,
            },
        )
        return AgentRunResult(
            outcome="failed",
            identity=activation.identity,
            candidate_run_id=activation.candidate_run_id,
            activation_id=activation_id,
            turns_used=_required_budgets(state).used_turns,
            failure=failure,
            model_snapshot=(telemetry.model_snapshot if telemetry is not None else None),
            provider_snapshot=(
                telemetry.provider_snapshot if telemetry is not None else None
            ),
            usage=(telemetry.usage if telemetry is not None else {}),
        )

    def _write_telemetry(
        self,
        activation: AgentActivation,
        activation_id: str,
        state: AgentState,
        telemetry: _ActivationTelemetry,
        *,
        outcome: str,
        evidence_paths: list[str],
    ) -> str:
        budgets = _required_budgets(state)
        return write_activation_document(
            activation.project_path,
            activation.identity,
            activation_id,
            "telemetry.json",
            {
                "schema_version": 2,
                "retry_budget_scope_version": RETRY_BUDGET_SCOPE_VERSION,
                "candidate_run_id": activation.candidate_run_id,
                "activation_id": activation_id,
                "role": activation.identity.role,
                "phase": activation.phase,
                "outcome": outcome,
                "started_at": telemetry.started_at.isoformat(),
                "finished_at": datetime.now(UTC).isoformat(),
                "latency_ms": round(
                    (perf_counter() - telemetry.started_monotonic) * 1000,
                    3,
                ),
                "llm_calls": telemetry.llm_calls,
                "tool_calls": telemetry.tool_calls,
                "tool_errors": telemetry.tool_errors,
                "validation_failures": telemetry.validation_failures,
                "context_characters": telemetry.context_characters,
                "activation_turns": budgets.used_turns,
                "activation_tool_schema_repairs": telemetry.tool_schema_repairs,
                "activation_transport_retries": telemetry.transport_retries,
                "candidate_budgets": budgets.model_dump(mode="json"),
                "usage": telemetry.usage,
                "model_snapshot": telemetry.model_snapshot,
                "provider_snapshot": telemetry.provider_snapshot,
                "evidence_paths": evidence_paths,
            },
        )

    @staticmethod
    def _emit(activation: AgentActivation, payload: dict[str, Any]) -> None:
        if activation.on_event is not None:
            activation.on_event(payload)


def _required_budgets(state: AgentState) -> AgentBudgets:
    if state.budgets is None:
        raise ValueError("Agent state is missing its retry policy and local usage.")
    return state.budgets


def _reset_activation_usage(budgets: AgentBudgets) -> None:
    """Reset action-local usage while preserving the candidate semantic chain."""
    budgets.used_turns = 0
    budgets.used_tool_schema_repairs = 0
    budgets.used_transport_retries = 0


def _tool_result_content(result: ToolExecutionResult) -> dict[str, Any]:
    return {
        "status": result.status,
        "content": result.content,
        "recoverable": result.recoverable,
        "error_code": result.error_code,
        "message": result.message,
        "checkpoint_id": result.checkpoint_id,
        "allowed_actions": result.allowed_actions,
    }


def _context_characters(result: ToolExecutionResult) -> int:
    if result.tool_name == "get_loop_context":
        sources = result.content.get("sources", [])
        if not isinstance(sources, list):
            return 0
        return sum(
            item.get("included_characters", 0)
            for item in sources
            if isinstance(item, dict)
            and isinstance(item.get("included_characters"), int)
        )
    if result.tool_name == "read_chapter_evidence":
        content = result.content.get("content")
        return len(content) if isinstance(content, str) else 0
    return 0


def _redacted_tool_call(call: ToolCall, profile: Any) -> ToolCall:
    return call.model_copy(
        update={
            "arguments": _redact_value(call.arguments, profile),
            "raw_arguments": redact_profile_secrets(call.raw_arguments, profile),
            "parse_error": (
                redact_profile_secrets(call.parse_error, profile)
                if call.parse_error is not None
                else None
            ),
        }
    )


def _redact_value(value: Any, profile: Any) -> Any:
    if isinstance(value, str):
        return redact_profile_secrets(value, profile)
    if isinstance(value, list):
        return [_redact_value(item, profile) for item in value]
    if isinstance(value, dict):
        return {key: _redact_value(item, profile) for key, item in value.items()}
    return value


def _terminal_outcome(
    tool_name: str,
) -> Literal["candidate", "waiting_user", "blocked"]:
    if tool_name == "request_user_decision":
        return "waiting_user"
    if tool_name == "report_blocker":
        return "blocked"
    return "candidate"


def _terminal_lifecycle(
    outcome: Literal["candidate", "waiting_user", "blocked"],
) -> Literal["waiting_user", "blocked", "completed"]:
    if outcome == "waiting_user":
        return "waiting_user"
    if outcome == "blocked":
        return "blocked"
    return "completed"
