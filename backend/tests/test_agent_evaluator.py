from pydantic import SecretStr

from app.harness.agents.evaluator import (
    EvaluationValidationError,
    evaluate_candidate,
    persist_evaluation_views,
)
from app.harness.agents.models import AgentIdentity, EvaluationEvidence, EvaluationInput
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
            structured_output={
                "schema_version": 1,
                "outcome": "pass",
                "contract_satisfied": True,
                "summary": "候选满足本轮契约。",
                "issues": [],
                "signals": [
                    {
                        "name": "contract_alignment",
                        "value": True,
                        "evidence_locator": "book:direction:r1",
                    }
                ],
                "repair_brief": None,
                "upstream_blocker": None,
            },
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
            structured_output={
                "schema_version": 1,
                "outcome": "local_repair",
                "contract_satisfied": False,
                "summary": "存在冲突。",
                "issues": [
                    {
                        "category": "continuity",
                        "severity": "blocking",
                        "candidate_locator": "candidate:paragraph-2",
                        "evidence_locator": "invented:evidence",
                        "explanation": "与证据冲突。",
                    }
                ],
                "signals": [],
                "repair_brief": "修改当前候选，不要改动已提交内容。",
                "upstream_blocker": None,
            },
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
            structured_output={
                "schema_version": 1,
                "outcome": "local_repair",
                "contract_satisfied": False,
                "summary": "The candidate needs one local repair.",
                "issues": [
                    {
                        "category": "coverage",
                        "severity": "blocking",
                        "candidate_locator": "candidate_artifact_id#direction_markdown",
                        "evidence_locator": "deterministic_prechecks.coverage_count",
                        "explanation": "The candidate omits a confirmed decision.",
                    }
                ],
                "signals": [
                    {
                        "name": "decision_coverage",
                        "value": False,
                        "evidence_locator": "book:direction:r1#confirmed_decisions",
                    }
                ],
                "repair_brief": "Cover the missing confirmed decision.",
                "upstream_blocker": None,
            },
            model_snapshot="judge-model",
            provider_snapshot="openai-compatible",
        )

    record = evaluate_candidate(_profile(), _input(), evaluator_call=fake_call)

    assert record.result.outcome == "local_repair"


def test_evaluator_repairs_one_invalid_locator_with_fixed_input() -> None:
    calls = []

    def fake_call(_profile, request):
        calls.append(request)
        if len(calls) == 1:
            payload = {
                "schema_version": 1,
                "outcome": "local_repair",
                "contract_satisfied": False,
                "summary": "One citation is malformed.",
                "issues": [
                    {
                        "category": "coverage",
                        "severity": "blocking",
                        "candidate_locator": "book/candidate/direction-typo.md#body",
                        "evidence_locator": "book:direction:r1",
                        "explanation": "The candidate needs a local correction.",
                    }
                ],
                "signals": [],
                "repair_brief": "Cover the missing decision.",
                "upstream_blocker": None,
            }
        else:
            payload = {
                "schema_version": 1,
                "outcome": "pass",
                "contract_satisfied": True,
                "summary": "The fixed candidate passes.",
                "issues": [],
                "signals": [],
                "repair_brief": None,
                "upstream_blocker": None,
            }
        return ChatResult(
            content="{}",
            structured_output=payload,
            model_snapshot="judge-model",
            provider_snapshot="openai-compatible",
        )

    record = evaluate_candidate(_profile(), _input(), evaluator_call=fake_call)

    assert record.result.outcome == "pass"
    assert len(calls) == 2
    assert "allowed_candidate_locator_roots" in calls[1].messages[-1].content
    assert calls[0].messages[1].content == calls[1].messages[1].content


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
            structured_output={
                "schema_version": 1,
                "outcome": "pass",
                "contract_satisfied": True,
                "summary": "The candidate passes.",
                "issues": [],
                "signals": [],
                "repair_brief": None,
                "upstream_blocker": None,
            },
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
            structured_output={
                "schema_version": 1,
                "outcome": "pass",
                "contract_satisfied": True,
                "summary": "The candidate passes.",
                "issues": [],
                "signals": [],
                "repair_brief": None,
                "upstream_blocker": None,
            },
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
    return EvaluationInput(
        identity=AgentIdentity(project_id="project-1", role="book"),
        checkpoint="book_direction",
        candidate_artifact_id="book/candidate/direction.md",
        candidate_revision=1,
        candidate_content="# Direction\n\nA coherent direction.",
        evidence=[
            EvaluationEvidence(
                locator="book:direction:r1",
                excerpt="The approved discussion requires a coherent direction.",
            )
        ],
        deterministic_prechecks={"schema_valid": True},
        rubric_version="book-direction-v1",
    )
