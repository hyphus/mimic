"""In-process pub/sub broker backing the admin SSE stream.

Single worker only: a subscriber attached to another process cannot be woken
from here. See the `workers = 1` note in gunicorn.conf.py.
"""

from __future__ import annotations

import json
import queue
import threading
from collections.abc import Iterable, Iterator
from typing import Any

# Enough to absorb a burst; past this the client is told to resync rather than
# letting the queue grow without bound.
QUEUE_MAXSIZE = 500

# Only to keep idle connections from being dropped by proxies.
HEARTBEAT_SECONDS = 15

_subscribers: set[queue.Queue] = set()
_lock = threading.Lock()


def subscribe() -> queue.Queue:
    q: queue.Queue = queue.Queue(maxsize=QUEUE_MAXSIZE)
    with _lock:
        _subscribers.add(q)
    return q


def unsubscribe(q: queue.Queue) -> None:
    with _lock:
        _subscribers.discard(q)


def subscriber_count() -> int:
    with _lock:
        return len(_subscribers)


def publish(event: str, data: Any) -> None:
    """Fan out to every subscriber. Never blocks the caller."""
    with _lock:
        targets = list(_subscribers)
    for q in targets:
        try:
            q.put_nowait((event, data))
        except queue.Full:
            _mark_stale(q)


def _mark_stale(q: queue.Queue) -> None:
    """Discard the backlog and leave a single resync marker in its place."""
    try:
        while True:
            q.get_nowait()
    except queue.Empty:
        pass
    try:
        q.put_nowait(("resync", {}))
    except queue.Full:
        pass


def frame(event: str, data: Any) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


def stream(backlog: Iterable[tuple[str, Any]] = ()) -> Iterator[bytes]:
    """SSE frames for one client: the backlog, then live events, then heartbeats."""
    q = subscribe()
    try:
        yield b": connected\n\n"
        for event, data in backlog:
            yield frame(event, data)
        while True:
            try:
                event, data = q.get(timeout=HEARTBEAT_SECONDS)
            except queue.Empty:
                yield b": heartbeat\n\n"
                continue
            yield frame(event, data)
    finally:
        unsubscribe(q)
