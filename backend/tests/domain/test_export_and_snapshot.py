from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from alembic import command

from app.db.engine import create_sqlite_async_engine
from app.db.maintenance import alembic_config
from app.domain.chapter.commands import ChapterCommandService
from app.domain.chapter.contracts import CommitChapterRequest
from app.domain.export import ManuscriptExportService
from app.domain.snapshots import SnapshotQueryService
from app.store.command_bus import CommandBus
from tests.domain.test_chapter_lifecycle import _prepare_reviewed_chapter


def test_snapshot_and_export_are_bound_to_current_approved_baselines(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "export.sqlite3"
    export_root = tmp_path / "exports"
    command.upgrade(alembic_config(database), "head")

    async def exercise() -> None:
        engine = create_sqlite_async_engine(database)
        try:
            ready = await _prepare_reviewed_chapter(
                engine,
                project_id="export-project",
                target_chapter_count=2,
                canon_change=False,
            )
            committed = await ChapterCommandService(CommandBus(engine)).commit_chapter_and_canon(
                CommitChapterRequest(
                    project_id=ready.foundation.project_id,
                    chapter_id=ready.chapter_id,
                    submission_id=ready.submission_id,
                    review_id=ready.review_id,
                    expected_canon_baseline_id=ready.foundation.canon_baseline_id,
                ),
                idempotency_key="export-project:commit-chapter",
            )

            snapshot_service = SnapshotQueryService(CommandBus(engine))
            first_snapshot = await snapshot_service.current(
                project_id=ready.foundation.project_id
            )
            second_snapshot = await snapshot_service.current(
                project_id=ready.foundation.project_id
            )

            assert first_snapshot == second_snapshot
            assert first_snapshot.fingerprint == second_snapshot.fingerprint
            assert first_snapshot.book_baseline_id == ready.foundation.book_baseline_id
            assert first_snapshot.canon_baseline_id == committed.result.canon_after_id
            assert [arc.arc_baseline_id for arc in first_snapshot.arcs] == [
                ready.foundation.arc_baseline_id
            ]
            assert [chapter.chapter_baseline_id for chapter in first_snapshot.chapters] == [
                committed.result.chapter_baseline_id
            ]
            assert first_snapshot.task_evidence
            assert all(task.status == "succeeded" for task in first_snapshot.task_evidence)
            assert all(
                task.successful_attempt_id is not None
                for task in first_snapshot.task_evidence
            )

            export_service = ManuscriptExportService(
                CommandBus(engine),
                export_root=export_root,
            )
            first_export = await export_service.export(
                project_id=ready.foundation.project_id
            )
            destination = Path(first_export.path)
            first_bytes = destination.read_bytes()
            destination.unlink()
            second_export = await export_service.export(
                project_id=ready.foundation.project_id
            )
            second_bytes = Path(second_export.path).read_bytes()

            assert first_export == second_export
            assert first_bytes == second_bytes
            assert first_bytes.decode("utf-8") == (
                "# Echo Testimony\n\n"
                "## Chapter 1: The Witness Who Remembered Twice\n\n"
                "Mara laid the two statements side by side. "
                "The blue ink changed while she watched, adding a confession she had never heard.\n"
            )
            assert first_export.book_baseline_id == ready.foundation.book_baseline_id
            assert first_export.canon_baseline_id == committed.result.canon_after_id
            assert [chapter.chapter_baseline_id for chapter in first_export.chapters] == [
                committed.result.chapter_baseline_id
            ]

            snapshot_before_failed_write = await snapshot_service.current(
                project_id=ready.foundation.project_id
            )

            def fail_write(_destination: Path, _payload: bytes) -> None:
                raise OSError("simulated export filesystem failure")

            monkeypatch.setattr(export_service, "_atomic_write", fail_write)
            with pytest.raises(OSError, match="simulated export filesystem failure"):
                await export_service.export(project_id=ready.foundation.project_id)
            snapshot_after_failed_write = await snapshot_service.current(
                project_id=ready.foundation.project_id
            )
            assert snapshot_after_failed_write == snapshot_before_failed_write
            assert Path(second_export.path).read_bytes() == second_bytes
        finally:
            await engine.dispose()

    asyncio.run(exercise())
