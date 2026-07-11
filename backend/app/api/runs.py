import asyncio
from collections.abc import AsyncIterator
from io import BytesIO
from pathlib import Path
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from app.harness.orchestrator import HarnessOrchestrator, HarnessRunContext
from app.harness.run_control import (
    active_project_transition_lock,
    begin_active_runner,
    end_active_runner,
    has_active_runner,
)
from app.schemas.events import HarnessEvent
from app.schemas.runs import RunAdvanceRequest
from app.storage.json_files import write_json
from app.storage.events import append_event, read_events
from app.storage.projects import (
    get_active_project_path,
    project_metadata_lock,
    read_project_metadata,
    write_project_metadata,
)
from app.storage.readiness import build_project_readiness
from app.storage.retries import retry_scope_for_chapter

router = APIRouter()


def _active_project_or_404() -> Path:
    project_path = get_active_project_path()
    if project_path is None:
        raise HTTPException(status_code=404, detail="No active project.")
    return project_path


@router.get("/archive")
def download_run_archive() -> Response:
    project_path = _active_project_or_404()
    payload = _build_run_archive(project_path)
    return Response(
        content=payload,
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="novelpilot-run.zip"'},
    )


def _build_run_archive(project_path: Path) -> bytes:
    buffer = BytesIO()
    with ZipFile(buffer, mode="w", compression=ZIP_DEFLATED) as archive:
        for path in sorted(project_path.rglob("*")):
            if not path.is_file() or path.name.endswith(".tmp"):
                continue
            archive.write(path, path.relative_to(project_path).as_posix())
    return buffer.getvalue()


@router.post("/start")
def start_run(run_request: RunAdvanceRequest | None = None) -> dict[str, str]:
    advance_request = run_request or RunAdvanceRequest()
    project_path = _begin_active_runner_or_409()
    run_id = str(uuid4())
    try:
        metadata = read_project_metadata(project_path)
        _ensure_run_can_start(metadata.run_status)
        _ensure_run_can_start_new(project_path, metadata.run_status)
        _ensure_project_is_ready_to_run(project_path)
        with project_metadata_lock(project_path):
            metadata = read_project_metadata(project_path)
            _ensure_run_can_start(metadata.run_status)
            _ensure_run_can_start_new(project_path, metadata.run_status)
            metadata.run_status = "running"
            write_project_metadata(project_path, metadata)
        append_event(
            project_path,
            HarnessEvent(
                project_id=metadata.project_id,
                run_id=run_id,
                kind="run_started",
                loop_layer="system",
                status="started",
                message="Harness run started.",
            ),
        )
        _advance_run_until_stop(project_path, run_id, advance_request)
    finally:
        end_active_runner(project_path)

    metadata = read_project_metadata(project_path)
    return {"run_id": run_id, "status": metadata.run_status}


@router.post("/pause")
def pause_run() -> dict[str, str]:
    project_path = _active_project_or_404()
    metadata = read_project_metadata(project_path)
    if metadata.run_status != "running":
        append_event(
            project_path,
            HarnessEvent(
                project_id=metadata.project_id,
                kind="pause_ignored",
                loop_layer="system",
                atomic_action="pause_run",
                status="completed",
                routing_decision="none",
                message=(
                    "Pause request ignored because no harness run is currently running."
                ),
                payload={"run_status": metadata.run_status},
            ),
        )
        return {"status": metadata.run_status}

    metadata.run_status = "pause_requested"
    write_project_metadata(project_path, metadata)
    append_event(
        project_path,
        HarnessEvent(
            project_id=metadata.project_id,
            kind="pause_requested",
            loop_layer="system",
            status="requested",
            message="Pause requested; it will apply at the next safe checkpoint.",
        ),
    )
    return {"status": metadata.run_status}


@router.post("/resume")
def resume_run(run_request: RunAdvanceRequest | None = None) -> dict[str, str]:
    advance_request = run_request or RunAdvanceRequest()
    project_path = _begin_active_runner_or_409()
    run_id = str(uuid4())
    try:
        metadata = read_project_metadata(project_path)
        _ensure_run_can_start(metadata.run_status)
        _ensure_project_is_ready_to_run(project_path)
        with project_metadata_lock(project_path):
            metadata = read_project_metadata(project_path)
            _ensure_run_can_start(metadata.run_status)
            metadata.run_status = "running"
            write_project_metadata(project_path, metadata)
        append_event(
            project_path,
            HarnessEvent(
                project_id=metadata.project_id,
                run_id=run_id,
                kind="run_resumed",
                loop_layer="system",
                status="started",
                message="Harness run resumed from committed state.",
            ),
        )
        _advance_run_until_stop(project_path, run_id, advance_request)
    finally:
        end_active_runner(project_path)
    metadata = read_project_metadata(project_path)
    return {"status": metadata.run_status}


@router.post("/recover-stale")
def recover_stale_run() -> dict[str, str]:
    project_path = _begin_active_runner_or_409(
        detail=(
            "A harness runner is still active; request pause and wait for a safe checkpoint."
        )
    )
    try:
        with project_metadata_lock(project_path):
            metadata = read_project_metadata(project_path)
            if metadata.run_status not in {"running", "pause_requested"}:
                append_event(
                    project_path,
                    HarnessEvent(
                        project_id=metadata.project_id,
                        kind="run_recovery_ignored",
                        loop_layer="system",
                        atomic_action="recover_stale_run",
                        status="completed",
                        routing_decision="none",
                        message="Stale run recovery ignored because no run lock is present.",
                        payload={"run_status": metadata.run_status},
                    ),
                )
                return {
                    "status": metadata.run_status,
                    "previous_status": metadata.run_status,
                }

            previous_status = metadata.run_status
            metadata.run_status = "paused"
            write_project_metadata(project_path, metadata)
            append_event(
                project_path,
                HarnessEvent(
                    project_id=metadata.project_id,
                    kind="run_recovered",
                    loop_layer="system",
                    atomic_action="recover_stale_run",
                    status="completed",
                    routing_decision="pause",
                    message=(
                        "Recovered stale run lock; harness is paused and can resume from "
                        "committed state."
                    ),
                    payload={
                        "previous_status": previous_status,
                        "run_status": metadata.run_status,
                    },
                ),
            )
            return {"status": metadata.run_status, "previous_status": previous_status}
    finally:
        end_active_runner(project_path)


@router.post("/retry-current-chapter")
def retry_current_chapter() -> dict[str, str]:
    project_path = _begin_active_runner_or_409()
    try:
        return _retry_current_chapter(project_path)
    finally:
        end_active_runner(project_path)


def _retry_current_chapter(project_path: Path) -> dict[str, str]:
    with project_metadata_lock(project_path):
        metadata = read_project_metadata(project_path)
        if metadata.run_status in {"running", "pause_requested"}:
            raise HTTPException(status_code=400, detail="A harness run is already in progress.")
        if metadata.active_chapter_id is None:
            raise HTTPException(status_code=400, detail="No active chapter to retry.")

        chapter_id = metadata.active_chapter_id
        chapter_path = project_path / "chapters" / chapter_id
        if not chapter_path.exists():
            raise HTTPException(status_code=400, detail="Active chapter directory is missing.")

        retry_scope, artifact_names = retry_scope_for_chapter(chapter_path)
        if retry_scope is None:
            raise HTTPException(status_code=400, detail="No retryable chapter failure was found.")

        attempt_path = _next_attempt_path(chapter_path)
        attempt_path.mkdir(parents=True, exist_ok=False)
        archived = _archive_retry_artifacts(chapter_path, attempt_path, artifact_names)
        manifest_path = attempt_path / "retry_manifest.json"
        manifest_relative = manifest_path.relative_to(project_path).as_posix()
        write_json(
            manifest_path,
            {
                "schema_version": 1,
                "chapter_id": chapter_id,
                "retry_scope": retry_scope,
                "archived_artifacts": archived,
            },
        )
        metadata.run_status = "idle"
        write_project_metadata(project_path, metadata)
        append_event(
            project_path,
            HarnessEvent(
                project_id=metadata.project_id,
                kind="chapter_retry_prepared",
                loop_layer="chapter",
                atomic_action="prepare_chapter_retry",
                status="completed",
                artifact_path=manifest_relative,
                routing_decision="retry",
                message=f"Prepared retry for {chapter_id}: {retry_scope}.",
                payload={
                    "chapter_id": chapter_id,
                    "retry_scope": retry_scope,
                    "archived_artifacts": archived,
                },
            ),
        )
        return {
            "status": metadata.run_status,
            "retry_scope": retry_scope,
            "artifact_path": manifest_relative,
        }


def _advance_run_until_stop(
    project_path: Path,
    run_id: str,
    advance_request: RunAdvanceRequest,
) -> None:
    orchestrator = HarnessOrchestrator(HarnessRunContext(project_path=project_path, run_id=run_id))
    for step_index in range(advance_request.max_steps):
        orchestrator.advance_to_next_checkpoint()
        metadata = read_project_metadata(project_path)
        events = read_events(project_path)
        last_event = events[-1] if events else None
        if metadata.run_status in {"waiting_for_user", "failed", "pause_requested", "paused"}:
            break
        if last_event and last_event.kind == "state_patch_rejected":
            break
        if (
            advance_request.stop_after_chapter
            and last_event
            and last_event.atomic_action == "chapter_complete"
        ):
            break
        if step_index < advance_request.max_steps - 1:
            metadata.run_status = "running"
            write_project_metadata(project_path, metadata)
    else:
        _emit_step_budget_reached(project_path, run_id, advance_request.max_steps)


def _emit_step_budget_reached(project_path: Path, run_id: str, max_steps: int) -> None:
    metadata = read_project_metadata(project_path)
    append_event(
        project_path,
        HarnessEvent(
            project_id=metadata.project_id,
            run_id=run_id,
            kind="run_step_budget_reached",
            loop_layer="system",
            atomic_action="advance_run_until_stop",
            status="completed",
            routing_decision="continue",
            message=(
                f"Run step budget reached after {max_steps} safe checkpoints; "
                "resume to continue."
            ),
            payload={"max_steps": max_steps},
        ),
    )


def _ensure_run_can_start(run_status: str) -> None:
    if run_status in {"running", "pause_requested"}:
        raise HTTPException(status_code=400, detail="A harness run is already in progress.")


def _ensure_run_can_start_new(project_path: Path, run_status: str) -> None:
    if run_status != "idle" or _has_started_before(project_path):
        raise HTTPException(
            status_code=400,
            detail="Harness run has already started; use resume.",
        )


def _begin_active_runner_or_409(
    *,
    detail: str = "A harness run is already in progress.",
) -> Path:
    with active_project_transition_lock():
        project_path = _active_project_or_404()
        if begin_active_runner(project_path):
            return project_path
        raise HTTPException(status_code=400, detail=detail)


def _ensure_project_is_ready_to_run(project_path: Path) -> None:
    readiness = build_project_readiness(
        project_path,
        active_runner=has_active_runner(project_path),
    )
    blocking_gates = [
        gate
        for gate in readiness.gates
        if gate.required and gate.status != "passed"
    ]
    if not blocking_gates:
        return

    detail = "; ".join(
        f"{gate.id}={gate.status}: {gate.message}" for gate in blocking_gates
    )
    raise HTTPException(status_code=400, detail=f"Run is not ready: {detail}")


def _next_attempt_path(chapter_path: Path) -> Path:
    attempts_path = chapter_path / "attempts"
    attempts_path.mkdir(parents=True, exist_ok=True)
    index = 1
    while True:
        attempt_path = attempts_path / f"attempt-{index:03d}"
        if not attempt_path.exists():
            return attempt_path
        index += 1


def _archive_retry_artifacts(
    chapter_path: Path,
    attempt_path: Path,
    artifact_names: list[str],
) -> list[str]:
    archived: list[str] = []
    for artifact_name in artifact_names:
        source = chapter_path / artifact_name
        if not source.exists():
            continue
        destination = attempt_path / artifact_name
        source.replace(destination)
        archived.append(destination.relative_to(chapter_path).as_posix())
    return archived


def _has_started_before(project_path: Path) -> bool:
    return any(event.kind in {"run_started", "run_resumed"} for event in read_events(project_path))


@router.get("/events")
async def stream_events(request: Request) -> StreamingResponse:
    project_path = _active_project_or_404()
    last_event_id = request.headers.get("last-event-id")

    async def event_generator() -> AsyncIterator[str]:
        sent_event_ids: set[str] = set()
        first_batch = True
        yield "event: stream_ready\ndata: {\"ready\": true}\n\n"
        while not await request.is_disconnected():
            events = read_events(project_path)
            if first_batch:
                events = _events_after_last_event_id(events, last_event_id)
                first_batch = False
            for event in events:
                if event.event_id in sent_event_ids:
                    continue
                sent_event_ids.add(event.event_id)
                yield _format_sse_event(event)
            await asyncio.sleep(0.5)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


def _events_after_last_event_id(
    events: list[HarnessEvent],
    last_event_id: str | None,
) -> list[HarnessEvent]:
    if last_event_id is None:
        return events
    for index, event in enumerate(events):
        if event.event_id == last_event_id:
            return events[index + 1 :]
    return events


def _format_sse_event(event: HarnessEvent) -> str:
    return f"id: {event.event_id}\nevent: harness_event\ndata: {event.model_dump_json()}\n\n"
