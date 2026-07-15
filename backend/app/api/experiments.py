from pathlib import Path

from fastapi import APIRouter, HTTPException

from app.harness.run_control import (
    active_project_transition_lock,
    begin_active_runner,
    end_active_runner,
)
from app.schemas.events import HarnessEvent
from app.schemas.experiments import (
    ExperimentFixtureCreateResponse,
    ExperimentFixtureStatus,
    ExperimentRunConfigurationRequest,
    ExperimentRunConfigurationResponse,
)
from app.storage.events import append_event
from app.storage.experiment_fixtures import (
    ExperimentFixtureIneligibleError,
    create_fixture,
    get_fixture_status,
)
from app.storage.experiment_runs import create_run_configuration
from app.storage.projects import get_active_project_path, read_project_metadata
from app.storage.setup import enqueue_pending_setup_event


router = APIRouter()


def _active_project_or_404() -> Path:
    project_path = get_active_project_path()
    if project_path is None:
        raise HTTPException(status_code=404, detail="No active project.")
    return project_path


@router.get("/fixtures/status", response_model=ExperimentFixtureStatus)
def fixture_status() -> ExperimentFixtureStatus:
    project_path = _active_project_or_404()
    try:
        return get_fixture_status(project_path)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/fixtures", response_model=ExperimentFixtureCreateResponse)
def freeze_fixture() -> ExperimentFixtureCreateResponse:
    with active_project_transition_lock():
        project_path = _active_project_or_404()
        if not begin_active_runner(project_path):
            raise HTTPException(
                status_code=409,
                detail="当前项目仍有活动中的 Harness 请求，暂时不能冻结。",
            )

    try:
        try:
            response = create_fixture(project_path, ignore_active_runner=True)
        except ExperimentFixtureIneligibleError as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "当前项目还没有到达可冻结的实验检查点。",
                    "issues": [issue.model_dump(mode="json") for issue in exc.issues],
                },
            ) from exc
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if response.created:
            _record_fixture_event(project_path, response)
        return response
    finally:
        end_active_runner(project_path)


@router.post("/run-configurations", response_model=ExperimentRunConfigurationResponse)
def create_experiment_run_configuration(
    request: ExperimentRunConfigurationRequest,
) -> ExperimentRunConfigurationResponse:
    with active_project_transition_lock():
        project_path = _active_project_or_404()
        if not begin_active_runner(project_path):
            raise HTTPException(
                status_code=409,
                detail="A Harness runner is active; wait before freezing experiment policy.",
            )
    try:
        try:
            return create_run_configuration(project_path, request)
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        end_active_runner(project_path)


def _record_fixture_event(
    project_path: Path,
    response: ExperimentFixtureCreateResponse,
) -> None:
    metadata = read_project_metadata(project_path)
    fixture = response.fixture
    event = HarnessEvent(
        project_id=metadata.project_id,
        kind="benchmark_fixture_frozen",
        loop_layer="system",
        atomic_action="freeze_experiment_fixture",
        status="completed",
        routing_decision="fixture_ready",
        message=f"Experiment fixture {fixture.fixture_id} frozen from committed state.",
        payload={
            "fixture_id": fixture.fixture_id,
            "checkpoint_fingerprint": fixture.checkpoint.checkpoint_fingerprint,
            "fixture_relative_path": fixture.relative_path,
            "source_active_arc_id": fixture.checkpoint.active_arc_id,
        },
    )
    try:
        append_event(project_path, event)
    except OSError:
        enqueue_pending_setup_event(project_path, event)
