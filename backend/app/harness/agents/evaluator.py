import json
from collections.abc import Callable
from hashlib import sha256
from pathlib import Path

from pydantic import ValidationError

from app.core.paths import ensure_relative_artifact_path
from app.harness.agents.models import (
    CandidateComponentName,
    EvaluationInput,
    EvaluationIssue,
    EvaluationRecord,
    EvaluationResult,
    ModelEvaluationResult,
    NewEvaluationIssue,
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
from app.schemas.profiles import LlmProfile
from app.storage.json_files import read_json
from app.storage.profiles import profile_fingerprint
from app.storage.transactions import commit_file_transaction


EvaluatorCall = Callable[[LlmProfile, ChatRequest], ChatResult]


class EvaluationValidationError(RuntimeError):
    pass


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
    messages = [
        ChatMessage(role="system", content=_evaluator_system_prompt()),
        ChatMessage(
            role="user",
            content=json.dumps(
                evaluation_input.model_dump(mode="json"),
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
            on_retry=on_transport_retry,
        )
        try:
            evaluation = _validated_evaluation(profile, evaluation_input, result)
            break
        except EvaluationValidationError as exc:
            if attempt >= max_validation_repairs:
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
    _validate_evidence_locators(evaluation_input, model_result)
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
                "Do not change the fixed candidate, invent evidence, or explain your repair."
            ),
            "validation_error": str(error),
            "allowed_candidate_locator_roots": [
                evaluation_input.candidate_artifact_id,
                "candidate_artifact_id",
                "candidate",
            ],
            "allowed_evidence_locator_roots": [
                evaluation_input.candidate_artifact_id,
                *(item.locator for item in evaluation_input.evidence),
                "candidate_artifact_id",
                "candidate",
                "deterministic_prechecks",
            ],
            "committed_evidence_locator_roots": [
                item.locator for item in evaluation_input.evidence
            ],
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


def _validate_evidence_locators(
    evaluation_input: EvaluationInput,
    result: ModelEvaluationResult,
) -> None:
    committed_evidence = {item.locator for item in evaluation_input.evidence}
    approved_evidence = {
        evaluation_input.candidate_artifact_id,
        *committed_evidence,
    }
    missing = sorted(
        {
            *(
                item.candidate_locator
                for item in result.new_issues
                if not _locator_is_scoped_to(
                    item.candidate_locator,
                    {evaluation_input.candidate_artifact_id},
                )
                and not _virtual_locator_is_scoped_to(
                    item.candidate_locator,
                    {"candidate_artifact_id", "candidate"},
                )
            ),
            *(
                item.evidence_locator
                for item in result.new_issues
                if not _locator_is_scoped_to(item.evidence_locator, approved_evidence)
                and not _virtual_locator_is_scoped_to(
                    item.evidence_locator,
                    {
                        "candidate_artifact_id",
                        "candidate",
                        "deterministic_prechecks",
                    },
                )
            ),
            *(
                item.evidence_locator
                for item in result.signals
                if not _locator_is_scoped_to(item.evidence_locator, approved_evidence)
                and not _virtual_locator_is_scoped_to(
                    item.evidence_locator,
                    {
                        "candidate_artifact_id",
                        "candidate",
                        "deterministic_prechecks",
                    },
                )
            ),
            *(
                item.evidence_locator
                for item in result.rubric_checks
                if not _locator_is_scoped_to(item.evidence_locator, approved_evidence)
                and not _virtual_locator_is_scoped_to(
                    item.evidence_locator,
                    {
                        "candidate_artifact_id",
                        "candidate",
                        "deterministic_prechecks",
                    },
                )
            ),
            *(
                item.evidence_locator
                for item in result.prior_issue_checks
                if not _locator_is_scoped_to(item.evidence_locator, approved_evidence)
                and not _virtual_locator_is_scoped_to(
                    item.evidence_locator,
                    {
                        "candidate_artifact_id",
                        "candidate",
                        "deterministic_prechecks",
                    },
                )
            ),
        }
    )
    blocker = result.upstream_blocker
    if blocker is not None and not _locator_is_scoped_to(
        blocker.committed_evidence_locator,
        committed_evidence,
    ):
        missing.append(blocker.committed_evidence_locator)
    if missing:
        raise EvaluationValidationError(
            "Evaluator cited evidence outside the approved bundle: "
            + ", ".join(sorted(set(missing)))
        )


def _normalize_evaluation(
    evaluation_input: EvaluationInput,
    result: ModelEvaluationResult,
) -> EvaluationResult:
    expected_dimensions = [
        item.dimension_id for item in evaluation_input.rubric.dimensions
    ]
    actual_dimensions = [item.dimension_id for item in result.rubric_checks]
    if len(actual_dimensions) != len(set(actual_dimensions)):
        raise EvaluationValidationError("Evaluator returned duplicate rubric checks.")
    if set(actual_dimensions) != set(expected_dimensions):
        raise EvaluationValidationError(
            "Evaluator must return exactly one check for every rubric dimension."
        )

    prior_open: dict[str, EvaluationIssue] = {}
    if evaluation_input.review_history:
        for issue in evaluation_input.review_history[-1].result.issues:
            if issue.issue_id is None:
                raise EvaluationValidationError(
                    "Persisted review history contains an issue without a stable ID."
                )
            prior_open[issue.issue_id] = issue
    checks = {item.issue_id: item for item in result.prior_issue_checks}
    if len(checks) != len(result.prior_issue_checks):
        raise EvaluationValidationError("Evaluator returned duplicate prior-issue checks.")
    if set(checks) != set(prior_open):
        raise EvaluationValidationError(
            "Evaluator must account for every open prior issue exactly once."
        )
    if evaluation_input.mode == "initial" and result.prior_issue_checks:
        raise EvaluationValidationError(
            "Initial evaluation cannot report prior-issue checks."
        )

    remaining = [
        prior_open[issue_id]
        for issue_id, check in checks.items()
        if check.status == "remaining"
    ]
    resolved_issue_ids = sorted(
        issue_id for issue_id, check in checks.items() if check.status == "resolved"
    )
    new_issues: list[EvaluationIssue] = []
    for index, proposal in enumerate(result.new_issues):
        issue_id = _stable_issue_id(evaluation_input, proposal, index)
        new_issues.append(
            EvaluationIssue(
                issue_id=issue_id,
                discovery=(
                    "late_discovery"
                    if evaluation_input.mode == "repair_verification"
                    else "initial_discovery"
                ),
                **proposal.model_dump(),
            )
        )
    open_issues = [*remaining, *new_issues]
    component_names = set(evaluation_input.component_fingerprints)
    repair_scope = list(dict.fromkeys(result.repair_scope))
    if any(name not in component_names for name in repair_scope):
        raise EvaluationValidationError(
            "Evaluator repair scope contains a component outside this candidate kind."
        )

    all_rubric_pass = all(item.status == "pass" for item in result.rubric_checks)
    blocking_issues = [item for item in open_issues if item.severity == "blocking"]
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
        required_components = {
            component
            for issue in blocking_issues
            if (component := _issue_component(evaluation_input, issue)) is not None
        }
        if len(required_components) != len(blocking_issues):
            raise EvaluationValidationError(
                "Every blocking local issue must identify one candidate component."
            )
        if not required_components.issubset(set(repair_scope)):
            raise EvaluationValidationError(
                "Repair scope does not cover every blocking issue locator."
            )

    return EvaluationResult(
        schema_version=2,
        outcome=result.outcome,
        contract_satisfied=result.contract_satisfied,
        summary=result.summary,
        issues=open_issues,
        signals=result.signals,
        repair_brief=result.repair_brief,
        upstream_blocker=result.upstream_blocker,
        rubric_checks=result.rubric_checks,
        prior_issue_checks=result.prior_issue_checks,
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
            "candidate_locator": issue.candidate_locator,
            "evidence_locator": issue.evidence_locator,
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
        if locator == f"candidate.{component}" or locator.startswith(
            f"candidate.{component}."
        ):
            return component
        if locator == f"candidate_artifact_id#{component}" or any(
            locator == f"{artifact_id}#{component}"
            for artifact_id in candidate_artifact_ids
        ):
            return component
    return None


def _locator_is_scoped_to(locator: str, allowed_roots: set[str]) -> bool:
    return any(locator == root or locator.startswith(f"{root}#") for root in allowed_roots)


def _virtual_locator_is_scoped_to(locator: str, allowed_roots: set[str]) -> bool:
    return any(
        locator == root
        or locator.startswith(f"{root}.")
        or locator.startswith(f"{root}#")
        for root in allowed_roots
    )


def _evaluator_system_prompt() -> str:
    return (
        "You are NovelPilot's stateless semantic Evaluator. The supplied object is the complete "
        "evaluation context: a fixed typed candidate, an explicit versioned rubric, approved "
        "evidence, and (during repair verification) the complete prior review history. Check "
        "every rubric dimension exactly once. During repair verification, account for every "
        "currently open prior issue exactly once as resolved or remaining; do not silently "
        "erase it. Put only genuinely new findings in new_issues; the Harness assigns their "
        "stable IDs. A new issue is allowed even when it concerns an unchanged "
        "component, because the full history is present. Candidate locators must identify one "
        "typed component as candidate.<component>. Evidence locators must use candidate.<component>, "
        "deterministic_prechecks.<field>, or an approved evidence locator with an optional #field "
        "fragment. For local_repair, repair_scope must list every candidate component needed to "
        "fix current blocking issues and no unrelated component. Do not edit or propose mutation "
        "of committed prose or canon. A committed_evidence_locator for cross-Loop escalation must "
        "use approved committed evidence, never a virtual locator. Return exactly one schema-v2 "
        "result. Use cross_loop_escalation only when an exact upper-contract field and revision "
        "is impossible to satisfy alongside cited committed evidence; otherwise use local_repair."
    )


def _json_document(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"
