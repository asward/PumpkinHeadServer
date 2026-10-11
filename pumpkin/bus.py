"""Fan-out of JSON events to every connected browser (/ws/events)."""
import asyncio
import json


class EventBus:
    def __init__(self):
        self._subs: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    @property
    def has_subscribers(self) -> bool:
        return bool(self._subs)

    def publish(self, type_: str, **data) -> None:
        """Must be called on the event loop thread."""
        msg = json.dumps({"type": type_, **data})
        for q in list(self._subs):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                pass  # slow client: drop rather than stall everyone
