from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.domain.commands import CommandPreconditionError, canonical_json_bytes
from app.store.command_bus import CommandBus


class ArcSnapshotIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    arc_id: str
    ordinal: int = Field(ge=1)
    purpose: str
    lifecycle_status: str
    arc_baseline_id: str


class ChapterSnapshotIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    chapter_id: str
    arc_id: str
    book_ordinal: int = Field(ge=1)
    arc_ordinal: int = Field(ge=1)
    chapter_baseline_id: str


class TaskEvidenceIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str
    status: str
    successful_attempt_id: str | None
    delivery_state: str


class ProjectSnapshotManifest(BaseModel):
    """Read-only identity contract for a future immutable fixture publisher."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_id: str = "project-snapshot-manifest-v1"
    project_id: str
    book_id: str
    book_baseline_id: str
    canon_baseline_id: str
    arcs: list[ArcSnapshotIdentity]
    chapters: list[ChapterSnapshotIdentity]
    task_evidence: list[TaskEvidenceIdentity]
    last_domain_event_sequence: int = Field(ge=0)

    @property
    def fingerprint(self) -> str:
        import hashlib

        return hashlib.sha256(canonical_json_bytes(self.model_dump(mode="json"))).hexdigest()


class SnapshotQueryService:
    def __init__(self, command_bus: CommandBus) -> None:
        self._command_bus = command_bus

    async def current(self, *, project_id: str) -> ProjectSnapshotManifest:
        async with self._command_bus.read_unit_of_work() as session:
            project = await session.projects.get(project_id)
            book = await session.books.get_for_project(project_id)
            if (
                project is None
                or book is None
                or book.current_baseline_id is None
            ):
                raise CommandPreconditionError("Project has no approved Book snapshot.")
            arcs = await session.snapshots.list_arcs(
                project_id=project_id,
                book_id=book.id,
            )
            chapters = await session.snapshots.list_chapters(
                project_id=project_id,
                book_id=book.id,
            )
            tasks = await session.snapshots.list_tasks(project_id=project_id)
            sequence = await session.snapshots.last_event_sequence(project_id=project_id)
        return ProjectSnapshotManifest(
            project_id=project_id,
            book_id=book.id,
            book_baseline_id=book.current_baseline_id,
            canon_baseline_id=project.current_canon_baseline_id,
            arcs=[
                ArcSnapshotIdentity(
                    arc_id=item.arc_id,
                    ordinal=item.ordinal,
                    purpose=item.purpose,
                    lifecycle_status=item.lifecycle_status,
                    arc_baseline_id=item.arc_baseline_id,
                )
                for item in arcs
            ],
            chapters=[
                ChapterSnapshotIdentity(
                    chapter_id=item.chapter_id,
                    arc_id=item.arc_id,
                    book_ordinal=item.book_ordinal,
                    arc_ordinal=item.arc_ordinal,
                    chapter_baseline_id=item.chapter_baseline_id,
                )
                for item in chapters
            ],
            task_evidence=[
                TaskEvidenceIdentity(
                    task_id=item.task_id,
                    status=item.status,
                    successful_attempt_id=item.successful_attempt_id,
                    delivery_state=item.delivery_state,
                )
                for item in tasks
            ],
            last_domain_event_sequence=sequence,
        )
