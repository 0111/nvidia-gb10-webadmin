"""FastAPI app entry point.

Run with: uvicorn web.main:app --host 0.0.0.0 --port 8000
(or `python -m web.main` for a quick local/manual run).
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from core.config import load_config
from web import model_cache
from web.background_tasks import MetricsCollector
from web.routers import (
    api_directory_router,
    auth_router,
    components_router,
    debug_router,
    env_router,
    gateway_router,
    logs_router,
    metrics_router,
    models_router,
    overview_router,
    perf_router,
    runtime_stats_router,
    searxng_router,
    settings_router,
)
from web.state import reconcile_from_disk
from web.ws_hub import manager as ws_manager
from web.ws_router import router as ws_router

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="NVIDIA DGX Spark / GB10 Manager", version="0.2.0")

# CORS: convenient for local frontend dev (Vite on a different port). In
# production this should be tightened or disabled entirely — controlled by
# config rather than hardcoded, see ENABLE_CORS below.
ENABLE_CORS = True
if ENABLE_CORS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

# Public routes.
app.include_router(auth_router.router)

# Protected routes (each router declares get_current_user as a dependency).
app.include_router(overview_router.router)
app.include_router(models_router.router)
app.include_router(components_router.router)
app.include_router(env_router.router)
app.include_router(logs_router.router)
app.include_router(debug_router.router)
app.include_router(settings_router.router)
app.include_router(searxng_router.router)
app.include_router(perf_router.router)
app.include_router(metrics_router.router)
app.include_router(runtime_stats_router.router)
app.include_router(api_directory_router.router)

# OpenAI / Claude 兼容对外网关（api-key 鉴权，非 JWT）。
app.include_router(gateway_router.router)

# WebSocket endpoint (auth handled per-connection via query token, not a
# FastAPI Depends, since browsers can't set custom headers on the WS handshake).
app.include_router(ws_router)

_metrics_collector: MetricsCollector | None = None


@app.on_event("startup")
async def on_startup() -> None:
    global _metrics_collector
    config = load_config()
    logger.info("Starting web backend on %s:%s", config.web_host, config.web_port)
    reconcile_from_disk(config.data_dir)
    _metrics_collector = MetricsCollector(ws_manager, data_dir=config.data_dir)
    _metrics_collector.start()

    # Model scanning is now a MANUAL action (POST /api/models/rescan or
    # `cli model_check`) — it's expensive (stat-walks every file, parses
    # config.json/index.json, reads GGUF headers), so it must not run on
    # every request. Here we only warm the in-memory cache from the persisted
    # result on disk (cheap); web.model_cache.get_models() bootstraps a
    # one-time scan only if no result file exists yet (fresh deployment).
    await asyncio.to_thread(model_cache.get_models, config)


@app.on_event("shutdown")
async def on_shutdown() -> None:
    if _metrics_collector is not None:
        await _metrics_collector.stop()


@app.get("/api/health", tags=["health"])
def health() -> dict:
    """Unauthenticated liveness probe."""
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    cfg = load_config()
    uvicorn.run("web.main:app", host=cfg.web_host, port=cfg.web_port, reload=False)
