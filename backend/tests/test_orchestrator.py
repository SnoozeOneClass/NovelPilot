import json
from pathlib import Path

import pytest
from pydantic import SecretStr

from app.harness import orchestrator
from app.harness.agents.loop_runners import (
    ChapterAgentResult,
    ChapterPatchEvidenceRepairResult,
    StoryArcAgentResult,
)
from app.harness.agents.models import (
    AgentBudgets,
    AgentIdentity,
    AgentRunResult,
    AgentState,
    EvaluationRecord,
    EvaluationResult,
    UpstreamBlockerProposal,
)
from app.harness.agents.persistence import read_agent_state, save_agent_state
from app.harness.loops.book import BookDirectionSynthesis
from app.harness.orchestrator import HarnessOrchestrator, HarnessRunContext
from app.llm.gateway import ChatChunk, ChatMessage, ChatRequest, ChatResult
from app.schemas.arcs import StoryArcPlanProposal
from app.schemas.artifacts import CandidateObservations, ChapterVerification
from app.schemas.events import HarnessEvent
from app.schemas.patches import CandidateStatePatch
from app.schemas.profiles import LlmProfile
from app.schemas.projects import ProjectMetadata
from app.schemas.setup import (
    BookDirectionConstraints,
    BookDirectionReview,
    BookTitleSuggestion,
    ConfirmedDecisionCoverage,
    SetupStateDocument,
)
from app.storage import arcs as arc_storage
from app.storage import book_revisions as book_revision_storage
from app.storage.events import append_event
from app.storage.events import read_events
from app.storage.json_files import read_json, write_json


@pytest.fixture(autouse=True)
def _bridge_legacy_llm_fixtures_to_agent_results(monkeypatch) -> None:
    monkeypatch.setattr(orchestrator, "run_story_arc_agent", _fixture_story_arc_agent)
    monkeypatch.setattr(orchestrator, "run_chapter_agent", _fixture_chapter_agent)


def _fixture_story_arc_agent(
    project_path,
    metadata,
    policy,
    *,
    arc_id,
    intent,
    expected_revision,
    instruction,
    **_kwargs,
) -> StoryArcAgentResult:
    action = "plan_current_arc" if intent == "create" else "revise_current_arc_plan"
    response = orchestrator.call_llm(
        policy.profile,
        ChatRequest(
            profile_id=policy.profile.id,
            messages=[ChatMessage(role="user", content=instruction)],
            metadata={"atomic_action": action},
        ),
    )
    payload = json.loads(response.content)
    proposal = StoryArcPlanProposal.model_validate(payload)
    identity = AgentIdentity(
        project_id=metadata.project_id,
        role="story_arc",
        scope_id=arc_id,
    )
    evaluation = _passing_evaluation(
        identity,
        f"arcs/{arc_id}/agent-fixture-candidate.json",
        expected_revision + 1,
        profile_id=policy.evaluator_profile.id,
    )
    return StoryArcAgentResult(
        proposal=proposal,
        evaluation=evaluation,
        run_result=AgentRunResult(
            outcome="candidate",
            identity=identity,
            candidate_run_id="fixture-run",
            activation_id="fixture-activation",
            turns_used=1,
            model_snapshot=response.model_snapshot,
            provider_snapshot=response.provider_snapshot,
            usage=response.usage,
        ),
        candidate_artifact_path=evaluation.candidate_artifact_id,
    )


def _fixture_chapter_agent(
    project_path: Path,
    metadata,
    policy,
    *,
    chapter_id: str,
    instruction: str,
    **_kwargs,
) -> ChapterAgentResult:
    chapter_path = project_path / "chapters" / chapter_id

    def action(name: str, fallback: str = "") -> str:
        response = orchestrator.call_llm(
            policy.profile,
            ChatRequest(
                profile_id=policy.profile.id,
                messages=[ChatMessage(role="user", content=instruction)],
                metadata={"atomic_action": name},
            ),
        )
        return response.content or fallback

    plan = (
        (chapter_path / "goal.md").read_text(encoding="utf-8")
        if (chapter_path / "goal.md").exists()
        else action("generate_chapter_goal", "# Goal")
    )
    draft = (
        (chapter_path / "draft.md").read_text(encoding="utf-8")
        if (chapter_path / "draft.md").exists()
        else action("draft_chapter", "Draft")
    )
    if (chapter_path / "observations.json").exists():
        raw_observations = read_json(chapter_path / "observations.json")
    else:
        raw_observations = json.loads(action("extract_candidate_observations", "{}"))
    raw_observations = raw_observations if isinstance(raw_observations, dict) else {}
    raw_observations["based_on"] = f"chapters/{chapter_id}/draft.md"
    raw_observations.setdefault("status", "candidate")
    observations = CandidateObservations.model_validate(raw_observations)
    if not (chapter_path / "review.md").exists():
        action("semantic_review", "# Review")
    verification = _fixture_chapter_verification(
        chapter_id,
        action("verify_chapter", ""),
    )
    if (chapter_path / "candidate_state_patch.json").exists():
        raw_patch = read_json(chapter_path / "candidate_state_patch.json")
    else:
        try:
            raw_patch = json.loads(action("generate_candidate_state_patch", "{}"))
        except json.JSONDecodeError:
            raw_patch = {}
    raw_patch = raw_patch if isinstance(raw_patch, dict) else {}
    raw_patch.setdefault("status", "candidate")
    raw_patch["based_on"] = {
        "chapter_final": f"chapters/{chapter_id}/final.md",
        "observations": f"chapters/{chapter_id}/observations.json",
    }
    raw_patch.setdefault("operations", [])
    patch = CandidateStatePatch.model_validate(raw_patch)
    root = chapter_path / "agent-fixture" / "candidate"
    root.mkdir(parents=True, exist_ok=True)
    (root / "plan.md").write_text(plan, encoding="utf-8")
    (root / "draft.md").write_text(draft, encoding="utf-8")
    identity = AgentIdentity(
        project_id=metadata.project_id,
        role="chapter",
        scope_id=chapter_id,
    )
    evaluation = _evaluation_from_verification(
        identity,
        verification,
        profile_id=policy.evaluator_profile.id,
    )
    from app.harness.agents.domain_tools import SubmitChapterCandidateInput

    return ChapterAgentResult(
        submission=SubmitChapterCandidateInput(
            chapter_id=chapter_id,
            expected_revision=0,
            candidate_revision=1,
            plan_revision=1,
            draft_revision=1,
            summary="Fixture chapter candidate.",
            observations=observations,
            state_patch=patch,
        ),
        evaluation=evaluation,
        verification=verification,
        run_result=AgentRunResult(
            outcome="candidate",
            identity=identity,
            candidate_run_id="fixture-run",
            activation_id="fixture-activation",
            turns_used=1,
            model_snapshot=policy.profile.model,
            provider_snapshot=policy.profile.protocol,
        ),
        candidate_root=root.relative_to(project_path).as_posix(),
    )


def _fixture_chapter_verification(chapter_id: str, content: str) -> ChapterVerification:
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return ChapterVerification(
            chapter_id=chapter_id,
            goal_satisfied=False,
            commit_allowed=False,
            routing_decision="rewrite",
            reasons=["Fixture verifier output was not JSON."],
        )
    if not isinstance(payload, dict):
        raise AssertionError("Fixture verifier output must be an object.")
    payload["chapter_id"] = chapter_id
    return ChapterVerification.model_validate(payload)


def _passing_evaluation(
    identity: AgentIdentity,
    artifact: str,
    revision: int,
    *,
    profile_id: str,
) -> EvaluationRecord:
    return EvaluationRecord(
        candidate_artifact_id=artifact,
        candidate_revision=revision,
        evaluator_profile_id=profile_id,
        evaluator_model_snapshot="fixture-model",
        evaluator_provider_snapshot="openai-compatible",
        rubric_version="fixture-v1",
        result=EvaluationResult(
            schema_version=1,
            outcome="pass",
            contract_satisfied=True,
            summary="Fixture candidate passes.",
            issues=[],
            signals=[],
            repair_brief=None,
            upstream_blocker=None,
        ),
    )


def _evaluation_from_verification(
    identity: AgentIdentity,
    verification: ChapterVerification,
    *,
    profile_id: str,
) -> EvaluationRecord:
    if verification.commit_allowed:
        return _passing_evaluation(identity, "fixture/chapter.json", 1, profile_id=profile_id)
    return EvaluationRecord(
        candidate_artifact_id="fixture/chapter.json",
        candidate_revision=1,
        evaluator_profile_id=profile_id,
        evaluator_model_snapshot="fixture-model",
        evaluator_provider_snapshot="openai-compatible",
        rubric_version="fixture-v1",
        result=EvaluationResult(
            schema_version=1,
            outcome="local_repair",
            contract_satisfied=False,
            summary="Fixture candidate needs repair.",
            issues=[],
            signals=[],
            repair_brief="Repair the chapter contract failure.",
            upstream_blocker=None,
        ),
    )


def test_harness_rejects_ineligible_cross_loop_proposal_without_direct_activation(
    tmp_path: Path,
) -> None:
    project_path = tmp_path / "project"
    project_path.mkdir()
    metadata = ProjectMetadata(
        project_id="project-1",
        title="Novel",
        active_arc_id="arc-001",
        active_chapter_id="chapter-001",
    )
    write_json(project_path / "project.json", metadata.model_dump(mode="json"))
    runner = HarnessOrchestrator(
        HarnessRunContext(project_path=project_path, run_id="run-1")
    )
    evaluation = EvaluationRecord(
        candidate_artifact_id="chapters/chapter-001/candidates/c1/manifest.json",
        candidate_revision=1,
        evaluator_profile_id="main",
        evaluator_model_snapshot="model",
        evaluator_provider_snapshot="openai-compatible",
        rubric_version="chapter-v1",
        result=EvaluationResult(
            schema_version=1,
            outcome="cross_loop_escalation",
            contract_satisfied=False,
            summary="The active arc contract conflicts with committed chapter evidence.",
            issues=[],
            signals=[],
            repair_brief=None,
            upstream_blocker=UpstreamBlockerProposal(
                owner="story_arc",
                contract_field="ending_instruction",
                contract_revision=2,
                committed_evidence_locator="chapters/chapter-001/final.md#ending",
                impossibility_reason="The current candidate cannot satisfy both facts.",
            ),
        ),
    )

    handled = runner._handle_evaluation_control(
        metadata,
        evaluation,
        loop_layer="chapter",
        action="run_chapter_agent",
    )

    assert handled is True
    assert read_json(project_path / "project.json")["run_status"] == "waiting_for_user"
    events = read_events(project_path)
    assert events[-2].kind == "cross_loop_proposal_recorded"
    assert events[-2].routing_decision == "propose_to_story_arc"
    assert events[-2].payload["upstream_blocker"]["owner"] == "story_arc"
    assert events[-1].kind == "cross_loop_route_rejected"
    assert events[-1].payload["reason"] == "stale_or_unknown_story_arc_revision"
    assert not any(item.kind == "atomic_action_started" for item in read_events(project_path))


def test_harness_routes_eligible_chapter_blocker_to_story_arc_agent(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_path = tmp_path / "project"
    chapter_path = project_path / "chapters" / "chapter-001"
    arc_path = project_path / "arcs" / "arc-001"
    chapter_path.mkdir(parents=True)
    arc_path.mkdir(parents=True)
    metadata = ProjectMetadata(
        project_id="project-1",
        title="Novel",
        active_arc_id="arc-001",
        active_chapter_id="chapter-001",
    )
    write_json(project_path / "project.json", metadata.model_dump(mode="json"))
    write_json(arc_path / "state.json", {"schema_version": 1, "version": 2})
    (arc_path / "plan.md").write_text("# Current Arc\n", encoding="utf-8")
    chapter_identity = AgentIdentity(
        project_id="project-1",
        role="chapter",
        scope_id="chapter-001",
    )
    save_agent_state(
        project_path,
        AgentState(
            identity=chapter_identity,
            lifecycle="completed",
            candidate_run_id="chapter-run-1",
            budgets=AgentBudgets(max_turns=30, used_turns=7),
        ),
    )
    for name in ["context_snapshot.json", "draft.md", "evaluation.json", "verification.json"]:
        (chapter_path / name).write_text("candidate\n", encoding="utf-8")

    profile = LlmProfile(
        id="main",
        name="Main",
        protocol="openai-compatible",
        base_url="https://api.example.com/v1",
        api_key=SecretStr("secret"),
        model="story-model",
    )
    monkeypatch.setattr(orchestrator, "get_active_profile", lambda: profile)
    routed: list[str] = []
    runner = HarnessOrchestrator(
        HarnessRunContext(project_path=project_path, run_id="run-1")
    )

    def fake_revise(_profile, _metadata, feedback, *, source_label="User feedback"):
        routed.append(f"{source_label}\n{feedback}")
        return "arcs/arc-001/plan.md"

    monkeypatch.setattr(runner, "_revise_current_arc_plan_from_feedback", fake_revise)
    evaluation = EvaluationRecord(
        candidate_artifact_id="agents/chapter/candidate/manifest.json",
        candidate_revision=1,
        evaluator_profile_id="main",
        evaluator_model_snapshot="model",
        evaluator_provider_snapshot="openai-compatible",
        rubric_version="chapter-v1",
        result=EvaluationResult(
            schema_version=1,
            outcome="cross_loop_escalation",
            contract_satisfied=False,
            summary="The current Arc ending instruction is impossible.",
            issues=[],
            signals=[],
            repair_brief=None,
            upstream_blocker=UpstreamBlockerProposal(
                owner="story_arc",
                contract_field="ending_instruction",
                contract_revision=2,
                committed_evidence_locator="arcs/arc-001/plan.md",
                impossibility_reason="It conflicts with committed canon evidence.",
            ),
        ),
    )

    assert runner._handle_evaluation_control(
        metadata,
        evaluation,
        loop_layer="chapter",
        action="run_chapter_agent",
        candidate_run_id="chapter-run-1",
    )

    assert len(routed) == 1
    assert "Harness-routed Chapter blocker" in routed[0]
    assert not (chapter_path / "context_snapshot.json").exists()
    assert not (chapter_path / "draft.md").exists()
    assert not (chapter_path / "evaluation.json").exists()
    assert not (chapter_path / "verification.json").exists()
    route_files = list((chapter_path / "upstream-routes").glob("route-*.json"))
    assert len(route_files) == 1
    route_record = read_json(route_files[0])
    assert route_record["committed_artifacts_touched"] is False
    assert route_record["revised_arc_path"] == "arcs/arc-001/plan.md"
    assert read_json(chapter_path / "upstream-resume.json") == {
        "schema_version": 1,
        "route_id": route_record["route_id"],
        "candidate_run_id": "chapter-run-1",
        "revised_arc_path": "arcs/arc-001/plan.md",
    }
    blocked_state = read_agent_state(project_path, chapter_identity)
    assert blocked_state.lifecycle == "blocked"
    assert blocked_state.budgets is not None
    assert blocked_state.budgets.used_turns == 7
    events = read_events(project_path)
    assert [item.kind for item in events] == [
        "cross_loop_proposal_recorded",
        "cross_loop_route_accepted",
        "cross_loop_route_completed",
    ]
    assert events[-1].routing_decision == "retry_chapter"


def test_cross_loop_route_never_invalidates_committed_chapter_work(tmp_path: Path) -> None:
    project_path = tmp_path / "project"
    chapter_path = project_path / "chapters" / "chapter-001"
    arc_path = project_path / "arcs" / "arc-001"
    chapter_path.mkdir(parents=True)
    arc_path.mkdir(parents=True)
    metadata = ProjectMetadata(
        project_id="project-1",
        title="Novel",
        active_arc_id="arc-001",
        active_chapter_id="chapter-001",
    )
    write_json(project_path / "project.json", metadata.model_dump(mode="json"))
    write_json(arc_path / "state.json", {"schema_version": 1, "version": 2})
    (arc_path / "plan.md").write_text("# Current Arc\n", encoding="utf-8")
    (chapter_path / "final.md").write_text("Committed prose\n", encoding="utf-8")
    runner = HarnessOrchestrator(
        HarnessRunContext(project_path=project_path, run_id="run-1")
    )

    rejection = runner._story_arc_route_rejection(
        metadata,
        "chapter",
        {
            "owner": "story_arc",
            "contract_field": "ending_instruction",
            "contract_revision": 2,
            "committed_evidence_locator": "arcs/arc-001/plan.md",
            "impossibility_reason": "The current contract is impossible.",
        },
    )

    assert rejection == "committed_chapter_work_cannot_be_invalidated"
    assert (chapter_path / "final.md").read_text(encoding="utf-8") == "Committed prose\n"


def test_pending_story_arc_route_finishes_cleanup_without_rerunning_agent(
    tmp_path: Path,
) -> None:
    project_path = tmp_path / "project"
    chapter_path = project_path / "chapters" / "chapter-001"
    arc_path = project_path / "arcs" / "arc-001"
    chapter_path.mkdir(parents=True)
    arc_path.mkdir(parents=True)
    route_id = "route-restart-story"
    metadata = ProjectMetadata(
        project_id="project-1",
        title="Novel",
        active_arc_id="arc-001",
        active_chapter_id="chapter-001",
        run_status="running",
    )
    write_json(project_path / "project.json", metadata.model_dump(mode="json"))
    write_json(
        arc_path / "state.json",
        {"schema_version": 1, "version": 3, "arc_id": "arc-001"},
    )
    (arc_path / "plan.md").write_text("# Revised Arc\n", encoding="utf-8")
    (arc_path / "revision.md").write_text(
        f"# Arc Revision\n\n{route_id}\n",
        encoding="utf-8",
    )
    for name in ["context_snapshot.json", "draft.md", "evaluation.json"]:
        (chapter_path / name).write_text("candidate\n", encoding="utf-8")
    pending_path = (
        project_path / "book" / "harness" / "pending-cross-loop-route.json"
    )
    write_json(
        pending_path,
        {
            "schema_version": 1,
            "route_id": route_id,
            "loop_layer": "chapter",
            "action": "run_chapter_agent",
            "source_artifact": "chapters/chapter-001/agent_candidate.json",
            "proposal": {
                "target_owner": "story_arc",
                "candidate_run_id": "chapter-run-1",
                "contract_revision": 2,
                "contract_field": "ending_instruction",
                "committed_evidence_locator": "arcs/arc-001/plan.md",
                "impossibility_reason": "Committed evidence requires a revised ending.",
            },
        },
    )

    handled = HarnessOrchestrator(
        HarnessRunContext(project_path=project_path, run_id="run-restart")
    )._process_pending_cross_loop_route(metadata)

    assert handled is True
    assert not pending_path.exists()
    assert not (chapter_path / "context_snapshot.json").exists()
    assert not (chapter_path / "draft.md").exists()
    route_record = read_json(
        chapter_path / "upstream-routes" / f"{route_id}.json"
    )
    assert route_record["committed_artifacts_touched"] is False
    assert read_events(project_path)[-1].kind == "cross_loop_route_recovered"


def test_full_auto_book_route_waits_for_explicit_user_approval(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_path = tmp_path / "project"
    book_path = project_path / "book"
    chapter_path = project_path / "chapters" / "chapter-001"
    arc_path = project_path / "arcs" / "arc-001"
    book_path.mkdir(parents=True)
    chapter_path.mkdir(parents=True)
    arc_path.mkdir(parents=True)
    metadata = ProjectMetadata(
        project_id="project-1",
        title="Novel",
        operation_mode="full_auto",
        active_arc_id="arc-001",
        active_chapter_id="chapter-001",
    )
    write_json(project_path / "project.json", metadata.model_dump(mode="json"))
    write_json(
        book_path / "setup.json",
        SetupStateDocument(
            phase="approved",
            approved=True,
            approved_title="Novel",
            direction_draft="# Approved direction",
            confirmed_decisions=["Keep committed history."],
        ).model_dump(mode="json"),
    )
    (book_path / "direction.md").write_text(
        "# Approved direction\n", encoding="utf-8"
    )
    (book_path / "settings.md").write_text(
        "# Approved direction\n", encoding="utf-8"
    )
    (book_path / "outline.md").write_text("# Approved outline\n", encoding="utf-8")
    write_json(book_path / "constraints.json", {"schema_version": 1})
    write_json(
        book_path / "state.json",
        {
            "schema_version": 2,
            "version": 4,
            "book_direction_version": 2,
            "setup_approved": True,
            "confirmed_decisions": ["Keep committed history."],
        },
    )
    (arc_path / "plan.md").write_text("# Arc\n", encoding="utf-8")
    write_json(arc_path / "state.json", {"schema_version": 1, "version": 1})

    profile = LlmProfile(
        id="main",
        name="Main",
        protocol="openai-compatible",
        base_url="https://api.example.com/v1",
        api_key=SecretStr("secret"),
        model="book-model",
    )
    monkeypatch.setattr(orchestrator, "get_active_profile", lambda: profile)
    evaluation = _passing_evaluation(
        AgentIdentity(project_id="project-1", role="book"),
        "book/agent/a/test/candidates/book-direction.json",
        3,
        profile_id="main",
    )
    synthesis = BookDirectionSynthesis(
        direction_markdown="# Candidate revision\n\nFuture-only change.",
        constraints=BookDirectionConstraints(
            confirmed=["Keep committed history."],
            must_preserve=["Committed prose and canon."],
            must_avoid=["Retcons."],
            creative_freedoms=["Future reveal."],
            open_decisions=[],
        ),
        confirmed_decision_coverage=[
            ConfirmedDecisionCoverage(
                decision="Keep committed history.",
                candidate_evidence="Committed history remains fixed.",
            )
        ],
        recommended_titles=[
            BookTitleSuggestion(title=f"Title {index}", rationale="Keep current title.")
            for index in range(1, 4)
        ],
        rolling_plan_markdown="# Candidate outline\n\nRevise future arcs only.",
        model_snapshot="book-model",
        provider_snapshot="openai-compatible",
        usage={},
    )
    review = BookDirectionReview(
        status="passed",
        summary="Candidate preserves history.",
        issues=[],
        signals=[],
    )
    monkeypatch.setattr(
        orchestrator,
        "run_book_revision_agent",
        lambda *_args, **_kwargs: (synthesis, evaluation, review),
    )
    runner = HarnessOrchestrator(
        HarnessRunContext(project_path=project_path, run_id="run-1")
    )

    assert runner._route_cross_loop_proposal(
        metadata,
        loop_layer="chapter",
        action="run_chapter_agent",
        proposal={
            "owner": "book",
            "candidate_run_id": "chapter-run-1",
            "summary": "The future reveal is impossible.",
            "contract_field": "ending.reveal",
            "contract_revision": 4,
            "committed_evidence_locator": "book/direction.md",
            "impossibility_reason": "It conflicts with committed chapter evidence.",
        },
        source_artifact="chapters/chapter-001/agent-candidate.json",
    )

    pending = book_revision_storage.read_pending_book_revision(project_path)
    assert pending is not None
    assert pending.status == "awaiting_approval"
    assert (book_path / "direction.md").read_text(encoding="utf-8") == (
        "# Approved direction\n"
    )
    assert read_json(project_path / "project.json")["run_status"] == "waiting_for_user"
    assert read_events(project_path)[-1].kind == "book_revision_approval_required"


def test_chapter_retry_reuses_routed_candidate_run_budget(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_path = tmp_path / "project"
    chapter_path = project_path / "chapters" / "chapter-001"
    chapter_path.mkdir(parents=True)
    write_json(
        chapter_path / "upstream-resume.json",
        {
            "schema_version": 1,
            "route_id": "route-1",
            "candidate_run_id": "chapter-run-1",
            "revised_arc_path": "arcs/arc-001/plan.md",
        },
    )
    write_json(chapter_path / "context_snapshot.json", {"sources": []})
    metadata = ProjectMetadata(
        project_id="project-1",
        title="Novel",
        active_arc_id="arc-001",
        active_chapter_id="chapter-001",
    )
    write_json(project_path / "project.json", metadata.model_dump(mode="json"))
    profile = LlmProfile(
        id="main",
        name="Main",
        protocol="openai-compatible",
        base_url="https://api.example.com/v1",
        api_key=SecretStr("secret"),
        model="chapter-model",
    )
    captured: list[str | None] = []

    def stop_after_capture(*_args, candidate_run_id=None, **_kwargs):
        captured.append(candidate_run_id)
        raise RuntimeError("stop after candidate-run capture")

    monkeypatch.setattr(orchestrator, "run_chapter_agent", stop_after_capture)
    runner = HarnessOrchestrator(
        HarnessRunContext(project_path=project_path, run_id="run-1")
    )

    with pytest.raises(RuntimeError, match="stop after candidate-run capture"):
        runner._run_chapter_agent(
            profile,
            metadata,
            "chapter-001",
            chapter_path,
        )

    assert captured == ["chapter-run-1"]
    assert (chapter_path / "upstream-resume.json").exists()


def test_chapter_agent_projects_safe_public_draft_events(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_path = tmp_path / "project"
    chapter_path = project_path / "chapters" / "chapter-001"
    candidate_draft = chapter_path / "agent" / "a" / "activation" / "draft.md"
    candidate_draft.parent.mkdir(parents=True)
    candidate_draft.write_text("公开正文\n", encoding="utf-8")
    write_json(chapter_path / "context_snapshot.json", {"sources": []})
    metadata = ProjectMetadata(
        project_id="project-1",
        title="Novel",
        active_arc_id="arc-001",
        active_chapter_id="chapter-001",
    )
    write_json(project_path / "project.json", metadata.model_dump(mode="json"))
    profile = LlmProfile(
        id="main",
        name="Main",
        protocol="openai-compatible",
        base_url="https://api.example.com/v1",
        api_key=SecretStr("secret"),
        model="chapter-model",
    )

    def stream_then_stop(*_args, on_tool_event=None, on_event=None, **_kwargs):
        assert on_tool_event is not None
        assert on_event is not None
        on_tool_event(
            ChatChunk(
                event_type="tool_call_start",
                tool_call_id="call-1",
                tool_name="write_chapter_draft",
                tool_index=0,
                provider_snapshot="test",
            )
        )
        on_tool_event(
            ChatChunk(
                event_type="tool_argument_delta",
                tool_call_id="call-1",
                tool_name="write_chapter_draft",
                tool_index=0,
                arguments_delta=(
                    '{"content":"公开正文","state_patch":"PRIVATE-PATCH"}'
                ),
                provider_snapshot="test",
            )
        )
        on_tool_event(
            ChatChunk(
                event_type="tool_call_stop",
                tool_call_id="call-1",
                tool_name="write_chapter_draft",
                tool_index=0,
                provider_snapshot="test",
            )
        )
        on_event(
            {
                "kind": "agent_tool_result",
                "tool_name": "write_chapter_draft",
                "tool_call_id": "call-1",
                "status": "ok",
                "artifact_paths": [
                    "chapters/chapter-001/agent/a/activation/draft.md"
                ],
            }
        )
        raise RuntimeError("stop after stream projection")

    monkeypatch.setattr(orchestrator, "run_chapter_agent", stream_then_stop)
    runner = HarnessOrchestrator(
        HarnessRunContext(project_path=project_path, run_id="run-1")
    )

    with pytest.raises(RuntimeError, match="stop after stream projection"):
        runner._run_chapter_agent(profile, metadata, "chapter-001", chapter_path)

    events = read_events(project_path)
    assert [event.kind for event in events if event.kind.startswith("chapter_draft_")] == [
        "chapter_draft_stream_started",
        "chapter_draft_delta",
        "chapter_draft_stream_committed",
    ]
    assert "".join(
        str(event.payload.get("text_delta", ""))
        for event in events
        if event.kind == "chapter_draft_delta"
    ) == "公开正文"
    assert "PRIVATE-PATCH" not in repr(events)


def _make_project(tmp_path, *, setup_approved: bool = False):
    project_path = tmp_path / "novel"
    (project_path / "book").mkdir(parents=True)
    (project_path / "canon").mkdir(parents=True)
    (project_path / "arcs").mkdir(parents=True)
    write_json(project_path / "project.json", ProjectMetadata(title="Novel").model_dump(mode="json"))
    write_json(
        project_path / "book" / "setup.json",
        {
            "schema_version": 1,
            "approved": setup_approved,
            "approved_at": None,
            "questions": [],
            "answers": [],
            "next_question": None,
        },
    )
    (project_path / "book" / "settings.md").write_text("# Book Settings\n", encoding="utf-8")
    write_json(project_path / "book" / "state.json", {"schema_version": 1, "version": 1})
    (project_path / "events.jsonl").write_text("", encoding="utf-8")
    return project_path


def test_orchestrator_waits_for_unapproved_book_setup(tmp_path) -> None:
    project_path = _make_project(tmp_path)

    HarnessOrchestrator(
        HarnessRunContext(project_path=project_path, run_id="run-1")
    ).advance_to_next_checkpoint()

    metadata = read_json(project_path / "project.json")
    events = read_events(project_path)

    assert metadata["run_status"] == "waiting_for_user"
    assert events[-1].kind == "book_setup_required"
    assert events[-1].routing_decision == "pause"


def test_orchestrator_plans_initial_arc_with_active_profile(tmp_path, monkeypatch) -> None:
    project_path = _make_project(tmp_path, setup_approved=True)
    (project_path / "book" / "outline.md").write_text(
        "# Rolling Contract\n\nOnly plan the current arc and return on constraint conflict.",
        encoding="utf-8",
    )
    profile = LlmProfile(
        id="main",
        name="Main",
        protocol="openai-compatible",
        base_url="https://api.example.com/v1",
        api_key=SecretStr("secret"),
        model="story-model",
    )

    monkeypatch.setattr(orchestrator, "get_active_profile", lambda: profile)
    captured_prompts: list[str] = []

    def fake_call_llm(_profile, request):
        captured_prompts.append(request.messages[-1].content)
        return ChatResult(
            content=(
                '{"plan_markdown":"# Arc 1\\n\\nA focused first arc.",'
                '"target_chapter_count":9}'
            ),
            model_snapshot="story-model",
            provider_snapshot="openai-compatible",
        )

    monkeypatch.setattr(orchestrator, "call_llm", fake_call_llm)

    HarnessOrchestrator(
        HarnessRunContext(project_path=project_path, run_id="run-1")
    ).advance_to_next_checkpoint()

    metadata = read_json(project_path / "project.json")
    arc_state = read_json(project_path / "arcs" / "arc-001" / "state.json")
    plan = (project_path / "arcs" / "arc-001" / "plan.md").read_text(encoding="utf-8")
    events = read_events(project_path)

    assert metadata["active_arc_id"] == "arc-001"
    assert metadata["run_status"] == "idle"
    assert arc_state["model_snapshot"] == "story-model"
    assert arc_state["recommended_target_chapter_count"] == 9
    assert arc_state["target_chapter_count"] == 9
    assert plan.startswith("# Arc 1")
    assert "Approved rolling story arc contract" in captured_prompts[-1]
    assert "该项目从旧版全书设定迁移而来" in captured_prompts[-1]
    assert any(
        event.kind == "llm_output_delta"
        and event.payload.get("text_delta") == "# Arc 1\n\nA focused first arc."
        for event in events
    )
    assert events[-1].kind == "artifact_written"


def test_orchestrator_fails_closed_for_invalid_story_arc_plan_output(
    tmp_path,
    monkeypatch,
) -> None:
    project_path = _make_project(tmp_path, setup_approved=True)
    profile = LlmProfile(
        id="main",
        name="Main",
        protocol="openai-compatible",
        base_url="https://api.example.com/v1",
        api_key=SecretStr("secret"),
        model="story-model",
    )
    monkeypatch.setattr(orchestrator, "get_active_profile", lambda: profile)
    monkeypatch.setattr(
        orchestrator,
        "call_llm",
        lambda _profile, _request: ChatResult(
            content='{"plan_markdown":"# Arc 1","target_chapter_count":0}',
            model_snapshot="story-model",
            provider_snapshot="openai-compatible",
        ),
    )

    HarnessOrchestrator(
        HarnessRunContext(project_path=project_path, run_id="run-1")
    ).advance_to_next_checkpoint()

    metadata = read_json(project_path / "project.json")
    events = read_events(project_path)
    assert metadata["run_status"] == "failed"
    assert events[-1].kind == "run_failed"
    assert not (project_path / "arcs" / "arc-001" / "state.json").exists()


def _assert_sanitized_llm_payload(event: HarnessEvent) -> None:
    assert event.payload["profile_id"] == "main"
    assert event.payload["model_snapshot"] == "story-model"
    assert "api_key" not in event.payload
    assert "base_url" not in event.payload
    assert "provider_snapshot" not in event.payload
    assert "secret" not in str(event.payload)
    assert "https://api.example.com/v1" not in str(event.payload)


def test_orchestrator_redacts_profile_secrets_in_run_failed_event(tmp_path, monkeypatch) -> None:
    project_path = _make_project(tmp_path, setup_approved=True)
    profile = LlmProfile(
        id="main",
        name="Main",
        protocol="openai-compatible",
        base_url="https://api.example.com/v1",
        api_key=SecretStr("secret-key"),
        model="story-model",
    )

    monkeypatch.setattr(orchestrator, "get_active_profile", lambda: profile)
    monkeypatch.setattr(
        orchestrator,
        "call_llm",
        lambda _profile, _request: (_ for _ in ()).throw(
            RuntimeError(
                "provider echoed secret-key while calling https://api.example.com/v1"
            )
        ),
    )

    HarnessOrchestrator(
        HarnessRunContext(project_path=project_path, run_id="run-1")
    ).advance_to_next_checkpoint()

    events = read_events(project_path)
    raw_events = (project_path / "events.jsonl").read_text(encoding="utf-8")

    assert events[-1].kind == "run_failed"
    assert "[redacted]" in events[-1].message
    assert "secret-key" not in events[-1].message
    assert "https://api.example.com/v1" not in events[-1].message
    assert "secret-key" not in raw_events
    assert "https://api.example.com/v1" not in raw_events


def test_pause_request_becomes_paused_at_safe_checkpoint(tmp_path, monkeypatch) -> None:
    project_path = _make_project(tmp_path, setup_approved=True)
    profile = LlmProfile(
        id="main",
        name="Main",
        protocol="openai-compatible",
        base_url="https://api.example.com/v1",
        api_key=SecretStr("secret"),
        model="story-model",
    )
    monkeypatch.setattr(orchestrator, "get_active_profile", lambda: profile)

    def fake_call_llm(_profile, _request):
        metadata_payload = read_json(project_path / "project.json")
        metadata_payload["run_status"] = "pause_requested"
        write_json(project_path / "project.json", metadata_payload)
        return ChatResult(
            content=(
                '{"plan_markdown":"# Arc 1\\n\\nA focused first arc.",'
                '"target_chapter_count":9}'
            ),
            model_snapshot="story-model",
            provider_snapshot="openai-compatible",
        )

    monkeypatch.setattr(orchestrator, "call_llm", fake_call_llm)

    HarnessOrchestrator(
        HarnessRunContext(project_path=project_path, run_id="run-1")
    ).advance_to_next_checkpoint()

    metadata = read_json(project_path / "project.json")
    events = read_events(project_path)

    assert metadata["run_status"] == "paused"
    assert events[-1].kind == "run_paused"
    assert events[-1].routing_decision == "pause"


def test_participatory_arc_waits_for_approval_before_chapter_loop(
    tmp_path,
    monkeypatch,
) -> None:
    project_path = _make_project(tmp_path, setup_approved=True)
    metadata = ProjectMetadata(title="Novel", operation_mode="participatory")
    write_json(project_path / "project.json", metadata.model_dump(mode="json"))
    profile = LlmProfile(
        id="main",
        name="Main",
        protocol="openai-compatible",
        base_url="https://api.example.com/v1",
        api_key=SecretStr("secret"),
        model="story-model",
    )
    monkeypatch.setattr(orchestrator, "get_active_profile", lambda: profile)
    monkeypatch.setattr(
        orchestrator,
        "call_llm",
        lambda _profile, _request: ChatResult(
            content=(
                '{"plan_markdown":"# Arc 1\\n\\nA focused first arc.",'
                '"target_chapter_count":9}'
            ),
            model_snapshot="story-model",
            provider_snapshot="openai-compatible",
        ),
    )

    harness = HarnessOrchestrator(HarnessRunContext(project_path=project_path, run_id="run-1"))
    harness.advance_to_next_checkpoint()
    harness.advance_to_next_checkpoint()

    metadata_payload = read_json(project_path / "project.json")
    arc_state = read_json(project_path / "arcs" / "arc-001" / "state.json")
    events = read_events(project_path)

    assert metadata_payload["run_status"] == "waiting_for_user"
    assert arc_state["human_review"] == "awaiting_review"
    assert events[-1].kind == "story_arc_review_required"
    assert not (project_path / "chapters" / "chapter-001").exists()


def test_approving_participatory_arc_allows_chapter_loop(tmp_path, monkeypatch) -> None:
    project_path = _make_project(tmp_path, setup_approved=True)
    metadata = ProjectMetadata(
        title="Novel",
        operation_mode="participatory",
        active_arc_id="arc-001",
        run_status="waiting_for_user",
    )
    write_json(project_path / "project.json", metadata.model_dump(mode="json"))
    (project_path / "arcs" / "arc-001").mkdir(parents=True)
    write_json(
        project_path / "arcs" / "arc-001" / "state.json",
        {
            "schema_version": 1,
            "version": 1,
            "arc_id": "arc-001",
            "status": "planned",
            "plan_path": "arcs/arc-001/plan.md",
            "human_review": "awaiting_review",
            "approved_at": None,
            "recommended_target_chapter_count": 9,
            "target_chapter_count": 9,
        },
    )
    (project_path / "arcs" / "arc-001" / "plan.md").write_text("# Arc 1\n", encoding="utf-8")
    profile = LlmProfile(
        id="main",
        name="Main",
        protocol="openai-compatible",
        base_url="https://api.example.com/v1",
        api_key=SecretStr("secret"),
        model="story-model",
    )
    monkeypatch.setattr(orchestrator, "get_active_profile", lambda: profile)

    arc_storage.approve_current_arc(project_path, target_chapter_count=12)
    HarnessOrchestrator(
        HarnessRunContext(project_path=project_path, run_id="run-1")
    ).advance_to_next_checkpoint()

    metadata_payload = read_json(project_path / "project.json")
    arc_state = read_json(project_path / "arcs" / "arc-001" / "state.json")

    assert metadata_payload["active_chapter_id"] == "chapter-001"
    assert arc_state["human_review"] == "approved"
    assert arc_state["recommended_target_chapter_count"] == 9
    assert arc_state["target_chapter_count"] == 12
    assert (project_path / "chapters" / "chapter-001" / "context_snapshot.json").exists()


def test_pending_arc_review_is_not_bypassed_after_switch_to_full_auto(
    tmp_path,
    monkeypatch,
) -> None:
    project_path = _make_project(tmp_path, setup_approved=True)
    metadata = ProjectMetadata(
        title="Novel",
        operation_mode="full_auto",
        active_arc_id="arc-001",
        run_status="idle",
    )
    write_json(project_path / "project.json", metadata.model_dump(mode="json"))
    (project_path / "arcs" / "arc-001").mkdir(parents=True)
    write_json(
        project_path / "arcs" / "arc-001" / "state.json",
        {
            "arc_id": "arc-001",
            "status": "planned",
            "plan_path": "arcs/arc-001/plan.md",
            "human_review": "awaiting_review",
            "approved_at": None,
        },
    )
    (project_path / "arcs" / "arc-001" / "plan.md").write_text(
        "# Arc 1\n",
        encoding="utf-8",
    )
    profile = LlmProfile(
        id="main",
        name="Main",
        protocol="openai-compatible",
        base_url="https://api.example.com/v1",
        api_key=SecretStr("secret"),
        model="story-model",
    )
    monkeypatch.setattr(orchestrator, "get_active_profile", lambda: profile)

    HarnessOrchestrator(
        HarnessRunContext(project_path=project_path, run_id="run-1")
    ).advance_to_next_checkpoint()

    metadata_payload = read_json(project_path / "project.json")
    events = read_events(project_path)
    assert metadata_payload["run_status"] == "waiting_for_user"
    assert events[-1].kind == "story_arc_review_required"
    assert not (project_path / "chapters" / "chapter-001").exists()


def test_orchestrator_writes_chapter_context_snapshot(tmp_path, monkeypatch) -> None:
    project_path = _make_project(tmp_path, setup_approved=True)
    metadata = ProjectMetadata(title="Novel", active_arc_id="arc-001")
    write_json(project_path / "project.json", metadata.model_dump(mode="json"))
    (project_path / "arcs" / "arc-001").mkdir(parents=True)
    (project_path / "arcs" / "arc-001" / "plan.md").write_text(
        "# Arc 1\n",
        encoding="utf-8",
    )
    write_json(
        project_path / "arcs" / "arc-001" / "state.json",
        {
            "schema_version": 1,
            "version": 7,
            "arc_id": "arc-001",
            "status": "planned",
            "target_chapter_count": 3,
            "completed_chapter_ids": [],
        },
    )
    write_json(
        project_path / "canon" / "characters.json",
        {"schema_version": 1, "version": 3, "items": {"hero": {"name": "Hero"}}},
    )
    profile = LlmProfile(
        id="main",
        name="Main",
        protocol="openai-compatible",
        base_url="https://api.example.com/v1",
        api_key=SecretStr("secret"),
        model="story-model",
    )
    monkeypatch.setattr(orchestrator, "get_active_profile", lambda: profile)

    HarnessOrchestrator(
        HarnessRunContext(project_path=project_path, run_id="run-1")
    ).advance_to_next_checkpoint()

    metadata_payload = read_json(project_path / "project.json")
    snapshot = read_json(project_path / "chapters" / "chapter-001" / "context_snapshot.json")
    events = read_events(project_path)

    assert metadata_payload["active_chapter_id"] == "chapter-001"
    assert snapshot["chapter_id"] == "chapter-001"
    assert snapshot["sources"][0]["id"] == "book-settings"
    sources_by_id = {source["id"]: source for source in snapshot["sources"]}
    excluded_sources = {item["source"] for item in snapshot["excluded"]}
    assert sources_by_id["current-arc-state"]["version"] == 7
    assert sources_by_id["canon-characters"]["version"] == 3
    assert sources_by_id["canon-characters"]["usage"] == "summary"
    assert sources_by_id["canon-relationships"]["path"] == "canon/relationships.json"
    assert "chapters/chapter-001/draft.md" in excluded_sources
    assert "chapters/chapter-001/observations.json" in excluded_sources
    assert "chapters/chapter-001/candidate_state_patch.json" in excluded_sources
    assert "future-story-arcs" in excluded_sources
    assert "raw prompt" not in snapshot["assembly_rationale"].lower()
    assert events[-1].artifact_path == "chapters/chapter-001/context_snapshot.json"


def test_context_snapshot_summarizes_prior_committed_chapters_only(
    tmp_path,
    monkeypatch,
) -> None:
    project_path = _make_project(tmp_path, setup_approved=True)
    metadata = ProjectMetadata(
        title="Novel",
        active_arc_id="arc-001",
        active_chapter_id="chapter-002",
    )
    write_json(project_path / "project.json", metadata.model_dump(mode="json"))
    (project_path / "arcs" / "arc-001").mkdir(parents=True)
    (project_path / "arcs" / "arc-001" / "plan.md").write_text("# Arc 1\n", encoding="utf-8")
    unheaded_chapter = project_path / "chapters" / "chapter-000"
    unheaded_chapter.mkdir(parents=True)
    (unheaded_chapter / "final.md").write_text(
        "This is a very long opening prose line that must not be copied into the snapshot "
        "summary even when the committed final lacks a Markdown heading.",
        encoding="utf-8",
    )
    first_chapter = project_path / "chapters" / "chapter-001"
    first_chapter.mkdir(parents=True)
    (first_chapter / "final.md").write_text(
        "# First final\n\nThis full committed body should not be copied into the snapshot.",
        encoding="utf-8",
    )
    active_chapter = project_path / "chapters" / "chapter-002"
    active_chapter.mkdir(parents=True)
    (active_chapter / "draft.md").write_text(
        "Candidate text that must remain excluded.",
        encoding="utf-8",
    )
    write_json(
        active_chapter / "observations.json",
        {"status": "candidate", "events": [{"summary": "not canon"}]},
    )
    profile = LlmProfile(
        id="main",
        name="Main",
        protocol="openai-compatible",
        base_url="https://api.example.com/v1",
        api_key=SecretStr("secret"),
        model="story-model",
    )
    monkeypatch.setattr(orchestrator, "get_active_profile", lambda: profile)

    HarnessOrchestrator(
        HarnessRunContext(project_path=project_path, run_id="run-1")
    ).advance_to_next_checkpoint()

    snapshot = read_json(active_chapter / "context_snapshot.json")
    sources_by_id = {source["id"]: source for source in snapshot["sources"]}
    prior_summary = sources_by_id["prior-committed-chapters"]["summary"]
    excluded_sources = {item["source"] for item in snapshot["excluded"]}

    assert "chapter-001" in prior_summary
    assert "First final" in prior_summary
    assert "chapter-000" in prior_summary
    assert "committed final without Markdown heading" in prior_summary
    assert "very long opening prose line" not in prior_summary
    assert "full committed body" not in prior_summary
    assert "Candidate text" not in prior_summary
    assert "chapters/chapter-002/draft.md" in excluded_sources
    assert "chapters/chapter-002/observations.json" in excluded_sources


def test_chapter_goal_prompt_uses_context_snapshot_sources(
    tmp_path,
    monkeypatch,
) -> None:
    project_path = _make_project(tmp_path, setup_approved=True)
    metadata = ProjectMetadata(title="Novel", active_arc_id="arc-001")
    write_json(project_path / "project.json", metadata.model_dump(mode="json"))
    (project_path / "book" / "settings.md").write_text(
        "# Book Settings\n\nSpecial premise for direct injection.",
        encoding="utf-8",
    )
    (project_path / "book" / "outline.md").write_text(
        "# Rolling Contract\n\nReturn to the book loop on constraint conflict.",
        encoding="utf-8",
    )
    write_json(
        project_path / "book" / "state.json",
        {
            "schema_version": 1,
            "version": 5,
            "answers": [{"question_id": "tone", "answer": "quiet dread"}],
            "current_strategy": "keep pressure rising",
        },
    )
    (project_path / "arcs" / "arc-001").mkdir(parents=True)
    (project_path / "arcs" / "arc-001" / "plan.md").write_text(
        "# Arc 1\n\nHold the first rupture.",
        encoding="utf-8",
    )
    write_json(
        project_path / "arcs" / "arc-001" / "state.json",
        {
            "schema_version": 1,
            "version": 7,
            "arc_id": "arc-001",
            "plan_path": "arcs/arc-001/plan.md",
            "status": "planned",
            "target_chapter_count": 3,
            "completed_chapter_ids": [],
        },
    )
    write_json(
        project_path / "canon" / "characters.json",
        {"schema_version": 1, "version": 3, "items": {"hero": {"name": "Hero"}}},
    )
    profile = LlmProfile(
        id="main",
        name="Main",
        protocol="openai-compatible",
        base_url="https://api.example.com/v1",
        api_key=SecretStr("secret"),
        model="story-model",
    )
    captured_prompts: list[str] = []
    monkeypatch.setattr(orchestrator, "get_active_profile", lambda: profile)

    def fake_call_llm(_profile, request):
        captured_prompts.append(request.messages[-1].content)
        return ChatResult(content="# Goal\n", model_snapshot="story-model")

    monkeypatch.setattr(orchestrator, "call_llm", fake_call_llm)

    harness = HarnessOrchestrator(HarnessRunContext(project_path=project_path, run_id="run-1"))
    harness.advance_to_next_checkpoint()
    harness.advance_to_next_checkpoint()

    prompt = captured_prompts[-1]

    assert "Assembled context" in prompt
    assert "Special premise for direct injection." in prompt
    assert "该项目从旧版全书设定迁移而来" in prompt
    assert "keep pressure rising" in prompt
    assert "# Arc 1" in prompt
    assert '"target_chapter_count": 3' in prompt
    assert "canon/characters.json has 1 committed item(s)." in prompt
    assert "Excluded sources:" in prompt
    assert "chapters/chapter-001/draft.md" in prompt
    assert "chapters/chapter-001/observations.json" in prompt


def test_orchestrator_processes_feedback_before_next_checkpoint(
    tmp_path,
    monkeypatch,
) -> None:
    project_path = _make_project(tmp_path, setup_approved=True)
    metadata = ProjectMetadata(
        title="Novel",
        active_arc_id="arc-001",
        active_chapter_id="chapter-001",
    )
    write_json(project_path / "project.json", metadata.model_dump(mode="json"))
    (project_path / "arcs" / "arc-001").mkdir(parents=True)
    (project_path / "arcs" / "arc-001" / "plan.md").write_text("# Arc 1\n", encoding="utf-8")
    profile = LlmProfile(
        id="main",
        name="Main",
        protocol="openai-compatible",
        base_url="https://api.example.com/v1",
        api_key=SecretStr("secret"),
        model="story-model",
    )
    monkeypatch.setattr(orchestrator, "get_active_profile", lambda: profile)
    append_event(
        project_path,
        HarnessEvent(
            project_id=metadata.project_id,
            kind="user_feedback",
            message="Make the next chapter quieter.",
            payload={"feedback": "Make the next chapter quieter."},
        ),
    )

    harness = HarnessOrchestrator(HarnessRunContext(project_path=project_path, run_id="run-1"))
    harness.advance_to_next_checkpoint()
    harness.advance_to_next_checkpoint()

    events = read_events(project_path)
    snapshot = read_json(project_path / "chapters" / "chapter-001" / "context_snapshot.json")
    processed = next(event for event in events if event.kind == "feedback_processed")

    assert processed.routing_decision == "apply_to_current_chapter_context"
    assert any(source["id"] == "processed-user-feedback" for source in snapshot["sources"])


def test_orchestrator_injects_feedback_after_context_snapshot_exists(
    tmp_path,
    monkeypatch,
) -> None:
    project_path = _make_project(tmp_path, setup_approved=True)
    metadata = ProjectMetadata(
        title="Novel",
        active_arc_id="arc-001",
        active_chapter_id="chapter-001",
    )
    write_json(project_path / "project.json", metadata.model_dump(mode="json"))
    (project_path / "book" / "settings.md").write_bytes(b"\xef\xbb\xbf# Book Settings\n")
    (project_path / "arcs" / "arc-001").mkdir(parents=True)
    (project_path / "arcs" / "arc-001" / "plan.md").write_bytes(b"\xef\xbb\xbf# Arc 1\n")
    chapter_path = project_path / "chapters" / "chapter-001"
    chapter_path.mkdir(parents=True)
    write_json(chapter_path / "context_snapshot.json", {"schema_version": 1})
    profile = LlmProfile(
        id="main",
        name="Main",
        protocol="openai-compatible",
        base_url="https://api.example.com/v1",
        api_key=SecretStr("secret"),
        model="story-model",
    )
    captured_prompts: list[str] = []
    monkeypatch.setattr(orchestrator, "get_active_profile", lambda: profile)

    def fake_call_llm(_profile, request):
        captured_prompts.append(request.messages[-1].content)
        return ChatResult(content="# Goal\n", model_snapshot="story-model")

    monkeypatch.setattr(orchestrator, "call_llm", fake_call_llm)
    append_event(
        project_path,
        HarnessEvent(
            project_id=metadata.project_id,
            kind="user_feedback",
            message="Make the next scene quieter.",
            payload={"feedback": "Make the next scene quieter."},
        ),
    )

    harness = HarnessOrchestrator(HarnessRunContext(project_path=project_path, run_id="run-1"))
    harness.advance_to_next_checkpoint()
    harness.advance_to_next_checkpoint()

    snapshot = read_json(chapter_path / "context_snapshot.json")
    assert captured_prompts
    assert "\ufeff" not in captured_prompts[-1]
    assert "# Arc 1" in captured_prompts[-1]
    assert "User checkpoint feedback" in captured_prompts[-1]
    assert "Make the next scene quieter." in captured_prompts[-1]
    assert any(source["id"] == "processed-user-feedback" for source in snapshot["sources"])


def test_arc_feedback_revises_current_arc_plan_and_reopens_participatory_review(
    tmp_path,
    monkeypatch,
) -> None:
    project_path = _make_project(tmp_path, setup_approved=True)
    metadata = ProjectMetadata(
        title="Novel",
        operation_mode="participatory",
        active_arc_id="arc-001",
    )
    write_json(project_path / "project.json", metadata.model_dump(mode="json"))
    (project_path / "arcs" / "arc-001").mkdir(parents=True)
    write_json(
        project_path / "arcs" / "arc-001" / "state.json",
        {
            "schema_version": 1,
            "version": 1,
            "arc_id": "arc-001",
            "status": "approved",
            "plan_path": "arcs/arc-001/plan.md",
            "human_review": "approved",
            "approved_at": "2026-07-08T00:00:00+00:00",
        },
    )
    (project_path / "arcs" / "arc-001" / "plan.md").write_text(
        "# Arc 1\n\nMove quickly.",
        encoding="utf-8",
    )
    profile = LlmProfile(
        id="main",
        name="Main",
        protocol="openai-compatible",
        base_url="https://api.example.com/v1",
        api_key=SecretStr("secret"),
        model="story-model",
    )
    captured_prompt = ""
    monkeypatch.setattr(orchestrator, "get_active_profile", lambda: profile)

    def fake_call_llm(_profile, request):
        nonlocal captured_prompt
        captured_prompt = request.messages[-1].content
        return ChatResult(
            content=(
                '{"plan_markdown":"# Arc 1\\n\\nSlow the pacing and emphasize recovery.",'
                '"target_chapter_count":7}'
            ),
            model_snapshot="story-model",
            provider_snapshot="openai-compatible",
        )

    monkeypatch.setattr(orchestrator, "call_llm", fake_call_llm)
    append_event(
        project_path,
        HarnessEvent(
            project_id=metadata.project_id,
            kind="user_feedback",
            message="The arc pacing should slow down.",
            payload={"feedback": "The arc pacing should slow down."},
        ),
    )

    HarnessOrchestrator(
        HarnessRunContext(project_path=project_path, run_id="run-1")
    ).advance_to_next_checkpoint()

    metadata_payload = read_json(project_path / "project.json")
    arc_state = read_json(project_path / "arcs" / "arc-001" / "state.json")
    plan = (project_path / "arcs" / "arc-001" / "plan.md").read_text(encoding="utf-8")
    revision = (project_path / "arcs" / "arc-001" / "revision.md").read_text(encoding="utf-8")
    events = read_events(project_path)
    revision_event = next(
        event
        for event in events
        if event.kind == "feedback_artifact_written"
        and event.atomic_action == "revise_current_arc_plan"
    )

    assert "The arc pacing should slow down." in captured_prompt
    assert "Slow the pacing" in plan
    assert "User Feedback" in revision
    assert arc_state["version"] == 2
    assert arc_state["recommended_target_chapter_count"] == 7
    assert arc_state["target_chapter_count"] == 7
    assert arc_state["human_review"] == "awaiting_review"
    assert metadata_payload["run_status"] == "waiting_for_user"
    assert any(event.kind == "feedback_artifact_written" for event in events)
    _assert_sanitized_llm_payload(revision_event)
    assert revision_event.payload["revision_path"] == "arcs/arc-001/revision.md"
    assert events[-1].kind == "feedback_processed"
    assert events[-1].artifact_path == "arcs/arc-001/plan.md"


def test_book_feedback_writes_long_term_memo_and_context_source(
    tmp_path,
    monkeypatch,
) -> None:
    project_path = _make_project(tmp_path, setup_approved=True)
    metadata = ProjectMetadata(title="Novel", active_arc_id="arc-001")
    write_json(project_path / "project.json", metadata.model_dump(mode="json"))
    (project_path / "arcs" / "arc-001").mkdir(parents=True)
    (project_path / "arcs" / "arc-001" / "plan.md").write_text("# Arc 1\n", encoding="utf-8")
    profile = LlmProfile(
        id="main",
        name="Main",
        protocol="openai-compatible",
        base_url="https://api.example.com/v1",
        api_key=SecretStr("secret"),
        model="story-model",
    )
    monkeypatch.setattr(orchestrator, "get_active_profile", lambda: profile)
    monkeypatch.setattr(
        orchestrator,
        "call_llm",
        lambda _profile, _request: ChatResult(
            content="Future arcs should preserve a tragic ending promise.",
            model_snapshot="story-model",
            provider_snapshot="openai-compatible",
        ),
    )
    append_event(
        project_path,
        HarnessEvent(
            project_id=metadata.project_id,
            kind="user_feedback",
            message="Change the ending into a tragic ending.",
            payload={"feedback": "Change the ending into a tragic ending."},
        ),
    )

    harness = HarnessOrchestrator(HarnessRunContext(project_path=project_path, run_id="run-1"))
    harness.advance_to_next_checkpoint()
    harness.advance_to_next_checkpoint()

    book_feedback = (project_path / "book" / "feedback.md").read_text(encoding="utf-8")
    book_state = read_json(project_path / "book" / "state.json")
    snapshot = read_json(project_path / "chapters" / "chapter-001" / "context_snapshot.json")
    events = read_events(project_path)
    processed = next(event for event in events if event.kind == "feedback_processed")
    feedback_event = next(
        event
        for event in events
        if event.kind == "feedback_artifact_written"
        and event.atomic_action == "record_book_feedback"
    )

    assert "Change the ending into a tragic ending." in book_feedback
    assert "tragic ending promise" in book_feedback
    assert book_state["feedback_path"] == "book/feedback.md"
    assert any(source["id"] == "book-feedback" for source in snapshot["sources"])
    _assert_sanitized_llm_payload(feedback_event)
    assert processed.artifact_path == "book/feedback.md"


def test_orchestrator_uses_semantic_verifier_routing(tmp_path, monkeypatch) -> None:
    project_path = _make_project(tmp_path, setup_approved=True)
    metadata = ProjectMetadata(title="Novel", active_arc_id="arc-001", active_chapter_id="chapter-001")
    write_json(project_path / "project.json", metadata.model_dump(mode="json"))
    (project_path / "arcs" / "arc-001").mkdir(parents=True)
    (project_path / "arcs" / "arc-001" / "plan.md").write_text("# Arc 1\n", encoding="utf-8")
    chapter_path = project_path / "chapters" / "chapter-001"
    chapter_path.mkdir(parents=True)
    write_json(chapter_path / "context_snapshot.json", {"schema_version": 1})
    write_json(chapter_path / "observations.json", {"schema_version": 1, "status": "candidate"})
    (chapter_path / "goal.md").write_text("Resolve the scene without killing the mentor.", encoding="utf-8")
    (chapter_path / "draft.md").write_text("The mentor dies abruptly.", encoding="utf-8")
    (chapter_path / "review.md").write_text("The draft violates the scene contract.", encoding="utf-8")
    profile = LlmProfile(
        id="main",
        name="Main",
        protocol="openai-compatible",
        base_url="https://api.example.com/v1",
        api_key=SecretStr("secret"),
        model="story-model",
    )
    monkeypatch.setattr(orchestrator, "get_active_profile", lambda: profile)
    monkeypatch.setattr(
        orchestrator,
        "call_llm",
        lambda _profile, _request: ChatResult(
            content=(
                '{"goal_satisfied":false,"commit_allowed":false,'
                '"routing_decision":"rewrite",'
                '"signals":[{"name":"chapter_contract","status":"failed",'
                '"evidence":"The mentor dies abruptly."}],'
                '"reasons":["The draft violates the chapter contract."]}'
            ),
            model_snapshot="story-model",
            provider_snapshot="openai-compatible",
        ),
    )

    HarnessOrchestrator(
        HarnessRunContext(project_path=project_path, run_id="run-1")
    ).advance_to_next_checkpoint()

    verification = read_json(chapter_path / "verification.json")
    events = read_events(project_path)

    assert verification["commit_allowed"] is False
    assert verification["routing_decision"] == "rewrite"
    assert verification["signals"][0]["name"] == "chapter_contract"
    assert not (chapter_path / "final.md").exists()
    assert any(
        event.kind == "verification_completed"
        and event.routing_decision == "rewrite"
        for event in events
    )
    assert events[-1].kind == "agent_semantic_revision_exhausted"
    assert events[-1].routing_decision == "pause"
    assert read_json(project_path / "project.json")["run_status"] == "waiting_for_user"


def test_orchestrator_advances_chapter_to_committed_state_patch(
    tmp_path,
    monkeypatch,
) -> None:
    project_path = _make_project(tmp_path, setup_approved=True)
    metadata = ProjectMetadata(title="Novel", active_arc_id="arc-001")
    write_json(project_path / "project.json", metadata.model_dump(mode="json"))
    (project_path / "arcs" / "arc-001").mkdir(parents=True)
    (project_path / "arcs" / "arc-001" / "plan.md").write_text(
        "# Arc 1\n",
        encoding="utf-8",
    )
    for name in ["characters", "relationships", "world_facts", "foreshadowing"]:
        write_json(
            project_path / "canon" / f"{name}.json",
            {"schema_version": 1, "version": 1, "items": {}},
        )
    profile = LlmProfile(
        id="main",
        name="Main",
        protocol="openai-compatible",
        base_url="https://api.example.com/v1",
        api_key=SecretStr("secret"),
        model="story-model",
    )
    monkeypatch.setattr(orchestrator, "get_active_profile", lambda: profile)

    def fake_call_llm(_profile, request):
        action = request.metadata["atomic_action"]
        if action == "extract_candidate_observations":
            content = (
                '{"schema_version":1,"status":"candidate","based_on":"chapters/chapter-001/draft.md",'
                '"events":[{"summary":"trust changes"}],"character_changes":[],'
                '"relationship_changes":[],"world_fact_candidates":[],'
                '"foreshadowing_candidates":[],"requires_commit":true}'
            )
        elif action == "generate_candidate_state_patch":
            content = (
                '{"schema_version":1,"status":"candidate","based_on":{},'
                '"operations":[{"op":"upsert","target_file":"canon/characters.json",'
                '"target_id":"protagonist","expected_version":1,'
                '"value":{"belief":"trusts companions"},'
                '"evidence":[{"file":"chapters/chapter-001/final.md","quote":"trusts companions"}],'
                '"rationale":"The final chapter says the protagonist trusts companions."}]}'
            )
        elif action == "draft_chapter":
            content = "The protagonist trusts companions after the trial."
        elif action == "verify_chapter":
            content = (
                '{"goal_satisfied":true,"commit_allowed":true,"routing_decision":"commit",'
                '"signals":[{"name":"chapter_contract","status":"passed",'
                '"evidence":"The trust shift is visible."}],'
                '"reasons":[]}'
            )
        else:
            content = f"# {action}\n"
        return ChatResult(
            content=content,
            model_snapshot="story-model",
            provider_snapshot="openai-compatible",
        )

    monkeypatch.setattr(orchestrator, "call_llm", fake_call_llm)

    harness = HarnessOrchestrator(HarnessRunContext(project_path=project_path, run_id="run-1"))
    for _ in range(4):
        harness.advance_to_next_checkpoint()

    chapter_path = project_path / "chapters" / "chapter-001"
    characters = read_json(project_path / "canon" / "characters.json")
    events = read_events(project_path)

    assert (chapter_path / "context_snapshot.json").exists()
    assert (chapter_path / "goal.md").exists()
    assert (chapter_path / "draft.md").exists()
    assert (chapter_path / "observations.json").exists()
    assert (chapter_path / "review.md").exists()
    assert (chapter_path / "verification.json").exists()
    assert (chapter_path / "final.md").exists()
    assert (chapter_path / "candidate_state_patch.json").exists()
    assert (chapter_path / "committed_state_patch.json").exists()
    assert characters["version"] == 2
    assert characters["items"]["protagonist"]["belief"] == "trusts companions"
    assert events[-1].kind == "state_patch_committed"
    assert (chapter_path / "evaluation.json").exists()
    agent_events = [
        event
        for event in events
        if event.atomic_action == "run_chapter_agent" and event.payload.get("profile_id")
    ]
    assert agent_events
    for event in agent_events:
        _assert_sanitized_llm_payload(event)


def test_orchestrator_rejects_legacy_partial_chapter_without_agent_patch(
    tmp_path, monkeypatch
) -> None:
    project_path = _make_project(tmp_path, setup_approved=True)
    metadata = ProjectMetadata(
        title="Novel",
        active_arc_id="arc-001",
        active_chapter_id="chapter-001",
    )
    write_json(project_path / "project.json", metadata.model_dump(mode="json"))
    (project_path / "arcs" / "arc-001").mkdir(parents=True)
    (project_path / "arcs" / "arc-001" / "plan.md").write_text("# Arc 1\n", encoding="utf-8")
    chapter_path = project_path / "chapters" / "chapter-001"
    chapter_path.mkdir(parents=True)
    for artifact in ["context_snapshot.json", "observations.json"]:
        write_json(chapter_path / artifact, {"schema_version": 1})
    write_json(
        chapter_path / "verification.json",
        {
            "schema_version": 1,
            "chapter_id": "chapter-001",
            "goal_satisfied": True,
            "commit_allowed": True,
            "routing_decision": "commit",
            "signals": [],
            "reasons": [],
        },
    )
    for artifact in ["goal.md", "draft.md", "review.md", "final.md"]:
        (chapter_path / artifact).write_text(
            "The protagonist trusts companions.",
            encoding="utf-8",
        )
    profile = LlmProfile(
        id="main",
        name="Main",
        protocol="openai-compatible",
        base_url="https://api.example.com/v1",
        api_key=SecretStr("secret"),
        model="story-model",
    )
    monkeypatch.setattr(orchestrator, "get_active_profile", lambda: profile)
    monkeypatch.setattr(
        orchestrator,
        "call_llm",
        lambda _profile, _request: ChatResult(
            content="No canon changes.",
            model_snapshot="story-model",
            provider_snapshot="openai-compatible",
        ),
    )

    HarnessOrchestrator(
        HarnessRunContext(project_path=project_path, run_id="run-1")
    ).advance_to_next_checkpoint()

    metadata_payload = read_json(project_path / "project.json")
    events = read_events(project_path)

    assert not (chapter_path / "candidate_state_patch.json").exists()
    assert not (chapter_path / "committed_state_patch.json").exists()
    assert metadata_payload["run_status"] == "failed"
    assert events[-1].kind == "run_failed"
    assert not (chapter_path / "state_patch_rejection.json").exists()


@pytest.mark.parametrize("operation_mode", ["full_auto", "participatory"])
def test_repairs_rejected_patch_quotes_without_changing_operations(
    tmp_path: Path,
    monkeypatch,
    operation_mode: str,
) -> None:
    project_path = _make_project(tmp_path, setup_approved=True)
    metadata = ProjectMetadata(
        title="Novel",
        operation_mode=operation_mode,
        active_arc_id="arc-001",
        active_chapter_id="chapter-001",
        run_status="running",
    )
    write_json(project_path / "project.json", metadata.model_dump(mode="json"))
    write_json(
        project_path / "arcs" / "arc-001" / "state.json",
        {
            "arc_id": "arc-001",
            "status": "approved",
            "plan_path": "arcs/arc-001/plan.md",
            "human_review": "approved",
            "approved_at": "2026-07-15T00:00:00+00:00",
        },
    )
    chapter_path = project_path / "chapters" / "chapter-001"
    chapter_path.mkdir(parents=True)
    (chapter_path / "context_snapshot.json").write_text("{}\n", encoding="utf-8")
    (chapter_path / "draft.md").write_text(
        "The protagonist trusts companions after the trial.\n",
        encoding="utf-8",
    )
    (chapter_path / "final.md").write_text(
        "The protagonist trusts companions after the trial.\n",
        encoding="utf-8",
    )
    write_json(
        chapter_path / "observations.json",
        {
            "schema_version": 1,
            "status": "candidate",
            "based_on": "chapters/chapter-001/draft.md",
        },
    )
    write_json(
        chapter_path / "verification.json",
        {
            "schema_version": 1,
            "chapter_id": "chapter-001",
            "goal_satisfied": True,
            "commit_allowed": True,
            "routing_decision": "commit",
            "signals": [],
            "reasons": [],
        },
    )
    patch = CandidateStatePatch.model_validate(
        {
            "based_on": {
                "chapter_final": "chapters/chapter-001/final.md",
                "observations": "chapters/chapter-001/observations.json",
            },
            "operations": [
                {
                    "op": "upsert",
                    "target_file": "canon/characters.json",
                    "target_id": "protagonist",
                    "expected_version": 1,
                    "value": {"belief": "trusts companions"},
                    "evidence": [
                        {
                            "file": "chapters/chapter-001/final.md",
                            "quote": "paraphrased trust",
                        }
                    ],
                    "rationale": "The chapter changes the protagonist's belief.",
                }
            ],
        }
    )
    write_json(
        chapter_path / "candidate_state_patch.json",
        patch.model_dump(mode="json"),
    )
    for name in ["characters", "relationships", "world_facts", "foreshadowing"]:
        write_json(
            project_path / "canon" / f"{name}.json",
            {"schema_version": 1, "version": 1, "items": {}},
        )
    profile = LlmProfile(
        id="main",
        name="Main",
        protocol="openai-compatible",
        base_url="https://api.example.com/v1",
        api_key=SecretStr("secret"),
        model="story-model",
    )
    monkeypatch.setattr(orchestrator, "get_active_profile", lambda: profile)

    def fake_repair(*_args, **_kwargs) -> ChapterPatchEvidenceRepairResult:
        repaired = patch.model_copy(
            update={
                "operations": [
                    patch.operations[0].model_copy(
                        update={
                            "evidence": [
                                patch.operations[0].evidence[0].model_copy(
                                    update={"quote": "trusts companions"}
                                )
                            ]
                        }
                    )
                ]
            }
        )
        identity = AgentIdentity(
            project_id=metadata.project_id,
            role="chapter",
            scope_id="chapter-001",
        )
        return ChapterPatchEvidenceRepairResult(
            patch=repaired,
            run_result=AgentRunResult(
                outcome="candidate",
                identity=identity,
                candidate_run_id="patch-repair-run",
                activation_id="patch-repair-activation",
                turns_used=1,
            ),
            candidate_artifact_path=(
                "chapters/chapter-001/agent/a/patch-repair/c/"
                "state-patch-evidence-repair.json"
            ),
        )

    monkeypatch.setattr(
        orchestrator,
        "run_chapter_patch_evidence_repair_agent",
        fake_repair,
    )
    harness = HarnessOrchestrator(
        HarnessRunContext(project_path=project_path, run_id="run-1")
    )

    harness.advance_to_next_checkpoint()
    assert read_events(project_path)[-1].routing_decision == "repair_current_candidate"
    assert read_json(project_path / "project.json")["run_status"] == "running"

    harness.advance_to_next_checkpoint()
    repaired_patch = read_json(chapter_path / "candidate_state_patch.json")
    assert repaired_patch["operations"][0]["value"] == {
        "belief": "trusts companions"
    }
    assert repaired_patch["operations"][0]["evidence"][0]["quote"] == (
        "trusts companions"
    )

    harness.advance_to_next_checkpoint()
    assert (chapter_path / "committed_state_patch.json").exists()
    assert read_json(project_path / "canon" / "characters.json")["items"] == {
        "protagonist": {"belief": "trusts companions"}
    }


def test_orchestrator_marks_chapter_complete_and_starts_next_chapter(
    tmp_path,
    monkeypatch,
) -> None:
    project_path = _make_project(tmp_path, setup_approved=True)
    metadata = ProjectMetadata(title="Novel", active_arc_id="arc-001", active_chapter_id="chapter-001")
    write_json(project_path / "project.json", metadata.model_dump(mode="json"))
    (project_path / "arcs" / "arc-001").mkdir(parents=True)
    write_json(
        project_path / "arcs" / "arc-001" / "state.json",
        {
            "schema_version": 1,
            "version": 1,
            "arc_id": "arc-001",
            "status": "planned",
            "plan_path": "arcs/arc-001/plan.md",
            "target_chapter_count": 2,
            "completed_chapter_ids": [],
        },
    )
    (project_path / "arcs" / "arc-001" / "plan.md").write_text("# Arc 1\n", encoding="utf-8")
    (project_path / "chapters" / "chapter-001").mkdir(parents=True)
    for artifact in [
        "context_snapshot.json",
        "observations.json",
        "verification.json",
        "candidate_state_patch.json",
        "committed_state_patch.json",
    ]:
        write_json(project_path / "chapters" / "chapter-001" / artifact, {"schema_version": 1})
    for artifact in ["goal.md", "draft.md", "review.md", "final.md"]:
        (project_path / "chapters" / "chapter-001" / artifact).write_text(
            artifact,
            encoding="utf-8",
        )
    profile = LlmProfile(
        id="main",
        name="Main",
        protocol="openai-compatible",
        base_url="https://api.example.com/v1",
        api_key=SecretStr("secret"),
        model="story-model",
    )
    monkeypatch.setattr(orchestrator, "get_active_profile", lambda: profile)

    harness = HarnessOrchestrator(HarnessRunContext(project_path=project_path, run_id="run-1"))
    harness.advance_to_next_checkpoint()
    harness.advance_to_next_checkpoint()

    metadata_payload = read_json(project_path / "project.json")
    arc_state = read_json(project_path / "arcs" / "arc-001" / "state.json")

    assert arc_state["status"] == "in_progress"
    assert arc_state["completed_chapter_ids"] == ["chapter-001"]
    assert metadata_payload["active_arc_id"] == "arc-001"
    assert metadata_payload["active_chapter_id"] == "chapter-002"
    assert (project_path / "chapters" / "chapter-002" / "context_snapshot.json").exists()


def test_completed_arc_rolls_to_next_arc_plan(tmp_path, monkeypatch) -> None:
    project_path = _make_project(tmp_path, setup_approved=True)
    metadata = ProjectMetadata(title="Novel", active_arc_id="arc-001", active_chapter_id="chapter-001")
    write_json(project_path / "project.json", metadata.model_dump(mode="json"))
    (project_path / "arcs" / "arc-001").mkdir(parents=True)
    write_json(
        project_path / "arcs" / "arc-001" / "state.json",
        {
            "schema_version": 1,
            "version": 1,
            "arc_id": "arc-001",
            "status": "planned",
            "plan_path": "arcs/arc-001/plan.md",
            "target_chapter_count": 1,
            "completed_chapter_ids": [],
        },
    )
    (project_path / "arcs" / "arc-001" / "plan.md").write_text("# Arc 1\n", encoding="utf-8")
    (project_path / "chapters" / "chapter-001").mkdir(parents=True)
    for artifact in [
        "context_snapshot.json",
        "observations.json",
        "verification.json",
        "candidate_state_patch.json",
        "committed_state_patch.json",
    ]:
        write_json(project_path / "chapters" / "chapter-001" / artifact, {"schema_version": 1})
    for artifact in ["goal.md", "draft.md", "review.md", "final.md"]:
        (project_path / "chapters" / "chapter-001" / artifact).write_text(
            artifact,
            encoding="utf-8",
        )
    profile = LlmProfile(
        id="main",
        name="Main",
        protocol="openai-compatible",
        base_url="https://api.example.com/v1",
        api_key=SecretStr("secret"),
        model="story-model",
    )
    monkeypatch.setattr(orchestrator, "get_active_profile", lambda: profile)
    monkeypatch.setattr(
        orchestrator,
        "call_llm",
        lambda _profile, _request: ChatResult(
            content=(
                '{"plan_markdown":"# Arc 2\\n\\nThe next rolling arc.",'
                '"target_chapter_count":11}'
            ),
            model_snapshot="story-model",
            provider_snapshot="openai-compatible",
        ),
    )

    harness = HarnessOrchestrator(HarnessRunContext(project_path=project_path, run_id="run-1"))
    harness.advance_to_next_checkpoint()
    harness.advance_to_next_checkpoint()

    metadata_payload = read_json(project_path / "project.json")
    arc_one_state = read_json(project_path / "arcs" / "arc-001" / "state.json")
    arc_two_plan = (project_path / "arcs" / "arc-002" / "plan.md").read_text(encoding="utf-8")

    assert arc_one_state["status"] == "completed"
    assert arc_one_state["completed_chapter_ids"] == ["chapter-001"]
    assert metadata_payload["active_arc_id"] == "arc-002"
    assert metadata_payload["active_chapter_id"] is None
    assert read_json(project_path / "arcs" / "arc-002" / "state.json")[
        "target_chapter_count"
    ] == 11
    assert arc_two_plan.startswith("# Arc 2")
