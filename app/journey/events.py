# =========================================================
# journey/events.py - in-process pub/sub for Server-Sent Events
# =========================================================
#
# One topic per user ("user:<id>"). The webhook handlers publish status changes to
# the user's topic; the SSE endpoint streams them to the browser. This is what makes
# the application journey update live instead of requiring the user to poll.
#
# In-process only. A multi-worker uvicorn (--workers N) would give each worker its
# own bus, so a webhook handled by worker A would not reach a client on worker B.
# For a multi-worker deployment put a Redis pub/sub behind the same interface.

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import AsyncGenerator, Optional

logger = logging.getLogger("events")

# A slow client must not block a publisher, so each subscriber gets a bounded queue
# and the oldest event is dropped when it overflows.
QUEUE_MAXSIZE = 100


class EventBus:
    def __init__(self):
        self._subscribers: dict[str, set[asyncio.Queue]] = {}
        self._lock = asyncio.Lock()

    async def publish(self, topic: str, event: dict) -> None:
        payload = {
            "event": event.get("type", "update"),
            "data": {**event, "ts": datetime.now(timezone.utc).isoformat()},
        }
        async with self._lock:
            queues = list(self._subscribers.get(topic, ()))
        for queue in queues:
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                # Drop the oldest event to make room: the client only cares about the latest state.
                try:
                    queue.get_nowait()
                    queue.put_nowait(payload)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass

    async def subscribe(self, topic: str) -> AsyncGenerator[dict, None]:
        queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
        async with self._lock:
            self._subscribers.setdefault(topic, set()).add(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            async with self._lock:
                self._subscribers.get(topic, set()).discard(queue)
                if not self._subscribers.get(topic):
                    self._subscribers.pop(topic, None)

    async def subscriber_count(self, topic: str) -> int:
        async with self._lock:
            return len(self._subscribers.get(topic, ()))


bus = EventBus()


def user_topic(user_id: str) -> str:
    return f"user:{user_id}"


async def publish_user_event(user_id: str, event_type: str, **payload) -> None:
    """Convenience wrapper: publish a journey event to one user."""
    await bus.publish(user_topic(user_id), {"type": event_type, **payload})


async def publish_journey_update(user_id: str, reason: str) -> Optional[dict]:
    """
    Publish the user's full journey state. The client can render the whole
    application tracker from this single event.
    """
    from app.journey import state as journey_state

    snapshot = await journey_state.get_journey(user_id)
    await publish_user_event(user_id, "journey", reason=reason, journey=snapshot)
    return snapshot
