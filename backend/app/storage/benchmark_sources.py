from pathlib import Path

from app.schemas.arcs import CurrentArcState
from app.schemas.events import HarnessEvent
from app.schemas.experiments import (
    ExperimentFixtureTransition,
    ExperimentFixtureSummary,
)
from app.schemas.projects import BenchmarkFixtureLifecycle
from app.storage import arcs as arc_storage
from app.storage.events import append_event
from app.storage.experiment_fixtures import (
    ExperimentFixtureIneligibleError,
    create_fixture,
    get_fixture_status,
)
from app.storage.projects import (
    read_project_metadata,
    update_benchmark_fixture_lifecycle,
)
from app.storage.setup import enqueue_pending_setup_event


class BenchmarkCheckpointError(ValueError):
    pass


def validate_pending_arc2_checkpoint(project_path: Path) -> CurrentArcState:
    metadata = read_project_metadata(project_path)
    if metadata.project_kind != "benchmark_mother":
        raise BenchmarkCheckpointError("当前项目不是在新建时声明的实验母本项目。")
    if metadata.active_arc_id != "arc-002":
        raise BenchmarkCheckpointError(
            "实验母本只能在批准第二故事弧时冻结。"
        )
    if metadata.active_chapter_id is not None:
        raise BenchmarkCheckpointError(
            "第二故事弧已经存在活动章节，不能作为母本检查点。"
        )

    current_arc = arc_storage.read_current_arc_state(project_path)
    if current_arc is None or current_arc.arc_id != "arc-002":
        raise BenchmarkCheckpointError("第二故事弧计划状态缺失或与项目不一致。")
    if current_arc.human_review != "awaiting_review":
        raise BenchmarkCheckpointError("第二故事弧计划当前不在等待审批状态。")
    if current_arc.completed_chapter_ids:
        raise BenchmarkCheckpointError("第二故事弧已经开始提交章节，不能再制作母本。")

    first_arc = arc_storage.read_arc_state(project_path, "arc-001")
    if (
        first_arc is None
        or first_arc.status != "completed"
        or first_arc.completed_at is None
        or not first_arc.completed_chapter_ids
    ):
        raise BenchmarkCheckpointError(
            "批准第二故事弧前，第一故事弧必须已经完成并提交章节。"
        )

    status = get_fixture_status(
        project_path,
        ignore_active_runner=True,
        allow_pending_current_arc_review=True,
    )
    if not status.eligible:
        detail = "; ".join(issue.message for issue in status.issues)
        raise BenchmarkCheckpointError(detail or "实验母本检查点尚未就绪。")
    if status.checkpoint is None or status.checkpoint.active_arc_id != "arc-002":
        raise BenchmarkCheckpointError("无法确认实验母本检查点。")
    if "arc-001" not in status.checkpoint.completed_arc_ids:
        raise BenchmarkCheckpointError("实验母本检查点缺少第一故事弧历史。")
    return current_arc


def validate_approved_arc2_checkpoint(project_path: Path) -> CurrentArcState:
    metadata = read_project_metadata(project_path)
    if metadata.project_kind != "benchmark_mother":
        raise BenchmarkCheckpointError("当前项目不是在新建时声明的实验母本项目。")
    if metadata.active_arc_id != "arc-002" or metadata.active_chapter_id is not None:
        raise BenchmarkCheckpointError(
            "母本源项目不在第二故事弧批准后的章前检查点。"
        )
    current_arc = arc_storage.read_current_arc_state(project_path)
    if (
        current_arc is None
        or current_arc.arc_id != "arc-002"
        or current_arc.human_review != "approved"
        or current_arc.completed_chapter_ids
    ):
        raise BenchmarkCheckpointError(
            "第二故事弧必须已经批准，并且尚未提交任何章节。"
        )
    first_arc = arc_storage.read_arc_state(project_path, "arc-001")
    if (
        first_arc is None
        or first_arc.status != "completed"
        or first_arc.completed_at is None
        or not first_arc.completed_chapter_ids
    ):
        raise BenchmarkCheckpointError("母本源项目缺少已完成的第一故事弧历史。")
    return current_arc


def publish_approved_benchmark_fixture(project_path: Path) -> ExperimentFixtureTransition:
    try:
        validate_approved_arc2_checkpoint(project_path)
        response = create_fixture(project_path, ignore_active_runner=True)
        _record_fixture_event(project_path, response.fixture)
        update_benchmark_fixture_lifecycle(
            project_path,
            BenchmarkFixtureLifecycle(
                status="frozen",
                fixture_id=response.fixture.fixture_id,
                checkpoint_fingerprint=(
                    response.fixture.checkpoint.checkpoint_fingerprint
                ),
            ),
        )
        return ExperimentFixtureTransition(
            status="frozen",
            fixture=response.fixture,
        )
    except ExperimentFixtureIneligibleError as exc:
        message = "母本检查点校验失败：" + "；".join(
            issue.message for issue in exc.issues
        )
        return _record_freeze_failure(
            project_path,
            code="fixture_ineligible",
            message=message,
        )
    except OSError:
        return _record_freeze_failure(
            project_path,
            code="fixture_publication_failed",
            message="母本文件发布失败，请在实验室重试。",
        )
    except ValueError:
        return _record_freeze_failure(
            project_path,
            code="fixture_validation_failed",
            message="母本数据校验失败，请在实验室查看状态后重试。",
        )


def frozen_transition(project_path: Path) -> ExperimentFixtureTransition:
    metadata = read_project_metadata(project_path)
    lifecycle = metadata.benchmark_fixture
    if lifecycle is None or lifecycle.status != "frozen":
        raise BenchmarkCheckpointError("实验母本源项目尚未完成冻结。")
    status = get_fixture_status(project_path, ignore_active_runner=True)
    fixture = status.existing_fixture
    if fixture is None or fixture.fixture_id != lifecycle.fixture_id:
        raise BenchmarkCheckpointError(
            "已登记的实验母本缺失或完整性校验失败。"
        )
    return ExperimentFixtureTransition(status="frozen", fixture=fixture)


def failed_transition(project_path: Path) -> ExperimentFixtureTransition:
    metadata = read_project_metadata(project_path)
    lifecycle = metadata.benchmark_fixture
    if lifecycle is None or lifecycle.status != "freeze_failed":
        raise BenchmarkCheckpointError("实验母本当前没有待重试的发布失败。")
    return ExperimentFixtureTransition(
        status="freeze_failed",
        failure_code=lifecycle.failure_code,
        failure_message=lifecycle.failure_message,
    )


def record_story_arc_approved_event(
    project_path: Path,
    arc: CurrentArcState,
) -> None:
    metadata = read_project_metadata(project_path)
    event = HarnessEvent(
        event_id=f"story-arc-approved-{metadata.project_id}-{arc.arc_id}",
        project_id=metadata.project_id,
        kind="story_arc_approved",
        loop_layer="story_arc",
        atomic_action="approve_current_arc",
        status="completed",
        artifact_path=arc.plan_path,
        routing_decision="freeze_benchmark_fixture",
        message=f"{arc.arc_id} approved as the benchmark fixture checkpoint.",
        payload={
            "arc_id": arc.arc_id,
            "recommended_target_chapter_count": (
                arc.recommended_target_chapter_count
            ),
            "target_chapter_count": arc.target_chapter_count,
        },
    )
    try:
        append_event(project_path, event)
    except OSError:
        try:
            enqueue_pending_setup_event(project_path, event)
        except OSError:
            pass


def _record_fixture_event(
    project_path: Path,
    fixture: ExperimentFixtureSummary,
) -> None:
    metadata = read_project_metadata(project_path)
    event = HarnessEvent(
        event_id=f"benchmark-fixture-frozen-{fixture.fixture_id}",
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


def _record_freeze_failure(
    project_path: Path,
    *,
    code: str,
    message: str,
) -> ExperimentFixtureTransition:
    safe_message = message[:1_000]
    update_benchmark_fixture_lifecycle(
        project_path,
        BenchmarkFixtureLifecycle(
            status="freeze_failed",
            failure_code=code,
            failure_message=safe_message,
        ),
    )
    return ExperimentFixtureTransition(
        status="freeze_failed",
        failure_code=code,
        failure_message=safe_message,
    )
