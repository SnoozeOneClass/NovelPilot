from pydantic import SecretStr

from app.harness import orchestrator
from app.harness.orchestrator import HarnessOrchestrator, HarnessRunContext
from app.llm.gateway import ChatResult
from app.schemas.events import HarnessEvent
from app.schemas.profiles import LlmProfile
from app.schemas.projects import ProjectMetadata
from app.storage import arcs as arc_storage
from app.storage.events import append_event
from app.storage.events import read_events
from app.storage.json_files import read_json, write_json


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
            content="# Arc 1\n\nA focused first arc.",
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
    assert plan.startswith("# Arc 1")
    assert "Approved rolling story arc contract" in captured_prompts[-1]
    assert "该项目从旧版全书设定迁移而来" in captured_prompts[-1]
    assert any(
        event.kind == "llm_output_delta"
        and event.payload.get("text_delta") == "# Arc 1\n\nA focused first arc."
        for event in events
    )
    assert events[-1].kind == "artifact_written"


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
            content="# Arc 1\n\nA focused first arc.",
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
            content="# Arc 1\n\nA focused first arc.",
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

    arc_storage.approve_current_arc(project_path)
    HarnessOrchestrator(
        HarnessRunContext(project_path=project_path, run_id="run-1")
    ).advance_to_next_checkpoint()

    metadata_payload = read_json(project_path / "project.json")
    arc_state = read_json(project_path / "arcs" / "arc-001" / "state.json")

    assert metadata_payload["active_chapter_id"] == "chapter-001"
    assert arc_state["human_review"] == "approved"
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
            content="# Arc 1\n\nSlow the pacing and emphasize recovery.",
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
    assert events[-1].kind == "verification_completed"
    assert events[-1].routing_decision == "rewrite"


def test_structured_chapter_actions_request_json_response_format(
    tmp_path,
    monkeypatch,
) -> None:
    project_path = _make_project(tmp_path, setup_approved=True)
    metadata = ProjectMetadata(title="Novel", active_arc_id="arc-001", active_chapter_id="chapter-001")
    write_json(project_path / "project.json", metadata.model_dump(mode="json"))
    (project_path / "arcs" / "arc-001").mkdir(parents=True)
    (project_path / "arcs" / "arc-001" / "plan.md").write_text("# Arc 1\n", encoding="utf-8")
    chapter_path = project_path / "chapters" / "chapter-001"
    chapter_path.mkdir(parents=True)
    write_json(chapter_path / "context_snapshot.json", {"schema_version": 1})
    (chapter_path / "goal.md").write_text("Build trust.", encoding="utf-8")
    (chapter_path / "draft.md").write_text("The protagonist trusts companions.", encoding="utf-8")
    profile = LlmProfile(
        id="main",
        name="Main",
        protocol="openai-compatible",
        base_url="https://api.example.com/v1",
        api_key=SecretStr("secret"),
        model="story-model",
    )
    captured_formats: dict[str, dict[str, object] | None] = {}

    def fake_call_llm(_profile, request):
        action = request.metadata["atomic_action"]
        captured_formats[action] = request.response_format
        if action == "extract_candidate_observations":
            content = (
                '{"schema_version":1,"status":"candidate",'
                '"based_on":"chapters/chapter-001/draft.md","events":[],'
                '"character_changes":[],"relationship_changes":[],"world_fact_candidates":[],'
                '"foreshadowing_candidates":[],"requires_commit":true}'
            )
        elif action == "verify_chapter":
            content = (
                '{"goal_satisfied":true,"commit_allowed":true,"routing_decision":"commit",'
                '"signals":[],"reasons":[]}'
            )
        elif action == "generate_candidate_state_patch":
            content = '{"schema_version":1,"status":"candidate","based_on":{},"operations":[]}'
        else:
            content = "# Review\n\nLooks coherent."
        return ChatResult(
            content=content,
            model_snapshot="story-model",
            provider_snapshot="openai-compatible",
        )

    monkeypatch.setattr(orchestrator, "call_llm", fake_call_llm)
    harness = HarnessOrchestrator(HarnessRunContext(project_path=project_path, run_id="run-1"))
    harness._extract_observations(profile, metadata, "chapter-001", chapter_path)
    harness._review_chapter(profile, metadata, "chapter-001", chapter_path)
    harness._verify_chapter(profile, metadata, "chapter-001", chapter_path)
    harness._write_final_chapter(metadata, "chapter-001", chapter_path)
    harness._generate_candidate_state_patch(profile, metadata, "chapter-001", chapter_path)

    assert captured_formats["extract_candidate_observations"] == {"type": "json_object"}
    assert captured_formats["verify_chapter"] == {"type": "json_object"}
    assert captured_formats["generate_candidate_state_patch"] == {"type": "json_object"}
    assert captured_formats["semantic_review"] is None


def test_orchestrator_rejects_unparseable_verifier_output(tmp_path, monkeypatch) -> None:
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
    write_json(chapter_path / "context_snapshot.json", {"schema_version": 1})
    write_json(chapter_path / "observations.json", {"schema_version": 1, "status": "candidate"})
    (chapter_path / "goal.md").write_text("Keep the mentor alive.", encoding="utf-8")
    (chapter_path / "draft.md").write_text("The mentor survives.", encoding="utf-8")
    (chapter_path / "review.md").write_text("The draft appears coherent.", encoding="utf-8")
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
            content="Looks fine, commit it.",
            model_snapshot="story-model",
            provider_snapshot="openai-compatible",
        ),
    )

    harness = HarnessOrchestrator(HarnessRunContext(project_path=project_path, run_id="run-1"))
    harness.advance_to_next_checkpoint()
    harness.advance_to_next_checkpoint()

    verification = read_json(chapter_path / "verification.json")
    metadata_payload = read_json(project_path / "project.json")
    events = read_events(project_path)

    assert verification["commit_allowed"] is False
    assert verification["routing_decision"] == "rewrite"
    assert "could not be parsed as JSON" in verification["reasons"][0]
    assert not (chapter_path / "final.md").exists()
    assert metadata_payload["run_status"] == "waiting_for_user"
    assert events[-1].kind == "routing_decision"
    assert events[-1].routing_decision == "rewrite"


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
    for _ in range(9):
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
    llm_artifact_actions = {
        "generate_chapter_goal",
        "draft_chapter",
        "extract_candidate_observations",
        "semantic_review",
        "verify_chapter",
        "generate_candidate_state_patch",
    }
    llm_artifact_events = [
        event
        for event in events
        if event.atomic_action in llm_artifact_actions and event.artifact_path is not None
    ]
    assert {event.atomic_action for event in llm_artifact_events} == llm_artifact_actions
    for event in llm_artifact_events:
        _assert_sanitized_llm_payload(event)


def test_orchestrator_rejects_unparseable_state_patch_output(tmp_path, monkeypatch) -> None:
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

    rejection = read_json(chapter_path / "state_patch_rejection.json")
    metadata_payload = read_json(project_path / "project.json")
    events = read_events(project_path)

    assert rejection["schema"] == "failed"
    assert "could not be parsed as JSON" in rejection["reasons"][0]
    assert not (chapter_path / "candidate_state_patch.json").exists()
    assert not (chapter_path / "committed_state_patch.json").exists()
    assert metadata_payload["run_status"] == "waiting_for_user"
    assert events[-1].kind == "state_patch_rejected"
    assert events[-1].atomic_action == "generate_candidate_state_patch"
    _assert_sanitized_llm_payload(events[-1])
    assert events[-1].payload["reasons"] == [
        "State patch generator output could not be parsed as JSON."
    ]


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
            content="# Arc 2\n\nThe next rolling arc.",
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
    assert arc_two_plan.startswith("# Arc 2")
