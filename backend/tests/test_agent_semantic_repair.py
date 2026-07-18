import json
from pathlib import Path

import pytest
from pydantic import SecretStr

from app.harness.agents import loop_runners
from app.harness.agents.domain_tools import build_default_tool_registry
from app.harness.agents.loop_runners import (
    AgentControlCheckpoint,
    run_book_direction_agent,
    run_chapter_agent,
    run_story_arc_agent,
)
from app.harness.agents.models import (
    AgentIdentity,
    EvaluationInput,
    EvaluationIssue,
    EvaluationRecord,
    EvaluationResult,
    RepairContract,
)
from app.harness.agents.persistence import read_agent_state
from app.harness.agents.policy import ResolvedAgentPolicy
from app.harness.agents.runtime import AgentActivation, AgentRuntime
from app.llm.gateway import ChatResult, ToolCall
from app.schemas.profiles import LlmProfile
from app.schemas.projects import ProjectMetadata
from app.schemas.setup import SetupStateDocument


def test_user_decision_tool_is_not_exposed_to_downstream_agents() -> None:
    assert "request_user_decision" not in loop_runners.STORY_ARC_AGENT_TOOLS
    assert "request_user_decision" not in loop_runners.CHAPTER_AGENT_TOOLS


def test_story_arc_agent_repairs_local_semantic_failure_with_candidate_budget(
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
    evaluation_inputs: list[EvaluationInput] = []
    events: list[dict[str, object]] = []

    def fake_evaluate(_profile, evaluation_input: EvaluationInput, **_kwargs):
        nonlocal evaluation_count
        evaluation_count += 1
        evaluation_inputs.append(evaluation_input)
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
                issues=(
                    []
                    if passed
                    else [
                        EvaluationIssue(
                            category="continuity",
                            severity="blocking",
                            candidate_locator="candidate.plan",
                            evidence_locator="candidate.plan",
                            explanation="The clue chronology is inconsistent.",
                        )
                    ]
                ),
                signals=[],
                repair_brief=None if passed else "Keep the clue chronology consistent.",
                upstream_blocker=None,
                repair_scope=[] if passed else ["plan"],
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
    assert [item.mode for item in evaluation_inputs] == [
        "initial",
        "repair_verification",
    ]
    assert [item.candidate_revision for item in evaluation_inputs] == [1, 2]
    assert len(evaluation_inputs[1].review_history) == 1
    assert evaluation_inputs[1].expected_repair is not None
    assert evaluation_inputs[1].expected_repair.allowed_components == ["plan"]
    assert len(requests) == 2
    assert "Keep the clue chronology consistent" in requests[1].messages[-1].content
    assert "complete_review_history" in requests[1].messages[-1].content
    assert "fresh candidate workspace" not in requests[1].messages[-1].content
    state = read_agent_state(
        tmp_path,
        AgentIdentity(project_id="project-1", role="story_arc", scope_id="arc-001"),
    )
    assert state.budgets is not None
    assert state.budgets.used_turns == 1
    assert state.budgets.used_semantic_revisions == 1
    activation_roots = sorted((tmp_path / "arcs" / "arc-001" / "agent" / "a").iterdir())
    assert len(activation_roots) == 2
    assert all((root / "evaluation.json").is_file() for root in activation_roots)
    assert sum((root / "semantic-repair.json").is_file() for root in activation_roots) == 1
    repair_chain = json.loads(
        (tmp_path / "arcs" / "arc-001" / "agent" / "repair-chain.json").read_text(
            encoding="utf-8"
        )
    )
    assert [entry["candidate_revision"] for entry in repair_chain["entries"]] == [
        1,
        2,
    ]
    assert repair_chain["entries"][1]["changed_components"] == ["plan"]
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

    def fake_evaluate(_profile, evaluation_input: EvaluationInput, **_kwargs):
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


def test_chapter_repair_chain_preserves_draft_before_late_discovery(
    tmp_path: Path,
    monkeypatch,
) -> None:
    profile = LlmProfile(
        id="main",
        name="Main",
        protocol="openai-compatible",
        base_url="https://api.example.com/v1",
        api_key=SecretStr("secret"),
        model="chapter-model",
    )
    policy = ResolvedAgentPolicy(
        role="chapter",
        profile=profile,
        evaluator_profile=profile,
        max_turns=8,
        tool_schema_repair_limit=2,
        semantic_revision_limit=10,
        transport_retry_limit=1,
    )
    evaluation_inputs: list[EvaluationInput] = []
    chat_calls = 0

    def fake_chat(_profile, request):
        nonlocal chat_calls
        chat_calls += 1
        tool_names = {tool.name for tool in request.tools}
        prior_calls = {
            call.name for message in request.messages for call in message.tool_calls
        }
        prompt = "\n".join(message.content for message in request.messages)
        if "plan_chapter_candidate" in tool_names:
            name, arguments = _next_initial_chapter_tool(prior_calls)
        elif '"next_candidate_revision": 2' in prompt:
            name = "submit_chapter_candidate"
            arguments = _chapter_submission(candidate_revision=2, draft_revision=1)
        elif "edit_chapter_draft" not in prior_calls:
            name = "edit_chapter_draft"
            arguments = {
                "chapter_id": "chapter-001",
                "expected_revision": 0,
                "draft_revision": 1,
                "next_draft_revision": 2,
                "anchor": "The witness places the wet key on the table.",
                "replacement": (
                    "The witness places the wet key on the table. "
                    "The stopped clock confirms the missing interval."
                ),
            }
        elif "inspect_chapter_consistency" not in prior_calls:
            name = "inspect_chapter_consistency"
            arguments = {
                "chapter_id": "chapter-001",
                "expected_revision": 0,
                "draft_revision": 2,
            }
        else:
            name = "submit_chapter_candidate"
            arguments = _chapter_submission(candidate_revision=3, draft_revision=2)
        return ChatResult(
            content="",
            tool_calls=[
                ToolCall(
                    id=f"chapter-call-{chat_calls}",
                    name=name,
                    arguments=arguments,
                    raw_arguments=json.dumps(arguments),
                )
            ],
            finish_reason="tool_call",
            model_snapshot="chapter-model",
            provider_snapshot="openai-compatible",
        )

    def fake_evaluate(_profile, evaluation_input: EvaluationInput, **_kwargs):
        evaluation_inputs.append(evaluation_input)
        index = len(evaluation_inputs)
        if index == 1:
            issues = [
                EvaluationIssue(
                    category="state_patch",
                    severity="blocking",
                    candidate_locator="candidate.state_patch",
                    evidence_locator="candidate.state_patch",
                    explanation="The wet-key state change is missing.",
                )
            ]
            outcome = "local_repair"
            scope = ["state_patch"]
            repair_brief = "Record the wet-key state change only."
        elif index == 2:
            issues = [
                EvaluationIssue(
                    category="chronology",
                    severity="blocking",
                    candidate_locator="candidate.draft",
                    evidence_locator="candidate.draft",
                    explanation="The stopped clock confirmation is still missing.",
                )
            ]
            outcome = "local_repair"
            scope = ["draft"]
            repair_brief = "Add the stopped-clock confirmation to the draft only."
        else:
            issues = []
            outcome = "pass"
            scope = []
            repair_brief = None
        return EvaluationRecord(
            candidate_run_id=evaluation_input.candidate_run_id,
            candidate_artifact_id=evaluation_input.candidate_artifact_id,
            candidate_revision=evaluation_input.candidate_revision,
            evaluator_profile_id="main",
            evaluator_model_snapshot="chapter-model",
            evaluator_provider_snapshot="openai-compatible",
            rubric_version=evaluation_input.rubric_version,
            result=EvaluationResult(
                schema_version=1,
                outcome=outcome,
                contract_satisfied=outcome == "pass",
                summary="Candidate passes." if outcome == "pass" else repair_brief,
                issues=issues,
                signals=[],
                repair_brief=repair_brief,
                upstream_blocker=None,
                repair_scope=scope,
            ),
        )

    monkeypatch.setattr(loop_runners, "_require_policy_capabilities", lambda _policy: None)
    monkeypatch.setattr(loop_runners, "evaluate_candidate", fake_evaluate)
    result = run_chapter_agent(
        tmp_path,
        ProjectMetadata(project_id="project-1", active_arc_id="arc-001"),
        policy,
        chapter_id="chapter-001",
        expected_revision=0,
        instruction="Write the first chapter.",
        candidate_run_id="chapter-5-regression",
        runtime=AgentRuntime(build_default_tool_registry(), chat_call=fake_chat),
    )

    assert result.evaluation.result.outcome == "pass"
    assert [item.candidate_revision for item in evaluation_inputs] == [1, 2, 3]
    assert [len(item.review_history) for item in evaluation_inputs] == [0, 1, 2]
    assert (
        evaluation_inputs[0].component_fingerprints["draft"]
        == evaluation_inputs[1].component_fingerprints["draft"]
    )
    assert (
        evaluation_inputs[1].component_fingerprints["draft"]
        != evaluation_inputs[2].component_fingerprints["draft"]
    )
    chain = json.loads(
        (
            tmp_path
            / "chapters"
            / "chapter-001"
            / "agent"
            / "repair-chain.json"
        ).read_text(encoding="utf-8")
    )
    assert [entry["changed_components"] for entry in chain["entries"]] == [
        [],
        ["state_patch"],
        ["draft"],
    ]
    assert chain["review_history"][1]["result"]["issues"][0]["discovery"] == (
        "late_discovery"
    )
    assert chain["used_semantic_revisions"] == 2


def test_pending_chapter_repair_resumes_after_process_boundary(
    tmp_path: Path,
    monkeypatch,
) -> None:
    profile = LlmProfile(
        id="main",
        name="Main",
        protocol="openai-compatible",
        base_url="https://api.example.com/v1",
        api_key=SecretStr("secret"),
        model="chapter-model",
    )
    policy = ResolvedAgentPolicy(
        role="chapter",
        profile=profile,
        evaluator_profile=profile,
        max_turns=8,
        tool_schema_repair_limit=2,
        semantic_revision_limit=10,
        transport_retry_limit=1,
    )
    requests = []
    evaluation_inputs: list[EvaluationInput] = []

    def fake_chat(_profile, request):
        requests.append(request)
        tool_names = {tool.name for tool in request.tools}
        prior_calls = {
            call.name for message in request.messages for call in message.tool_calls
        }
        if "plan_chapter_candidate" in tool_names:
            name, arguments = _next_initial_chapter_tool(prior_calls)
        else:
            name = "submit_chapter_candidate"
            arguments = _chapter_submission(candidate_revision=2, draft_revision=1)
        return ChatResult(
            content="",
            tool_calls=[
                ToolCall(
                    id=f"restart-call-{len(requests)}",
                    name=name,
                    arguments=arguments,
                    raw_arguments=json.dumps(arguments),
                )
            ],
            finish_reason="tool_call",
            model_snapshot="chapter-model",
            provider_snapshot="openai-compatible",
        )

    def fake_evaluate(_profile, evaluation_input: EvaluationInput, **_kwargs):
        evaluation_inputs.append(evaluation_input)
        first = len(evaluation_inputs) == 1
        return EvaluationRecord(
            candidate_run_id=evaluation_input.candidate_run_id,
            candidate_artifact_id=evaluation_input.candidate_artifact_id,
            candidate_revision=evaluation_input.candidate_revision,
            evaluator_profile_id="main",
            evaluator_model_snapshot="chapter-model",
            evaluator_provider_snapshot="openai-compatible",
            rubric_version=evaluation_input.rubric_version,
            result=EvaluationResult(
                schema_version=1,
                outcome="local_repair" if first else "pass",
                contract_satisfied=not first,
                summary=(
                    "Record the missing state change."
                    if first
                    else "Candidate passes."
                ),
                issues=(
                    [
                        EvaluationIssue(
                            category="state_patch",
                            severity="blocking",
                            candidate_locator="candidate.state_patch",
                            evidence_locator="candidate.state_patch",
                            explanation="The wet-key state change is missing.",
                        )
                    ]
                    if first
                    else []
                ),
                signals=[],
                repair_brief="Record the wet-key state change only." if first else None,
                upstream_blocker=None,
                repair_scope=["state_patch"] if first else [],
            ),
        )

    monkeypatch.setattr(loop_runners, "_require_policy_capabilities", lambda _policy: None)
    monkeypatch.setattr(loop_runners, "evaluate_candidate", fake_evaluate)
    metadata = ProjectMetadata(project_id="project-1", active_arc_id="arc-001")
    with pytest.raises(RuntimeError, match="simulated process boundary"):
        run_chapter_agent(
            tmp_path,
            metadata,
            policy,
            chapter_id="chapter-001",
            expected_revision=0,
            instruction="Write the first chapter.",
            candidate_run_id="chapter-restart-run",
            runtime=_CrashAfterRepairScheduleRuntime(
                build_default_tool_registry(),
                chat_call=fake_chat,
            ),
        )

    resumed = run_chapter_agent(
        tmp_path,
        metadata,
        policy,
        chapter_id="chapter-001",
        expected_revision=0,
        instruction="This initial instruction must be replaced by pending repair context.",
        candidate_run_id="chapter-restart-run",
        runtime=AgentRuntime(build_default_tool_registry(), chat_call=fake_chat),
    )

    assert resumed.evaluation.result.outcome == "pass"
    assert [item.mode for item in evaluation_inputs] == [
        "initial",
        "repair_verification",
    ]
    assert len(requests) == 5
    resumed_tools = {tool.name for tool in requests[-1].tools}
    assert "plan_chapter_candidate" not in resumed_tools
    assert "write_chapter_draft" not in resumed_tools
    assert any(
        "complete_review_history" in message.content
        for message in requests[-1].messages
    )


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


def _next_initial_chapter_tool(
    prior_calls: set[str],
) -> tuple[str, dict[str, object]]:
    if "plan_chapter_candidate" not in prior_calls:
        return "plan_chapter_candidate", {
            "chapter_id": "chapter-001",
            "expected_revision": 0,
            "plan_revision": 1,
            "plan_markdown": "# Chapter goal\n\nReveal one fair clue.",
        }
    if "write_chapter_draft" not in prior_calls:
        return "write_chapter_draft", {
            "chapter_id": "chapter-001",
            "expected_revision": 0,
            "plan_revision": 1,
            "draft_revision": 1,
            "mode": "write",
            "content": "The witness places the wet key on the table.",
        }
    if "inspect_chapter_consistency" not in prior_calls:
        return "inspect_chapter_consistency", {
            "chapter_id": "chapter-001",
            "expected_revision": 0,
            "draft_revision": 1,
        }
    return "submit_chapter_candidate", {
        "chapter_id": "chapter-001",
        "expected_revision": 0,
        "candidate_revision": 1,
        "plan_revision": 1,
        "draft_revision": 1,
        "summary": "The fair clue is visible.",
        "observations": _chapter_observations(),
        "state_patch": {"operations": []},
    }


def _chapter_submission(
    *,
    candidate_revision: int,
    draft_revision: int,
) -> dict[str, object]:
    return {
        "chapter_id": "chapter-001",
        "expected_revision": 0,
        "candidate_revision": candidate_revision,
        "plan_revision": 1,
        "draft_revision": draft_revision,
        "summary": "The fair clue and state change are recorded.",
        "observations": _chapter_observations(),
        "state_patch": {
            "operations": [
                {
                    "op": "upsert",
                    "target_file": "canon/world_facts.json",
                    "target_id": "wet-key",
                    "expected_version": 1,
                    "value_fields": [
                        {"key": "status", "json_value": '"found"'}
                    ],
                    "evidence_quotes": ["wet key"],
                    "rationale": "The draft places the wet key on the table.",
                }
            ]
        },
    }


def _chapter_observations() -> dict[str, object]:
    return {
        "events": [],
        "character_changes": [],
        "relationship_changes": [],
        "world_fact_candidates": [],
        "foreshadowing_candidates": [],
        "requires_commit": False,
    }


class _CrashAfterRepairScheduleRuntime(AgentRuntime):
    def request_semantic_revision(
        self,
        activation: AgentActivation,
        *,
        repair_contract: RepairContract,
    ) -> bool:
        scheduled = super().request_semantic_revision(
            activation,
            repair_contract=repair_contract,
        )
        if scheduled:
            raise RuntimeError("simulated process boundary")
        return False
