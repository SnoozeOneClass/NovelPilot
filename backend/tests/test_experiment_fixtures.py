from pathlib import Path

import pytest
from fastapi import HTTPException

from app.api import experiments as experiment_api
from app.core import config
from app.harness.run_control import begin_active_runner, end_active_runner
from app.schemas.projects import ProjectMetadata
from app.schemas.setup import SetupStateDocument
from app.storage import experiment_fixtures
from app.storage.events import read_events
from app.storage.experiment_fixtures import (
    ExperimentFixtureIneligibleError,
    ExperimentFixtureIntegrityError,
    create_fixture,
    get_fixture_status,
    verify_fixture,
)
from app.storage.json_files import read_json, write_json


def test_fixture_status_requires_the_approved_pre_chapter_checkpoint(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_path = _eligible_project(tmp_path, monkeypatch)

    metadata = read_json(project_path / "project.json")
    metadata["active_chapter_id"] = "chapter-003"
    write_json(project_path / "project.json", metadata)
    current_arc = read_json(project_path / "arcs" / "arc-002" / "state.json")
    current_arc["human_review"] = "awaiting_review"
    write_json(project_path / "arcs" / "arc-002" / "state.json", current_arc)

    status = get_fixture_status(project_path)

    assert status.eligible is False
    assert {issue.code for issue in status.issues} >= {
        "active_chapter",
        "current_arc_not_approved",
    }


def test_fixture_status_requires_a_completed_warmup_arc(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_path = _eligible_project(tmp_path, monkeypatch)
    first_arc = read_json(project_path / "arcs" / "arc-001" / "state.json")
    first_arc["status"] = "in_progress"
    first_arc["completed_at"] = None
    write_json(project_path / "arcs" / "arc-001" / "state.json", first_arc)

    status = get_fixture_status(project_path)

    assert status.eligible is False
    assert "missing_warmup_arc" in {issue.code for issue in status.issues}


def test_fixture_status_does_not_treat_a_later_completed_arc_as_warmup(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_path = _eligible_project(tmp_path, monkeypatch)
    first_arc_path = project_path / "arcs" / "arc-001"
    later_arc_path = project_path / "arcs" / "arc-003"
    first_arc_path.rename(later_arc_path)
    later_arc = read_json(later_arc_path / "state.json")
    later_arc["arc_id"] = "arc-003"
    later_arc["plan_path"] = "arcs/arc-003/plan.md"
    write_json(later_arc_path / "state.json", later_arc)

    status = get_fixture_status(project_path)

    assert status.eligible is False
    assert "missing_warmup_arc" in {issue.code for issue in status.issues}


def test_create_fixture_copies_only_committed_allowlist_and_is_immutable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_path = _eligible_project(tmp_path, monkeypatch)

    created = create_fixture(project_path)
    fixture_path = config.OUTPUT_DIR / created.fixture.relative_path
    manifest = verify_fixture(fixture_path)
    published_paths = {entry.path for entry in manifest.files}

    assert created.created is True
    assert "snapshot/book/direction.md" in published_paths
    assert "snapshot/arcs/arc-002/plan.md" in published_paths
    assert "snapshot/chapters/chapter-001/final.md" in published_paths
    assert "snapshot/chapters/chapter-001/committed_state_patch.json" in published_paths
    assert "snapshot/chapters/chapter-001/draft.md" not in published_paths
    assert "snapshot/project.json" not in published_paths
    assert "snapshot/events.jsonl" not in published_paths
    assert "snapshot/book/setup.json" not in published_paths
    assert not any("attempts" in path for path in published_paths)

    direct_prompt = (fixture_path / "direct_prompt.md").read_text(encoding="utf-8")
    assert direct_prompt.index("## 已批准全书方向") < direct_prompt.index("## arc-002 计划")
    assert direct_prompt.index("## arc-002 计划") < direct_prompt.index("## 角色正史")
    assert "第一章已经提交。" in direct_prompt

    frozen_chapter = fixture_path / "snapshot" / "chapters" / "chapter-001" / "final.md"
    (project_path / "chapters" / "chapter-001" / "final.md").write_text(
        "源项目后来发生变化。",
        encoding="utf-8",
    )

    assert frozen_chapter.read_text(encoding="utf-8") == "# 第1章\n\n第一章已经提交。\n"
    verify_fixture(fixture_path)


def test_create_fixture_is_idempotent_for_the_same_checkpoint(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_path = _eligible_project(tmp_path, monkeypatch)

    first = create_fixture(project_path)
    second = create_fixture(project_path)

    assert first.created is True
    assert second.created is False
    assert second.fixture.fixture_id == first.fixture.fixture_id
    assert len(list((config.OUTPUT_DIR / "experiments" / "fixtures").iterdir())) == 1


def test_verify_fixture_rejects_modified_payload(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_path = _eligible_project(tmp_path, monkeypatch)
    created = create_fixture(project_path)
    fixture_path = config.OUTPUT_DIR / created.fixture.relative_path
    (fixture_path / "direct_prompt.md").write_text("tampered", encoding="utf-8")

    with pytest.raises(ExperimentFixtureIntegrityError, match="integrity check failed"):
        verify_fixture(fixture_path)


def test_verify_fixture_rejects_an_unlisted_nested_manifest_file(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_path = _eligible_project(tmp_path, monkeypatch)
    created = create_fixture(project_path)
    fixture_path = config.OUTPUT_DIR / created.fixture.relative_path
    nested_manifest = fixture_path / "snapshot" / "manifest.json"
    nested_manifest.write_text("{}", encoding="utf-8")

    with pytest.raises(ExperimentFixtureIntegrityError, match="file set"):
        verify_fixture(fixture_path)


def test_create_fixture_cleans_staging_after_publication_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_path = _eligible_project(tmp_path, monkeypatch)

    def fail_verification(_fixture_path: Path):
        raise OSError("injected fixture verification failure")

    monkeypatch.setattr(experiment_fixtures, "verify_fixture", fail_verification)

    with pytest.raises(OSError, match="injected fixture verification failure"):
        create_fixture(project_path)

    experiment_root = config.OUTPUT_DIR / "experiments"
    assert not (experiment_root / "fixtures").exists()
    assert not (experiment_root / ".creating").exists()


def test_create_fixture_fails_closed_when_harness_runner_is_active(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_path = _eligible_project(tmp_path, monkeypatch)
    assert begin_active_runner(project_path) is True
    try:
        status = get_fixture_status(project_path)
        with pytest.raises(ExperimentFixtureIneligibleError):
            create_fixture(project_path)
    finally:
        end_active_runner(project_path)

    assert status.eligible is False
    assert "active_runner" in {issue.code for issue in status.issues}


def test_experiment_api_records_one_sanitized_event_for_new_fixture(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_path = _eligible_project(tmp_path, monkeypatch)
    monkeypatch.setattr(experiment_api, "get_active_project_path", lambda: project_path)

    first = experiment_api.freeze_fixture()
    second = experiment_api.freeze_fixture()
    events = [event for event in read_events(project_path) if event.kind == "benchmark_fixture_frozen"]

    assert first.created is True
    assert second.created is False
    assert len(events) == 1
    assert events[0].payload == {
        "fixture_id": first.fixture.fixture_id,
        "checkpoint_fingerprint": first.fixture.checkpoint.checkpoint_fingerprint,
        "fixture_relative_path": first.fixture.relative_path,
        "source_active_arc_id": "arc-002",
    }
    assert "api_key" not in str(events[0].payload)
    assert "base_url" not in str(events[0].payload)


def test_experiment_api_returns_conflict_when_checkpoint_is_ineligible(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_path = _eligible_project(tmp_path, monkeypatch)
    metadata = read_json(project_path / "project.json")
    metadata["active_chapter_id"] = "chapter-003"
    write_json(project_path / "project.json", metadata)
    monkeypatch.setattr(experiment_api, "get_active_project_path", lambda: project_path)

    with pytest.raises(HTTPException) as caught:
        experiment_api.freeze_fixture()

    assert caught.value.status_code == 409
    assert "检查点" in caught.value.detail["message"]


def _eligible_project(tmp_path: Path, monkeypatch) -> Path:
    output_dir = tmp_path / "output"
    monkeypatch.setattr(config, "OUTPUT_DIR", output_dir)
    project_path = output_dir / "project-fixture-source"
    for relative in ["book", "arcs/arc-001", "arcs/arc-002", "chapters", "canon"]:
        (project_path / relative).mkdir(parents=True, exist_ok=True)

    metadata = ProjectMetadata(
        project_id="source-project-id",
        title="退潮前的十一分钟",
        operation_mode="participatory",
        active_arc_id="arc-002",
        run_status="idle",
    )
    write_json(project_path / "project.json", metadata.model_dump(mode="json"))
    write_json(
        project_path / "book" / "setup.json",
        SetupStateDocument(
            phase="approved",
            approved=True,
            approved_title="退潮前的十一分钟",
        ).model_dump(mode="json"),
    )
    (project_path / "book" / "direction.md").write_text(
        "# 全书方向\n\n封闭空间群像悬疑。\n",
        encoding="utf-8",
    )
    write_json(project_path / "book" / "constraints.json", {"must_preserve": ["公平线索"]})
    (project_path / "book" / "settings.md").write_text("# 全书设定\n\n孤岛潮汐站。\n", encoding="utf-8")
    (project_path / "book" / "outline.md").write_text("# 滚动契约\n\n只规划当前故事弧。\n", encoding="utf-8")
    write_json(project_path / "book" / "state.json", {"schema_version": 2, "version": 4})

    write_json(
        project_path / "arcs" / "arc-001" / "state.json",
        {
            "arc_id": "arc-001",
            "status": "completed",
            "plan_path": "arcs/arc-001/plan.md",
            "human_review": "approved",
            "approved_at": "2026-07-13T00:00:00Z",
            "recommended_target_chapter_count": 2,
            "target_chapter_count": 2,
            "completed_chapter_ids": ["chapter-001", "chapter-002"],
            "completed_at": "2026-07-13T01:00:00Z",
        },
    )
    (project_path / "arcs" / "arc-001" / "plan.md").write_text(
        "# 第一故事弧\n\n共享预热历史。\n",
        encoding="utf-8",
    )
    write_json(
        project_path / "arcs" / "arc-002" / "state.json",
        {
            "arc_id": "arc-002",
            "status": "approved",
            "plan_path": "arcs/arc-002/plan.md",
            "human_review": "approved",
            "approved_at": "2026-07-13T02:00:00Z",
            "recommended_target_chapter_count": 11,
            "target_chapter_count": 11,
            "completed_chapter_ids": [],
            "completed_at": None,
        },
    )
    (project_path / "arcs" / "arc-002" / "plan.md").write_text(
        "# 第二故事弧\n\n还原十一分钟。\n",
        encoding="utf-8",
    )

    for number in [1, 2]:
        chapter_id = f"chapter-{number:03d}"
        chapter_path = project_path / "chapters" / chapter_id
        chapter_path.mkdir()
        chapter_path.joinpath("final.md").write_text(
            f"# 第{number}章\n\n第{'一' if number == 1 else '二'}章已经提交。\n",
            encoding="utf-8",
        )
        write_json(
            chapter_path / "committed_state_patch.json",
            {"schema_version": 1, "status": "committed", "operations": []},
        )
        chapter_path.joinpath("draft.md").write_text("候选草稿不得复制。", encoding="utf-8")
        (chapter_path / "attempts" / "attempt-001").mkdir(parents=True)
        chapter_path.joinpath("attempts/attempt-001/draft.md").write_text(
            "失败尝试不得复制。",
            encoding="utf-8",
        )

    canon_payloads = {
        "characters": {"protagonist": {"name": "林砚"}},
        "relationships": {"lin-zhou": {"status": "distrust"}},
        "world_facts": {"tide": {"rule": "低潮可进入廊道"}},
        "foreshadowing": {"eleven-minutes": {"status": "open"}},
    }
    for name, items in canon_payloads.items():
        write_json(
            project_path / "canon" / f"{name}.json",
            {"schema_version": 1, "version": 3, "items": items},
        )
    (project_path / "events.jsonl").write_text("", encoding="utf-8")
    return project_path
