import json
from pathlib import Path

from pydantic import SecretStr

from app.harness.agents import loop_runners
from app.harness.agents.domain_tools import build_default_tool_registry
from app.harness.agents.evaluator import evaluation_input_fingerprint
from app.harness.agents.loop_runners import (
    recover_completed_chapter_agent,
    run_chapter_agent,
)
from app.harness.agents.models import EvaluationInput, EvaluationRecord, EvaluationResult
from app.harness.agents.policy import ResolvedAgentPolicy
from app.harness.agents.runtime import AgentRuntime
from app.llm.gateway import ChatRequest, ChatResult, ToolCall
from app.schemas.profiles import LlmProfile
from app.schemas.projects import ProjectMetadata


def test_completed_chapter_candidate_reuses_or_repairs_only_its_evaluation(
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
        tool_schema_repair_limit=1,
        semantic_revision_limit=2,
        transport_retry_limit=1,
    )
    metadata = ProjectMetadata(project_id="project-1", active_arc_id="arc-001")
    chat_calls = 0

    def fake_chat(_profile: LlmProfile, request: ChatRequest) -> ChatResult:
        nonlocal chat_calls
        chat_calls += 1
        prior_calls = {
            call.name for message in request.messages for call in message.tool_calls
        }
        name, arguments = _next_chapter_tool(prior_calls)
        return ChatResult(
            content="",
            tool_calls=[
                ToolCall(
                    id=f"call-{chat_calls}",
                    name=name,
                    arguments=arguments,
                    raw_arguments=json.dumps(arguments),
                )
            ],
            finish_reason="tool_call",
            model_snapshot="chapter-model",
            provider_snapshot="openai-compatible",
        )

    evaluation_calls = 0

    def fake_evaluate(
        evaluator_profile: LlmProfile,
        evaluation_input: EvaluationInput,
        **_kwargs,
    ) -> EvaluationRecord:
        nonlocal evaluation_calls
        evaluation_calls += 1
        return EvaluationRecord(
            candidate_run_id=evaluation_input.candidate_run_id,
            input_fingerprint=evaluation_input_fingerprint(
                evaluator_profile,
                evaluation_input,
            ),
            candidate_artifact_id=evaluation_input.candidate_artifact_id,
            candidate_revision=evaluation_input.candidate_revision,
            evaluator_profile_id=evaluator_profile.id,
            evaluator_model_snapshot=evaluator_profile.model,
            evaluator_provider_snapshot=evaluator_profile.protocol,
            rubric_version=evaluation_input.rubric_version,
            result=EvaluationResult(
                schema_version=1,
                outcome="pass",
                contract_satisfied=True,
                summary="The durable candidate passes.",
                issues=[],
                signals=[],
                repair_brief=None,
                upstream_blocker=None,
            ),
        )

    monkeypatch.setattr(loop_runners, "_require_policy_capabilities", lambda _policy: None)
    monkeypatch.setattr(loop_runners, "evaluate_candidate", fake_evaluate)
    runtime = AgentRuntime(build_default_tool_registry(), chat_call=fake_chat)

    created = run_chapter_agent(
        tmp_path,
        metadata,
        policy,
        chapter_id="chapter-001",
        expected_revision=0,
        instruction="Write the first chapter.",
        candidate_run_id="chapter-run-stable",
        runtime=runtime,
    )
    activation_id = created.run_result.activation_id
    candidate_root = tmp_path / created.candidate_root
    candidate_snapshot = {
        path.relative_to(candidate_root).as_posix(): path.read_bytes()
        for path in candidate_root.rglob("*")
        if path.is_file()
    }
    evaluation_path = (
        tmp_path
        / "chapters"
        / "chapter-001"
        / "agent"
        / "a"
        / activation_id
        / "evaluation.json"
    )

    reused = recover_completed_chapter_agent(
        tmp_path,
        metadata,
        policy,
        chapter_id="chapter-001",
    )
    assert reused is not None
    assert reused.run_result.candidate_run_id == "chapter-run-stable"
    assert evaluation_calls == 1
    assert chat_calls == 4

    evaluation_path.unlink()
    repaired = recover_completed_chapter_agent(
        tmp_path,
        metadata,
        policy,
        chapter_id="chapter-001",
    )
    assert repaired is not None
    assert repaired.run_result.candidate_run_id == "chapter-run-stable"
    assert repaired.run_result.activation_id == activation_id
    assert evaluation_calls == 2
    assert chat_calls == 4
    assert evaluation_path.is_file()
    assert repaired.evaluation.candidate_artifact_id == created.evaluation.candidate_artifact_id
    assert repaired.evaluation.candidate_revision == created.evaluation.candidate_revision
    assert repaired.evaluation.input_fingerprint == created.evaluation.input_fingerprint
    assert {
        path.relative_to(candidate_root).as_posix(): path.read_bytes()
        for path in candidate_root.rglob("*")
        if path.is_file()
    } == candidate_snapshot


def _next_chapter_tool(
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
        "summary": "The first fair clue is visible.",
        "observations": {
            "events": [],
            "character_changes": [],
            "relationship_changes": [],
            "world_fact_candidates": [],
            "foreshadowing_candidates": [],
            "requires_commit": False,
        },
        "state_patch": {"operations": []},
    }
