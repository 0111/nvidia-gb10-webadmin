"""WebSocket connection manager / topic broadcaster.

A single `/ws` endpoint is shared by all frontend pages; each connection
declares which topics it wants (metrics / logs / load_progress / ...) and
the hub only sends it messages for those topics. Broadcasting to one dead
connection must never prevent delivery to the others — every send is
individually guarded.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from fastapi import WebSocket

logger = logging.getLogger(__name__)


@dataclass
class _Connection:
    websocket: WebSocket
    topics: set[str] = field(default_factory=set)
    # Per-connection serialization of send_json: the metrics broadcast task
    # and the log streamer task can both target the same socket — concurrent
    # sends to one Starlette WebSocket corrupt frames, so every send goes
    # through this lock.
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Live log streaming target for this connection (set via the WS
    # "set_log_target" action); None = not viewing logs.
    log_component: str | None = None
    log_lines: int = 200
    log_last: str | None = None


class ConnectionManager:
    """Tracks active WebSocket connections and their topic subscriptions."""

    def __init__(self) -> None:
        self._connections: list[_Connection] = []
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket, topics: list[str]) -> None:
        await websocket.accept()
        async with self._lock:
            self._connections.append(_Connection(websocket=websocket, topics=set(topics)))
        logger.info("WS connected, topics=%s, total=%d", topics, len(self._connections))

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self._lock:
            self._connections = [c for c in self._connections if c.websocket is not websocket]
        logger.info("WS disconnected, total=%d", len(self._connections))

    async def update_topics(self, websocket: WebSocket, topics: list[str]) -> None:
        """Allow a connected client to change its topic subscriptions."""
        async with self._lock:
            for c in self._connections:
                if c.websocket is websocket:
                    c.topics = set(topics)
                    break

    async def broadcast(self, topic: str, data: dict) -> None:
        """Send {topic, data, ts} to every connection subscribed to `topic`.

        A failed send (client gone, socket closed mid-flight, etc.) only
        drops that one connection — it never raises out of this method and
        never blocks delivery to remaining connections.
        """
        envelope = {"topic": topic, "data": data, "ts": time.time()}

        async with self._lock:
            targets = [c for c in self._connections if topic in c.topics]

        dead: list[WebSocket] = []
        for conn in targets:
            try:
                async with conn.send_lock:
                    await conn.websocket.send_json(envelope)
            except Exception as exc:  # pragma: no cover - defensive, any client error
                logger.debug("WS send failed, dropping connection: %s", exc)
                dead.append(conn.websocket)

        if dead:
            async with self._lock:
                self._connections = [c for c in self._connections if c.websocket not in dead]

    def set_log_target(self, websocket: WebSocket, component: str | None, lines: int = 200) -> None:
        """Record which component's logs this connection wants streamed (or
        None to stop). Synchronous: just flips fields the streamer reads."""
        for c in self._connections:
            if c.websocket is websocket:
                c.log_component = component or None
                c.log_lines = lines
                c.log_last = None  # force an immediate first push of current content
                break

    async def push_log_updates(self, fetch) -> None:
        """For every connection with a log target, fetch its logs via
        `fetch(component, lines) -> str` (run in a thread by the caller-side
        fetch impl) and send a `logs` message to *that* connection only when
        the content changed. Single-task driven (the LogStreamer loop), and
        each send is serialized by the connection's send_lock, so it never
        races the metrics/page broadcasts on the same socket.
        """
        async with self._lock:
            targets = [c for c in self._connections if c.log_component]
        for conn in targets:
            try:
                content = await fetch(conn.log_component, conn.log_lines)
                if content == conn.log_last:
                    continue
                conn.log_last = content
                envelope = {"topic": "logs",
                            "data": {"component": conn.log_component, "content": content},
                            "ts": time.time()}
                async with conn.send_lock:
                    await conn.websocket.send_json(envelope)
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("WS 日志推送失败: %s", exc)

    @property
    def connection_count(self) -> int:
        return len(self._connections)


# Process-wide singleton; main.py and background_tasks.py share this instance.
manager = ConnectionManager()
