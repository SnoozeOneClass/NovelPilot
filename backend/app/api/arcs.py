from pathlib import Path

from fastapi import APIRouter, HTTPException

from app.schemas.arcs import (
    CurrentArcApprovalRequest,
    CurrentArcApprovalResponse,
    CurrentArcState,
)
from app.schemas.events import HarnessEvent
from app.harness.run_host import continue_after_user_gate
from app.harness.run_control import (
    active_project_transition_lock,
    begin_active_runner,
    end_active_runner,
)
from app.storage import arcs as arc_storage
from app.storage import benchmark_sources
from app.storage.events import append_event
from app.storage.projects import get_active_project_path, read_project_metadata
from app.storage.run_state import (
    read_run_control_state,
    set_run_intent,
    write_run_control_state,
)

router = APIRouter()


def _active_project_or_404() -> Path:
    project_path = get_active_project_path()
    if project_path is None:
        raise HTTPException(status_code=404, detail="No active project.")
    return project_path


@router.get("/current", response_model=CurrentArcState | None)
def get_current_arc() -> CurrentArcState | None:
    project_path = _active_project_or_404()
    return arc_storage.read_current_arc_state(project_path)


@router.post("/current/approve", response_model=CurrentArcApprovalResponse)
def approve_current_arc(
    request: CurrentArcApprovalRequest | None = None,
) -> CurrentArcApprovalResponse:
    project_path = _active_project_or_404()
    metadata = read_project_metadata(project_path)
    if (
        metadata.project_kind == "benchmark_mother"
        and metadata.active_arc_id == "arc-002"
    ):
        return _approve_benchmark_current_arc(project_path, request)
    return _approve_ordinary_current_arc(project_path, request)


def _approve_ordinary_current_arc(
    project_path: Path,
    request: CurrentArcApprovalRequest | None,
) -> CurrentArcApprovalResponse:
    try:
        arc = arc_storage.approve_current_arc(
            project_path,
            request.target_chapter_count if request is not None else None,
        )
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    metadata = read_project_metadata(project_path)
    append_event(
        project_path,
        HarnessEvent(
            project_id=metadata.project_id,
            kind="story_arc_approved",
            loop_layer="story_arc",
            atomic_action="approve_current_arc",
            status="completed",
            artifact_path=arc.plan_path,
            routing_decision="continue",
            message=f"{arc.arc_id} approved for chapter writing.",
            payload={
                "arc_id": arc.arc_id,
                "recommended_target_chapter_count": (
                    arc.recommended_target_chapter_count
                ),
                "target_chapter_count": arc.target_chapter_count,
            },
        ),
    )
    continue_after_user_gate(project_path)
    return CurrentArcApprovalResponse(arc=arc, run_status=metadata.run_status)


def _approve_benchmark_current_arc(
    expected_project_path: Path,
    request: CurrentArcApprovalRequest | None,
) -> CurrentArcApprovalResponse:
    with active_project_transition_lock():
        project_path = _active_project_or_404()
        if project_path.resolve() != expected_project_path.resolve():
            raise HTTPException(status_code=409, detail="The active project changed.")
        if not begin_active_runner(project_path):
            raise HTTPException(
                status_code=409,
                detail="当前项目仍有活动中的 Harness 请求，请等待安全检查点。",
            )

    try:
        metadata = read_project_metadata(project_path)
        current_arc = arc_storage.read_current_arc_state(project_path)
        lifecycle = metadata.benchmark_fixture
        if current_arc is not None and current_arc.human_review == "approved":
            try:
                if lifecycle is not None and lifecycle.status == "frozen":
                    transition = benchmark_sources.frozen_transition(project_path)
                elif lifecycle is not None and lifecycle.status == "freeze_failed":
                    transition = benchmark_sources.failed_transition(project_path)
                else:
                    raise HTTPException(
                        status_code=409,
                        detail="故事弧已经批准，请前往实验室重试母本冻结。",
                    )
            except benchmark_sources.BenchmarkCheckpointError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            return CurrentArcApprovalResponse(
                arc=current_arc,
                run_status=metadata.run_status,
                fixture_transition=transition,
            )

        try:
            benchmark_sources.validate_pending_arc2_checkpoint(project_path)
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        previous_control = read_run_control_state(project_path)
        set_run_intent(
            project_path,
            desired_state="stopped",
            clear_provider_wait=True,
        )
        try:
            arc = arc_storage.approve_current_arc(
                project_path,
                request.target_chapter_count if request is not None else None,
            )
        except (FileNotFoundError, ValueError) as exc:
            write_run_control_state(project_path, previous_control)
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        benchmark_sources.record_story_arc_approved_event(project_path, arc)
        transition = benchmark_sources.publish_approved_benchmark_fixture(project_path)
        metadata = read_project_metadata(project_path)
        return CurrentArcApprovalResponse(
            arc=arc,
            run_status=metadata.run_status,
            fixture_transition=transition,
        )
    finally:
        end_active_runner(project_path)
