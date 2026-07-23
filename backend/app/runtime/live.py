from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Generic, TypeVar

EventT = TypeVar("EventT")


class LiveSubscription(Generic[EventT]):
    def __init__(
        self,
        owner: LossyLiveFanout[EventT],
        subscription_id: str,
        queue: asyncio.Queue[EventT],
    ) -> None:
        self._owner = owner
        self._subscription_id = subscription_id
        self._queue = queue
        self._closed = False

    def __aiter__(self) -> AsyncIterator[EventT]:
        return self

    async def __anext__(self) -> EventT:
        if self._closed:
            raise StopAsyncIteration
        return await self._queue.get()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._owner.unsubscribe(self._subscription_id)

    async def __aenter__(self) -> LiveSubscription[EventT]:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object | None,
    ) -> None:
        self.close()


class LossyLiveFanout(Generic[EventT]):
    """In-memory live delivery; slow/disconnected clients can never block a task."""

    def __init__(self, *, queue_size: int = 128) -> None:
        if queue_size < 1:
            raise ValueError("queue_size must be positive.")
        self._queue_size = queue_size
        self._subscribers: dict[str, asyncio.Queue[EventT]] = {}

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def subscribe(self) -> LiveSubscription[EventT]:
        subscription_id = uuid.uuid4().hex
        queue: asyncio.Queue[EventT] = asyncio.Queue(maxsize=self._queue_size)
        self._subscribers[subscription_id] = queue
        return LiveSubscription(self, subscription_id, queue)

    def unsubscribe(self, subscription_id: str) -> None:
        self._subscribers.pop(subscription_id, None)

    async def publish(self, event: EventT) -> None:
        # No awaits are needed for fan-out: full queues lose their oldest transient event.
        for queue in tuple(self._subscribers.values()):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:  # pragma: no cover - one event-loop turn is atomic.
                    pass
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:  # pragma: no cover - defensive, delivery is explicitly lossy.
                pass
