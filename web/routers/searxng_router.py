"""SearXNG control + search/proxy debug endpoints.

Mirrors the style of web/routers/models_router.py / env_router.py: thin
adapters over core.docker_helper.SearxngManager (start/stop/status of the
fixed deploy/searxng-compose.yml stack) and core.searxng_client (HTTP
search + proxy reachability checks). No business logic lives here.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from core import searxng_client
from core.config import load_config
from core.docker_helper import SearxngManager
from web.auth import get_current_user
from web.schemas import (
    SearxngActionResponse,
    SearxngProxyTestRequest,
    SearxngProxyTestResponse,
    SearxngSearchResponse,
    SearxngStatusResponse,
)

router = APIRouter(prefix="/api/searxng", tags=["searxng"], dependencies=[Depends(get_current_user)])

_manager = SearxngManager()


def _to_command_result(result) -> dict:
    return {
        "ok": result.ok, "command": result.command, "stdout": result.stdout,
        "stderr": result.stderr, "returncode": result.returncode, "error": result.error,
    }


@router.get("/status", response_model=SearxngStatusResponse)
def get_status() -> SearxngStatusResponse:
    result = _manager.status()
    # `docker compose ps` exits 0 even with zero running containers; a
    # non-empty stdout (one JSON object per running service line) is the
    # simplest reachable signal that the stack is up.
    running = result.ok and bool(result.stdout.strip())
    return SearxngStatusResponse(running=running, command_result=_to_command_result(result))


@router.post("/start", response_model=SearxngActionResponse)
def start_searxng() -> SearxngActionResponse:
    result = _manager.start()
    return SearxngActionResponse(
        ok=result.ok,
        message="SearXNG 已启动" if result.ok else (result.error or result.stderr or "启动失败"),
        command_result=_to_command_result(result),
    )


@router.post("/stop", response_model=SearxngActionResponse)
def stop_searxng() -> SearxngActionResponse:
    result = _manager.stop()
    return SearxngActionResponse(
        ok=result.ok,
        message="SearXNG 已停止" if result.ok else (result.error or result.stderr or "停止失败"),
        command_result=_to_command_result(result),
    )


@router.get("/search", response_model=SearxngSearchResponse)
def search(q: str) -> SearxngSearchResponse:
    config = load_config()
    # Use the persisted network proxy (高级设置 -> SearXNG 网络代理) for every
    # real search, not just the one-off "测试代理" probe — see
    # core.searxng_client.search()'s `proxy` parameter.
    result = searxng_client.search(q, config.searxng_url, proxy=config.searxng_proxy_url or None)
    return SearxngSearchResponse(ok=result["ok"], query=q, data=result["data"], error=result["error"])


@router.post("/proxy/test", response_model=SearxngProxyTestResponse)
def test_proxy(payload: SearxngProxyTestRequest) -> SearxngProxyTestResponse:
    config = load_config()
    # If the request body doesn't specify a proxy, fall back to the
    # persisted searxng_proxy_url so "测试代理" reflects what's actually
    # saved/used by real search() calls, rather than always testing "no proxy".
    proxy = payload.proxy if payload.proxy is not None else config.searxng_proxy_url
    result = searxng_client.check_proxy(config.searxng_url, proxy)
    return SearxngProxyTestResponse(**result)
