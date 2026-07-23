from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from app.domain.commands import CommandPreconditionError, canonical_json_bytes
from app.store.command_bus import CommandBus


class ManuscriptChapterIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    chapter_id: str
    chapter_baseline_id: str
    book_ordinal: int = Field(ge=1)
    prose_ref_id: str


class ManuscriptExportResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str
    book_baseline_id: str
    canon_baseline_id: str
    chapters: list[ManuscriptChapterIdentity]
    snapshot_fingerprint: str
    content_sha256: str
    byte_count: int = Field(ge=1)
    path: str


class ManuscriptExportService:
    def __init__(self, command_bus: CommandBus, *, export_root: Path) -> None:
        self._command_bus = command_bus
        self._export_root = export_root.resolve()

    async def export(self, *, project_id: str) -> ManuscriptExportResult:
        async with self._command_bus.read_unit_of_work() as session:
            project = await session.projects.get(project_id)
            book = await session.books.get_for_project(project_id)
            if project is None or book is None or book.current_baseline_id is None:
                raise CommandPreconditionError("Project has no approved manuscript to export.")
            baseline = await session.books.get_baseline(
                project_id=project_id,
                book_id=book.id,
                baseline_id=book.current_baseline_id,
            )
            if baseline is None:
                raise CommandPreconditionError("Current Book baseline is missing.")
            chapter_rows = await session.snapshots.list_chapters(
                project_id=project_id,
                book_id=book.id,
            )
            prose = [
                (
                    await session.content.get_packed(
                        project_id=project_id,
                        ref_id=chapter.prose_ref_id,
                    )
                )
                .unpack_and_verify()
                .decode("utf-8")
                for chapter in chapter_rows
            ]
            canon_baseline_id = project.current_canon_baseline_id
        if not chapter_rows:
            raise CommandPreconditionError("Project has no committed Chapters to export.")
        identities = [
            ManuscriptChapterIdentity(
                chapter_id=chapter.chapter_id,
                chapter_baseline_id=chapter.chapter_baseline_id,
                book_ordinal=chapter.book_ordinal,
                prose_ref_id=chapter.prose_ref_id,
            )
            for chapter in chapter_rows
        ]
        snapshot = {
            "schema": "manuscript-export-snapshot-v1",
            "project_id": project_id,
            "book_baseline_id": baseline.id,
            "canon_baseline_id": canon_baseline_id,
            "chapters": [item.model_dump(mode="json") for item in identities],
        }
        snapshot_fingerprint = hashlib.sha256(canonical_json_bytes(snapshot)).hexdigest()
        sections = [f"# {baseline.approved_title.strip()}"]
        sections.extend(
            f"## Chapter {chapter.book_ordinal}: {chapter.chapter_title.strip()}\n\n"
            f"{chapter_prose.strip()}"
            for chapter, chapter_prose in zip(chapter_rows, prose, strict=True)
        )
        payload = ("\n\n".join(sections).rstrip() + "\n").encode("utf-8")
        content_sha256 = hashlib.sha256(payload).hexdigest()
        destination = self._destination(project_id)
        self._atomic_write(destination, payload)
        return ManuscriptExportResult(
            project_id=project_id,
            book_baseline_id=baseline.id,
            canon_baseline_id=canon_baseline_id,
            chapters=identities,
            snapshot_fingerprint=snapshot_fingerprint,
            content_sha256=content_sha256,
            byte_count=len(payload),
            path=str(destination),
        )

    def _destination(self, project_id: str) -> Path:
        slug = re.sub(r"[^0-9A-Za-z._-]+", "-", project_id).strip("-.") or "project"
        suffix = hashlib.sha256(project_id.encode("utf-8")).hexdigest()[:10]
        destination = (self._export_root / f"{slug[:48]}-{suffix}.md").resolve()
        if destination.parent != self._export_root:
            raise CommandPreconditionError("Export destination escaped the configured root.")
        return destination

    def _atomic_write(self, destination: Path, payload: bytes) -> None:
        self._export_root.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=self._export_root,
                prefix=".manuscript-",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                temporary.write(payload)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, destination)
        except BaseException:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
            raise
