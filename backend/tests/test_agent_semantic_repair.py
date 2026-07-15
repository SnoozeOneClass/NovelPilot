from pathlib import Path

import pytest
from pydantic import SecretStr

from app.harness.agents import loop_runners
from app.harness.agents.domain_tools import build_default_tool_registry
from app.harness.agents.loop_runners import (
    AgentControlCheckpoint,
    run_book_direction_agent,
    run_story_arc_agent,
)
from app.harness.agents.models import (
    AgentIdentity,
    EvaluationInput,
    EvaluationRecord,
    EvaluationResult,
)
from app.harness.agents.persistence import read_agent_state
from app.harness.agents.policy import ResolvedAgentPolicy
from app.harness.agents.runtime import AgentRuntime
from app.llm.gateway import ChatResult, ToolCall
from app.schemas.profiles import LlmProfile
from app.schemas.projects import ProjectMetadata
from app.schemas.setup import SetupStateDocument


def test_user_decision_tool_is_not_exposed_to_downstream_agents() -> None:
    assert "request_user_decision" not in loop_runners.STORY_ARC_AGENT_TOOLS
    assert "request_user_decision" not in loop_runners.CHAPTER_AGENT_TOOLS


def test_story_arc_agent_repairs_local_semantic_failure_with_frozen_budget(
    tmp_path: Path,
    monkeypatch,
) -> None:
    profile = LlmProfile(
        id="main",
        name="Main",
        protocol="openai-compatible",
        base_url="https://api.example.com/v1",
        api_key=SecretStr("secret"),
        model="story-model",
    )
    policy = ResolvedAgentPolicy(
        role="story_arc",
        profile=profile,
        evaluator_profile=profile,
        max_turns=4,
        tool_schema_repair_limit=1,
        semantic_revision_limit=2,
        transport_retry_limit=1,
    )
    tool_responses = iter(
        [
            _arc_tool_response("call-initial", "Initial plan with a continuity defect."),
            _arc_tool_response("call-revised", "Revised plan preserves committed evidence."),
        ]
    )
    requests = []

    def fake_chat(_profile, request):
        requests.append(request)
        return next(tool_responses)

    evaluation_count = 0
    events: list[dict[str, object]] = []

    def fake_evaluate(_profile, evaluation_input: EvaluationInput):
        nonlocal evaluation_count
        evaluation_count += 1
        passed = evaluation_count == 2
        return EvaluationRecord(
            candidate_artifact_id=evaluation_input.candidate_artifact_id,
            candidate_revision=evaluation_input.candidate_revision,
            evaluator_profile_id="main",
            evaluator_model_snapshot="story-model",
            evaluator_provider_snapshot="openai-compatible",
            rubric_version=evaluation_input.rubric_version,
            result=EvaluationResult(
                schema_version=1,
                outcome="pass" if passed else "local_repair",
                contract_satisfied=passed,
                summary="Candidate passes." if passed else "Repair the current arc candidate.",
                issues=[],
                signals=[],
                repair_brief=None if passed else "Keep the clue chronology consistent.",
                upstream_blocker=None,
            ),
        )

    monkeypatch.setattr(loop_runners, "_require_policy_capabilities", lambda _policy: None)
    monkeypatch.setattr(loop_runners, "evaluate_candidate", fake_evaluate)
    runtime = AgentRuntime(build_default_tool_registry(), chat_call=fake_chat)

    result = run_story_arc_agent(
        tmp_path,
        ProjectMetadata(project_id="project-1"),
        policy,
        arc_id="arc-001",
        intent="create",
        expected_revision=0,
        instruction="Create the first rolling arc.",
        on_event=events.append,
        runtime=runtime,
    )

    assert result.evaluation.result.outcome == "pass"
    assert result.proposal.plan_markdown.startswith("Revised plan")
    assert evaluation_count == 2
    assert len(requests) == 2
    assert "Keep the clue chronology consistent" in requests[1].messages[-1].content
    state = read_agent_state(
        tmp_path,
        AgentIdentity(project_id="project-1", role="story_arc", scope_id="arc-001"),
    )
    assert state.budgets is not None
    assert state.budgets.used_turns == 2
    assert state.budgets.used_semantic_revisions == 1
    activation_roots = sorted((tmp_path / "arcs" / "arc-001" / "agent" / "a").iterdir())
    assert len(activation_roots) == 2
    assert all((root / "evaluation.json").is_file() for root in activation_roots)
    assert sum((root / "semantic-repair.json").is_file() for root in activation_roots) == 1
    event_kinds = [event["kind"] for event in events]
    assert event_kinds.count("agent_evaluation_started") == 2
    assert event_kinds.count("agent_evaluation_completed") == 2
    completed = [
        event for event in events if event["kind"] == "agent_evaluation_completed"
    ]
    assert completed[-1]["outcome"] == "pass"
    for event in completed:
        paths = event["evidence_paths"]
        assert isinstance(paths, list)
        assert any(
            isinstance(path, str) and path.endswith("evaluation.json")
            for path in paths
        )


def test_book_evaluator_needs_user_becomes_one_standard_discussion_question(
    tmp_path: Path,
    monkeypatch,
) -> None:
    profile = LlmProfile(
        id="main",
        name="Main",
        protocol="openai-compatible",
        base_url="https://api.example.com/v1",
        api_key=SecretStr("secret"),
        model="story-model",
    )
    policy = ResolvedAgentPolicy(
        role="book",
        profile=profile,
        evaluator_profile=profile,
        max_turns=4,
        tool_schema_repair_limit=1,
        semantic_revision_limit=1,
        transport_retry_limit=1,
    )
    candidate_arguments = {
        "expected_revision": 1,
        "candidate_revision": 1,
        "direction_markdown": "# Direction\n\nA fair-play mystery with an unresolved ending cost.",
        "constraints": {
            "confirmed": [],
            "must_preserve": ["All reveals use visible evidence."],
            "must_avoid": [],
            "creative_freedoms": [],
            "open_decisions": ["Which relationship pays the ending cost."],
        },
        "confirmed_decision_coverage": [],
        "recommended_titles": [
            {"title": "Harbor of Trust", "rationale": "The confirmed formal title."},
            {"title": "Eleven Minutes", "rationale": "Names the missing interval."},
            {"title": "The Closed Window", "rationale": "Names a recurring clue."},
        ],
        "rolling_plan_markdown": "Plan only the first active story arc.",
    }
    decision_arguments = {
        "question": "结局必须由哪段关系承担不可逆的代价？",
        "context": "审查无法替用户决定最终的关系代价。",
        "suggestions": [
            {
                "label": "师徒决裂",
                "message": "让师徒关系承担永久决裂的代价。",
                "rationale": "直接兑现信任主题。",
            },
            {
                "label": "手足分离",
                "message": "让手足关系承担永久分离的代价。",
                "rationale": "把旧案后果落到家庭关系。",
            },
        ],
    }
    responses = iter(
        [
            ChatResult(
                content="",
                tool_calls=[
                    ToolCall(
                        id="book-candidate",
                        name="submit_book_direction_candidate",
                        arguments=candidate_arguments,
                        raw_arguments="{}",
                    )
                ],
                finish_reason="tool_call",
                model_snapshot="story-model",
                provider_snapshot="openai-compatible",
            ),
            ChatResult(
                content="",
                tool_calls=[
                    ToolCall(
                        id="book-user-decision",
                        name="request_user_decision",
                        arguments=decision_arguments,
                        raw_arguments="{}",
                    )
                ],
                finish_reason="tool_call",
                model_snapshot="story-model",
                provider_snapshot="openai-compatible",
            ),
        ]
    )

    def fake_evaluate(_profile, evaluation_input: EvaluationInput):
        return EvaluationRecord(
            candidate_artifact_id=evaluation_input.candidate_artifact_id,
            candidate_revision=evaluation_input.candidate_revision,
            evaluator_profile_id="main",
            evaluator_model_snapshot="story-model",
            evaluator_provider_snapshot="openai-compatible",
            rubric_version=evaluation_input.rubric_version,
            result=EvaluationResult(
                schema_version=1,
                outcome="needs_user",
                contract_satisfied=False,
                summary="The ending cost requires an explicit human choice.",
                issues=[],
                signals=[],
                repair_brief=None,
                upstream_blocker=None,
            ),
        )

    monkeypatch.setattr(loop_runners, "_require_policy_capabilities", lambda _policy: None)
    monkeypatch.setattr(loop_runners, "evaluate_candidate", fake_evaluate)
    runtime = AgentRuntime(
        build_default_tool_registry(),
        chat_call=lambda _profile, _request: next(responses),
    )
    state = SetupStateDocument(
        revision=1,
        direction_draft="# Direction\n\nA fair-play mystery.",
        discussion_summary="The story direction is otherwise converged.",
        selected_title="Harbor of Trust",
    )

    with pytest.raises(AgentControlCheckpoint) as caught:
        run_book_direction_agent(
            tmp_path,
            ProjectMetadata(project_id="project-1"),
            state,
            policy,
            runtime=runtime,
        )

    checkpoint = caught.value
    assert checkpoint.run_result.outcome == "waiting_user"
    assert checkpoint.payload["question"] == decision_arguments["question"]
    assert checkpoint.payload["suggestions"] == decision_arguments["suggestions"]
    assert (tmp_path / checkpoint.artifact_path).is_file()


def test_story_arc_blocker_stops_at_typed_control_checkpoint(
    tmp_path: Path,
    monkeypatch,
) -> None:
    profile = LlmProfile(
        id="main",
        name="Main",
        protocol="openai-compatible",
        base_url="https://api.example.com/v1",
        api_key=SecretStr("secret"),
        model="story-model",
    )
    policy = ResolvedAgentPolicy(
        role="story_arc",
        profile=profile,
        evaluator_profile=profile,
        max_turns=2,
        tool_schema_repair_limit=1,
        semantic_revision_limit=1,
        transport_retry_limit=1,
    )
    response = ChatResult(
        content="",
        tool_calls=[
            ToolCall(
                id="call-blocker",
                name="report_blocker",
                arguments={
                    "kind": "cross_loop",
                    "summary": "The approved ending contradicts committed evidence.",
                    "evidence": ["chapters/chapter-001/final.md#ending"],
                    "target_owner": "book",
                    "contract_field": "ending_constraint",
                    "contract_revision": 2,
                    "committed_evidence_locator": (
                        "chapters/chapter-001/final.md#ending"
                    ),
                    "impossibility_reason": (
                        "No current arc can satisfy both instructions."
                    ),
                },
                raw_arguments="{}",
            )
        ],
        finish_reason="tool_call",
        model_snapshot="story-model",
        provider_snapshot="openai-compatible",
    )
    monkeypatch.setattr(loop_runners, "_require_policy_capabilities", lambda _policy: None)
    monkeypatch.setattr(
        loop_runners,
        "evaluate_candidate",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("A blocker checkpoint must not be evaluated as a candidate.")
        ),
    )
    runtime = AgentRuntime(
        build_default_tool_registry(),
        chat_call=lambda _profile, _request: response,
    )

    with pytest.raises(AgentControlCheckpoint) as caught:
        run_story_arc_agent(
            tmp_path,
            ProjectMetadata(project_id="project-1"),
            policy,
            arc_id="arc-001",
            intent="create",
            expected_revision=0,
            instruction="Create the first rolling arc.",
            runtime=runtime,
        )

    checkpoint = caught.value
    assert checkpoint.run_result.outcome == "blocked"
    assert checkpoint.payload["routing_status"] == "proposal_only"
    assert checkpoint.payload["target_owner"] == "book"
    assert (tmp_path / checkpoint.artifact_path).is_file()


def _arc_tool_response(call_id: str, plan_markdown: str) -> ChatResult:
    return ChatResult(
        content="",
        tool_calls=[
            ToolCall(
                id=call_id,
                name="submit_story_arc_candidate",
                arguments={
                    "expected_revision": 0,
                    "intent": "create",
                    "arc_id": "arc-001",
                    "plan_markdown": plan_markdown,
                    "target_chapter_count": 10,
                    "change_summary": "Created or repaired the rolling arc.",
                },
                raw_arguments="{}",
            )
        ],
        finish_reason="tool_call",
        model_snapshot="story-model",
        provider_snapshot="openai-compatible",
    )
