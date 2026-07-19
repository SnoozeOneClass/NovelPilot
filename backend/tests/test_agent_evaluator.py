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
    RepairContract,
)
from app.harness.agents.rubrics import component_fingerprints, resolve_rubric
from app.llm.gateway import ChatResult
from app.schemas.profiles import LlmProfile
from app.storage.json_files import read_json


def test_evaluator_provider_boundary_contains_only_semantic_context_and_schema() -> None:
    captured = {}
    evaluation_input = _book_input().model_copy(
        update={
            "evidence": [
                *_book_input().evidence,
                EvaluationEvidence(
                    locator="book/state.json",
                    excerpt=json.dumps(
                        {
                            "schema_version": 1,
                            "version": 7,
                            "book_direction_version": 3,
                            "confirmed_decisions": ["Keep clues fair."],
                        }
                    ),
                ),
            ],
            "deterministic_prechecks": {
                "direction_version": 3,
                "has_direction": True,
            },
        }
    )

    def fake_call(_profile, request):
        captured["request"] = request
        return _result(_provider_payload(summary="The candidate passes."))

    evaluate_candidate(_profile(), evaluation_input, evaluator_call=fake_call)
    request = captured["request"]
    payload = json.loads(request.messages[1].content)
    serialized_payload = json.dumps(payload, ensure_ascii=False)
    assert payload["approved_evidence"][0] == {
        "excerpt": "The approved discussion requires a coherent direction."
    }
    assert "Keep clues fair." in payload["approved_evidence"][1]["excerpt"]
    assert "candidate_revision" not in serialized_payload
    assert "candidate_artifact_id" not in serialized_payload
    assert "component_fingerprints" not in serialized_payload
    assert "evaluation_id" not in serialized_payload
    assert "book/candidate/direction.md" not in serialized_payload
    assert "book:direction:r1" not in serialized_payload
    assert '"version": 7' not in serialized_payload
    assert "book_direction_version" not in serialized_payload
    assert "direction_version" not in serialized_payload
    assert payload["deterministic_prechecks"] == {"has_direction": True}

    property_names: set[str] = set()

    def collect(value: object) -> None:
        if isinstance(value, dict):
            properties = value.get("properties")
            if isinstance(properties, dict):
                property_names.update(properties)
            for child in value.values():
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)

    collect(request.response_schema.json_schema)
    assert not property_names.intersection(
        {
            "schema_version",
            "dimension_id",
            "issue_id",
            "candidate_locator",
            "evidence_locator",
            "repair_scope",
            "contract_revision",
            "committed_evidence_locator",
        }
    )


def test_evaluator_binds_semantic_issue_to_internal_scope_and_persists_views(
    tmp_path,
) -> None:
    record = evaluate_candidate(
        _profile(),
        _book_input(),
        evaluator_call=lambda _profile, _request: _result(
            _provider_payload(
                outcome="local_repair",
                contract_satisfied=False,
                summary="The reader promise needs one repair.",
                new_issues=[
                    {
                        "category": "reader_promise",
                        "severity": "blocking",
                        "affected_area": "story_direction",
                        "evidence_hint": "The direction is coherent but not concrete.",
                        "explanation": "State the central reader promise concretely.",
                    }
                ],
                repair_brief="Clarify the central reader promise.",
            )
        ),
    )

    assert record.result.repair_scope == ["direction"]
    assert record.result.issues[0].candidate_locator == "candidate.direction"
    assert record.result.issues[0].evidence_locator == "candidate.direction"
    assert record.result.issues[0].issue_id is not None
    persist_evaluation_views(
        tmp_path,
        record,
        evaluation_path="book/candidate/evaluation.json",
        review_path="book/candidate/review.md",
        verification_path="book/candidate/verification.json",
    )
    evaluation = read_json(tmp_path / "book/candidate/evaluation.json")
    verification = read_json(tmp_path / "book/candidate/verification.json")
    assert evaluation["evaluation_id"] == verification["evaluation_id"]
    assert verification["commit_allowed"] is False


def test_evaluator_rejects_semantic_area_from_another_candidate_kind() -> None:
    with pytest.raises(
        EvaluationValidationError,
        match="semantic area outside this candidate kind",
    ):
        evaluate_candidate(
            _profile(),
            _book_input(),
            evaluator_call=lambda _profile, _request: _result(
                _provider_payload(
                    outcome="local_repair",
                    contract_satisfied=False,
                    summary="An invalid semantic area was selected.",
                    new_issues=[
                        {
                            "category": "prose",
                            "severity": "blocking",
                            "affected_area": "chapter_draft",
                            "evidence_hint": "The prose is unclear.",
                            "explanation": "This area does not belong to a Book candidate.",
                        }
                    ],
                    repair_brief="Repair the prose.",
                )
            ),
            max_validation_repairs=0,
        )


def test_evaluator_validation_repair_never_teaches_exact_control_protocol() -> None:
    calls = []

    def fake_call(_profile, request):
        calls.append(request)
        if len(calls) == 1:
            payload = _provider_payload(
                outcome="local_repair",
                contract_satisfied=False,
                summary="The first output includes a forbidden control field.",
                new_issues=[
                    {
                        "category": "coverage",
                        "severity": "blocking",
                        "affected_area": "story_direction",
                        "evidence_hint": "The direction omits the promise.",
                        "explanation": "The promise is missing.",
                        "candidate_locator": "candidate.direction",
                    }
                ],
                repair_brief="Cover the promise.",
            )
        else:
            payload = _provider_payload(summary="The corrected result passes.")
        return _result(payload)

    record = evaluate_candidate(_profile(), _book_input(), evaluator_call=fake_call)

    assert record.result.outcome == "pass"
    assert len(calls) == 2
    repair_context = json.loads(calls[1].messages[-1].content)
    serialized = json.dumps(repair_context, ensure_ascii=False)
    assert "allowed_candidate_locators" not in serialized
    assert "allowed_evidence_locator_roots" not in serialized
    assert "candidate_artifact_id" not in serialized
    assert "component_fingerprints" not in serialized
    assert repair_context["semantic_evaluation_context"] == json.loads(
        calls[0].messages[1].content
    )


def test_evaluator_rejects_text_json_without_native_structured_output() -> None:
    with pytest.raises(
        EvaluationValidationError,
        match="missing native Structured Output",
    ):
        evaluate_candidate(
            _profile(),
            _book_input(),
            evaluator_call=lambda _profile, _request: ChatResult(
                content='{"outcome":"pass"}',
                structured_output=None,
                model_snapshot="judge-model",
                provider_snapshot="openai-compatible",
            ),
            max_validation_repairs=0,
        )


def test_evaluator_retries_transient_transport_failure() -> None:
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
        return _result(_provider_payload(summary="The candidate passes."))

    record = evaluate_candidate(
        _profile(),
        _book_input(),
        evaluator_call=fake_call,
        transport_retry_limit=2,
        on_transport_retry=lambda retry, limit, _exc: retries.append((retry, limit)),
    )
    assert record.result.outcome == "pass"
    assert calls == 2
    assert retries == [(1, 2)]


def test_evaluation_fingerprint_still_binds_internal_candidate_and_profile() -> None:
    passing = lambda _profile, _request: _result(  # noqa: E731
        _provider_payload(summary="The candidate passes.")
    )
    first = evaluate_candidate(_profile(), _book_input(), evaluator_call=passing)
    second = evaluate_candidate(
        _profile(),
        _book_input().model_copy(update={"candidate_revision": 2}),
        evaluator_call=passing,
    )
    third = evaluate_candidate(
        _profile().model_copy(update={"model": "judge-model-v2"}),
        _book_input(),
        evaluator_call=passing,
    )
    assert first.input_fingerprint != second.input_fingerprint
    assert first.input_fingerprint != third.input_fingerprint


def test_repair_verification_binds_prior_issues_by_order_and_records_late_findings() -> None:
    first_input = _book_input()
    first = evaluate_candidate(
        _profile(),
        first_input,
        evaluator_call=lambda _profile, _request: _result(
            _provider_payload(
                outcome="local_repair",
                contract_satisfied=False,
                summary="The constraints conflict.",
                new_issues=[
                    {
                        "category": "constraints",
                        "severity": "blocking",
                        "affected_area": "story_constraints",
                        "evidence_hint": "The constraints conflict with the direction.",
                        "explanation": "Remove the contradiction.",
                    }
                ],
                repair_brief="Repair the constraints.",
            )
        ),
    )
    prior_issue_id = first.result.issues[0].issue_id
    assert prior_issue_id is not None
    repaired_candidate = first_input.candidate.model_copy(
        update={"constraints": {"must_avoid": ["No contradiction."]}}
    )
    contract = RepairContract(
        evaluation_id=first.evaluation_id,
        source_activation_id="activation-1",
        source_candidate_artifact_id=first.candidate_artifact_id,
        source_candidate_revision=1,
        next_candidate_revision=2,
        open_issue_ids=[prior_issue_id],
        repair_brief="Repair the constraints.",
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
        component_fingerprints=component_fingerprints(repaired_candidate),
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
        evaluator_call=lambda _profile, _request: _result(
            _provider_payload(
                outcome="local_repair",
                contract_satisfied=False,
                summary="The old issue is resolved; a new direction issue remains.",
                prior_issue_checks=[
                    {
                        "status": "resolved",
                        "evidence_hint": "The contradiction is gone.",
                        "explanation": "The repaired constraint now agrees.",
                    }
                ],
                new_issues=[
                    {
                        "category": "reader_promise",
                        "severity": "blocking",
                        "affected_area": "story_direction",
                        "evidence_hint": "The direction is still too general.",
                        "explanation": "Clarify the reader promise.",
                    }
                ],
                repair_brief="Clarify the direction.",
            )
        ),
    )
    assert second.result.resolved_issue_ids == [prior_issue_id]
    assert second.result.repair_scope == ["direction"]
    assert second.result.issues[0].discovery == "late_discovery"


def test_chapter_cross_loop_semantics_are_bound_to_current_arc_control_data() -> None:
    record = evaluate_candidate(
        _profile(),
        _chapter_input(),
        evaluator_call=lambda _profile, _request: _result(
            _provider_payload(
                outcome="cross_loop_escalation",
                contract_satisfied=False,
                summary="The chapter cannot satisfy the current Arc promise.",
                candidate_kind="chapter",
                upstream_blocker={
                    "upper_scope": "story_arc_contract",
                    "contract_concern": "The Arc requires the bell clue to remain fair.",
                    "evidence_hint": "The approved Arc requires a fair bell clue.",
                    "impossibility_reason": "The current Chapter instruction forbids showing it.",
                },
            )
        ),
    )
    blocker = record.result.upstream_blocker
    assert blocker is not None
    assert blocker.owner == "story_arc"
    assert blocker.contract_revision == 6
    assert blocker.committed_evidence_locator == "arcs/arc-001/plan.md"


def _profile() -> LlmProfile:
    return LlmProfile(
        id="judge",
        name="Judge",
        protocol="openai-compatible",
        base_url="https://api.example.com/v1",
        api_key=SecretStr("secret"),
        model="judge-model",
    )


def _book_input() -> EvaluationInput:
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
        plan="Reveal one fair bell clue.",
        draft="The bell remains hidden from every witness.",
        observations={"events": []},
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
        candidate_artifact_id="chapters/chapter-001/candidate.json",
        candidate_revision=1,
        candidate=candidate,
        component_fingerprints=component_fingerprints(candidate),
        evidence=[
            EvaluationEvidence(
                locator="book/state.json",
                excerpt='{"schema_version":1,"version":11}',
            ),
            EvaluationEvidence(
                locator="arcs/arc-001/plan.md",
                excerpt="The approved Arc requires a fair bell clue.",
            ),
            EvaluationEvidence(
                locator="arcs/arc-001/state.json",
                excerpt='{"schema_version":1,"version":6}',
            ),
        ],
        deterministic_prechecks={"schema_valid": True},
        rubric=resolve_rubric("chapter"),
    )


def _provider_payload(
    *,
    outcome: str = "pass",
    contract_satisfied: bool = True,
    summary: str,
    new_issues: list[dict[str, object]] | None = None,
    signals: list[dict[str, object]] | None = None,
    prior_issue_checks: list[dict[str, object]] | None = None,
    repair_brief: str | None = None,
    upstream_blocker: dict[str, object] | None = None,
    candidate_kind: CandidateKind = "book_direction",
) -> dict[str, object]:
    return {
        "outcome": outcome,
        "contract_satisfied": contract_satisfied,
        "summary": summary,
        "rubric_checks": [
            {
                "status": "pass",
                "evidence_hint": "The candidate aligns with the supplied semantic evidence.",
                "explanation": "The rubric instruction was checked semantically.",
            }
            for _item in resolve_rubric(candidate_kind).dimensions
        ],
        "prior_issue_checks": prior_issue_checks or [],
        "new_issues": new_issues or [],
        "signals": signals or [],
        "repair_brief": repair_brief,
        "upstream_blocker": upstream_blocker,
    }


def _result(payload: dict[str, object]) -> ChatResult:
    return ChatResult(
        content="{}",
        structured_output=payload,
        model_snapshot="judge-model",
        provider_snapshot="openai-compatible",
    )
