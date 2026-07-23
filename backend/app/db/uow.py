from __future__ import annotations

from types import TracebackType
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncTransaction

from app.store.content import ContentRepository
from app.store.commands import CommandRepository
from app.store.completion import CompletionRepository
from app.store.arcs import ArcRepository
from app.store.books import BookRepository
from app.store.canon import CanonRepository
from app.store.change_requests import ChangeRequestRepository
from app.store.chapters import ChapterRepository
from app.store.execution import ExecutionRepository
from app.store.feedback import FeedbackRepository
from app.store.projects import ProjectRepository
from app.store.runs import RunRepository
from app.store.snapshots import SnapshotRepository

BeginMode = Literal["DEFERRED", "IMMEDIATE"]


class StoreSession:
    """Repository boundary for one short SQLite transaction."""

    def __init__(self, connection: AsyncConnection) -> None:
        self._connection = connection
        self.content = ContentRepository(connection)
        self.arcs = ArcRepository(connection)
        self.commands = CommandRepository(connection)
        self.completion = CompletionRepository(connection)
        self.projects = ProjectRepository(connection)
        self.books = BookRepository(connection)
        self.canon = CanonRepository(connection)
        self.changes = ChangeRequestRepository(connection)
        self.chapters = ChapterRepository(connection)
        self.execution = ExecutionRepository(connection)
        self.feedback = FeedbackRepository(connection)
        self.runs = RunRepository(connection)
        self.snapshots = SnapshotRepository(connection)


class UnitOfWork:
    """Own exactly one connection and transaction; never span Provider waits."""

    def __init__(self, engine: AsyncEngine, *, begin_mode: BeginMode = "DEFERRED") -> None:
        self._engine = engine
        self._begin_mode = begin_mode
        self._connection: AsyncConnection | None = None
        self._transaction: AsyncTransaction | None = None

    async def __aenter__(self) -> StoreSession:
        if self._connection is not None:
            raise RuntimeError("A UnitOfWork instance cannot be entered twice.")
        connection = await self._engine.connect()
        try:
            connection = await connection.execution_options(
                novelpilot_sqlite_begin_mode=self._begin_mode
            )
            transaction = await connection.begin()
        except BaseException:
            await connection.close()
            raise
        self._connection = connection
        self._transaction = transaction
        return StoreSession(connection)

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        connection = self._connection
        transaction = self._transaction
        self._connection = None
        self._transaction = None
        if connection is None or transaction is None:
            return
        try:
            if exc_type is None:
                await transaction.commit()
            else:
                await transaction.rollback()
        finally:
            await connection.close()
