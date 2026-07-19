import json
from collections.abc import Callable
from hashlib import sha256
from pathlib import Path

from pydantic import ValidationError

from app.core.paths import ensure_relative_artifact_path
from app.harness.agents.evidence_matching import resolve_semantic_evidence_quote
from app.harness.agents.models import (
    CandidateComponentName,
    EvaluationCallTelemetry,
    EvaluationInput,
    EvaluationIssue,
    EvaluationRecord,
    EvaluationResult,
    EvaluationRubricCheck,
    EvaluationSignal,
    EvaluationTelemetry,
    ModelEvaluationResult,
    NewEvaluationIssue,
    PriorIssueCheck,
    UpstreamBlockerProposal,
)
from app.harness.agents.semantic_boundary import (
    semantic_model_text,
    semantic_model_value,
)
from app.llm.gateway import (
    ChatMessage,
    ChatRequest,
    ChatResult,
    ResponseSchema,
    call_llm,
    strict_model_json_schema,
)
from app.llm.redaction import redact_profile_secrets
from app.llm.retry import TransportRetryCallback, call_llm_with_transport_retries
from app.llm.usage import merge_usage
from app.schemas.profiles import LlmProfile
from app.storage.json_files import read_json
from app.storage.profiles import profile_fingerprint
from app.storage.transactions import commit_file_transaction


EvaluatorCall = Callable[[LlmProfile, ChatRequest], ChatResult]


class EvaluationValidationError(RuntimeError):
    telemetry: EvaluationTelemetry | None = None


def evaluate_candidate(
    profile: LlmProfile,
    evaluation_input: EvaluationInput,
    *,
    evaluator_call: EvaluatorCall | None = None,
    max_validation_repairs: int = 2,
    transport_retry_limit: int = 3,
    on_transport_retry: TransportRetryCallback | None = None,
) -> EvaluationRecord:
    if max_validation_repairs < 0:
        raise ValueError("Evaluator validation repair limit must not be negative.")
    call = evaluator_call or call_llm
    evaluation_payload = _semantic_evaluation_payload(evaluation_input)
    messages = [
        ChatMessage(role="system", content=_evaluator_system_prompt()),
        ChatMessage(
            role="user",
            content=json.dumps(
                evaluation_payload,
                ensure_ascii=False,
            ),
        ),
    ]
    response_schema = ResponseSchema(
        name="novelpilot_evaluation_result",
        description="One stateless semantic evaluation for a fixed candidate revision.",
        json_schema=strict_model_json_schema(ModelEvaluationResult),
    )
    evaluation: EvaluationResult | None = None
    result: ChatResult | None = None
    attempts: list[EvaluationCallTelemetry] = []
    aggregate_usage: dict[str, object] = {}
    transport_retries = 0

    def record_transport_retry(retry: int, limit: int, exc: Exception) -> None:
        nonlocal transport_retries
        transport_retries += 1
        if on_transport_retry is not None:
            on_transport_retry(retry, limit, exc)

    for attempt in range(max_validation_repairs + 1):
        request = ChatRequest(
            profile_id=profile.id,
            stream=False,
            messages=messages,
            response_schema=response_schema,
        )
        result = call_llm_with_transport_retries(
            profile,
            request,
            retry_limit=transport_retry_limit,
            llm_call=call,
            on_retry=record_transport_retry,
        )
        aggregate_usage = merge_usage(aggregate_usage, result.usage)
        attempts.append(
            EvaluationCallTelemetry(
                attempt=attempt + 1,
                call_type="initial" if attempt == 0 else "validation_repair",
                usage=result.usage,
                usage_available=bool(result.usage),
                model_snapshot=result.model_snapshot,
                provider_snapshot=result.provider_snapshot,
            )
        )
        try:
            evaluation = _validated_evaluation(profile, evaluation_input, result)
            break
        except EvaluationValidationError as exc:
            if attempt >= max_validation_repairs:
                exc.telemetry = EvaluationTelemetry(
                    calls=len(attempts),
                    validation_repairs=max(len(attempts) - 1, 0),
                    transport_retries=transport_retries,
                    usage=aggregate_usage,
                    usage_available=all(item.usage_available for item in attempts),
                    attempts=attempts,
                )
                raise
            messages = [
                *messages,
                ChatMessage(
                    role="assistant",
                    content=_evaluator_output_for_repair(result),
                ),
                ChatMessage(
                    role="user",
                    content=_evaluator_validation_repair_prompt(evaluation_input, exc),
                ),
            ]
    if evaluation is None or result is None:
        raise EvaluationValidationError("Evaluator did not produce a validated result.")
    return EvaluationRecord(
        candidate_run_id=evaluation_input.candidate_run_id,
        input_fingerprint=evaluation_input_fingerprint(profile, evaluation_input),
        candidate_artifact_id=evaluation_input.candidate_artifact_id,
        candidate_revision=evaluation_input.candidate_revision,
        evaluator_profile_id=profile.id,
        evaluator_model_snapshot=result.model_snapshot,
        evaluator_provider_snapshot=result.provider_snapshot,
        rubric_version=evaluation_input.rubric_version,
        evaluation_mode=evaluation_input.mode,
        result=evaluation,
        telemetry=EvaluationTelemetry(
            calls=len(attempts),
            validation_repairs=max(len(attempts) - 1, 0),
            transport_retries=transport_retries,
            usage=aggregate_usage,
            usage_available=all(item.usage_available for item in attempts),
            attempts=attempts,
        ),
    )


def evaluation_input_fingerprint(
    profile: LlmProfile,
    evaluation_input: EvaluationInput,
) -> str:
    """Identify the complete, immutable input evaluated by one model profile."""
    canonical = json.dumps(
        {
            "evaluation_input": evaluation_input.model_dump(mode="json"),
            "evaluator_profile_fingerprint": profile_fingerprint(profile),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


def _validated_evaluation(
    profile: LlmProfile,
    evaluation_input: EvaluationInput,
    result: ChatResult,
) -> EvaluationResult:
    payload = result.structured_output
    if payload is None:
        raise EvaluationValidationError(
            "Evaluator response is missing native Structured Output; prompt-parsed JSON "
            "fallbacks are not supported."
        )
    try:
        model_result = ModelEvaluationResult.model_validate(payload)
    except ValidationError as exc:
        raise EvaluationValidationError(
            redact_profile_secrets(
                f"Evaluator result failed local schema validation: {exc}",
                profile,
            )
        ) from exc
    return _normalize_evaluation(evaluation_input, model_result)


def _evaluator_output_for_repair(result: ChatResult) -> str:
    payload: object = (
        result.structured_output
        if result.structured_output is not None
        else result.content[:20_000]
    )
    return json.dumps(payload, ensure_ascii=False, default=str)


def _evaluator_validation_repair_prompt(
    evaluation_input: EvaluationInput,
    error: EvaluationValidationError,
) -> str:
    return json.dumps(
        {
            "instruction": (
                "Return a complete corrected evaluation through native Structured Output. "
                "Keep rubric_checks and prior_issue_checks in the same semantic order as "
                "the supplied rubric and prior issues. Describe affected meaning and evidence; "
                "do not return IDs, revisions, paths, locators, fingerprints, or exact quote "
                "tokens. Harness will bind all control data. Do not change the fixed candidate "
                "or explain your repair outside the schema."
            ),
            "validation_error": str(error),
            "semantic_evaluation_context": _semantic_evaluation_payload(
                evaluation_input
            ),
        },
        ensure_ascii=False,
    )


def persist_evaluation_views(
    project_path: Path,
    record: EvaluationRecord,
    *,
    evaluation_path: str,
    review_path: str,
    verification_path: str,
    verification_payload: dict[str, object] | None = None,
) -> None:
    paths = [evaluation_path, review_path, verification_path]
    for value in paths:
        ensure_relative_artifact_path(value)
    existing_payload = read_json(project_path / evaluation_path, default=None)
    if existing_payload is not None:
        existing = EvaluationRecord.model_validate(existing_payload)
        if existing != record:
            raise EvaluationValidationError(
                "An immutable evaluation already exists for this artifact path."
            )
        return
    commit_file_transaction(
        project_path,
        kind="agent-evaluation",
        files=evaluation_view_files(
            record,
            evaluation_path=evaluation_path,
            review_path=review_path,
            verification_path=verification_path,
            verification_payload=verification_payload,
        ),
    )


def evaluation_view_files(
    record: EvaluationRecord,
    *,
    evaluation_path: str,
    review_path: str,
    verification_path: str,
    verification_payload: dict[str, object] | None = None,
) -> dict[str, str | bytes]:
    """Render evaluation projections for inclusion in a larger transaction."""
    paths = [evaluation_path, review_path, verification_path]
    for value in paths:
        ensure_relative_artifact_path(value)
    return {
        evaluation_path: _json_document(record.model_dump(mode="json")),
        review_path: render_review_markdown(record),
        verification_path: _json_document(
            verification_payload or render_verification(record)
        ),
    }


def render_review_markdown(record: EvaluationRecord) -> str:
    result = record.result
    lines = [
        "# 语义评测",
        "",
        f"- 结论：`{result.outcome}`",
        f"- 契约满足：`{str(result.contract_satisfied).lower()}`",
        f"- 候选版本：`{record.candidate_revision}`",
        "",
        result.summary.strip(),
    ]
    if result.issues:
        lines.extend(["", "## 问题"])
        for issue in result.issues:
            lines.extend(
                [
                    "",
                    f"- **{issue.severity} / {issue.category}**：{issue.explanation}",
                    f"  - 候选位置：`{issue.candidate_locator}`",
                    f"  - 证据：`{issue.evidence_locator}`",
                ]
            )
    if result.repair_brief:
        lines.extend(["", "## 修订要求", "", result.repair_brief.strip()])
    return "\n".join(lines).rstrip() + "\n"


def render_verification(record: EvaluationRecord) -> dict[str, object]:
    result = record.result
    return {
        "schema_version": 1,
        "evaluation_id": record.evaluation_id,
        "candidate_artifact_id": record.candidate_artifact_id,
        "candidate_revision": record.candidate_revision,
        "commit_allowed": result.outcome == "pass" and result.contract_satisfied,
        "routing_decision": result.outcome,
        "summary": result.summary,
        "issues": [item.model_dump(mode="json") for item in result.issues],
        "signals": [item.model_dump(mode="json") for item in result.signals],
        "upstream_blocker": (
            result.upstream_blocker.model_dump(mode="json")
            if result.upstream_blocker is not None
            else None
        ),
    }


def _normalize_evaluation(
    evaluation_input: EvaluationInput,
    result: ModelEvaluationResult,
) -> EvaluationResult:
    dimensions = evaluation_input.rubric.dimensions
    if len(result.rubric_checks) != len(dimensions):
        raise EvaluationValidationError(
            "Evaluator must return one semantic check for every rubric item, in order."
        )
    rubric_checks = [
        EvaluationRubricCheck(
            dimension_id=dimension.dimension_id,
            status=check.status,
            evidence_locator=_bind_evidence_locator(
                evaluation_input,
                check.evidence_hint,
            ),
            explanation=check.explanation,
        )
        for dimension, check in zip(dimensions, result.rubric_checks, strict=True)
    ]

    prior_open: list[EvaluationIssue] = []
    if evaluation_input.review_history:
        for issue in evaluation_input.review_history[-1].result.issues:
            if issue.issue_id is None:
                raise EvaluationValidationError(
                    "Persisted review history contains an issue without a stable ID."
                )
            prior_open.append(issue)
    if len(result.prior_issue_checks) != len(prior_open):
        raise EvaluationValidationError(
            "Evaluator must account for every open prior issue in the supplied order."
        )
    if evaluation_input.mode == "initial" and result.prior_issue_checks:
        raise EvaluationValidationError(
            "Initial evaluation cannot report prior-issue checks."
        )

    prior_issue_checks: list[PriorIssueCheck] = []
    remaining: list[EvaluationIssue] = []
    resolved_issue_ids: list[str] = []
    for issue, check in zip(prior_open, result.prior_issue_checks, strict=True):
        issue_id = issue.issue_id
        if issue_id is None:
            raise EvaluationValidationError(
                "Persisted review history contains an issue without a stable ID."
            )
        component = _issue_component(evaluation_input, issue)
        prior_issue_checks.append(
            PriorIssueCheck(
                issue_id=issue_id,
                status=check.status,
                evidence_locator=_bind_evidence_locator(
                    evaluation_input,
                    check.evidence_hint,
                    preferred_component=component,
                ),
                explanation=check.explanation,
            )
        )
        if check.status == "remaining":
            remaining.append(issue)
        else:
            resolved_issue_ids.append(issue_id)

    new_issues: list[EvaluationIssue] = []
    for index, proposal in enumerate(result.new_issues):
        component = _component_for_semantic_area(evaluation_input, proposal.affected_area)
        issue_id = _stable_issue_id(evaluation_input, proposal, index)
        new_issues.append(
            EvaluationIssue(
                issue_id=issue_id,
                discovery=(
                    "late_discovery"
                    if evaluation_input.mode == "repair_verification"
                    else "initial_discovery"
                ),
                category=proposal.category,
                severity=proposal.severity,
                candidate_locator=f"candidate.{component}",
                evidence_locator=_bind_evidence_locator(
                    evaluation_input,
                    proposal.evidence_hint,
                    preferred_component=component,
                ),
                explanation=proposal.explanation,
            )
        )
    open_issues = [*remaining, *new_issues]
    signals = [
        EvaluationSignal(
            name=signal.name,
            value=signal.value,
            evidence_locator=_bind_evidence_locator(
                evaluation_input,
                signal.evidence_hint,
            ),
        )
        for signal in result.signals
    ]
    upstream_blocker = _bind_upstream_blocker(evaluation_input, result)

    all_rubric_pass = all(item.status == "pass" for item in rubric_checks)
    blocking_issues = [item for item in open_issues if item.severity == "blocking"]
    repair_scope: list[CandidateComponentName] = []
    for issue in blocking_issues:
        component = _issue_component(evaluation_input, issue)
        if component is None:
            raise EvaluationValidationError(
                "Harness could not bind a blocking issue to a candidate component."
            )
        if component not in repair_scope:
            repair_scope.append(component)
    if result.outcome == "pass":
        if (
            not result.contract_satisfied
            or not all_rubric_pass
            or open_issues
            or repair_scope
        ):
            raise EvaluationValidationError(
                "Passing evaluation requires all rubric checks to pass, no open issues, "
                "and an empty repair scope."
            )
    if result.outcome == "local_repair":
        if result.contract_satisfied or not blocking_issues or not repair_scope:
            raise EvaluationValidationError(
                "Local repair requires an unsatisfied contract, a blocking issue, "
                "and non-empty repair scope."
            )

    return EvaluationResult(
        schema_version=2,
        outcome=result.outcome,
        contract_satisfied=result.contract_satisfied,
        summary=result.summary,
        issues=open_issues,
        signals=signals,
        repair_brief=result.repair_brief,
        upstream_blocker=upstream_blocker,
        rubric_checks=rubric_checks,
        prior_issue_checks=prior_issue_checks,
        new_issue_ids=[item.issue_id for item in new_issues if item.issue_id is not None],
        resolved_issue_ids=resolved_issue_ids,
        repair_scope=repair_scope,
    )


def _stable_issue_id(
    evaluation_input: EvaluationInput,
    issue: NewEvaluationIssue,
    index: int,
) -> str:
    canonical = json.dumps(
        {
            "candidate_run_id": evaluation_input.candidate_run_id,
            "candidate_revision": evaluation_input.candidate_revision,
            "index": index,
            "category": issue.category,
            "severity": issue.severity,
            "affected_area": issue.affected_area,
            "evidence_hint": issue.evidence_hint,
            "explanation": issue.explanation,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "issue-" + sha256(canonical.encode("utf-8")).hexdigest()[:20]


def _issue_component(
    evaluation_input: EvaluationInput,
    issue: EvaluationIssue,
) -> CandidateComponentName | None:
    locator = issue.candidate_locator
    candidate_artifact_ids = {
        evaluation_input.candidate_artifact_id,
        *(item.candidate_artifact_id for item in evaluation_input.review_history),
    }
    for component in evaluation_input.component_fingerprints:
        if (
            locator == f"candidate.{component}"
            or locator.startswith(f"candidate.{component}.")
            or locator.startswith(f"candidate.{component}#")
        ):
            return component
        if locator == f"candidate_artifact_id#{component}" or locator.startswith(
            f"candidate_artifact_id#{component}#"
        ) or any(
            locator == f"{artifact_id}#{component}"
            or locator.startswith(f"{artifact_id}#{component}#")
            for artifact_id in candidate_artifact_ids
        ):
            return component
    return None


def _component_for_semantic_area(
    evaluation_input: EvaluationInput,
    affected_area: str,
) -> CandidateComponentName:
    mappings: dict[str, dict[str, CandidateComponentName]] = {
        "book_direction": {
            "story_direction": "direction",
            "story_constraints": "constraints",
            "decision_coverage": "confirmed_decision_coverage",
            "title_comparison": "recommended_titles",
            "rolling_plan": "rolling_plan",
        },
        "story_arc": {
            "arc_plan": "plan",
            "chapter_count": "target_chapter_count",
            "change_summary": "change_summary",
        },
        "chapter": {
            "chapter_plan": "plan",
            "chapter_draft": "draft",
            "observations": "observations",
            "canon_changes": "state_patch",
        },
    }
    component = mappings[evaluation_input.candidate.kind].get(affected_area)
    if component is None or component not in evaluation_input.component_fingerprints:
        raise EvaluationValidationError(
            "Evaluator selected a semantic area outside this candidate kind."
        )
    return component


def _bind_evidence_locator(
    evaluation_input: EvaluationInput,
    evidence_hint: str,
    *,
    preferred_component: CandidateComponentName | None = None,
) -> str:
    """Materialize an internal locator from semantic evidence supplied by the model."""

    if preferred_component is not None:
        return f"candidate.{preferred_component}"

    candidate_payload = evaluation_input.candidate.model_dump(mode="json")
    candidate_matches: list[CandidateComponentName] = []
    for component in evaluation_input.component_fingerprints:
        value = candidate_payload.get(component)
        if value is None:
            continue
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        if resolve_semantic_evidence_quote(text, [evidence_hint]) is not None:
            candidate_matches.append(component)
    if len(candidate_matches) == 1:
        return f"candidate.{candidate_matches[0]}"

    committed_matches = [
        item.locator
        for item in evaluation_input.evidence
        if resolve_semantic_evidence_quote(item.excerpt, [evidence_hint]) is not None
    ]
    if len(committed_matches) == 1:
        return committed_matches[0]
    return evaluation_input.candidate_artifact_id


def _bind_upstream_blocker(
    evaluation_input: EvaluationInput,
    result: ModelEvaluationResult,
) -> UpstreamBlockerProposal | None:
    blocker = result.upstream_blocker
    if blocker is None:
        return None
    role = evaluation_input.identity.role
    if blocker.upper_scope == "story_arc_contract":
        if role != "chapter":
            raise EvaluationValidationError(
                "Only a Chapter evaluation may escalate to a Story Arc contract."
            )
        owner = "story_arc"
        state_locator = next(
            (
                item.locator
                for item in evaluation_input.evidence
                if item.locator.startswith("arcs/")
                and item.locator.endswith("/state.json")
            ),
            None,
        )
        candidates = [
            item
            for item in evaluation_input.evidence
            if item.locator.startswith("arcs/") and item.locator.endswith("/plan.md")
        ]
    else:
        if role == "book":
            raise EvaluationValidationError("Book evaluation cannot escalate to itself.")
        owner = "book"
        state_locator = "book/state.json"
        candidates = [
            item
            for item in evaluation_input.evidence
            if item.locator
            in {
                "book/direction.md",
                "book/settings.md",
                "book/outline.md",
                "book/constraints.json",
                "book/state.json",
            }
        ]
    hints = [
        blocker.evidence_hint,
        blocker.contract_concern,
        blocker.impossibility_reason,
    ]
    matched = [
        item.locator
        for item in candidates
        if resolve_semantic_evidence_quote(item.excerpt, hints) is not None
    ]
    if len(matched) != 1:
        raise EvaluationValidationError(
            "Harness could not bind the upstream concern to one committed evidence source."
        )
    contract_revision = _contract_revision_from_evidence(
        evaluation_input,
        state_locator=state_locator,
    )
    return UpstreamBlockerProposal(
        owner=owner,
        contract_field=blocker.contract_concern,
        contract_revision=contract_revision,
        committed_evidence_locator=matched[0],
        impossibility_reason=blocker.impossibility_reason,
    )


def _contract_revision_from_evidence(
    evaluation_input: EvaluationInput,
    *,
    state_locator: str | None,
) -> int:
    if state_locator is None:
        raise EvaluationValidationError(
            "Harness could not find the current upper-contract state evidence."
        )
    for item in evaluation_input.evidence:
        if item.locator != state_locator:
            continue
        try:
            payload = json.loads(item.excerpt)
        except json.JSONDecodeError:
            continue
        revision = payload.get("version") if isinstance(payload, dict) else None
        if isinstance(revision, int) and not isinstance(revision, bool) and revision >= 1:
            return revision
    raise EvaluationValidationError(
        "Harness could not read the current upper-contract revision from approved state."
    )


def _semantic_evaluation_payload(
    evaluation_input: EvaluationInput,
) -> dict[str, object]:
    last_issues = (
        evaluation_input.review_history[-1].result.issues
        if evaluation_input.review_history
        else []
    )
    return {
        "candidate_kind": evaluation_input.candidate.kind,
        "evaluation_mode": evaluation_input.mode,
        "candidate": semantic_model_value(
            evaluation_input.candidate.model_dump(mode="json")
        ),
        "rubric": [
            {"instruction": item.instruction}
            for item in evaluation_input.rubric.dimensions
        ],
        "current_prior_issues": [
            _semantic_issue_view(evaluation_input, issue) for issue in last_issues
        ],
        "complete_review_history": [
            {
                "outcome": entry.result.outcome,
                "summary": entry.result.summary,
                "issues": [
                    _semantic_issue_view(evaluation_input, issue)
                    for issue in entry.result.issues
                ],
                "rubric_checks": [
                    {
                        "status": check.status,
                        "explanation": check.explanation,
                    }
                    for check in entry.result.rubric_checks
                ],
                "repair_brief": entry.result.repair_brief,
            }
            for entry in evaluation_input.review_history
        ],
        "approved_evidence": [
            {"excerpt": semantic_model_text(item.excerpt)}
            for item in evaluation_input.evidence
        ],
        "deterministic_prechecks": semantic_model_value(
            evaluation_input.deterministic_prechecks
        ),
        "candidate_schema_invariants": _candidate_schema_invariants(evaluation_input),
    }


def _semantic_issue_view(
    evaluation_input: EvaluationInput,
    issue: EvaluationIssue,
) -> dict[str, object]:
    component = _issue_component(evaluation_input, issue)
    return {
        "category": issue.category,
        "severity": issue.severity,
        "affected_semantic_content": component or "candidate_as_a_whole",
        "explanation": issue.explanation,
    }


def _evaluator_system_prompt() -> str:
    return (
        "You are NovelPilot's stateless semantic Evaluator. Review the fixed candidate against "
        "each rubric instruction in the supplied order. During repair verification, review each "
        "currently open prior issue in the supplied order and mark its meaning resolved or "
        "remaining; do not silently erase it. Put only genuinely new semantic findings in "
        "new_issues. For every finding, choose the affected semantic area and describe evidence "
        "in your own words. Never return or reconstruct IDs, revisions, paths, locators, "
        "fingerprints, exact quote tokens, or mutation coordinates; Harness owns those bindings. "
        "Do not edit committed prose or canon. Treat candidate_schema_invariants as authoritative. "
        "Use cross_loop_escalation only when an upper semantic contract is genuinely impossible "
        "to satisfy; otherwise use local_repair. Return only the native structured result."
    )


def _candidate_schema_invariants(
    evaluation_input: EvaluationInput,
) -> dict[str, object]:
    kind = evaluation_input.candidate.kind
    common: dict[str, object] = {
        "complete_candidate_required": True,
        "all_components_must_remain_locally_schema_valid": True,
    }
    if kind == "book_direction":
        common.update(
            {
                "recommended_titles": {
                    "min_items": 3,
                    "max_items": 5,
                    "unique_title_text": True,
                    "interpretation": (
                        "The collection remains structurally required after one formal title is "
                        "selected. Additional entries are comparison/reference suggestions and do "
                        "not by themselves reopen the locked title decision."
                    ),
                },
                "direction": {"non_blank": True},
                "rolling_plan": {"non_blank": True},
            }
        )
    elif kind == "story_arc":
        common.update(
            {
                "plan": {"non_blank": True},
                "change_summary": {"non_blank": True},
                "target_chapter_count": {"minimum": 1, "maximum": 30},
            }
        )
    else:
        common.update(
            {
                "plan": {"non_blank": True},
                "draft": {"non_blank": True},
                "observations": {"required": True},
                "state_patch": {"required": True},
            }
        )
    return common


def _json_document(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"
