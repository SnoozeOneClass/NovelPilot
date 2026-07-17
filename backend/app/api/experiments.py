from pathlib import Path

from fastapi import APIRouter, HTTPException

from app.harness.run_control import (
    active_project_transition_lock,
    begin_active_runner,
    end_active_runner,
)
from app.schemas.experiments import (
    ExperimentFixtureIssue,
    ExperimentFixtureStatus,
    ExperimentFixtureTransition,
    ExperimentRunConfigurationRequest,
    ExperimentRunConfigurationResponse,
)
from app.storage import benchmark_sources
from app.storage.experiment_fixtures import get_fixture_status
from app.storage.experiment_runs import create_run_configuration
from app.storage.projects import get_active_project_path, read_project_metadata
from app.storage.run_state import set_run_intent


router = APIRouter()


def _active_project_or_404() -> Path:
    project_path = get_active_project_path()
    if project_path is None:
        raise HTTPException(status_code=404, detail="No active project.")
    return project_path


@router.get("/fixtures/status", response_model=ExperimentFixtureStatus)
def fixture_status() -> ExperimentFixtureStatus:
    project_path = _active_project_or_404()
    metadata = read_project_metadata(project_path)
    if metadata.project_kind != "benchmark_mother":
        return ExperimentFixtureStatus(
            project_kind="novel",
            eligible=False,
            issues=[
                ExperimentFixtureIssue(
                    code="not_benchmark_mother",
                    message="当前项目不是在新建时声明的实验母本项目。",
                )
            ],
        )
    try:
        status = get_fixture_status(project_path)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    lifecycle = status.lifecycle
    if (
        lifecycle is not None
        and lifecycle.status == "frozen"
        and status.existing_fixture is None
    ):
        return status.model_copy(
            update={
                "eligible": False,
                "issues": [
                    ExperimentFixtureIssue(
                        code="frozen_fixture_missing",
                        message="已登记的冻结母本缺失或完整性校验失败。",
                    )
                ],
            }
        )
    return status


@router.post("/fixtures", response_model=ExperimentFixtureTransition)
def freeze_fixture() -> ExperimentFixtureTransition:
    with active_project_transition_lock():
        project_path = _active_project_or_404()
        if not begin_active_runner(project_path):
            raise HTTPException(
                status_code=409,
                detail="当前项目仍有活动中的 Harness 请求，暂时不能冻结。",
            )

    try:
        metadata = read_project_metadata(project_path)
        if metadata.project_kind != "benchmark_mother":
            raise HTTPException(
                status_code=409,
                detail="普通小说项目不能转换为实验母本。",
            )
        lifecycle = metadata.benchmark_fixture
        if lifecycle is not None and lifecycle.status == "frozen":
            try:
                return benchmark_sources.frozen_transition(project_path)
            except benchmark_sources.BenchmarkCheckpointError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
        try:
            benchmark_sources.validate_approved_arc2_checkpoint(project_path)
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        set_run_intent(
            project_path,
            desired_state="stopped",
            clear_provider_wait=True,
        )
        return benchmark_sources.publish_approved_benchmark_fixture(project_path)
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
