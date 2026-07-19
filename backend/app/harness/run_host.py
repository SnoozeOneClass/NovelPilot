from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Condition, Thread

from app.harness.flow_router import RunFacts, route_run
from app.harness.orchestrator import HarnessOrchestrator, HarnessRunContext
from app.harness.run_control import begin_active_runner, end_active_runner
from app.schemas.events import HarnessEvent
from app.schemas.runs import HarnessCheckpoint
from app.storage.events import append_event, read_events
from app.storage.projects import (
    benchmark_source_is_generation_terminal,
    list_projects,
    read_project_metadata,
    write_project_metadata,
)
from app.storage.run_state import (
    action_key_for_project,
    begin_harness_checkpoint,
    checkpoint_candidate_identity,
    claim_run_dispatch,
    clear_provider_wait,
    finish_harness_checkpoint,
    read_run_control_state,
    set_run_intent,
)


class RunHost:
    """One local process owns autonomous forward progress for durable harness runs."""

    def __init__(self) -> None:
        self._condition = Condition()
        self._pending: set[Path] = set()
        self._delayed: dict[Path, datetime] = {}
        self._thread: Thread | None = None
        self._stopping = False

    @property
    def started(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def start(self) -> None:
        with self._condition:
            if self.started:
                return
            self._stopping = False
            self._thread = Thread(
                target=self._run,
                name="novelpilot-run-host",
                daemon=True,
            )
            self._thread.start()
        self.reconcile()

    def stop(self, timeout: float = 10.0) -> None:
        with self._condition:
            self._stopping = True
            self._condition.notify_all()
            thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
        with self._condition:
            if thread is not None and not thread.is_alive() and self._thread is thread:
                self._thread = None

    def wake(self, project_path: Path) -> None:
        resolved = project_path.resolve()
        with self._condition:
            self._delayed.pop(resolved, None)
            self._pending.add(resolved)
            self._condition.notify_all()

    def reconcile(self) -> None:
        for project in list_projects():
            project_path = Path(project.path)
            if benchmark_source_is_generation_terminal(project_path, project.metadata):
                set_run_intent(
                    project_path,
                    desired_state="stopped",
                    clear_provider_wait=True,
                )
                if project.metadata.run_status != "paused":
                    project.metadata.run_status = "paused"
                    write_project_metadata(project_path, project.metadata)
                continue
            try:
                state = read_run_control_state(project_path)
            except (OSError, ValueError):
                continue
            if state.desired_state == "running":
                self.wake(project_path)

    def _run(self) -> None:
        while True:
            project_path = self._next_project()
            if project_path is None:
                return
            try:
                next_wake = self._drive(project_path)
            except Exception as exc:  # defensive host boundary
                self._record_host_failure(project_path, exc)
                next_wake = None
            if next_wake is not None:
                with self._condition:
                    self._delayed[project_path] = next_wake

    def _next_project(self) -> Path | None:
        with self._condition:
            while True:
                if self._stopping:
                    return None
                now = datetime.now(UTC)
                due = [path for path, wake_at in self._delayed.items() if wake_at <= now]
                for path in due:
                    self._delayed.pop(path, None)
                    self._pending.add(path)
                if self._pending:
                    return self._pending.pop()
                timeout = None
                if self._delayed:
                    timeout = max(
                        0.05,
                        min(
                            (wake_at - now).total_seconds()
                            for wake_at in self._delayed.values()
                        ),
                    )
                self._condition.wait(timeout=timeout)

    def _drive(self, project_path: Path) -> datetime | None:
        if not begin_active_runner(project_path):
            return datetime.now(UTC) + timedelta(milliseconds=500)
        try:
            while True:
                metadata = read_project_metadata(project_path)
                if benchmark_source_is_generation_terminal(project_path, metadata):
                    set_run_intent(
                        project_path,
                        desired_state="stopped",
                        clear_provider_wait=True,
                    )
                    if metadata.run_status != "paused":
                        metadata.run_status = "paused"
                        write_project_metadata(project_path, metadata)
                    return None
                state = read_run_control_state(project_path)
                provider_wait = state.provider_wait
                provider_due = (
                    provider_wait is None
                    or provider_wait.next_wake_at <= datetime.now(UTC)
                )
                decision = route_run(
                    RunFacts(
                        desired_state=state.desired_state,
                        project_status=metadata.run_status,
                        provider_retry_due=provider_due,
                    )
                )
                if decision == "wait_provider":
                    return provider_wait.next_wake_at if provider_wait is not None else None
                if decision in {"wait_user", "stop", "fail"}:
                    return None

                run_id = state.run_id or f"recovered-{metadata.project_id}"
                action_key = action_key_for_project(project_path)
                dispatch, newly_claimed = claim_run_dispatch(
                    project_path,
                    run_id=run_id,
                    action_key=action_key,
                )
                if newly_claimed and dispatch is not None:
                    append_event(
                        project_path,
                        HarnessEvent(
                            project_id=metadata.project_id,
                            run_id=run_id,
                            kind="run_dispatch_claimed",
                            loop_layer="system",
                            atomic_action=action_key,
                            status="started",
                            routing_decision="advance",
                            message="RunHost claimed the accepted run action.",
                            payload={
                                "dispatch_id": dispatch.dispatch_id,
                                "dispatch_status": dispatch.status,
                                "action_key": dispatch.action_key,
                            },
                        ),
                    )
                metadata.run_status = "running"
                write_project_metadata(project_path, metadata)

                checkpoint, checkpoint_path = begin_harness_checkpoint(
                    project_path,
                    run_id=run_id,
                    action_key=action_key,
                )
                try:
                    HarnessOrchestrator(
                        HarnessRunContext(project_path=project_path, run_id=run_id)
                    ).advance_to_next_checkpoint()
                except Exception as exc:
                    self._finish_failed_action_checkpoint(
                        project_path,
                        run_id=run_id,
                        action_key=action_key,
                        checkpoint=checkpoint,
                        checkpoint_path=checkpoint_path,
                        exc=exc,
                    )
                    return None
                metadata_after = read_project_metadata(project_path)
                state_after = read_run_control_state(project_path)
                if state_after.desired_state == "stopped" and metadata_after.run_status in {
                    "running",
                    "pause_requested",
                    "waiting_for_provider",
                }:
                    state_after = clear_provider_wait(project_path)
                    metadata_after.run_status = "paused"
                    write_project_metadata(project_path, metadata_after)
                if (
                    metadata_after.run_status != "waiting_for_provider"
                    and state_after.provider_wait is not None
                ):
                    state_after = clear_provider_wait(
                        project_path,
                        expected_action_key=state_after.provider_wait.action_key,
                    )
                events_after = read_events(project_path)
                event_sequence_after = events_after[-1].seq or 0 if events_after else 0
                candidate_run_id, candidate_revision = checkpoint_candidate_identity(
                    project_path
                )
                if event_sequence_after <= checkpoint.event_sequence_before:
                    message = (
                        "Harness action made no durable progress; RunHost stopped it to "
                        "prevent a busy loop."
                    )
                    metadata_after.run_status = "failed"
                    write_project_metadata(project_path, metadata_after)
                    set_run_intent(project_path, desired_state="stopped")
                    append_event(
                        project_path,
                        HarnessEvent(
                            project_id=metadata_after.project_id,
                            run_id=run_id,
                            kind="run_progress_guard_failed",
                            loop_layer="system",
                            atomic_action=action_key,
                            status="failed",
                            routing_decision="stop",
                            message=message,
                        ),
                    )
                    events_after = read_events(project_path)
                    event_sequence_after = events_after[-1].seq or 0
                    finish_harness_checkpoint(
                        checkpoint_path,
                        checkpoint,
                        project_status="failed",
                        event_sequence=event_sequence_after,
                        status="failed",
                        failure=message,
                        candidate_run_id=candidate_run_id,
                        candidate_revision=candidate_revision,
                    )
                    return None
                checkpoint_status = {
                    "failed": "failed",
                    "waiting_for_provider": "waiting",
                    "waiting_for_user": "waiting",
                    "paused": "waiting",
                    "pause_requested": "waiting",
                }.get(metadata_after.run_status, "completed")
                finish_harness_checkpoint(
                    checkpoint_path,
                    checkpoint,
                    project_status=metadata_after.run_status,
                    event_sequence=event_sequence_after,
                    status=checkpoint_status,
                    result_artifacts=(
                        [events_after[-1].artifact_path]
                        if events_after and events_after[-1].artifact_path
                        else []
                    ),
                    failure=(
                        events_after[-1].message
                        if checkpoint_status == "failed" and events_after
                        else None
                    ),
                    candidate_run_id=candidate_run_id,
                    candidate_revision=candidate_revision,
                    provider_wait_attempt=(
                        state_after.provider_wait.attempt
                        if state_after.provider_wait is not None
                        else None
                    ),
                    next_wake_at=(
                        state_after.provider_wait.next_wake_at
                        if state_after.provider_wait is not None
                        else None
                    ),
                )
        finally:
            end_active_runner(project_path)

    def _finish_failed_action_checkpoint(
        self,
        project_path: Path,
        *,
        run_id: str,
        action_key: str,
        checkpoint: HarnessCheckpoint,
        checkpoint_path: Path,
        exc: Exception,
    ) -> None:
        message = (
            "RunHost stopped after an unexpected internal action error "
            f"({type(exc).__name__})."
        )
        metadata = read_project_metadata(project_path)
        metadata.run_status = "failed"
        write_project_metadata(project_path, metadata)
        set_run_intent(project_path, desired_state="stopped")
        append_event(
            project_path,
            HarnessEvent(
                project_id=metadata.project_id,
                run_id=run_id,
                kind="run_host_action_failed",
                loop_layer="system",
                atomic_action=action_key,
                status="failed",
                routing_decision="stop",
                message=message,
            ),
        )
        events = read_events(project_path)
        event_sequence = events[-1].seq or 0 if events else 0
        candidate_run_id, candidate_revision = checkpoint_candidate_identity(project_path)
        finish_harness_checkpoint(
            checkpoint_path,
            checkpoint,
            project_status="failed",
            event_sequence=event_sequence,
            status="failed",
            failure=message,
            candidate_run_id=candidate_run_id,
            candidate_revision=candidate_revision,
        )

    def _record_host_failure(self, project_path: Path, exc: Exception) -> None:
        try:
            metadata = read_project_metadata(project_path)
            metadata.run_status = "failed"
            write_project_metadata(project_path, metadata)
            state = set_run_intent(project_path, desired_state="stopped")
            append_event(
                project_path,
                HarnessEvent(
                    project_id=metadata.project_id,
                    run_id=state.run_id,
                    kind="run_host_failed",
                    loop_layer="system",
                    status="failed",
                    routing_decision="stop",
                    message=(
                        "RunHost stopped after an unexpected internal error "
                        f"({type(exc).__name__})."
                    ),
                ),
            )
        except Exception:
            return


_RUN_HOST = RunHost()


def get_run_host() -> RunHost:
    return _RUN_HOST


def continue_after_user_gate(project_path: Path) -> bool:
    """Wake an already-started continuous run after an allowed human checkpoint."""
    metadata = read_project_metadata(project_path)
    if benchmark_source_is_generation_terminal(project_path, metadata):
        set_run_intent(
            project_path,
            desired_state="stopped",
            clear_provider_wait=True,
        )
        if metadata.run_status != "paused":
            metadata.run_status = "paused"
            write_project_metadata(project_path, metadata)
        return False
    state = read_run_control_state(project_path)
    if state.desired_state != "running":
        return False
    metadata = read_project_metadata(project_path)
    if metadata.run_status == "waiting_for_user":
        metadata.run_status = "idle"
        write_project_metadata(project_path, metadata)
    get_run_host().wake(project_path)
    return True
