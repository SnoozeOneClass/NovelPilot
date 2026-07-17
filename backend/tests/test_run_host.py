from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.harness import run_host
from app.harness.flow_router import RunFacts, route_run
from app.schemas.events import HarnessEvent
from app.schemas.projects import ProjectMetadata
from app.storage.events import append_event, read_events
from app.storage.json_files import read_json, write_json
from app.storage.projects import read_project_metadata, write_project_metadata
from app.storage.run_state import (
    accept_run_dispatch,
    action_key_for_project,
    checkpoint_candidate_identity,
    read_run_control_state,
    schedule_provider_wait,
    set_run_intent,
    write_run_control_state,
)
from backend.tests.helpers.harness_invariants import (
    assert_committed_state_unchanged,
    capture_harness_invariants,
)


def test_flow_router_keeps_browser_out_of_forward_progress() -> None:
    assert route_run(RunFacts("running", "idle")) == "advance"
    assert route_run(RunFacts("running", "waiting_for_provider")) == "wait_provider"
    assert (
        route_run(RunFacts("running", "waiting_for_provider", provider_retry_due=True))
        == "advance"
    )
    assert route_run(RunFacts("running", "waiting_for_user")) == "wait_user"
    assert route_run(RunFacts("stopped", "idle")) == "stop"


def test_provider_backoff_is_action_local_and_capped(tmp_path) -> None:
    project_path = tmp_path / "project"
    project_path.mkdir()
    write_json(
        project_path / "project.json",
        ProjectMetadata(title="Novel").model_dump(mode="json"),
    )
    set_run_intent(project_path, desired_state="running", run_id="run-1")
    now = datetime(2026, 7, 16, tzinfo=UTC)

    waits = [
        schedule_provider_wait(
            project_path,
            action_key="chapter:001:evaluate",
            message="EOF",
            now=now,
            random_value=0.5,
        )
        for _ in range(8)
    ]

    assert [int((item.next_wake_at - now).total_seconds()) for item in waits] == [
        10,
        20,
        40,
        80,
        160,
        300,
        300,
        300,
    ]
    reset = schedule_provider_wait(
        project_path,
        action_key="chapter:002:write",
        message="EOF",
        now=now,
        random_value=0.5,
    )
    assert reset.attempt == 1
    assert read_run_control_state(project_path).desired_state == "running"


def test_checkpoint_candidate_identity_prefers_durable_candidate_revision(
    tmp_path,
) -> None:
    project_path = tmp_path / "project"
    project_path.mkdir()
    write_json(
        project_path / "project.json",
        ProjectMetadata(
            title="Novel",
            active_chapter_id="chapter-001",
        ).model_dump(mode="json"),
    )
    state_root = project_path / "chapters" / "chapter-001" / "agent"
    write_json(
        state_root / "state.json",
        {
            "schema_version": 1,
            "identity": {
                "project_id": read_project_metadata(project_path).project_id,
                "role": "chapter",
                "scope_id": "chapter-001",
            },
            "lifecycle": "completed",
            "candidate_run_id": "chapter-run-1",
            "activation_id": "activation-1",
            "phase": "chapter",
            "expected_revision": 7,
            "budgets": None,
            "last_checkpoint_id": "chapter:chapter-001:3",
            "summary": "Candidate completed.",
            "updated_at": datetime.now(UTC).isoformat(),
        },
    )
    write_json(
        state_root / "a" / "activation-1" / "c" / "manifest.json",
        {"candidate_revision": 3},
    )

    assert checkpoint_candidate_identity(project_path) == ("chapter-run-1", 3)


def test_run_host_drives_until_a_real_user_gate(tmp_path, monkeypatch) -> None:
    project_path = tmp_path / "project"
    project_path.mkdir()
    metadata = ProjectMetadata(title="Novel", run_status="idle")
    write_json(project_path / "project.json", metadata.model_dump(mode="json"))
    (project_path / "events.jsonl").write_text("", encoding="utf-8")
    set_run_intent(project_path, desired_state="running", run_id="run-1")

    class FakeOrchestrator:
        def __init__(self, context) -> None:
            self.context = context

        def advance_to_next_checkpoint(self) -> None:
            current = read_project_metadata(self.context.project_path)
            current.run_status = "waiting_for_user"
            write_project_metadata(self.context.project_path, current)
            append_event(
                self.context.project_path,
                HarnessEvent(
                    project_id=current.project_id,
                    run_id=self.context.run_id,
                    kind="story_arc_review_required",
                    loop_layer="story_arc",
                    status="requested",
                    message="Review required.",
                ),
            )

    monkeypatch.setattr(run_host, "HarnessOrchestrator", FakeOrchestrator)

    next_wake = run_host.RunHost()._drive(project_path)

    assert next_wake is None
    assert read_project_metadata(project_path).run_status == "waiting_for_user"
    assert read_run_control_state(project_path).desired_state == "running"
    checkpoint = read_json(
        project_path / "book" / "harness" / "checkpoints" / "00000001.json"
    )
    assert checkpoint["status"] == "waiting"
    assert checkpoint["event_sequence_after"] == 2


def test_run_host_claims_durable_dispatch_before_advancing(
    tmp_path, monkeypatch
) -> None:
    project_path = tmp_path / "project"
    project_path.mkdir()
    metadata = ProjectMetadata(title="Novel", run_status="running")
    write_json(project_path / "project.json", metadata.model_dump(mode="json"))
    (project_path / "events.jsonl").write_text("", encoding="utf-8")
    action_key = action_key_for_project(project_path)
    accepted = accept_run_dispatch(
        project_path,
        run_id="run-1",
        action_key=action_key,
    )

    class StopOrchestrator:
        def __init__(self, context) -> None:
            self.context = context

        def advance_to_next_checkpoint(self) -> None:
            set_run_intent(self.context.project_path, desired_state="stopped")
            current = read_project_metadata(self.context.project_path)
            current.run_status = "idle"
            write_project_metadata(self.context.project_path, current)
            append_event(
                self.context.project_path,
                HarnessEvent(
                    project_id=current.project_id,
                    run_id=self.context.run_id,
                    kind="artifact_written",
                    loop_layer="book",
                    atomic_action="book:bootstrap",
                    status="completed",
                    message="Bootstrap complete.",
                ),
            )

    monkeypatch.setattr(run_host, "HarnessOrchestrator", StopOrchestrator)

    assert run_host.RunHost()._drive(project_path) is None

    events = read_events(project_path)
    claimed = next(event for event in events if event.kind == "run_dispatch_claimed")
    assert claimed.payload == {
        "dispatch_id": accepted.dispatch_id,
        "dispatch_status": "claimed",
        "action_key": action_key,
    }


def test_run_host_reconciles_durable_running_intent_after_restart(
    tmp_path, monkeypatch
) -> None:
    project_path = tmp_path / "project"
    project_path.mkdir()
    write_json(
        project_path / "project.json",
        ProjectMetadata(title="Novel", run_status="running").model_dump(mode="json"),
    )
    set_run_intent(project_path, desired_state="running", run_id="run-1")
    monkeypatch.setattr(
        run_host,
        "list_projects",
        lambda: [SimpleNamespace(path=str(project_path))],
    )
    woken: list = []
    host = run_host.RunHost()
    monkeypatch.setattr(host, "wake", woken.append)

    host.reconcile()

    assert woken == [project_path]


def test_run_host_progress_guard_stops_an_action_without_durable_event(
    tmp_path, monkeypatch
) -> None:
    project_path = tmp_path / "project"
    project_path.mkdir()
    write_json(
        project_path / "project.json",
        ProjectMetadata(title="Novel", run_status="idle").model_dump(mode="json"),
    )
    (project_path / "events.jsonl").write_text("", encoding="utf-8")
    set_run_intent(project_path, desired_state="running", run_id="run-1")

    class NoProgressOrchestrator:
        def __init__(self, context) -> None:
            self.context = context

        def advance_to_next_checkpoint(self) -> None:
            return None

    monkeypatch.setattr(run_host, "HarnessOrchestrator", NoProgressOrchestrator)

    assert run_host.RunHost()._drive(project_path) is None
    assert read_project_metadata(project_path).run_status == "failed"
    assert read_run_control_state(project_path).desired_state == "stopped"
    assert read_events(project_path)[-1].kind == "run_progress_guard_failed"


def test_run_host_finishes_checkpoint_when_action_raises(
    tmp_path, monkeypatch
) -> None:
    project_path = tmp_path / "project"
    project_path.mkdir()
    write_json(
        project_path / "project.json",
        ProjectMetadata(title="Novel", run_status="idle").model_dump(mode="json"),
    )
    (project_path / "events.jsonl").write_text("", encoding="utf-8")
    set_run_intent(project_path, desired_state="running", run_id="run-1")

    class ExplodingOrchestrator:
        def __init__(self, context) -> None:
            self.context = context

        def advance_to_next_checkpoint(self) -> None:
            raise RuntimeError("provider secret should not be exposed")

    monkeypatch.setattr(run_host, "HarnessOrchestrator", ExplodingOrchestrator)

    assert run_host.RunHost()._drive(project_path) is None
    assert read_project_metadata(project_path).run_status == "failed"
    assert read_run_control_state(project_path).desired_state == "stopped"
    event = read_events(project_path)[-1]
    checkpoint = read_json(
        project_path / "book" / "harness" / "checkpoints" / "00000001.json"
    )
    assert event.kind == "run_host_action_failed"
    assert "provider secret" not in event.message
    assert checkpoint["status"] == "failed"
    assert checkpoint["event_sequence_after"] == event.seq
    assert "provider secret" not in checkpoint["failure"]


def test_run_host_preserves_pause_that_races_with_provider_wait(
    tmp_path, monkeypatch
) -> None:
    project_path = tmp_path / "project"
    project_path.mkdir()
    write_json(
        project_path / "project.json",
        ProjectMetadata(title="Novel", run_status="idle").model_dump(mode="json"),
    )
    (project_path / "events.jsonl").write_text("", encoding="utf-8")
    set_run_intent(project_path, desired_state="running", run_id="run-1")

    class ProviderWaitAfterPauseOrchestrator:
        def __init__(self, context) -> None:
            self.context = context

        def advance_to_next_checkpoint(self) -> None:
            schedule_provider_wait(
                self.context.project_path,
                action_key="chapter:001:evaluate",
                message="EOF",
                random_value=0.5,
            )
            set_run_intent(self.context.project_path, desired_state="stopped")
            current = read_project_metadata(self.context.project_path)
            current.run_status = "waiting_for_provider"
            write_project_metadata(self.context.project_path, current)
            append_event(
                self.context.project_path,
                HarnessEvent(
                    project_id=current.project_id,
                    run_id=self.context.run_id,
                    kind="run_waiting_for_provider",
                    loop_layer="system",
                    status="requested",
                    message="Waiting after a pause request.",
                ),
            )

    monkeypatch.setattr(
        run_host,
        "HarnessOrchestrator",
        ProviderWaitAfterPauseOrchestrator,
    )

    assert run_host.RunHost()._drive(project_path) is None
    assert read_project_metadata(project_path).run_status == "paused"
    state = read_run_control_state(project_path)
    assert state.desired_state == "stopped"
    assert state.provider_wait is None


def test_run_host_checkpoints_durable_provider_wait(tmp_path, monkeypatch) -> None:
    project_path = tmp_path / "project"
    project_path.mkdir()
    write_json(
        project_path / "project.json",
        ProjectMetadata(title="Novel", run_status="idle").model_dump(mode="json"),
    )
    (project_path / "events.jsonl").write_text("", encoding="utf-8")
    set_run_intent(project_path, desired_state="running", run_id="run-1")

    class WaitingOrchestrator:
        def __init__(self, context) -> None:
            self.context = context

        def advance_to_next_checkpoint(self) -> None:
            wait = schedule_provider_wait(
                self.context.project_path,
                action_key="chapter:001:evaluate",
                message="EOF",
                random_value=0.5,
            )
            current = read_project_metadata(self.context.project_path)
            current.run_status = "waiting_for_provider"
            write_project_metadata(self.context.project_path, current)
            append_event(
                self.context.project_path,
                HarnessEvent(
                    project_id=current.project_id,
                    run_id=self.context.run_id,
                    kind="run_waiting_for_provider",
                    loop_layer="system",
                    status="requested",
                    message="Waiting for provider recovery.",
                    payload={"next_wake_at": wait.next_wake_at.isoformat()},
                ),
            )

    monkeypatch.setattr(run_host, "HarnessOrchestrator", WaitingOrchestrator)

    next_wake = run_host.RunHost()._drive(project_path)

    state = read_run_control_state(project_path)
    checkpoint = read_json(
        project_path / "book" / "harness" / "checkpoints" / "00000001.json"
    )
    assert state.provider_wait is not None
    assert next_wake == state.provider_wait.next_wake_at
    assert checkpoint["status"] == "waiting"
    assert checkpoint["provider_wait_attempt"] == 1
    assert datetime.fromisoformat(
        checkpoint["next_wake_at"].replace("Z", "+00:00")
    ) == state.provider_wait.next_wake_at


def test_run_host_advances_multiple_internal_actions_without_browser(
    tmp_path, monkeypatch
) -> None:
    project_path = tmp_path / "project"
    project_path.mkdir()
    write_json(
        project_path / "project.json",
        ProjectMetadata(title="Novel", run_status="idle").model_dump(mode="json"),
    )
    (project_path / "events.jsonl").write_text("", encoding="utf-8")
    set_run_intent(project_path, desired_state="running", run_id="run-1")
    calls = 0

    class MultiStepOrchestrator:
        def __init__(self, context) -> None:
            self.context = context

        def advance_to_next_checkpoint(self) -> None:
            nonlocal calls
            calls += 1
            current = read_project_metadata(self.context.project_path)
            current.run_status = "idle"
            write_project_metadata(self.context.project_path, current)
            append_event(
                self.context.project_path,
                HarnessEvent(
                    project_id=current.project_id,
                    run_id=self.context.run_id,
                    kind="artifact_written",
                    loop_layer="chapter",
                    atomic_action=f"internal-step-{calls}",
                    status="completed",
                    message=f"Completed internal step {calls}.",
                ),
            )
            if calls == 4:
                set_run_intent(self.context.project_path, desired_state="stopped")

    monkeypatch.setattr(run_host, "HarnessOrchestrator", MultiStepOrchestrator)

    assert run_host.RunHost()._drive(project_path) is None
    assert calls == 4
    assert read_project_metadata(project_path).run_status == "idle"
    checkpoints = sorted(
        (project_path / "book" / "harness" / "checkpoints").glob("*.json")
    )
    assert len(checkpoints) == 4
    assert all(read_json(path)["status"] == "completed" for path in checkpoints)


def test_mocked_multi_chapter_run_survives_provider_waits_and_restart(
    tmp_path, monkeypatch
) -> None:
    project_path = tmp_path / "project"
    project_path.mkdir()
    write_json(
        project_path / "project.json",
        ProjectMetadata(
            title="Novel",
            operation_mode="full_auto",
            active_arc_id="arc-001",
            active_chapter_id="chapter-001",
            run_status="idle",
        ).model_dump(mode="json"),
    )
    (project_path / "events.jsonl").write_text("", encoding="utf-8")
    set_run_intent(project_path, desired_state="running", run_id="run-1")
    baseline = capture_harness_invariants(
        project_path,
        include_readiness=False,
    )
    calls = 0
    wait_attempts: list[int] = []

    class FaultInjectedOrchestrator:
        def __init__(self, context) -> None:
            self.context = context

        def advance_to_next_checkpoint(self) -> None:
            nonlocal calls
            calls += 1
            current = read_project_metadata(self.context.project_path)
            current.run_status = "idle"
            kind = f"mocked_step_{calls}"
            message = f"Completed mocked step {calls}."

            if calls in {2, 6}:
                action_key = (
                    "chapter:001:evaluate"
                    if calls == 2
                    else "chapter:002:evaluate"
                )
                wait = schedule_provider_wait(
                    self.context.project_path,
                    action_key=action_key,
                    message="temporary EOF",
                    random_value=0.5,
                )
                wait_attempts.append(wait.attempt)
                current.run_status = "waiting_for_provider"
                kind = "run_waiting_for_provider"
                message = "Provider recovery is scheduled durably."
            elif calls == 4:
                kind = "chapter_patch_evidence_repair_completed"
                message = "Malformed evidence was repaired automatically."
            elif calls == 5:
                current.active_chapter_id = "chapter-002"
                kind = "chapter_completed"
                message = "Chapter 1 committed; Chapter 2 is now active."
            elif calls == 8:
                kind = "chapter_completed"
                message = "Chapter 2 committed."
                set_run_intent(self.context.project_path, desired_state="stopped")

            write_project_metadata(self.context.project_path, current)
            append_event(
                self.context.project_path,
                HarnessEvent(
                    project_id=current.project_id,
                    run_id=self.context.run_id,
                    kind=kind,
                    loop_layer="chapter",
                    atomic_action=f"mocked-step-{calls}",
                    status="completed",
                    message=message,
                ),
            )

    monkeypatch.setattr(
        run_host,
        "HarnessOrchestrator",
        FaultInjectedOrchestrator,
    )

    first_wake = run_host.RunHost()._drive(project_path)
    assert first_wake is not None
    assert calls == 2

    state = read_run_control_state(project_path)
    assert state.provider_wait is not None
    state.provider_wait.next_wake_at = datetime.now(UTC) - timedelta(seconds=1)
    write_run_control_state(project_path, state)

    second_wake = run_host.RunHost()._drive(project_path)
    assert second_wake is not None
    assert calls == 6

    state = read_run_control_state(project_path)
    assert state.provider_wait is not None
    state.provider_wait.next_wake_at = datetime.now(UTC) - timedelta(seconds=1)
    write_run_control_state(project_path, state)

    assert run_host.RunHost()._drive(project_path) is None
    assert calls == 8
    assert wait_attempts == [1, 1]
    assert read_run_control_state(project_path).desired_state == "stopped"
    assert read_project_metadata(project_path).active_chapter_id == "chapter-002"
    completed = capture_harness_invariants(
        project_path,
        include_readiness=False,
    )
    assert_committed_state_unchanged(baseline, completed)
    assert completed.human_gate_count == baseline.human_gate_count
    kinds = [event.kind for event in read_events(project_path)]
    assert kinds.count("run_waiting_for_provider") == 2
    assert "chapter_patch_evidence_repair_completed" in kinds
    assert kinds.count("chapter_completed") == 2
    checkpoints = sorted(
        (project_path / "book" / "harness" / "checkpoints").glob("*.json")
    )
    assert len(checkpoints) == 8
    statuses = [read_json(path)["status"] for path in checkpoints]
    assert statuses.count("waiting") == 2
    assert statuses.count("completed") == 6
