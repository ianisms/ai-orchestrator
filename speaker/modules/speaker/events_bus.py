from __future__ import annotations

import asyncio
from typing import Any


class EventBus:
    """Minimal async pub/sub with per-subscriber queues and optional topics."""

    def __init__(self, maxsize: int = 1024):
        self.maxsize = maxsize
        self.subs_by_topic: dict[str, set[asyncio.Queue[Any]]] = {}

    def _topic_set(self, topic: str) -> set[asyncio.Queue[Any]]:
        if topic not in self.subs_by_topic:
            self.subs_by_topic[topic] = set()
        return self.subs_by_topic[topic]

    async def publish(self, evt: Any, topic: str = "*"):
        dead = []
        targets = set(self._topic_set(topic))
        targets.update(self._topic_set("*"))
        for q in list(targets):
            try:
                q.put_nowait(evt)
            except asyncio.QueueFull:
                # drop oldest to make room for latest
                try:
                    _ = q.get_nowait()
                    q.put_nowait(evt)
                except Exception:
                    dead.append(q)
            except Exception:
                dead.append(q)
        for q in dead:
            for queues in self.subs_by_topic.values():
                queues.discard(q)

    async def subscribe(self, topic: str = "*"):
        # Control events must never be dropped — use an unbounded queue.
        effective_maxsize = 0 if topic == "control" else self.maxsize
        q: asyncio.Queue[Any] = asyncio.Queue(maxsize=effective_maxsize)
        self._topic_set(topic).add(q)
        try:
            while True:
                evt = await q.get()
                yield evt
        finally:
            self._topic_set(topic).discard(q)
