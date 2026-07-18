import json

import pytest
from pydantic import SecretStr

from app.harness.agents.evaluator import (
    EvaluationValidationError,
    evaluate_candidate,
    persist_evaluation_views,
)
from app.harness.agents.models import (
    AgentIdentity,
    BookCandidateSnapshot,
    CandidateKind,
    ChapterCandidateSnapshot,
    EvaluationEvidence,
    EvaluationHistoryEntry,
    EvaluationInput,
    EvaluationIssue,
    EvaluationResult,
    RepairContract,
)
from app.harness.agents.rubrics import component_fingerprints, resolve_rubric
from app.llm.gateway import ChatResult
from app.schemas.profiles import LlmProfile
from app.storage.json_files import read_json


def test_evaluator_uses_native_schema_and_persists_one_source_for_both_views(
    tmp_path,
) -> None:
    captured = {}

    def fake_call(_profile, request):
        captured["request"] = request
        return ChatResult(
            content='{"schema_version":1,"outcome":"pass"}',
            structured_output=_provider_payload(
                outcome="pass",
                contract_satisfied=True,
                summary="候选满足本轮契约。",
                signals=[
                    {
                        "name": "contract_alignment",
                        "value": True,
                        "evidence_locator": "book:direction:r1",
                    }
                ],
            ),
            model_snapshot="judge-model",
            provider_snapshot="openai-compatible",
        )

    record = evaluate_candidate(_profile(), _input(), evaluator_call=fake_call)
    request = captured["request"]
    assert request.execution_mode == "structured_result"
    assert request.tools == []
    persist_evaluation_views(
        tmp_path,
        record,
        evaluation_path="book/candidate/evaluation.json",
        review_path="book/candidate/review.md",
        verification_path="book/candidate/verification.json",
    )

    evaluation = read_json(tmp_path / "book" / "candidate" / "evaluation.json")
    verification = read_json(tmp_path / "book" / "candidate" / "verification.json")
    review = (tmp_path / "book" / "candidate" / "review.md").read_text(encoding="utf-8")
    assert evaluation["evaluation_id"] == verification["evaluation_id"]
    assert verification["commit_allowed"] is True
    assert "候选满足本轮契约" in review


def test_evaluator_rejects_evidence_outside_approved_bundle() -> None:
    def fake_call(_profile, _request):
        return ChatResult(
            content="{}",
            structured_output=_provider_payload(
                outcome="local_repair",
                contract_satisfied=False,
                summary="存在冲突。",
                new_issues=[
                    {
                        "category": "continuity",
                        "severity": "blocking",
                        "candidate_locator": "candidate:paragraph-2",
                        "evidence_locator": "invented:evidence",
                        "explanation": "与证据冲突。",
                    }
                ],
                repair_brief="修改当前候选，不要改动已提交内容。",
                repair_scope=["direction"],
            ),
            model_snapshot="judge-model",
            provider_snapshot="openai-compatible",
        )

    try:
        evaluate_candidate(_profile(), _input(), evaluator_call=fake_call)
    except EvaluationValidationError as exc:
        assert "outside the approved bundle" in str(exc)
    else:
        raise AssertionError("Evaluator accepted an invented evidence locator.")


def test_evaluator_accepts_candidate_and_evidence_field_locators() -> None:
    def fake_call(_profile, _request):
        return ChatResult(
            content="{}",
            structured_output=_provider_payload(
                outcome="local_repair",
                contract_satisfied=False,
                summary="The candidate needs one local repair.",
                new_issues=[
                    {
                        "category": "coverage",
                        "severity": "blocking",
                        "candidate_locator": "candidate.direction",
                        "evidence_locator": "deterministic_prechecks.coverage_count",
                        "explanation": "The candidate omits a confirmed decision.",
                    }
                ],
                signals=[
                    {
                        "name": "decision_coverage",
                        "value": False,
                        "evidence_locator": "book:direction:r1#confirmed_decisions",
                    }
                ],
                repair_brief="Cover the missing confirmed decision.",
                repair_scope=["direction"],
            ),
            model_snapshot="judge-model",
            provider_snapshot="openai-compatible",
        )

    record = evaluate_candidate(_profile(), _input(), evaluator_call=fake_call)

    assert record.result.outcome == "local_repair"


def test_evaluator_accepts_multiple_blocking_issues_for_one_component() -> None:
    record = evaluate_candidate(
        _profile(),
        _input(),
        evaluator_call=lambda _profile, _request: ChatResult(
            content="{}",
            structured_output=_provider_payload(
                outcome="local_repair",
                contract_satisfied=False,
                summary="The direction has two independent local defects.",
                new_issues=[
                    {
                        "category": "reader_promise",
                        "severity": "blocking",
                        "candidate_locator": "candidate.direction",
                        "evidence_locator": "candidate.direction",
                        "explanation": "The reader promise is not concrete enough.",
                    },
                    {
                        "category": "structure",
                        "severity": "blocking",
                        "candidate_locator": "candidate.direction",
                        "evidence_locator": "candidate.direction",
                        "explanation": "The long-form escalation is not yet visible.",
                    },
                ],
                repair_brief="Repair both direction defects.",
                repair_scope=["direction"],
            ),
            model_snapshot="judge-model",
            provider_snapshot="openai-compatible",
        ),
        max_validation_repairs=0,
    )

    assert record.result.outcome == "local_repair"
    assert record.result.repair_scope == ["direction"]
    assert len(record.result.issues) == 2
    assert len({issue.issue_id for issue in record.result.issues}) == 2


def test_evaluator_rejects_one_unmapped_blocking_issue() -> None:
    with pytest.raises(
        EvaluationValidationError,
        match="Every blocking local issue must identify one candidate component",
    ):
        evaluate_candidate(
            _profile(),
            _input(),
            evaluator_call=lambda _profile, _request: ChatResult(
                content="{}",
                structured_output=_provider_payload(
                    outcome="local_repair",
                    contract_satisfied=False,
                    summary="One issue has no typed candidate component.",
                    new_issues=[
                        {
                            "category": "reader_promise",
                            "severity": "blocking",
                            "candidate_locator": "candidate.unknown_component",
                            "evidence_locator": "candidate.direction",
                            "explanation": "The locator is invalid.",
                        }
                    ],
                    repair_brief="Repair the direction.",
                    repair_scope=["direction"],
                ),
                model_snapshot="judge-model",
                provider_snapshot="openai-compatible",
            ),
            max_validation_repairs=0,
        )


def test_evaluator_rejects_repair_scope_missing_a_mapped_component() -> None:
    with pytest.raises(
        EvaluationValidationError,
        match="Repair scope does not cover every blocking issue locator",
    ):
        evaluate_candidate(
            _profile(),
            _input(),
            evaluator_call=lambda _profile, _request: ChatResult(
                content="{}",
                structured_output=_provider_payload(
                    outcome="local_repair",
                    contract_satisfied=False,
                    summary="Two components require repair.",
                    new_issues=[
                        {
                            "category": "reader_promise",
                            "severity": "blocking",
                            "candidate_locator": "candidate.direction",
                            "evidence_locator": "candidate.direction",
                            "explanation": "The direction needs repair.",
                        },
                        {
                            "category": "constraints",
                            "severity": "blocking",
                            "candidate_locator": "candidate.constraints",
                            "evidence_locator": "candidate.constraints",
                            "explanation": "The constraints need repair.",
                        },
                    ],
                    repair_brief="Repair the direction and constraints.",
                    repair_scope=["direction"],
                ),
                model_snapshot="judge-model",
                provider_snapshot="openai-compatible",
            ),
            max_validation_repairs=0,
        )


def test_evaluator_repairs_one_invalid_locator_with_fixed_input() -> None:
    calls = []

    def fake_call(_profile, request):
        calls.append(request)
        if len(calls) == 1:
            payload = _provider_payload(
                outcome="local_repair",
                contract_satisfied=False,
                summary="One citation is malformed.",
                new_issues=[
                    {
                        "category": "coverage",
                        "severity": "blocking",
                        "candidate_locator": "book/candidate/direction-typo.md#body",
                        "evidence_locator": "book:direction:r1",
                        "explanation": "The candidate needs a local correction.",
                    }
                ],
                repair_brief="Cover the missing decision.",
                repair_scope=["direction"],
            )
        else:
            payload = _provider_payload(
                outcome="pass",
                contract_satisfied=True,
                summary="The fixed candidate passes.",
            )
        return ChatResult(
            content="{}",
            structured_output=payload,
            model_snapshot="judge-model",
            provider_snapshot="openai-compatible",
        )

    record = evaluate_candidate(_profile(), _input(), evaluator_call=fake_call)

    assert record.result.outcome == "pass"
    assert len(calls) == 2
    repair_context = json.loads(calls[1].messages[-1].content)
    assert repair_context["allowed_candidate_components"] == [
        "confirmed_decision_coverage",
        "constraints",
        "direction",
        "recommended_titles",
        "rolling_plan",
    ]
    assert repair_context["allowed_candidate_locators"] == [
        "candidate.confirmed_decision_coverage",
        "candidate.constraints",
        "candidate.direction",
        "candidate.recommended_titles",
        "candidate.rolling_plan",
        "candidate_artifact_id#confirmed_decision_coverage",
        "candidate_artifact_id#constraints",
        "candidate_artifact_id#direction",
        "candidate_artifact_id#recommended_titles",
        "candidate_artifact_id#rolling_plan",
    ]
    assert repair_context["allowed_candidate_artifact_locators"] == [
        "book/candidate/direction.md#confirmed_decision_coverage",
        "book/candidate/direction.md#constraints",
        "book/candidate/direction.md#direction",
        "book/candidate/direction.md#recommended_titles",
        "book/candidate/direction.md#rolling_plan",
    ]
    assert calls[0].messages[1].content == calls[1].messages[1].content


def test_evaluator_validation_repair_lists_chapter_component_contract() -> None:
    calls = []

    def fake_call(_profile, request):
        calls.append(request)
        payload = (
            _provider_payload(
                outcome="local_repair",
                contract_satisfied=False,
                summary="The Chapter issue uses an invalid component locator.",
                new_issues=[
                    {
                        "category": "prose",
                        "severity": "blocking",
                        "candidate_locator": "candidate.chapter_body",
                        "evidence_locator": "candidate.draft",
                        "explanation": "The candidate locator must name a typed component.",
                    }
                ],
                repair_brief="Repair the Chapter draft.",
                repair_scope=["draft"],
                candidate_kind="chapter",
                rubric_evidence_locator="arcs/arc-001/plan.md",
            )
            if len(calls) == 1
            else _provider_payload(
                outcome="pass",
                contract_satisfied=True,
                summary="The Chapter candidate passes.",
                candidate_kind="chapter",
                rubric_evidence_locator="arcs/arc-001/plan.md",
            )
        )
        return ChatResult(
            content="{}",
            structured_output=payload,
            model_snapshot="judge-model",
            provider_snapshot="openai-compatible",
        )

    record = evaluate_candidate(
        _profile(),
        _chapter_input(),
        evaluator_call=fake_call,
    )

    assert record.result.outcome == "pass"
    repair_context = json.loads(calls[1].messages[-1].content)
    assert repair_context["allowed_candidate_components"] == [
        "draft",
        "observations",
        "plan",
        "state_patch",
    ]
    assert repair_context["allowed_candidate_locators"] == [
        "candidate.draft",
        "candidate.observations",
        "candidate.plan",
        "candidate.state_patch",
        "candidate_artifact_id#draft",
        "candidate_artifact_id#observations",
        "candidate_artifact_id#plan",
        "candidate_artifact_id#state_patch",
    ]
    assert "component_fingerprints" in calls[0].messages[0].content
    assert "multiple blocking issues may identify the same component" in (
        calls[0].messages[0].content
    )


def test_evaluator_rejects_text_json_when_native_structured_output_is_missing() -> None:
    def fake_call(_profile, _request):
        return ChatResult(
            content=(
                '{"schema_version":1,"outcome":"pass","contract_satisfied":true,'
                '"summary":"looks valid","issues":[],"signals":[],'
                '"repair_brief":null,"upstream_blocker":null}'
            ),
            structured_output=None,
            model_snapshot="judge-model",
            provider_snapshot="openai-compatible",
        )

    try:
        evaluate_candidate(_profile(), _input(), evaluator_call=fake_call)
    except EvaluationValidationError as exc:
        assert "missing native Structured Output" in str(exc)
        assert "fallbacks are not supported" in str(exc)
    else:
        raise AssertionError("Evaluator accepted prompt-parsed JSON fallback output.")


def test_evaluator_retries_a_transient_provider_failure_before_validation() -> None:
    calls = 0
    retries: list[tuple[int, int]] = []

    def fake_call(_profile, _request):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError(
                "OpenAI-compatible provider request failed: "
                "[SSL: UNEXPECTED_EOF_WHILE_READING]"
            )
        return ChatResult(
            content="{}",
            structured_output=_provider_payload(
                outcome="pass",
                contract_satisfied=True,
                summary="The candidate passes.",
            ),
            model_snapshot="judge-model",
            provider_snapshot="openai-compatible",
        )

    record = evaluate_candidate(
        _profile(),
        _input(),
        evaluator_call=fake_call,
        transport_retry_limit=2,
        on_transport_retry=lambda retry, limit, _exc: retries.append((retry, limit)),
    )

    assert record.result.outcome == "pass"
    assert calls == 2
    assert retries == [(1, 2)]


def test_evaluation_fingerprint_binds_candidate_input_and_profile() -> None:
    def passing_call(_profile, _request):
        return ChatResult(
            content="{}",
            structured_output=_provider_payload(
                outcome="pass",
                contract_satisfied=True,
                summary="The candidate passes.",
            ),
            model_snapshot="judge-model",
            provider_snapshot="openai-compatible",
        )

    first = evaluate_candidate(_profile(), _input(), evaluator_call=passing_call)
    second = evaluate_candidate(
        _profile(),
        _input().model_copy(update={"candidate_revision": 2}),
        evaluator_call=passing_call,
    )
    third = evaluate_candidate(
        _profile().model_copy(update={"model": "judge-model-v2"}),
        _input(),
        evaluator_call=passing_call,
    )

    assert first.input_fingerprint
    assert first.input_fingerprint != second.input_fingerprint
    assert first.input_fingerprint != third.input_fingerprint


def test_repair_verification_carries_history_and_marks_late_discovery() -> None:
    first_input = _input()
    first = evaluate_candidate(
        _profile(),
        first_input,
        evaluator_call=lambda _profile, _request: ChatResult(
            content="{}",
            structured_output=_provider_payload(
                outcome="local_repair",
                contract_satisfied=False,
                summary="The constraints need repair.",
                new_issues=[
                    {
                        "category": "constraints",
                        "severity": "blocking",
                        "candidate_locator": "candidate.constraints",
                        "evidence_locator": "candidate.constraints",
                        "explanation": "One constraint conflicts with the direction.",
                    }
                ],
                repair_brief="Repair the conflicting constraint.",
                repair_scope=["constraints"],
            ),
            model_snapshot="judge-model",
            provider_snapshot="openai-compatible",
        ),
    )
    prior_issue_id = first.result.issues[0].issue_id
    assert prior_issue_id is not None
    repaired_candidate = first_input.candidate.model_copy(
        update={"constraints": {"confirmed": ["The conflict is repaired."]}}
    )
    repaired_fingerprints = component_fingerprints(repaired_candidate)
    contract = RepairContract(
        evaluation_id=first.evaluation_id,
        source_activation_id="activation-1",
        source_candidate_artifact_id=first.candidate_artifact_id,
        source_candidate_revision=1,
        next_candidate_revision=2,
        open_issue_ids=[prior_issue_id],
        repair_brief="Repair the conflicting constraint.",
        allowed_components=["constraints"],
        source_component_fingerprints=first_input.component_fingerprints,
    )
    second_input = EvaluationInput(
        identity=first_input.identity,
        candidate_run_id="candidate-run-1",
        checkpoint=first_input.checkpoint,
        candidate_artifact_id="book/candidate/direction-r2.md",
        candidate_revision=2,
        mode="repair_verification",
        candidate=repaired_candidate,
        component_fingerprints=repaired_fingerprints,
        evidence=first_input.evidence,
        deterministic_prechecks=first_input.deterministic_prechecks,
        rubric=first_input.rubric,
        review_history=[
            EvaluationHistoryEntry(
                evaluation_id=first.evaluation_id,
                candidate_revision=1,
                candidate_artifact_id=first.candidate_artifact_id,
                component_fingerprints=first_input.component_fingerprints,
                result=first.result,
            )
        ],
        expected_repair=contract,
    )
    second = evaluate_candidate(
        _profile(),
        second_input,
        evaluator_call=lambda _profile, _request: ChatResult(
            content="{}",
            structured_output=_provider_payload(
                outcome="local_repair",
                contract_satisfied=False,
                summary="The old issue is resolved; a direction issue was found.",
                prior_issue_checks=[
                    {
                        "issue_id": prior_issue_id,
                        "status": "resolved",
                        "evidence_locator": "candidate.constraints",
                        "explanation": "The repaired constraint now agrees.",
                    }
                ],
                new_issues=[
                    {
                        "category": "reader_promise",
                        "severity": "blocking",
                        "candidate_locator": "candidate.direction",
                        "evidence_locator": "candidate.direction",
                        "explanation": "A previously missed reader-promise conflict remains.",
                    }
                ],
                repair_brief="Repair the reader-promise conflict in the direction.",
                repair_scope=["direction"],
            ),
            model_snapshot="judge-model",
            provider_snapshot="openai-compatible",
        ),
    )

    assert second.result.resolved_issue_ids == [prior_issue_id]
    assert len(second.result.issues) == 1
    assert second.result.issues[0].discovery == "late_discovery"
    assert (
        second_input.component_fingerprints["direction"]
        == first_input.component_fingerprints["direction"]
    )


def test_repair_verification_rejects_missing_prior_issue_status() -> None:
    first_input = _input()
    first_issue_id = "issue-prior"
    prior_evaluation = _history_result(first_issue_id)
    contract = RepairContract(
        evaluation_id="evaluation-1",
        source_activation_id="activation-1",
        source_candidate_artifact_id=first_input.candidate_artifact_id,
        source_candidate_revision=1,
        next_candidate_revision=2,
        open_issue_ids=[first_issue_id],
        repair_brief="Repair the prior issue.",
        allowed_components=["direction"],
        source_component_fingerprints=first_input.component_fingerprints,
    )
    repair_input = EvaluationInput(
        identity=first_input.identity,
        candidate_run_id="candidate-run-1",
        checkpoint=first_input.checkpoint,
        candidate_artifact_id="book/candidate/direction-r2.md",
        candidate_revision=2,
        mode="repair_verification",
        candidate=first_input.candidate,
        component_fingerprints=first_input.component_fingerprints,
        evidence=first_input.evidence,
        deterministic_prechecks=first_input.deterministic_prechecks,
        rubric=first_input.rubric,
        review_history=[
            EvaluationHistoryEntry(
                evaluation_id="evaluation-1",
                candidate_revision=1,
                candidate_artifact_id=first_input.candidate_artifact_id,
                component_fingerprints=first_input.component_fingerprints,
                result=prior_evaluation,
            )
        ],
        expected_repair=contract,
    )

    with pytest.raises(EvaluationValidationError, match="every open prior issue"):
        evaluate_candidate(
            _profile(),
            repair_input,
            evaluator_call=lambda _profile, _request: ChatResult(
                content="{}",
                structured_output=_provider_payload(
                    outcome="local_repair",
                    contract_satisfied=False,
                    summary="The prior issue was omitted.",
                    new_issues=[
                        {
                            "category": "direction",
                            "severity": "blocking",
                            "candidate_locator": "candidate.direction",
                            "evidence_locator": "candidate.direction",
                            "explanation": "A direction repair is still required.",
                        }
                    ],
                    repair_brief="Repair the direction.",
                    repair_scope=["direction"],
                ),
                model_snapshot="judge-model",
                provider_snapshot="openai-compatible",
            ),
            max_validation_repairs=0,
        )


def _profile() -> LlmProfile:
    return LlmProfile(
        id="judge",
        name="Judge",
        protocol="openai-compatible",
        base_url="https://api.example.com/v1",
        api_key=SecretStr("secret"),
        model="judge-model",
    )


def _input() -> EvaluationInput:
    candidate = BookCandidateSnapshot(
        direction="# Direction\n\nA coherent direction.",
        constraints={},
        confirmed_decision_coverage=[],
        recommended_titles=[
            {"title": "One", "rationale": "One"},
            {"title": "Two", "rationale": "Two"},
            {"title": "Three", "rationale": "Three"},
        ],
        rolling_plan="Plan one rolling arc.",
    )
    return EvaluationInput(
        identity=AgentIdentity(project_id="project-1", role="book"),
        checkpoint="book_direction",
        candidate_artifact_id="book/candidate/direction.md",
        candidate_revision=1,
        candidate=candidate,
        component_fingerprints=component_fingerprints(candidate),
        evidence=[
            EvaluationEvidence(
                locator="book:direction:r1",
                excerpt="The approved discussion requires a coherent direction.",
            )
        ],
        deterministic_prechecks={"schema_valid": True},
        rubric=resolve_rubric("book_direction"),
    )


def _chapter_input() -> EvaluationInput:
    candidate = ChapterCandidateSnapshot(
        plan="# Plan\n\nReveal one fair clue.",
        draft="The witness places the wet key on the table.",
        observations={"based_on": "candidate.draft", "items": []},
        state_patch={"operations": []},
    )
    return EvaluationInput(
        identity=AgentIdentity(
            project_id="project-1",
            role="chapter",
            scope_id="chapter-001",
        ),
        candidate_run_id="chapter-run-1",
        checkpoint="chapter_candidate",
        candidate_artifact_id=(
            "chapters/chapter-001/agent/a/activation-1/c/manifest.json"
        ),
        candidate_revision=1,
        candidate=candidate,
        component_fingerprints=component_fingerprints(candidate),
        evidence=[
            EvaluationEvidence(
                locator="arcs/arc-001/plan.md",
                excerpt="The approved Arc requires a fair clue.",
            )
        ],
        deterministic_prechecks={"schema_valid": True},
        rubric=resolve_rubric("chapter"),
    )


def _provider_payload(
    *,
    outcome: str,
    contract_satisfied: bool,
    summary: str,
    new_issues: list[dict[str, object]] | None = None,
    signals: list[dict[str, object]] | None = None,
    prior_issue_checks: list[dict[str, object]] | None = None,
    repair_brief: str | None = None,
    repair_scope: list[str] | None = None,
    candidate_kind: CandidateKind = "book_direction",
    rubric_evidence_locator: str = "book:direction:r1",
) -> dict[str, object]:
    return {
        "schema_version": 2,
        "outcome": outcome,
        "contract_satisfied": contract_satisfied,
        "summary": summary,
        "rubric_checks": [
            {
                "dimension_id": item.dimension_id,
                "status": "pass",
                "evidence_locator": rubric_evidence_locator,
                "explanation": "Checked against the approved evidence.",
            }
            for item in resolve_rubric(candidate_kind).dimensions
        ],
        "prior_issue_checks": prior_issue_checks or [],
        "new_issues": new_issues or [],
        "signals": signals or [],
        "repair_brief": repair_brief,
        "repair_scope": repair_scope or [],
        "upstream_blocker": None,
    }


def _history_result(issue_id: str) -> EvaluationResult:
    return EvaluationResult(
        schema_version=2,
        outcome="local_repair",
        contract_satisfied=False,
        summary="A prior direction issue remains open.",
        issues=[
            EvaluationIssue(
                issue_id=issue_id,
                category="direction",
                severity="blocking",
                candidate_locator="candidate.direction",
                evidence_locator="candidate.direction",
                explanation="The direction still needs repair.",
            )
        ],
        signals=[],
        repair_brief="Repair the direction.",
        upstream_blocker=None,
        repair_scope=["direction"],
        new_issue_ids=[issue_id],
    )
