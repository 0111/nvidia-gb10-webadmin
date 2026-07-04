"""HTTP client helpers for the local SearXNG instance.

Uses httpx (already a project dependency, see requirements.txt / web router
debug_router.py for the same pattern) instead of introducing `requests`.
Every public function here catches all exceptions itself and returns a
plain dict — callers (Web routers / CLI) never need to wrap calls in
try/except.
"""
from __future__ import annotations

import time

import httpx

DEFAULT_TIMEOUT_SECONDS = 8.0

# SearXNG 的 botdetection 取真实客户端 IP 时，若请求既无 X-Forwarded-For 也无
# X-Real-IP 头，会按 ERROR 级别打印 `X-Forwarded-For nor X-Real-IP header is set!`。
# 本项目后端从本机(127.0.0.1)直接请求本地 SearXNG，带上回环地址的这两个头如实
# 表明来源、消除该 ERROR 噪声（limiter 已关闭，不影响搜索本身）。
_LOCAL_IP_HEADERS = {"X-Forwarded-For": "127.0.0.1", "X-Real-IP": "127.0.0.1"}


def search(query: str, base_url: str, timeout: float = DEFAULT_TIMEOUT_SECONDS,
           proxy: str | None = None) -> dict:
    """Run a SearXNG JSON search query.

    Builds: {base_url}/search?q=<query>&format=json&categories=general&
    language=auto&safesearch=0  (per Project_Task.md「API 发布」章节约定).

    `proxy`: optional HTTP/HTTPS proxy URL applied to this real request
    (typically `config.searxng_proxy_url`), not just used by the one-off
    `check_proxy` probe — if the user has configured a network proxy in
    高级设置, every real search call should actually go through it.

    Returns {"ok": True, "data": <parsed json>} on success, or
    {"ok": False, "error": <message>} on any failure (connection refused,
    timeout, non-200 status, invalid JSON, etc.) — never raises.
    """
    url = base_url.rstrip("/") + "/search"
    params = {
        "q": query,
        "format": "json",
        "categories": "general",
        "language": "auto",
        "safesearch": "0",
    }
    try:
        client_kwargs: dict = {"timeout": timeout}
        if proxy:
            client_kwargs["proxy"] = proxy
        with httpx.Client(**client_kwargs) as client:
            response = client.get(url, params=params, headers=_LOCAL_IP_HEADERS)
    except httpx.TimeoutException:
        return {"ok": False, "data": None, "error": "请求超时"}
    except httpx.HTTPError as exc:
        return {"ok": False, "data": None, "error": f"连接失败: {exc}"}
    except Exception as exc:  # pragma: no cover - defensive
        return {"ok": False, "data": None, "error": str(exc)}

    if response.status_code != 200:
        return {
            "ok": False,
            "data": None,
            "error": f"SearXNG 返回非 200 状态码: {response.status_code}",
        }

    try:
        data = response.json()
    except ValueError as exc:
        return {"ok": False, "data": None, "error": f"响应不是合法 JSON: {exc}"}

    return {"ok": True, "data": data, "error": None}


def check_proxy(base_url: str, proxy: str | None = None,
                 timeout: float = DEFAULT_TIMEOUT_SECONDS) -> dict:
    """Test reachability of `base_url` through an optional HTTP/HTTPS proxy.

    `proxy` is a single proxy URL (e.g. "http://127.0.0.1:7890") applied to
    both http:// and https:// traffic, or None/empty to test without any
    proxy (direct connection).

    Returns {"ok": bool, "latency_ms": float | None, "status_code": int | None,
    "error": str | None} — never raises.
    """
    start = time.monotonic()
    try:
        client_kwargs: dict = {"timeout": timeout}
        if proxy:
            client_kwargs["proxy"] = proxy
        with httpx.Client(**client_kwargs) as client:
            response = client.get(base_url, headers=_LOCAL_IP_HEADERS)
        latency_ms = round((time.monotonic() - start) * 1000, 1)
        return {
            "ok": response.status_code < 400,
            "latency_ms": latency_ms,
            "status_code": response.status_code,
            "error": None,
        }
    except httpx.TimeoutException:
        return {"ok": False, "latency_ms": None, "status_code": None, "error": "请求超时"}
    except httpx.HTTPError as exc:
        return {"ok": False, "latency_ms": None, "status_code": None, "error": f"连接失败: {exc}"}
    except Exception as exc:  # pragma: no cover - defensive
        return {"ok": False, "latency_ms": None, "status_code": None, "error": str(exc)}
