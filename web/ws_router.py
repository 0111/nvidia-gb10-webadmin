"""WebSocket endpoint /ws — topic-based push channel.

Client connects with `?token=<jwt>&topics=metrics,logs,load_progress` (or
sends a `{"action": "subscribe", "topics": [...]}` message after connect
to change subscriptions). Topics in use this phase:

- metrics: every 10s, system resource snapshot (see background_tasks.py).
- load_progress: pushed by models_router on load/unload state changes.
- logs: reserved for future server-push log tailing (not implemented yet;
  logs_router.get_logs remains the pull-based source for now).

Auth: a valid JWT is required via the `token` query parameter (WebSocket
requests cannot carry an Authorization header from a browser easily); an
invalid/missing token closes the connection with code 4401 immediately
after accept, so the client gets a clean, distinguishable close reason
instead of a silent drop.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from web.auth import verify_token_for_ws
from web.ws_hub import manager

logger = logging.getLogger(__name__)

router = APIRouter()


@router.websocket("/ws")
async def ws_endpoint(
    websocket: WebSocket,
    token: str | None = Query(default=None),
    topics: str = Query(default="metrics,load_progress"),
) -> None:
    username = verify_token_for_ws(token)
    if username is None:
        await websocket.close(code=4401, reason="未授权：缺少或无效的 token")
        return

    topic_list = [t.strip() for t in topics.split(",") if t.strip()]
    await manager.connect(websocket, topic_list)

    try:
        while True:
            message = await websocket.receive_json()
            if isinstance(message, dict) and message.get("action") == "subscribe":
                new_topics = message.get("topics", [])
                if isinstance(new_topics, list):
                    await manager.update_topics(websocket, [str(t) for t in new_topics])
            elif isinstance(message, dict) and message.get("action") == "set_log_target":
                # Client (组件日志 / 模型配置 页) asks for live log streaming of a
                # given component; the LogStreamer in MetricsCollector pushes
                # `logs` messages to this connection only. component=None stops it.
                component = message.get("component")
                try:
                    lines = int(message.get("lines", 200))
                except (TypeError, ValueError):
                    lines = 200
                manager.set_log_target(websocket, str(component) if component else None, lines)
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # pragma: no cover - defensive: malformed client message, etc.
        logger.debug("WS loop ended with error: %s", exc)
    finally:
        await manager.disconnect(websocket)
