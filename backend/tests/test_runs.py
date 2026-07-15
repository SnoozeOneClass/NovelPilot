from io import BytesIO
from zipfile import ZipFile

import pytest
from fastapi import HTTPException

from app.api import runs as runs_api
from app.api.runs import _build_run_archive, _events_after_last_event_id, _format_sse_event
from app.harness.run_control import begin_active_runner, end_active_runner
from app.schemas.events import HarnessEvent
from app.schemas.projects import ProjectMetadata
from app.schemas.runs import RunAdvanceRequest
from app.storage import profiles as profile_storage
from app.storage.events import append_event, read_events
from app.storage.json_files import read_json, write_json


def _event(event_id: str) -> HarnessEvent:
    return HarnessEvent(
        event_id=event_id,
        project_id="project-1",
        kind="artifact_written",
        message=f"event {event_id}",
    )


def test_events_after_last_event_id_replays_only_newer_events() -> None:
    events = [_event("one"), _event("two"), _event("three")]

    replay = _events_after_last_event_id(events, "two")

    assert [event.event_id for event in replay] == ["three"]


def test_events_after_unknown_last_event_id_replays_all_events() -> None:
    events = [_event("one"), _event("two")]

    replay = _events_after_last_event_id(events, "missing")

    assert replay == events


def test_format_sse_event_includes_id_and_named_event() -> None:
    event = _event("one")

    payload = _format_sse_event(event)

    assert payload.startswith("id: one\nevent: harness_event\ndata: ")
    assert '"event_id":"one"' in payload
    assert payload.endswith("\n\n")


def test_build_run_archive_preserves_project_relative_paths(tmp_path) -> None:
    project_path = tmp_path / "novel"
    chapter_path = project_path / "chapters" / "chapter-001"
    chapter_path.mkdir(parents=True)
    (project_path / "events.jsonl").write_text('{"kind":"run_started"}\n', encoding="utf-8")
    (chapter_path / "final.md").write_bytes(b"# Chapter 1\n")
    (chapter_path / "draft.md.tmp").write_text("partial", encoding="utf-8")

    payload = _build_run_archive(project_path)

    with ZipFile(BytesIO(payload)) as archive:
        assert sorted(archive.namelist()) == [
            "chapters/chapter-001/final.md",
            "events.jsonl",
        ]
        assert archive.read("chapters/chapter-001/final.md").decode("utf-8") == "# Chapter 1\n"


@pytest.mark.parametrize("run_status", ["running", "pause_requested"])
@pytest.mark.parametrize("command", [runs_api.start_run, runs_api.resume_run])
def test_run_commands_reject_concurrent_run(
    tmp_path,
    monkeypatch,
    run_status: str,
    command,
) -> None:
    project_path = tmp_path / "novel"
    project_path.mkdir()
    write_json(
        project_path / "project.json",
        ProjectMetadata(title="Novel", run_status=run_status).model_dump(mode="json"),
    )
    monkeypatch.setattr(runs_api, "get_active_project_path", lambda: project_path)

    with pytest.raises(HTTPException) as exc:
        command()

    assert exc.value.status_code == 400
    assert exc.value.detail == "A harness run is already in progress."


@pytest.mark.parametrize("command", [runs_api.start_run, runs_api.resume_run])
def test_run_commands_reject_when_readiness_required_gates_are_pending(
    tmp_path,
    monkeypatch,
    command,
) -> None:
    project_path = tmp_path / "novel"
    (project_path / "book").mkdir(parents=True)
    write_json(
        project_path / "project.json",
        ProjectMetadata(title="Novel", run_status="idle").model_dump(mode="json"),
    )
    (project_path / "events.jsonl").write_text("", encoding="utf-8")
    monkeypatch.setattr(runs_api, "get_active_project_path", lambda: project_path)
    monkeypatch.setattr(
        profile_storage,
        "LLM_PROFILES_PATH",
        tmp_path / "config" / "llm-profiles.local.json",
    )

    with pytest.raises(HTTPException) as exc:
        command()

    metadata = read_json(project_path / "project.json")
    events = read_events(project_path)

    assert exc.value.status_code == 400
    assert "Run is not ready:" in str(exc.value.detail)
    assert "book_setup=pending" in str(exc.value.detail)
    assert "active_llm_profile=pending" in str(exc.value.detail)
    assert metadata["run_status"] == "idle"
    assert events == []


@pytest.mark.parametrize("run_status", ["idle", "paused", "waiting_for_user", "failed"])
def test_start_run_rejects_existing_run_history(
    tmp_path,
    monkeypatch,
    run_status: str,
) -> None:
    project_path = tmp_path / "novel"
    project_path.mkdir()
    metadata = ProjectMetadata(title="Novel", run_status=run_status)
    write_json(project_path / "project.json", metadata.model_dump(mode="json"))
    append_event(
        project_path,
        HarnessEvent(
            project_id=metadata.project_id,
            kind="run_started",
            loop_layer="system",
            status="started",
            message="Harness run started.",
        ),
    )
    monkeypatch.setattr(runs_api, "get_active_project_path", lambda: project_path)
    monkeypatch.setattr(runs_api, "_ensure_project_is_ready_to_run", lambda _path: None)

    with pytest.raises(HTTPException) as exc:
        runs_api.start_run()

    stored = read_json(project_path / "project.json")
    events = read_events(project_path)

    assert exc.value.status_code == 400
    assert exc.value.detail == "Harness run has already started; use resume."
    assert stored["run_status"] == run_status
    assert [event.kind for event in events] == ["run_started"]


def test_start_run_rejects_non_idle_status_without_history(tmp_path, monkeypatch) -> None:
    project_path = tmp_path / "novel"
    project_path.mkdir()
    write_json(
        project_path / "project.json",
        ProjectMetadata(title="Novel", run_status="paused").model_dump(mode="json"),
    )
    monkeypatch.setattr(runs_api, "get_active_project_path", lambda: project_path)
    monkeypatch.setattr(runs_api, "_ensure_project_is_ready_to_run", lambda _path: None)

    with pytest.raises(HTTPException) as exc:
        runs_api.start_run()

    stored = read_json(project_path / "project.json")
    events = read_events(project_path)

    assert exc.value.status_code == 400
    assert exc.value.detail == "Harness run has already started; use resume."
    assert stored["run_status"] == "paused"
    assert events == []


def test_pause_run_requests_pause_only_when_running(tmp_path, monkeypatch) -> None:
    project_path = tmp_path / "novel"
    project_path.mkdir()
    write_json(
        project_path / "project.json",
        ProjectMetadata(title="Novel", run_status="running").model_dump(mode="json"),
    )
    monkeypatch.setattr(runs_api, "get_active_project_path", lambda: project_path)

    result = runs_api.pause_run()

    metadata = read_json(project_path / "project.json")
    events = read_events(project_path)

    assert result["status"] == "pause_requested"
    assert metadata["run_status"] == "pause_requested"
    assert events[-1].kind == "pause_requested"


def test_pause_run_does_not_strand_idle_project(tmp_path, monkeypatch) -> None:
    project_path = tmp_path / "novel"
    project_path.mkdir()
    write_json(
        project_path / "project.json",
        ProjectMetadata(title="Novel", run_status="idle").model_dump(mode="json"),
    )
    monkeypatch.setattr(runs_api, "get_active_project_path", lambda: project_path)

    result = runs_api.pause_run()

    metadata = read_json(project_path / "project.json")
    events = read_events(project_path)

    assert result["status"] == "idle"
    assert metadata["run_status"] == "idle"
    assert events[-1].kind == "pause_ignored"
    assert events[-1].payload == {"run_status": "idle"}


@pytest.mark.parametrize("run_status", ["running", "pause_requested"])
def test_recover_stale_run_pauses_abandoned_run_lock(
    tmp_path,
    monkeypatch,
    run_status: str,
) -> None:
    project_path = tmp_path / "novel"
    project_path.mkdir()
    write_json(
        project_path / "project.json",
        ProjectMetadata(title="Novel", run_status=run_status).model_dump(mode="json"),
    )
    monkeypatch.setattr(runs_api, "get_active_project_path", lambda: project_path)

    result = runs_api.recover_stale_run()

    metadata = read_json(project_path / "project.json")
    events = read_events(project_path)

    assert result == {"status": "paused", "previous_status": run_status}
    assert metadata["run_status"] == "paused"
    assert events[-1].kind == "run_recovered"
    assert events[-1].atomic_action == "recover_stale_run"
    assert events[-1].routing_decision == "pause"
    assert events[-1].payload == {"previous_status": run_status, "run_status": "paused"}


def test_recover_stale_run_rejects_active_runner(tmp_path, monkeypatch) -> None:
    project_path = tmp_path / "novel"
    project_path.mkdir()
    write_json(
        project_path / "project.json",
        ProjectMetadata(title="Novel", run_status="running").model_dump(mode="json"),
    )
    monkeypatch.setattr(runs_api, "get_active_project_path", lambda: project_path)

    assert begin_active_runner(project_path) is True
    try:
        with pytest.raises(HTTPException) as exc:
            runs_api.recover_stale_run()
    finally:
        end_active_runner(project_path)

    metadata = read_json(project_path / "project.json")
    assert exc.value.status_code == 400
    assert "still active" in str(exc.value.detail)
    assert metadata["run_status"] == "running"


def test_recover_stale_run_ignores_non_locked_project(tmp_path, monkeypatch) -> None:
    project_path = tmp_path / "novel"
    project_path.mkdir()
    write_json(
        project_path / "project.json",
        ProjectMetadata(title="Novel", run_status="idle").model_dump(mode="json"),
    )
    monkeypatch.setattr(runs_api, "get_active_project_path", lambda: project_path)

    result = runs_api.recover_stale_run()

    metadata = read_json(project_path / "project.json")
    events = read_events(project_path)

    assert result == {"status": "idle", "previous_status": "idle"}
    assert metadata["run_status"] == "idle"
    assert events[-1].kind == "run_recovery_ignored"
    assert events[-1].payload == {"run_status": "idle"}


def test_advance_run_continues_after_chapter_checkpoint_by_default(
    tmp_path,
    monkeypatch,
) -> None:
    project_path = tmp_path / "novel"
    project_path.mkdir()
    write_json(
        project_path / "project.json",
        ProjectMetadata(title="Novel", run_status="running").model_dump(mode="json"),
    )
    calls: list[int] = []
    entry_statuses: list[str] = []

    class FakeOrchestrator:
        def __init__(self, context) -> None:
            self.context = context

        def advance_to_next_checkpoint(self) -> None:
            metadata = ProjectMetadata.model_validate(read_json(project_path / "project.json"))
            entry_statuses.append(metadata.run_status)
            calls.append(len(calls) + 1)
            if len(calls) == 1:
                _write_run_status(project_path, "idle")
                _append_test_event(
                    project_path,
                    self.context.run_id,
                    kind="safe_checkpoint_reached",
                    atomic_action="chapter_complete",
                    routing_decision="continue",
                )
                return

            _write_run_status(project_path, "waiting_for_user")
            _append_test_event(
                project_path,
                self.context.run_id,
                kind="story_arc_review_required",
                atomic_action="review_current_arc",
                routing_decision="pause",
            )

    monkeypatch.setattr(runs_api, "HarnessOrchestrator", FakeOrchestrator)

    runs_api._advance_run_until_stop(
        project_path,
        "run-1",
        RunAdvanceRequest(max_steps=5),
    )

    events = read_events(project_path)

    assert calls == [1, 2]
    assert entry_statuses == ["running", "running"]
    assert events[0].atomic_action == "chapter_complete"
    assert events[-1].kind == "story_arc_review_required"


def test_advance_run_can_stop_after_one_chapter_for_smoke_flows(
    tmp_path,
    monkeypatch,
) -> None:
    project_path = tmp_path / "novel"
    project_path.mkdir()
    write_json(
        project_path / "project.json",
        ProjectMetadata(title="Novel", run_status="running").model_dump(mode="json"),
    )
    calls: list[int] = []

    class FakeOrchestrator:
        def __init__(self, context) -> None:
            self.context = context

        def advance_to_next_checkpoint(self) -> None:
            calls.append(len(calls) + 1)
            _write_run_status(project_path, "idle")
            _append_test_event(
                project_path,
                self.context.run_id,
                kind="safe_checkpoint_reached",
                atomic_action="chapter_complete",
                routing_decision="continue",
            )

    monkeypatch.setattr(runs_api, "HarnessOrchestrator", FakeOrchestrator)

    runs_api._advance_run_until_stop(
        project_path,
        "run-1",
        RunAdvanceRequest(stop_after_chapter=True, max_steps=5),
    )

    events = read_events(project_path)
    metadata = read_json(project_path / "project.json")

    assert calls == [1]
    assert events[-1].atomic_action == "chapter_complete"
    assert metadata["run_status"] == "idle"


def test_advance_run_records_step_budget_checkpoint(
    tmp_path,
    monkeypatch,
) -> None:
    project_path = tmp_path / "novel"
    project_path.mkdir()
    write_json(
        project_path / "project.json",
        ProjectMetadata(title="Novel", run_status="running").model_dump(mode="json"),
    )
    calls: list[int] = []
    entry_statuses: list[str] = []

    class FakeOrchestrator:
        def __init__(self, context) -> None:
            self.context = context

        def advance_to_next_checkpoint(self) -> None:
            metadata = ProjectMetadata.model_validate(read_json(project_path / "project.json"))
            entry_statuses.append(metadata.run_status)
            calls.append(len(calls) + 1)
            _write_run_status(project_path, "idle")
            _append_test_event(
                project_path,
                self.context.run_id,
                kind="artifact_written",
                atomic_action=f"step_{len(calls)}",
                routing_decision="continue",
            )

    monkeypatch.setattr(runs_api, "HarnessOrchestrator", FakeOrchestrator)

    runs_api._advance_run_until_stop(
        project_path,
        "run-1",
        RunAdvanceRequest(max_steps=2),
    )

    events = read_events(project_path)
    metadata = read_json(project_path / "project.json")

    assert calls == [1, 2]
    assert entry_statuses == ["running", "running"]
    assert metadata["run_status"] == "idle"
    assert events[-1].kind == "run_step_budget_reached"
    assert events[-1].routing_decision == "continue"
    assert events[-1].payload == {"max_steps": 2}


def test_retry_current_chapter_archives_failed_verification_artifacts(
    tmp_path,
    monkeypatch,
) -> None:
    project_path = tmp_path / "novel"
    chapter_path = project_path / "chapters" / "chapter-001"
    chapter_path.mkdir(parents=True)
    write_json(
        project_path / "project.json",
        ProjectMetadata(
            title="Novel",
            active_chapter_id="chapter-001",
            run_status="waiting_for_user",
        ).model_dump(mode="json"),
    )
    for artifact in ["draft.md", "review.md"]:
        (chapter_path / artifact).write_text(artifact, encoding="utf-8")
    write_json(chapter_path / "observations.json", {"schema_version": 1})
    write_json(
        chapter_path / "verification.json",
        {
            "schema_version": 1,
            "chapter_id": "chapter-001",
            "goal_satisfied": False,
            "commit_allowed": False,
            "routing_decision": "rewrite",
            "signals": [],
            "reasons": ["missed goal"],
        },
    )
    monkeypatch.setattr(runs_api, "get_active_project_path", lambda: project_path)

    result = runs_api.retry_current_chapter()

    manifest = read_json(project_path / result["artifact_path"])
    events = read_events(project_path)

    assert result["retry_scope"] == "chapter_candidate"
    assert result["status"] == "idle"
    assert not (chapter_path / "draft.md").exists()
    assert not (chapter_path / "verification.json").exists()
    assert (chapter_path / "attempts" / "attempt-001" / "draft.md").exists()
    assert manifest["archived_artifacts"] == [
        "attempts/attempt-001/draft.md",
        "attempts/attempt-001/observations.json",
        "attempts/attempt-001/review.md",
        "attempts/attempt-001/verification.json",
    ]
    assert events[-1].kind == "chapter_retry_prepared"
    assert events[-1].routing_decision == "retry"


def test_retry_current_chapter_archives_rejected_state_patch(
    tmp_path,
    monkeypatch,
) -> None:
    project_path = tmp_path / "novel"
    chapter_path = project_path / "chapters" / "chapter-001"
    chapter_path.mkdir(parents=True)
    write_json(
        project_path / "project.json",
        ProjectMetadata(
            title="Novel",
            active_chapter_id="chapter-001",
            run_status="waiting_for_user",
        ).model_dump(mode="json"),
    )
    write_json(chapter_path / "candidate_state_patch.json", {"schema_version": 1})
    write_json(chapter_path / "state_patch_rejection.json", {"schema_version": 1})
    monkeypatch.setattr(runs_api, "get_active_project_path", lambda: project_path)

    result = runs_api.retry_current_chapter()

    manifest = read_json(project_path / result["artifact_path"])

    assert result["retry_scope"] == "state_patch"
    assert (chapter_path / "candidate_state_patch.json").exists()
    assert not (chapter_path / "state_patch_rejection.json").exists()
    assert not (
        chapter_path / "attempts" / "attempt-001" / "candidate_state_patch.json"
    ).exists()
    assert manifest["archived_artifacts"] == [
        "attempts/attempt-001/state_patch_rejection.json"
    ]


def test_retry_current_chapter_archives_patch_generation_rejection(
    tmp_path,
    monkeypatch,
) -> None:
    project_path = tmp_path / "novel"
    chapter_path = project_path / "chapters" / "chapter-001"
    chapter_path.mkdir(parents=True)
    write_json(
        project_path / "project.json",
        ProjectMetadata(
            title="Novel",
            active_chapter_id="chapter-001",
            run_status="waiting_for_user",
        ).model_dump(mode="json"),
    )
    write_json(
        chapter_path / "state_patch_rejection.json",
        {
            "schema": "failed",
            "versions": "passed",
            "evidence": "passed",
            "conflicts": "passed",
            "reasons": ["State patch generator output could not be parsed as JSON."],
        },
    )
    monkeypatch.setattr(runs_api, "get_active_project_path", lambda: project_path)

    result = runs_api.retry_current_chapter()

    manifest = read_json(project_path / result["artifact_path"])

    assert result["retry_scope"] == "state_patch"
    assert not (chapter_path / "state_patch_rejection.json").exists()
    assert (chapter_path / "attempts" / "attempt-001" / "state_patch_rejection.json").exists()
    assert manifest["archived_artifacts"] == [
        "attempts/attempt-001/state_patch_rejection.json"
    ]


def _write_run_status(project_path, run_status: str) -> None:
    metadata = ProjectMetadata.model_validate(read_json(project_path / "project.json"))
    metadata.run_status = run_status
    write_json(project_path / "project.json", metadata.model_dump(mode="json"))


def _append_test_event(
    project_path,
    run_id: str,
    *,
    kind: str,
    atomic_action: str,
    routing_decision: str,
) -> None:
    metadata = ProjectMetadata.model_validate(read_json(project_path / "project.json"))
    append_event(
        project_path,
        HarnessEvent(
            project_id=metadata.project_id,
            run_id=run_id,
            kind=kind,
            atomic_action=atomic_action,
            routing_decision=routing_decision,
            message=f"{kind}: {atomic_action}",
        ),
    )
